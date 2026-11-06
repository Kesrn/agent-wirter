"""章节标题解析器（P-1 阶段一：非破坏性解析，不写数据库）。

从小说原文中解析章节标题，输出带偏移/哈希的章节区间，供 dry-run 验收报告
与后续重切分使用。设计约束（方案 v3 §3.2）：

- 保存 source_ordinal（顺序）与 display_chapter_no（原文标题号）双编号；
- 同时支持阿拉伯数字（"第613章"）与中文数字（"第一章"）标题；
- "楔子/序章/番外"等非数字标题单独分类，不强行生成数字章号；
- 保留 original_label 与标题行偏移，区间校验可证明无重叠/无丢失。

阶段一只做解析与报告，不触碰 project_source_chapters。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# 解析器版本（写库时记录到 split_generations.parser_version，与原文哈希共同
# 构成"代际可复现"依据：同一原文 + 同一解析器 + 同一决策账本 → 同一代）
PARSER_VERSION = "chapter_parser-2026-08-02"

# ── 标题模式 ─────────────────────────────────────────────

# 阿拉伯数字："第613章 肉丘障碍" / "第 613 章" / 全角空格 / 全角数字（第１章）
_HEADING_ARABIC = re.compile(
    r"第\s*([0-9０-９]{1,5})\s*章\s*(.*)$"
)
# 中文数字："第一章" / "第一百二十三章"
_HEADING_CN = re.compile(
    r"第\s*([零一二两三四五六七八九十百千]+)\s*章\s*(.*)$"
)
# 非数字章节："楔子 / 序章 / 番外..."
_HEADING_SPECIAL = re.compile(
    r"^\s*(楔子|序章|序言|前言|后记|番外(?:\s*[^\n]*)?|外传(?:\s*[^\n]*)?)\s*$"
)

# 疑似标题但格式未知：行首"第" + "章/回/节/卷/篇/部" 类词，但无法解析。
# 例："第X回"（非章）、"第X卷"（卷标题）、"第X节"（节标题）。
_HEADING_UNKNOWN_LIKE = re.compile(r"第[^，。；\n]{1,10}(?:章|回|节|卷|篇|部)")

# ── 恢复规则（P-1 阶段四前置：真实标题被噪声破坏的格式） ──

# 作者编号跳跃："第956~967章 三方聚头"——作者把 956 的下一章误编号成 967，
# 正文连续。恢复为 display=967（区间末端），957~966 标记 author_numbering_skip。
_HEADING_RANGE = re.compile(r"第\s*(\d+)\s*[~～\-—～]\s*(\d+)\s*章\s*(.*)$")

# 噪声插入格式："第1313亮晶晶章 大雷爆"——真实章节标题被噪声字插入。
# 恢复为 display=1313，标记 format_recovered。
_HEADING_NOISY = re.compile(r"第\s*(\d+)[^0-9章\n]{1,8}章\s*(.*)$")

# ── 伪标题模式（P-1 阶段二：作者说明 / 翻页残片 / 手打水印 / 广告） ──

# 翻页残片："(第1/1页)" / "第1/2页，请点击下一页继续阅读"
_PAGE_ARTIFACT_RE = re.compile(r"第?\s*\d+\s*/\s*\d+\s*页")

# 作者更新说明特征："第一章：中午12点" → title="：中午12点"（冒号开头+时间词）。
# 注意：正常章节标题可能含"道歉"（"议长秘书的道歉"）、"晚上"（"晚上是禽兽"），
# 不得按关键词误判——只认"标题以冒号开头且含时间词"这一可靠格式。
_UPDATE_NOTE_TIME_RE = re.compile(r"(中午|傍晚|晚上|凌晨|上午|下午|深夜|早上|\d+\s*[点时]|24点)")

# 手打水印/网站尾巴："是由【无*错】【小-说-网】会员手打" / "更多章节请到"
_WATERMARK_MARKERS = ("会员手打", "是由", "更多章节请到", "无错", "小-说-网", "请到")

# 站点广告尾巴："全职法师 最快更新，无弹窗阅读请。"
_ADS_MARKERS = ("最快更新", "无弹窗", "请收藏", "请推荐", "请投推荐票")

# 短章节内容阈值：伪标题/残片的章节正文通常极短（紧跟下一标题）
MIN_CHAPTER_BODY_CHARS = 100

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# 全角数字 → 半角（"第１章"）
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 章节标题常见前后噪音（正文引用章节号时会被误判为标题，用行首校验排除）
_HEADING_MIN_CHARS = 2  # "第X章" 至少 3 字符（第+数字+章）


def cn_to_int(text: str) -> int | None:
    """中文数字转阿拉伯数字（支持 零~九千九百九十九）。

    返回 None 表示无法解析（如"千零"等非法组合）。
    """
    if not text:
        return None
    result = 0
    section = 0
    for ch in text:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            # "十四" 中"十"前无数字 → section=1（十=10）
            if section == 0:
                section = 1
            result += section * unit
            section = 0
        else:
            return None
    result += section
    return result if result > 0 else None


@dataclass
class UnparsedCandidate:
    """疑似标题但格式未知的行（结构化扫描结果）。

    line_no: 1-based 行号；offset: 行起始偏移；text: 原始文本；
    reason: 识别原因（行首"第"+章节词但无法解析）。
    """
    line_no: int
    offset: int
    text: str
    reason: str


@dataclass
class ChapterHeading:
    """解析出的章节标题与区间。

    ordinal: 数据库内顺序（1..N）；display_no: 原文标题号（非数字标题为 None）；
    start/end: 章节区间在原文中的字符偏移（end 为下一标题起点或全文末尾，
    即 [start, end) 包含标题行与正文）。
    """
    ordinal: int
    display_no: int | None
    label: str                 # original_label："第613章 肉丘障碍"
    title: str                 # 仅标题："肉丘障碍"
    kind: str                  # arabic / cn / special
    start: int
    end: int
    warning: str = ""          # dup / gap / out_of_order / unparsed 等
    content_hash: str = field(default="", repr=False)   # 完整 SHA-256（64 hex）

    def content(self, source: str) -> str:
        return source[self.start:self.end]

    def body_length(self, source: str) -> int:
        """正文长度（去标题行后）。"""
        body = source[self.start:self.end]
        nl = body.find("\n")
        if nl >= 0:
            body = body[nl:]
        return len(body.strip())


@dataclass
class ChapterAnomaly:
    """异常分类项（P-1 阶段二：供人工复核，不自动裁决）。

    分类：false_heading / page_artifact / duplicate_label /
          suspected_misnumber / source_gap / manual_review
    """
    category: str
    ordinal: int
    display_no: int | None
    label: str
    body_len: int
    prev_label: str = ""
    next_label: str = ""
    head_tail: str = ""          # 内容首尾片段（各 30 字）
    suggestion: str = ""         # 建议处理方式


def _parse_label(line: str) -> tuple[str, int | None, str, str]:
    """解析一行标题 → (kind, display_no, title, warning)。

    kind: arabic / cn / range_recovered / noise_recovered / special / unknown。
    """
    stripped = line.strip()
    m = _HEADING_ARABIC.match(stripped)
    if m:
        return "arabic", int(m.group(1).translate(_FULLWIDTH_DIGITS)), m.group(2).strip(), ""
    m = _HEADING_CN.match(stripped)
    if m:
        no = cn_to_int(m.group(1))
        if no is None:
            return "cn", None, m.group(2).strip(), "unparsed_cn_number"
        return "cn", no, m.group(2).strip(), ""
    # 恢复规则 1：作者编号跳跃 "第956~967章" → display=967。
    # 956 是已存在的真实章节（作者把 956 的下一章误编号成 967），
    # 跳号区间 = 957~966（10 个），956 不得写入跳号决议。
    m = _HEADING_RANGE.match(stripped)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if end > start + 1:
            return "range_recovered", end, m.group(3).strip(), (
                f"author_numbering_skip:{start + 1}~{end - 1}"
            )
    # 恢复规则 2：噪声插入 "第1313亮晶晶章" → display=1313
    m = _HEADING_NOISY.match(stripped)
    if m:
        return "noise_recovered", int(m.group(1)), m.group(2).strip(), "format_recovered"
    if _HEADING_SPECIAL.match(stripped):
        return "special", None, stripped, ""
    return "unknown", None, "", "unparsed_heading"


def parse_headings(source: str) -> list[ChapterHeading]:
    """按行解析全文章节标题，返回有序章节区间。

    - 标题行必须行首（可含前导空白/分隔符"------"之间的空行容错）；
    - 相邻标题之间为正文区间；全文首标题之前与末标题之后的内容单独报告。
    """
    headings: list[ChapterHeading] = []
    seen_offsets: dict[int, int] = {}   # display_no -> ordinal（重复检测）

    # 逐行扫描，记录标题行偏移
    pos = 0
    lines = source.splitlines(keepends=True)
    for line in lines:
        if _looks_like_heading_line(line):
            kind, no, title, warn = _parse_label(line)
            if kind in ("arabic", "cn", "range_recovered", "noise_recovered"):
                ordinal = len(headings) + 1
                label = line.strip()
                warnings: list[str] = []
                if no is not None and no in seen_offsets:
                    warnings.append(f"duplicate_display_no({seen_offsets[no]})")
                if no is not None:
                    seen_offsets[no] = ordinal
                if warn:
                    warnings.append(warn)
                headings.append(ChapterHeading(
                    ordinal=ordinal, display_no=no, label=label, title=title,
                    kind=kind, start=pos, end=len(source),
                    warning=";".join(warnings),
                ))
            elif kind == "special":
                headings.append(ChapterHeading(
                    ordinal=len(headings) + 1, display_no=None, label=line.strip(),
                    title=line.strip(), kind="special",
                    start=pos, end=len(source),
                    warning="special_non_numeric",
                ))
            # unknown 行跳过（正文中引用章节号的句子）
        pos += len(line)

    # 回填 end：下一标题起点
    for i, h in enumerate(headings):
        h.end = headings[i + 1].start if i + 1 < len(headings) else len(source)
        h.content_hash = _sha256(source[h.start:h.end])

    return headings


def _looks_like_heading_line(line: str) -> bool:
    """标题行判定：行首（允许前导空白）出现"第...章"，且该行足够短。"""
    stripped = line.strip()
    if not stripped:
        return False
    # 章节标题行通常 < 60 字符；正文长句不会被误判
    if len(stripped) > 60:
        return False
    return bool(
        _HEADING_ARABIC.match(stripped)
        or _HEADING_CN.match(stripped)
        or _HEADING_RANGE.match(stripped)
        or _HEADING_NOISY.match(stripped)
        or _HEADING_SPECIAL.match(stripped)
    )


def _sha256(text: str) -> str:
    """完整 SHA-256（64 hex，方案 v3 §3.2：正式数据保存完整哈希）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_unparsed_headings(source: str, sample_limit: int = 10) -> tuple[list[UnparsedCandidate], list[UnparsedCandidate]]:
    """扫描"疑似标题但格式未知"的行（结构化，计数完整不受截断）。

    条件：行首"第"开头、行短（≤60 字符）、含"章/回/节/卷/篇/部"等章节词，
    但 _parse_label 无法解析（如"第X回"、"第X卷"、非法中文数字组合）。

    返回 (all_candidates, samples)：all_candidates 为全量（真实计数），
    samples 为最多 sample_limit 条供展示。每项含行号/偏移/文本/识别原因。
    """
    all_cands: list[UnparsedCandidate] = []
    offset = 0
    line_no = 0
    for line in source.splitlines(keepends=True):
        line_no += 1
        stripped = line.strip()
        if stripped and len(stripped) <= 60 and stripped.startswith("第"):
            if _HEADING_UNKNOWN_LIKE.search(stripped):
                kind, _no, _title, _warn = _parse_label(stripped)
                if kind == "unknown":
                    all_cands.append(UnparsedCandidate(
                        line_no=line_no,
                        offset=offset,
                        text=stripped[:60],
                        reason="行首'第'+章节词但无法解析（如第X回/第X卷/非法中文数字）",
                    ))
        offset += len(line)
    return all_cands, all_cands[:sample_limit]


# ── 伪标题判定（P-1 阶段二） ─────────────────────────────

def is_page_artifact(heading: ChapterHeading, source: str) -> bool:
    """翻页/分页残片："(第1/1页)" / "第1/2页，请点击下一页继续阅读"。"""
    if _PAGE_ARTIFACT_RE.search(heading.title):
        return True
    body = source[heading.start:heading.end]
    return bool(_PAGE_ARTIFACT_RE.search(body[:60]))


def is_update_note(heading: ChapterHeading, source: str) -> bool:
    """作者更新说明："第一章：中午12点"（标题以冒号开头 + 时间词）。

    只认该格式；正常标题含"道歉/晚上"等词不判伪标题。
    """
    title = heading.title
    if not title:
        return False
    return title.startswith(("：", ":")) and bool(_UPDATE_NOTE_TIME_RE.search(title))


def is_watermark_tail(heading: ChapterHeading, source: str) -> bool:
    """手打水印/网站尾巴："第1426章杀回去！是由【无*错】...会员手打"。"""
    if any(m in heading.label for m in _WATERMARK_MARKERS):
        return True
    body = source[heading.start:heading.end]
    return any(m in body[:80] for m in _ADS_MARKERS)


def is_suspicious_short_body(heading: ChapterHeading, source: str) -> bool:
    """正文过短（伪标题/残片典型特征），但未命中明确模式 → 人工复核。"""
    return heading.body_length(source) < MIN_CHAPTER_BODY_CHARS


# ── 异常分类（P-1 阶段二） ─────────────────────────────

def classify_anomalies(
    source: str,
    headings: list[ChapterHeading],
) -> list[ChapterAnomaly]:
    """对全部章节输出异常清单（六类），供人工复核。

    只列出可疑项，不自动裁决；正常章节不出现。每项附前后章节、
    内容长度、首尾片段与建议处理。
    """
    anomalies: list[ChapterAnomaly] = []
    by_no: dict[int, list[ChapterHeading]] = {}
    for h in headings:
        if h.display_no is not None:
            by_no.setdefault(h.display_no, []).append(h)

    def ctx(h: ChapterHeading) -> tuple[str, str]:
        prev = headings[h.ordinal - 2].label if h.ordinal >= 2 else ""
        nxt = headings[h.ordinal].label if h.ordinal < len(headings) else ""
        return prev, nxt

    def head_tail(h: ChapterHeading) -> str:
        body = h.content(source)
        nl = body.find("\n")
        body = body[nl + 1:] if nl >= 0 else body
        body = " ".join(body.split())
        return f"{body[:30]} … {body[-30:]}"

    for h in headings:
        prev, nxt = ctx(h)
        base = dict(ordinal=h.ordinal, display_no=h.display_no,
                    label=h.label[:60], body_len=h.body_length(source),
                    prev_label=prev[:40], next_label=nxt[:40],
                    head_tail=head_tail(h))

        if is_page_artifact(h, source):
            anomalies.append(ChapterAnomaly(
                category="page_artifact", suggestion="删除残片，正文并入前一章",
                **base,
            ))
        elif is_update_note(h, source):
            anomalies.append(ChapterAnomaly(
                category="false_heading", suggestion="作者更新说明，删除；若正文有实质内容并入前一章",
                **base,
            ))
        elif is_watermark_tail(h, source):
            anomalies.append(ChapterAnomaly(
                category="false_heading", suggestion="手打水印/网站尾巴/广告行，删除标题行，正文保留一次",
                **base,
            ))
        elif h.body_length(source) == 0:
            # 空章节：标题后紧跟下一标题（纯标题残片，如翻页/分卷占位）
            anomalies.append(ChapterAnomaly(
                category="page_artifact", suggestion="空章节（无正文），删除占位标题",
                **base,
            ))
        elif is_suspicious_short_body(h, source):
            anomalies.append(ChapterAnomaly(
                category="manual_review",
                suggestion="正文过短（<100字），疑似残片/错切，人工核对",
                **base,
            ))

    # 重复章号：所有实例都标记（不止第一条）
    for no, items in by_no.items():
        if len(items) > 1:
            for h in items:
                prev, nxt = ctx(h)
                anomalies.append(ChapterAnomaly(
                    category="duplicate_label",
                    ordinal=h.ordinal, display_no=no, label=h.label[:60],
                    body_len=h.body_length(source),
                    prev_label=prev[:40], next_label=nxt[:40],
                    head_tail=head_tail(h),
                    suggestion="同一 display 章号出现多次：保留正文最完整的一条，其余并入或标记废弃",
                ))

    # 逆序（章号回跳）：疑似原文错标/拼接污染
    for i in range(1, len(headings)):
        cur, prev_h = headings[i], headings[i - 1]
        if (cur.display_no is not None and prev_h.display_no is not None
                and cur.display_no < prev_h.display_no):
            anomalies.append(ChapterAnomaly(
                category="suspected_misnumber",
                ordinal=cur.ordinal, display_no=cur.display_no,
                label=cur.label[:60],
                body_len=cur.body_length(source),
                prev_label=prev_h.label[:40], next_label=ctx(cur)[1][:40],
                head_tail=head_tail(cur),
                suggestion=f"章号 {prev_h.display_no}→{cur.display_no} 回跳：原文错标或拼接污染，人工确认后修正 display 号",
            ))

    # 缺号（source_gap）：连续缺失段
    if headings:
        nos = sorted({h.display_no for h in headings if h.display_no is not None})
        if nos:
            last = nos[-1]
            expected = set(range(1, last + 1))
            missing = sorted(expected - set(nos))
            # 连续段
            i = 0
            while i < len(missing):
                j = i
                while j + 1 < len(missing) and missing[j + 1] == missing[j] + 1:
                    j += 1
                start_no, end_no = missing[i], missing[j]
                anomalies.append(ChapterAnomaly(
                    category="source_gap",
                    ordinal=0, display_no=None,
                    label=f"缺号 {start_no}~{end_no}（共 {end_no - start_no + 1} 章）",
                    body_len=0,
                    suggestion="真实语料缺口或格式变体：与原始来源核对，缺章由人工确认",
                ))
                i = j + 1

    return anomalies


def _segments_of(nos: set[int]) -> list[dict]:
    """号集合 → 连续段 [{start, end, count}]。"""
    if not nos:
        return []
    segs: list[dict] = []
    ordered = sorted(nos)
    s = ordered[0]
    prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
            continue
        segs.append({"start": s, "end": prev, "count": prev - s + 1})
        s = prev = n
    segs.append({"start": s, "end": prev, "count": prev - s + 1})
    return segs


LANDMARK_SPECS = [
    # (display_no, 正文关键词, 名称)
    (613, "铁笼", "铁笼→613"),
    (3195, "梵葵", "梵葵陷阱→3195"),
]


def check_landmarks(
    headings: list[ChapterHeading],
    source: str,
) -> list[dict]:
    """地标验收（dryrun 与决议门槛共用）：铁笼→613、梵葵陷阱→3195、末章→3233(末位)。

    返回 [{name, pass, detail, candidates}]：重复章号时列出全部候选，
    选正文含关键词者；末章要求 display=3233 且位于末位。
    """
    checks: list[dict] = []
    for no, keyword, name in LANDMARK_SPECS:
        cands = [h for h in headings if h.display_no == no]
        if not cands:
            checks.append({"name": name, "pass": False, "detail": "未找到",
                           "candidates": []})
            continue
        hit = [h for h in cands if keyword in h.content(source)]
        if hit:
            checks.append({"name": name, "pass": True,
                           "detail": f"ordinal={hit[0].ordinal} ({hit[0].title})",
                           "candidates": [(h.ordinal, h.title) for h in cands]})
        else:
            checks.append({"name": name, "pass": False,
                           "detail": f"存在 {len(cands)} 个候选但均不含「{keyword}」",
                           "candidates": [(h.ordinal, h.title) for h in cands]})

    # 末章：display 3233 且位于末位
    cands = [h for h in headings if h.display_no == 3233]
    if cands and cands[-1].ordinal == len(headings):
        checks.append({"name": "末章→3233(末位)", "pass": True,
                       "detail": f"ordinal={cands[-1].ordinal} ({cands[-1].title})",
                       "candidates": [(h.ordinal, h.title) for h in cands]})
    else:
        checks.append({"name": "末章→3233(末位)", "pass": False,
                       "detail": "未找到或不在末位",
                       "candidates": [(h.ordinal, h.title) for h in cands]})
    return checks


def build_validation_report(
    source: str,
    headings: list[ChapterHeading],
) -> dict:
    """生成 dry-run 验收报告（JSON）。

    包含：解析统计、末章号、缺号清单、重复/逆序、无法解析标题、区间校验、
    全文覆盖（首标题前/末标题后残段）、伪标题/异常分类汇总。
    """
    report: dict = {
        "total_headings": len(headings),
        "arabic": 0,
        "cn": 0,
        "special": 0,
        "unparsed_heading_count": 0,
        "unparsed_samples": [],
        "duplicate_display_nos": [],
        "out_of_order": [],
        "chapter_gap_manifest": [],
        "last_display_no": None,
        "first_uncovered_offset": 0,
        "tail_uncovered_len": 0,
        "interval_check": {"overlap": 0, "gap": 0, "ok": True},
        "source_content_hash": _sha256(source),
        "anomaly_summary": {},
    }

    # 真正扫描疑似标题但格式未知的行（结构化候选，计数完整不受截断）
    unparsed_all, unparsed_samples = scan_unparsed_headings(source)
    report["unparsed_heading_count"] = len(unparsed_all)
    report["unparsed_samples"] = [
        {"line_no": c.line_no, "offset": c.offset, "text": c.text, "reason": c.reason}
        for c in unparsed_samples
    ]

    prev_no: int | None = None
    prev_ordinal = 0
    display_set: set[int] = set()
    author_skip: set[int] = set()
    range_recovered = 0
    noise_recovered = 0

    for h in headings:
        if h.kind == "arabic":
            report["arabic"] += 1
        elif h.kind == "cn":
            report["cn"] += 1
        elif h.kind == "range_recovered":
            range_recovered += 1
        elif h.kind == "noise_recovered":
            noise_recovered += 1
        elif h.kind == "special":
            report["special"] += 1

        # 作者编号跳跃区间（"第956~967章" → 957~966 为 skip，非缺章）
        for part in h.warning.split(";"):
            if part.startswith("author_numbering_skip:"):
                lo, hi = part.split(":", 1)[1].split("~")
                author_skip.update(range(int(lo), int(hi) + 1))

        if h.display_no is None:
            if "unparsed" in h.warning:
                report["unparsed_heading_count"] += 1
                if len(report["unparsed_samples"]) < 10:
                    report["unparsed_samples"].append(h.label[:50])
            continue

        no = h.display_no
        if no in display_set:
            report["duplicate_display_nos"].append({"display_no": no, "ordinal": h.ordinal})
        display_set.add(no)

        if prev_no is not None and no < prev_no:
            report["out_of_order"].append({
                "ordinal": h.ordinal, "display_no": no,
                "prev_display_no": prev_no, "prev_ordinal": prev_ordinal,
            })
        prev_no = no
        prev_ordinal = h.ordinal

    report["recovered_headings"] = {
        "range_recovered": range_recovered,
        "noise_recovered": noise_recovered,
    }
    report["author_numbering_skip_segments"] = _segments_of(author_skip)

    # 缺号清单：1..last_display_no 之间缺失的 display 章号（排除作者编号跳跃号）
    if display_set:
        last = max(display_set)
        report["last_display_no"] = last
        expected = set(range(1, last + 1)) - author_skip
        missing = sorted(expected - display_set)
        report["chapter_gap_manifest"] = missing
        report["missing_count"] = len(missing)

    # 区间校验：相邻章 end==下一章 start（解析保证），校验首尾覆盖
    if headings:
        report["first_uncovered_offset"] = headings[0].start
        report["tail_uncovered_len"] = len(source) - headings[-1].end
        for i in range(len(headings) - 1):
            if headings[i].end != headings[i + 1].start:
                report["interval_check"]["gap"] += 1
                report["interval_check"]["ok"] = False

    # 异常分类汇总（供 dryrun 分级验收）
    anomalies = classify_anomalies(source, headings)
    summary: dict[str, int] = {}
    for a in anomalies:
        summary[a.category] = summary.get(a.category, 0) + 1
    report["anomaly_summary"] = summary

    return report
