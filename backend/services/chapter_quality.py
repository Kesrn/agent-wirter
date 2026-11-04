"""Chapter quality service — 确定性质量检查模块。

硬规则优先用确定性代码，LLM 只负责文学语义判断。代码负责：
- 字数（去空白后字符计数，±20% 建议范围；超出仅提醒，由人工决定）
- 内部元信息禁止（【增强开始/结束】等标记）
- Markdown 标题/正文边界
- 完整结尾检查（末尾标点）
- 文本相似度（SequenceMatcher）
- 重复与扩写膨胀指标
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

QUALITY_TRACE = logging.getLogger("GENERATION_TRACE")  # 复用现有等级

# ── 质量检查数据结构 ──────────────────────────────────


@dataclass
class QualityIssue:
    code: str
    severity: str  # ERROR / WARNING
    message: str
    evidence: str | None = None


@dataclass
class QualityReport:
    passed: bool
    stage: str  # draft / edited / final
    target_chars: int = 0
    actual_chars: int = 0
    min_chars: int = 0
    max_chars: int = 0
    similarity: float | None = None
    changed_chars: int | None = None
    requires_revision: bool | None = None  # 只用于 edited 阶段
    duplicate_heading_removed: bool = False
    issues: list[QualityIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转为可序列化 dict，供 SSE / AiRunStep output snapshot 使用。"""
        return {
            "passed": self.passed,
            "stage": self.stage,
            "target_chars": self.target_chars,
            "actual_chars": self.actual_chars,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "similarity": self.similarity,
            "changed_chars": self.changed_chars,
            "requires_revision": self.requires_revision,
            "duplicate_heading_removed": self.duplicate_heading_removed,
            "issues": [{"code": i.code, "severity": i.severity, "message": i.message, "evidence": i.evidence} for i in self.issues],
            "metrics": self.metrics,
        }


@dataclass
class EditEffectivenessReport:
    """Editor 有效性报告。"""
    requires_revision: bool
    similarity: float
    changed_chars: int
    changed_original_chars: int = 0
    inserted_chars: int = 0
    instruction_results: list[dict] = field(default_factory=list)
    passed: bool = False

    def to_dict(self) -> dict:
        return {
            "requires_revision": self.requires_revision,
            "similarity": self.similarity,
            "changed_chars": self.changed_chars,
            "changed_original_chars": self.changed_original_chars,
            "inserted_chars": self.inserted_chars,
            "instruction_results": self.instruction_results,
            "passed": self.passed,
        }


# ── 工具函数 ──────────────────────────────────────────


def clean_generated_content(text: str) -> str:
    """清理生成正文中的常见杂质，返回纯正文。

    与前端 cleanGeneratedContent 保持一致，但后端用于质量检查前预处理。
    注意：清理后的正文仍要进行质量检查，不能把"清理成功"当质量通过。
    """
    if not text:
        return ""
    result = text
    # 去除开头的 Markdown 标题（章节名由系统管理）
    result = re.sub(r"^\s*#+\s*第[^章]*章[^\n]*\n*", "", result)
    # 去除内部增强标签
    result = re.sub(r"【[^】]*(?:增强开始|增强结束|开始|结束)[^】]*】", "", result)
    # 去除 Markdown 场景标题和写作说明
    result = re.sub(r"(^|\n)\s*#{1,6}\s*(?:场景|写作说明|修改说明|审校意见|任务卡)[^\n]*\n*", "", result)
    # 去除"场景目标/必须包含/禁止包含"等写作指导行
    result = re.sub(r"(?:场景目标|必须包含|禁止包含|word_budget)\s*[:：][^\n]*\n*", "", result)
    return result.strip()


def count_non_space_chars(text: str) -> int:
    """去空白后的字符数（与前端 cleanGeneratedContent 口径一致）。"""
    return len(re.sub(r"\s+", "", text or ""))


def normalize_for_comparison(text: str) -> str:
    """为相似度比较做规范化：去空白、统一标点。"""
    if not text:
        return ""
    t = re.sub(r"\s+", "", text)
    # 统一中文标点不影响检测结果，但去除格式噪声
    return t


def calculate_edit_change_metrics(initial: str, edited: str) -> tuple[float, int, int, int]:
    """计算编辑相似度和实际变更规模。

    ``changed_chars`` 不能用两个文本的长度差：等长全文重写会被误报为 0，
    单纯在末尾追加内容又可能伪装成有效编辑。这里基于 SequenceMatcher opcode：

    - replace 计 ``max(旧片段长度, 新片段长度)``；
    - delete 计被删除长度；insert 计新增长度；
    - ``changed_original_chars`` 单独统计原文中被替换/删除的字符数，防止
      “原文完全不动，只在末尾追加填充”通过编辑有效性验收。
    """
    matcher = SequenceMatcher(None, initial, edited, autojunk=False)
    changed_chars = 0
    changed_original_chars = 0
    inserted_chars = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        original_len = i2 - i1
        edited_len = j2 - j1
        changed_chars += max(original_len, edited_len)
        changed_original_chars += original_len
        if edited_len > original_len:
            inserted_chars += edited_len - original_len
    return matcher.ratio(), changed_chars, changed_original_chars, inserted_chars


def critique_requires_revision(structural_critique: dict | None) -> bool:
    """兼容新旧 Critic JSON，确定是否存在需要 Editor 落实的修改。

    旧提示词通常不输出 ``requires_revision``，因此不能只检查该字段或 P0。
    任意 P0/P1/P2 问题，或 delete/merge/rewrite 等可执行指令，都意味着需要
    实质修改；仅 ``keep`` 指令不要求改写。
    """
    if not structural_critique or not isinstance(structural_critique, dict):
        return False
    if structural_critique.get("requires_revision") is True:
        return True
    for key in ("p0", "p1", "p2"):
        issues = structural_critique.get(key)
        if isinstance(issues, list) and any(bool(item) for item in issues):
            return True
    instructions = structural_critique.get("edit_instructions")
    if isinstance(instructions, dict):
        for key in ("delete", "merge", "rewrite", "replace", "add"):
            actions = instructions.get(key)
            if isinstance(actions, list) and any(bool(item) for item in actions):
                return True
    return False


# ── 内部元信息禁止规则 ────────────────────────────────


FORBIDDEN_PATTERNS = [
    (r"【[^】]*(?:增强开始|增强结束|开始|结束)[^】]*】", "METADATA_LEAK", "正文包含内部规划标记"),
    (r"(^|\n)+\s*#{1,6}\s*(?:场景|写作说明|修改说明|审校意见|任务卡)", "METADATA_HEADING", "正文包含 Markdown 场景/写作说明标题"),
    (r"(^|\n)\s*(?:场景目标|必须包含|禁止包含|word_budget)\s*[:：]", "METADATA_INSTRUCTION", "正文包含写作指导字段（场景目标/必须包含/禁止包含等）"),
    (r"(?:以下是|下面是).{0,20}(?:正文|修改稿|候选稿)", "METADATA_INTRO", "正文包含元信息引导语"),
]

# ── 完整结尾规则 ───────────────────────────────────────


VALID_ENDINGS = (
    "。", "！", "？", "…",
    "。”", "！”", "？”",
    '。"', '！"', '？"',
    "。'", "！'", "？'",
)

INVALID_ENDING_PATTERNS = [
    (r"[,，]\s*$", "INCOMPLETE_COMMA", "正文以逗号结尾，疑似截断"),
    (r"[：:]\s*$", "INCOMPLETE_COLON", "正文以冒号结尾，疑似截断"),
    (r"[——\-—]\s*$", "INCOMPLETE_DASH", "正文以破折号结尾，疑似截断"),
    (r"`.?.?.?`\s*$", "UNCLOSED_CODE", "正文包含未闭合的内联代码块"),
]


# ── 重复与膨胀指标 ──────────────────────────────────────


def _detect_repeated_paragraphs(text: str, threshold: int = 2) -> list[QualityIssue]:
    """检测重复段落。"""
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(paragraphs) < threshold:
        return []
    seen: dict[str, list[int]] = {}
    for i, para in enumerate(paragraphs):
        if len(para) < 20:
            continue
        key = para[:80]
        seen.setdefault(key, []).append(i)
    issues: list[QualityIssue] = []
    for key, indices in seen.items():
        if len(indices) >= threshold:
            issues.append(QualityIssue(
                code="REPEATED_PARAGRAPH",
                severity="WARNING",
                message=f"疑似重复段落出现了 {len(indices)} 次",
                evidence=key[:100],
            ))
    return issues


def _detect_consecutive_repeat(text: str, window: int = 3) -> list[QualityIssue]:
    """检测连续重复句。"""
    if not text:
        return []
    sentences = re.split(r"[。！？…\.!\?]", text or "")
    sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) >= 8]
    if len(sentences) < window:
        return []
    ngram_counter: dict[str, int] = {}
    for i in range(len(sentences) - window + 1):
        ngram = "|".join(sentences[i:i + window])
        ngram_counter[ngram] = ngram_counter.get(ngram, 0) + 1
    issues: list[QualityIssue] = []
    for ngram, count in ngram_counter.items():
        if count >= 2:
            issues.append(QualityIssue(
                code="CONSECUTIVE_REPEAT",
                severity="WARNING",
                message=f"连续 {window} 句重复出现了 {count} 次",
                evidence=" → ".join(ngram.split("|")[:2]) + "...",
            ))
    return issues[:3]  # 最多报告 3 个


def _detect_ngram_repetition(text: str, n: int = 4, threshold_ratio: float = 0.08) -> list[QualityIssue]:
    """检测高频 n-gram 重复率。"""
    if not text:
        return []
    clean = re.sub(r"\s+", "", text)
    if len(clean) < n * 5:
        return []
    grams: dict[str, int] = {}
    for i in range(len(clean) - n + 1):
        grams.setdefault(clean[i:i + n], 0)
        grams[clean[i:i + n]] += 1
    repeated = {k: v for k, v in grams.items() if v >= 5}
    if repeated:
        max_count = max(repeated.values())
        total_unique = len(grams)
        if total_unique > 0 and max_count / total_unique > threshold_ratio:
            most_repeated = max(repeated, key=repeated.get)  # type: ignore[arg-type]
            return [QualityIssue(
                code="NGRAM_REPETITION",
                severity="WARNING",
                message=f"文本 n-gram 重复率偏高（最高 {n}-gram 出现 {repeated[most_repeated]} 次）",
                evidence=most_repeated,
            )]
    return []


# ── 核心检查函数 ───────────────────────────────────────


def check_length(text: str, target_words: int) -> tuple[bool, list[QualityIssue], int, int, int]:
    """字数软约束检查。

    字数属于创作偏好，不属于技术正确性。超出建议范围时保留候选稿并输出
    WARNING，让用户在最终人工审核中选择直接采用、让 AI 调整或丢弃；
    不再触发整章重写和失败回滚。

    Returns:
        passed, issues, actual_chars, min_chars, max_chars
    """
    actual = count_non_space_chars(text)
    if target_words <= 0:
        return True, [], actual, 0, 0
    min_chars = int(target_words * 0.8)
    max_chars = int(target_words * 1.2)
    issues: list[QualityIssue] = []
    if actual < min_chars:
        issues.append(QualityIssue(
            code="CONTENT_TOO_SHORT",
            severity="WARNING",
            message=f"篇幅偏短：目标约 {target_words} 字，实际 {actual} 字，建议不少于 {min_chars} 字，请在人工审核时确认",
        ))
    elif actual > max_chars:
        issues.append(QualityIssue(
            code="CONTENT_TOO_LONG",
            severity="WARNING",
            message=f"篇幅偏长：目标约 {target_words} 字，实际 {actual} 字，建议不超过 {max_chars} 字，请在人工审核时确认",
        ))
    return True, issues, actual, min_chars, max_chars


def check_forbidden_metadata(text: str) -> tuple[bool, list[QualityIssue]]:
    """检查正文中的禁止标记。"""
    issues: list[QualityIssue] = []
    for pattern, code, message in FORBIDDEN_PATTERNS:
        for match in re.finditer(pattern, text, re.MULTILINE | re.IGNORECASE):
            issues.append(QualityIssue(
                code=code,
                severity="ERROR",
                message=message,
                evidence=match.group(0)[:120],
            ))
    # 只保留第一个匹配的证据
    seen_codes: set[str] = set()
    unique: list[QualityIssue] = []
    for issue in issues:
        if issue.code not in seen_codes:
            seen_codes.add(issue.code)
            unique.append(issue)
    return len(unique) == 0, unique


def check_heading(text: str) -> tuple[bool, list[QualityIssue], str]:
    """检查开头 Markdown 标题并安全移除。

    Returns:
        had_heading, issues, cleaned_text
    """
    issues: list[QualityIssue] = []
    cleaned = text
    pattern = r"^\s*#+\s*第[^章]*章[^\n]*\n*"
    match = re.match(pattern, text)
    if match:
        issues.append(QualityIssue(
            code="DUPLICATE_HEADING",
            severity="WARNING",
            message="正文开头包含重复章节标题，已自动移除",
            evidence=match.group(0).strip(),
        ))
        cleaned = text[match.end():]
    return bool(match), issues, cleaned


def check_ending(text: str) -> tuple[bool, list[QualityIssue]]:
    """检查末尾是否自然结束。"""
    if not text or not text.strip():
        return False, [QualityIssue(
            code="EMPTY_CONTENT",
            severity="ERROR",
            message="正文为空",
        )]
    stripped = text.strip()
    # 先检查有效结尾
    for valid_end in VALID_ENDINGS:
        if stripped.endswith(valid_end):
            return True, []
    # 检查无效结尾模式
    issues: list[QualityIssue] = []
    for pattern, code, message in INVALID_ENDING_PATTERNS:
        if re.search(pattern, stripped, re.MULTILINE):
            issues.append(QualityIssue(
                code=code,
                severity="ERROR",
                message=message,
                evidence=stripped[-50:],
            ))
            break
    if not issues:
        # 以其他字符结尾
        issues.append(QualityIssue(
            code="INCOMPLETE_ENDING",
            severity="ERROR",
            message=f"正文结尾不是常见结束标点，可能未完成",
            evidence=stripped[-50:],
        ))
    return False, issues


def check_repetition(text: str) -> list[QualityIssue]:
    """检查重复与扩写膨胀指标（只出 WARNING）。"""
    issues: list[QualityIssue] = []
    issues.extend(_detect_repeated_paragraphs(text))
    issues.extend(_detect_consecutive_repeat(text))
    issues.extend(_detect_ngram_repetition(text))
    return issues


def validate_draft_quality(
    text: str,
    target_words: int,
    run_id: str = "",
) -> QualityReport:
    """对 Writer 初稿进行确定性质量检查。

    这是 chapter_writer 之后的第一个质量门：
    - 字数检查（WARNING，不阻断）
    - 内部标记检查（ERROR）
    - 标题重复（WARNING，自动清理）
    - 结尾检查（ERROR/WARNING）
    - 重复膨胀指标（WARNING）
    """
    original = text or ""
    had_heading, heading_issues, cleaned = check_heading(original)
    length_passed, length_issues, actual_chars, min_chars, max_chars = check_length(cleaned, target_words)
    metadata_passed, meta_issues = check_forbidden_metadata(cleaned)
    ending_passed, ending_issues = check_ending(cleaned)
    rep_issues = check_repetition(cleaned)

    all_issues: list[QualityIssue] = []
    all_issues.extend(length_issues)
    all_issues.extend(meta_issues)
    all_issues.extend(heading_issues)
    all_issues.extend(ending_issues)
    all_issues.extend(rep_issues)

    errors = [i for i in all_issues if i.severity == "ERROR"]
    passed = len(errors) == 0

    report = QualityReport(
        passed=passed,
        stage="draft",
        target_chars=target_words,
        actual_chars=actual_chars,
        min_chars=min_chars,
        max_chars=max_chars,
        duplicate_heading_removed=had_heading,
        issues=all_issues,
        metrics={
            "had_duplicate_heading": had_heading,
            "cleaned_chars": count_non_space_chars(cleaned),
            "original_chars": count_non_space_chars(original),
        },
    )

    QUALITY_TRACE.info(
        "QUALITY_TRACE stage=draft run_id=%s target=%d actual=%d passed=%s issues=%s",
        run_id, target_words, actual_chars, passed,
        [i.code for i in errors],
    )
    return report


def validate_editor_effectiveness(
    initial_draft: str,
    edited_draft: str,
    structural_critique: dict | None = None,
) -> EditEffectivenessReport:
    """验证 Editor 是否对正文进行了实质性修改。

    Critic 要求修改时：
    - similarity <= 0.98
    - changed_chars >= max(80, len(initial) * 0.03)
    - 仅删除内部标签不算有效编辑（基于清理后正文计算差异）

    Critic 无需修改时：允许跳过或相似度 1.0。
    """
    requires_revision = critique_requires_revision(structural_critique)

    if not initial_draft or not edited_draft:
        return EditEffectivenessReport(
            requires_revision=requires_revision,
            similarity=0.0,
            changed_chars=0,
            passed=False,
        )

    # 基于清理后的正文比较
    clean_initial = clean_generated_content(initial_draft)
    clean_edited = clean_generated_content(edited_draft)

    norm_initial = normalize_for_comparison(clean_initial)
    norm_edited = normalize_for_comparison(clean_edited)

    if not norm_initial or not norm_edited:
        return EditEffectivenessReport(
            requires_revision=requires_revision,
            similarity=0.0 if not norm_initial and not norm_edited else 0.0,
            changed_chars=0,
            passed=False,
        )

    similarity, changed_chars, changed_original_chars, inserted_chars = calculate_edit_change_metrics(
        norm_initial, norm_edited,
    )

    if not requires_revision:
        # Critic 认为无需修改，可以跳过或相似度 1.0
        return EditEffectivenessReport(
            requires_revision=False,
            similarity=similarity,
            changed_chars=changed_chars,
            changed_original_chars=changed_original_chars,
            inserted_chars=inserted_chars,
            passed=True,
        )

    # Critic 要求修改，检查是否实际修改
    min_changed = max(80, int(len(norm_initial) * 0.03))
    min_original_changed = max(40, int(len(norm_initial) * 0.015))
    similarity_ok = similarity <= 0.98
    changed_ok = changed_chars >= min_changed
    original_changed_ok = changed_original_chars >= min_original_changed

    instruction_results: list[dict] = []
    edit_instructions = (structural_critique or {}).get("edit_instructions", {}) if structural_critique else {}
    if edit_instructions and isinstance(edit_instructions, dict):
        # 汇总各类指令的执行状态（确定性判断先用整体修改量代替）
        total_actions = sum(len(v) for v in edit_instructions.values() if isinstance(v, list))
        if total_actions > 0:
            instruction_results.append({
                "id": "overall",
                "applied": similarity_ok and changed_ok and original_changed_ok,
                "evidence": (
                    f"similarity={similarity:.3f}, changed_chars={changed_chars}, "
                    f"changed_original_chars={changed_original_chars}, inserted_chars={inserted_chars}"
                ),
            })

    passed = similarity_ok and changed_ok and original_changed_ok

    return EditEffectivenessReport(
        requires_revision=True,
        similarity=similarity,
        changed_chars=changed_chars,
        changed_original_chars=changed_original_chars,
        inserted_chars=inserted_chars,
        instruction_results=instruction_results,
        passed=passed,
    )


def validate_edited_draft_quality(
    initial_draft: str,
    edited_draft: str,
    target_words: int,
    structural_critique: dict | None = None,
    run_id: str = "",
) -> QualityReport:
    """对 Editor 修改后的正文进行质量检查 + 编辑有效性验证。"""
    report = validate_draft_quality(edited_draft, target_words, run_id=run_id)
    report.stage = "edited"

    eff = validate_editor_effectiveness(initial_draft, edited_draft, structural_critique)
    report.similarity = eff.similarity
    report.changed_chars = eff.changed_chars
    report.requires_revision = eff.requires_revision
    report.metrics.update({
        "changed_original_chars": eff.changed_original_chars,
        "inserted_chars": eff.inserted_chars,
    })

    if not eff.passed and eff.requires_revision:
        report.issues.append(QualityIssue(
            code="EDITOR_NOT_EFFECTIVE",
            severity="ERROR",
            message=(
                f"编辑未落实审校意见：相似度 {eff.similarity:.3f}（要求 ≤0.98），"
                f"变更字符 {eff.changed_chars}，触及原文字符 {eff.changed_original_chars}"
            ),
        ))
        report.passed = False

    QUALITY_TRACE.info(
        "QUALITY_TRACE stage=edited run_id=%s similarity=%.3f changed_chars=%d passed=%s",
        run_id, eff.similarity, eff.changed_chars, report.passed,
    )
    return report


def validate_final_quality(
    edited_draft: str,
    target_words: int,
    edit_effectiveness: EditEffectivenessReport | None = None,
    run_id: str = "",
) -> QualityReport:
    """最终质量门：在 human_review 前执行。

    通过条件：
    - 无 ERROR issues
    - 正文非空
    - 长度超出建议范围时仅提示，由人工决定
    - 无禁止元数据
    - 结尾完整
    - 如果 Critic 要求修改，editor 有效性必须通过
    """
    report = validate_draft_quality(edited_draft, target_words, run_id=run_id)
    report.stage = "final"

    if edit_effectiveness:
        report.similarity = edit_effectiveness.similarity
        report.changed_chars = edit_effectiveness.changed_chars
        report.requires_revision = edit_effectiveness.requires_revision
        report.metrics.update({
            "changed_original_chars": edit_effectiveness.changed_original_chars,
            "inserted_chars": edit_effectiveness.inserted_chars,
        })
        if edit_effectiveness.requires_revision and not edit_effectiveness.passed:
            report.issues.append(QualityIssue(
                code="EDIT_EFFECTIVENESS_FAILED",
                severity="ERROR",
                message="编辑有效性验证未通过，不能进入人工审核",
            ))
            report.passed = False

    # 额外：确保非空
    if not edited_draft or not edited_draft.strip():
        report.issues.append(QualityIssue(
            code="EMPTY_CONTENT",
            severity="ERROR",
            message="最终正文为空",
        ))
        report.passed = False

    QUALITY_TRACE.info(
        "QUALITY_TRACE stage=final run_id=%s passed=%s",
        run_id, report.passed,
    )
    return report


def build_retry_prompt_errors(report: QualityReport, target_words: int) -> str:
    """根据质量报告生成重试提示（注入 writer/editor prompt）。"""
    errors = [i for i in report.issues if i.severity == "ERROR"]
    if not errors:
        return ""

    lines = ["上一次输出未通过系统验收："]
    for i, issue in enumerate(errors, 1):
        lines.append(f"{i}. {issue.message}")
    lines.append(f"\n目标字数约 {target_words} 字，请控制在 {report.min_chars}～{report.max_chars} 字之间。")
    lines.append("禁止输出场景标题、增强标签、写作说明和修改说明。")
    lines.append("请重新输出完整纯正文，不要解释。")
    return "\n".join(lines)
