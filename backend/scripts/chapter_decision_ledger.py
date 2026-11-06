"""P-1 阶段三：异常决策账本生成（非破坏性，不写数据库）。

两阶段决策：
  阶段 A（drop 判定）：伪标题/残片/水印/空章节 → auto drop；
  阶段 B（编号推断，在保留序列上）：
    - 正主（与前一保留章连续）→ keep；
    - 重复/回跳实例 → 推断目标号 = 前一保留章 + 1（回跳段按段首推断）：
        目标号在缺号清单且未被保留章节占用 → renumber（高置信，填补缺口）；
        目标号已被保留章节占用 → keep_duplicate（保留重复号，人工裁决）；
        其余 → unresolved。

产出：
  1. anomaly_review.csv     —— 按 ordinal 去重聚合的异常账本（含 auto_decision/人工决策列）
  2. renumber_candidates.csv —— 编号变更候选（keep/renumber/keep_duplicate/unresolved）
  3. gap_review.csv         —— 缺号段独立清单（与原始来源核对用）

用法：
  venv/bin/python scripts/chapter_decision_ledger.py <sqlite备份> <project_id> <source_id> <输出目录>
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.chapter_parser import (
    ChapterHeading,
    build_validation_report,
    classify_anomalies,
    is_page_artifact,
    is_update_note,
    is_watermark_tail,
    parse_headings,
)

FULL_BODY_CHARS = 300
MAX_BACKTRACK = 30


# ── 阶段 A：drop 判定 ───────────────────────────────────

def is_drop_candidate(h: ChapterHeading, source: str) -> bool:
    """伪标题/残片/水印/空章节 → drop。"""
    if is_update_note(h, source):
        return True
    if is_page_artifact(h, source):
        return True
    if is_watermark_tail(h, source):
        return True
    if h.body_length(source) == 0:
        return True
    return False


# ── 阶段 B：编号推断（保留序列上，逐步应用 effective 映射） ──

def build_numbering_decisions(
    headings: list[ChapterHeading],
    source: str,
    gap_nos: set[int],
) -> dict[int, dict]:
    """在保留序列上计算每个章节的编号决策。

    关键：占用集合**随改号逐步更新**（effective 映射）。前一个章节 renumber
    后，后续决策基于新号——否则 2401 组中 ord 2394 改 2400 后，
    ord 2395 仍被误判 keep_duplicate（应正常保留 2401）。

    流程：
    1. drop 判定（伪标题/残片/水印/空章节）；
    2. 回退段优先（display 回退点起、段内 +1 连续）：段首目标 = 段首前章
       effective + 1，段内偏移推断；
    3. 其余保留章节按 ordinal 升序单遍决策：目标 = 前一保留章 effective + 1；
       目标填补缺号且未被占用 → renumber；已被占用 → keep_duplicate；
       与自身标号一致 → keep；其余 → unresolved；
    4. 每次 renumber/keep 后更新 effective 映射，占用集随之变化。
    返回 {ordinal: {decision, target, note}}。
    """
    drops = {h.ordinal for h in headings if is_drop_candidate(h, source)}
    kept = [h for h in headings if h.ordinal not in drops]

    decisions: dict[int, dict] = {
        o: {"decision": "drop", "target": None,
            "note": "伪标题/噪音，drop_entire_span：整段排除，不并入前章"}
        for o in drops
    }
    effective: dict[int, int | None] = {h.ordinal: h.display_no for h in kept}

    def prev_kept(h: ChapterHeading) -> ChapterHeading | None:
        for k in reversed(kept):
            if k.ordinal < h.ordinal:
                return k
        return None

    def occupied_by_others(h: ChapterHeading, target: int) -> bool:
        """target 是否已被其他章节占用（已处理章节的 effective 值 ∪ 未处理章节的原始号）。"""
        for k in kept:
            if k.ordinal == h.ordinal:
                continue
            disp = effective.get(k.ordinal)
            if disp is not None and disp == target:
                return True
        return False

    def decide_target(h: ChapterHeading, target: int, base_note: str) -> dict:
        if h.display_no is not None and target == h.display_no:
            return {"decision": "keep", "target": None,
                    "note": f"{base_note}；目标 {target} 与自身标号一致"}
        if target in gap_nos and not occupied_by_others(h, target):
            return {"decision": "renumber", "target": target,
                    "note": f"{base_note}；目标 {target} 填补缺号，高置信"}
        if occupied_by_others(h, target):
            return {"decision": "keep_duplicate", "target": None,
                    "note": f"{base_note}；目标 {target} 已被其他章节占用，保留重复号待裁决"}
        return {"decision": "unresolved", "target": None,
                "note": f"{base_note}；目标 {target} 既非缺号也非占用，需人工核对"}

    def apply(h: ChapterHeading, dec: dict) -> None:
        decisions[h.ordinal] = dec
        if dec["decision"] in ("renumber",):
            effective[h.ordinal] = dec["target"]
        # keep / keep_duplicate / unresolved 保持原始号

    # 1) 回退段：display 回退点起、段内 +1 连续
    regression_segments: list[list[ChapterHeading]] = []
    i = 1
    while i < len(kept):
        cur, p = kept[i], kept[i - 1]
        if (cur.display_no is not None and p.display_no is not None
                and cur.display_no < p.display_no):
            seg = [cur]
            j = i + 1
            while (j < len(kept) and kept[j].display_no is not None
                   and kept[j - 1].display_no is not None
                   and kept[j].display_no == kept[j - 1].display_no + 1):
                seg.append(kept[j])
                j += 1
            regression_segments.append(seg)
            i = j
        else:
            i += 1

    regression_ords = {h.ordinal for seg in regression_segments for h in seg}
    for seg in regression_segments:
        seg_start = seg[0]
        pp = prev_kept(seg_start)
        if pp is None or effective.get(pp.ordinal) is None:
            for h in seg:
                apply(h, {"decision": "unresolved", "target": None,
                          "note": "回退段无锚点，需人工核对原文"})
            continue
        base = effective[pp.ordinal] + 1
        for h in seg:
            target = base + (h.ordinal - seg_start.ordinal)
            apply(h, decide_target(
                h, target,
                f"回退段推断（段首 ord={seg_start.ordinal}，前章 {pp.ordinal}→{effective[pp.ordinal]}）",
            ))

    # 2) 重复组（排除回退段章节），按 ordinal 升序单遍
    by_no: dict[int, list[ChapterHeading]] = {}
    for h in kept:
        if h.display_no is not None and h.ordinal not in regression_ords:
            by_no.setdefault(h.display_no, []).append(h)
    for no, items in by_no.items():
        if len(items) < 2:
            continue
        for h in sorted(items, key=lambda x: x.ordinal):
            prev = prev_kept(h)
            if prev is None:
                apply(h, {"decision": "keep", "target": None, "note": "首章，keep"})
                continue
            prev_disp = effective.get(prev.ordinal)
            if prev_disp is None:
                apply(h, {"decision": "unresolved", "target": None,
                          "note": "前章无编号，需人工核对"})
                continue
            if h.display_no == prev_disp + 1:
                apply(h, {"decision": "keep", "target": None,
                          "note": "与前一保留章连续（正主）"})
                continue
            target = prev_disp + 1
            apply(h, decide_target(
                h, target, f"重复号 #{no} 的非正主（前一保留章 {prev.ordinal}→{prev_disp}）"))

    # 3) 其余 → keep
    for h in kept:
        if h.ordinal not in decisions:
            apply(h, {"decision": "keep", "target": None, "note": "无冲突"})

    return decisions


def segment_gaps(gaps: list[int]) -> list[dict]:
    """缺号连续段合并为段：{gap_start, gap_end, missing_count}。"""
    segs: list[dict] = []
    i = 0
    while i < len(gaps):
        j = i
        while j + 1 < len(gaps) and gaps[j + 1] == gaps[j] + 1:
            j += 1
        segs.append({
            "gap_start": gaps[i], "gap_end": gaps[j],
            "missing_count": gaps[j] - gaps[i] + 1,
        })
        i = j + 1
    return segs


def project_gaps_after_renumber(
    original_gaps: list[int],
    decisions: dict[int, dict],
) -> list[int]:
    """应用 renumber 填补后的剩余缺号（459/1863-1866/2156/2400 等被移除）。"""
    filled = {d["target"] for d in decisions.values()
              if d["decision"] == "renumber" and d["target"] is not None}
    return sorted(n for n in original_gaps if n not in filled)


def apply_decisions_snapshot(
    headings: list[ChapterHeading],
    decisions: dict[int, dict],
) -> dict:
    """应用全部决策后的最终映射快照（用于不变量验收）。

    返回 {kept_count, regression_count, duplicate_groups, gaps}。
    """
    effective: dict[int, int | None] = {}
    for h in headings:
        d = decisions.get(h.ordinal, {"decision": "keep"})
        if d["decision"] == "drop":
            continue
        if d["decision"] == "renumber":
            effective[h.ordinal] = d["target"]
        else:
            effective[h.ordinal] = h.display_no

    ordered = sorted(effective.items())
    displays = [d for _, d in ordered if d is not None]

    # 回跳数
    regression_count = sum(
        1 for i in range(1, len(displays)) if displays[i] < displays[i - 1]
    )
    # 重复组数（display 出现多次）
    from collections import Counter
    cnt = Counter(displays)
    duplicate_groups = sum(1 for v in cnt.values() if v > 1)

    return {
        "kept_count": len(effective),
        "regression_count": regression_count,
        "duplicate_groups": duplicate_groups,
    }


# ── 阶段四第 0 步：最终决议快照（不可变）+ decisions_hash ──

def _unparsed_resolution(c: "object") -> str:
    """unparsed 候选的自动决议：含句子标点 → false_positive_body_text；否则 pending。"""
    if any(p in c.text for p in ("。", "，", "；", "：")):
        return "false_positive_body_text"
    return "pending_human_review"


def build_resolved_snapshot(
    headings: list[ChapterHeading],
    decisions: dict[int, dict],
    source: str,
    report: dict,
) -> dict:
    """生成不可变的最终决议快照（resolved_decision_ledger）。

    - decisions 按 ordinal 排序的规范化 JSON → decisions_hash（SHA-256）；
    - gap 决议分类：author_numbering_skip（从 heading warning 收集）、
      format_recovered（noise_recovered 章节）、accepted_missing_in_supplied_source
      （projected 剩余缺口）；
    - **ready_for_write 硬门槛（六条件 AND）**：
        unresolved == 0
        AND landmarks_pass（地标 613/3195/3233 全过）
        AND interval_check.ok（区间完整性）
        AND pending_unparsed == 0（无 pending_human_review 候选）
        AND regression_count == 0（应用决策后无回跳）
        AND duplicate_groups == keep_duplicate_count（重复组数与保留重复号一致）
    """
    import hashlib
    import json as _json

    from services.chapter_parser import check_landmarks, scan_unparsed_headings

    snapshot = {
        "schema_version": 1,
        "source_content_hash": report["source_content_hash"],
        "baseline": {
            "total_headings": len(headings),
            "last_display_no": report["last_display_no"],
            "missing_count": report["missing_count"],
        },
        "decisions": {
            "drop": [],
            "renumber": [],
            "keep_duplicate": [],
            "unresolved": [],
        },
    }

    for h in sorted(headings, key=lambda x: x.ordinal):
        d = decisions.get(h.ordinal, {"decision": "keep", "target": None, "note": ""})
        dec = d["decision"]
        if dec == "drop":
            snapshot["decisions"]["drop"].append({
                "ordinal": h.ordinal,
                "display_no": h.display_no,
                "label": h.label,
                "reason": d.get("note", ""),
            })
        elif dec == "renumber":
            snapshot["decisions"]["renumber"].append({
                "ordinal": h.ordinal,
                "display_no": h.display_no,
                "label": h.label,
                "to": d["target"],
                "reason": d.get("note", ""),
            })
        elif dec == "keep_duplicate":
            snapshot["decisions"]["keep_duplicate"].append({
                "ordinal": h.ordinal,
                "display_no": h.display_no,
                "label": h.label,
                "reason": d.get("note", ""),
            })
        elif dec == "unresolved":
            snapshot["decisions"]["unresolved"].append({
                "ordinal": h.ordinal,
                "display_no": h.display_no,
                "label": h.label,
                "reason": d.get("note", ""),
            })

    # gap 决议分类
    author_skip = set()
    recovered_nos = []
    for h in headings:
        for part in h.warning.split(";"):
            if part.startswith("author_numbering_skip:"):
                lo, hi = part.split(":", 1)[1].split("~")
                author_skip.update(range(int(lo), int(hi) + 1))
        if h.kind == "noise_recovered" and h.display_no is not None:
            recovered_nos.append(h.display_no)
    projected = project_gaps_after_renumber(report["chapter_gap_manifest"], decisions)
    snapshot["gap_resolution"] = {
        "author_numbering_skip": segment_gaps(sorted(author_skip)),
        "format_recovered": sorted(recovered_nos),
        "accepted_missing_in_supplied_source": {
            "segments": segment_gaps(projected),
            "count": len(projected),
        },
    }

    # decisions_hash：按 ordinal 排序的规范化最终决议 JSON
    norm = {
        "schema_version": 1,
        "source_content_hash": report["source_content_hash"],
        "decisions": {
            k: sorted(v, key=lambda x: (x.get("ordinal") or 0, x.get("display_no") or 0))
            for k, v in snapshot["decisions"].items()
        },
        "gap_resolution": snapshot["gap_resolution"],
    }
    snapshot["decisions_hash"] = hashlib.sha256(
        _json.dumps(norm, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # ── ready_for_write 硬门槛（六条件 AND） ──
    gate = {}

    gate["unresolved_zero"] = len(snapshot["decisions"]["unresolved"]) == 0

    landmarks = check_landmarks(headings, source)
    gate["landmarks_pass"] = all(c["pass"] for c in landmarks)
    snapshot["landmark_checks"] = landmarks

    gate["interval_ok"] = bool(report.get("interval_check", {}).get("ok"))

    unparsed_all, _samples = scan_unparsed_headings(source)
    pending = [c for c in unparsed_all if _unparsed_resolution(c) == "pending_human_review"]
    gate["pending_unparsed_zero"] = len(pending) == 0
    snapshot["unparsed_resolution"] = [
        {
            "line_no": c.line_no,
            "offset": c.offset,
            "text": c.text,
            "reason": c.reason,
            "resolution": _unparsed_resolution(c),
        }
        for c in unparsed_all
    ]

    applied = apply_decisions_snapshot(headings, decisions)
    snapshot["applied"] = applied
    gate["regression_zero"] = applied["regression_count"] == 0

    keep_dup_groups = {
        d["display_no"] for d in snapshot["decisions"]["keep_duplicate"]
    }
    gate["duplicate_groups_match"] = (
        applied["duplicate_groups"] == len(keep_dup_groups)
    )

    snapshot["ready_gate"] = gate
    ready = all(gate.values())
    snapshot["ready_for_write"] = ready
    if ready:
        snapshot["resolved_verdict"] = "PASS"
    else:
        failed = [k for k, v in gate.items() if not v]
        snapshot["resolved_verdict"] = f"FAIL：门槛未满足 {failed}"
    return snapshot


# ── 主流程 ──────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    db_path, project_id, source_id, out_dir = sys.argv[1:5]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT content FROM project_sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    ).fetchone()
    if not row:
        print(f"source not found: {source_id}")
        return 1
    source = row[0]

    headings = parse_headings(source)
    report = build_validation_report(source, headings)
    gap_nos = set(report["chapter_gap_manifest"])
    anomalies = classify_anomalies(source, headings)
    decisions = build_numbering_decisions(headings, source, gap_nos)

    # 分类优先级
    PRIORITY = ["false_heading", "page_artifact", "duplicate_label",
                "suspected_misnumber", "manual_review"]
    by_ordinal: dict[int, list] = {}
    for a in anomalies:
        if a.ordinal == 0:
            continue
        by_ordinal.setdefault(a.ordinal, []).append(a)

    def head_tail_text(h: ChapterHeading) -> str:
        body = h.content(source)
        nl = body.find("\n")
        body = body[nl + 1:] if nl >= 0 else body
        body = " ".join(body.split())
        return f"{body[:28]} … {body[-28:]}"

    ledger_rows: list[dict] = []
    for ordinal in sorted(by_ordinal):
        items = by_ordinal[ordinal]
        h = next(h for h in headings if h.ordinal == ordinal)
        cats = sorted({a.category for a in items},
                      key=lambda c: PRIORITY.index(c) if c in PRIORITY else 99)
        d = decisions.get(ordinal, {"decision": "keep", "target": None, "note": ""})
        prev_label = headings[ordinal - 2].label if ordinal >= 2 else ""
        next_label = headings[ordinal].label if ordinal < len(headings) else ""
        ledger_rows.append({
            "ordinal": ordinal,
            "display_no": h.display_no if h.display_no is not None else "",
            "category": cats[0],
            "sub_category": "/".join(cats[1:]),
            "label": h.label[:60],
            "body_len": h.body_length(source),
            "prev_label": prev_label[:36],
            "next_label": next_label[:36],
            "head_tail": head_tail_text(h),
            "auto_decision": d["decision"],
            "target_display": d["target"] if d["target"] else "",
            "auto_note": d["note"],
            "decision": "",   # 人工裁决
            "note": "",
        })

    with open(out / "anomaly_review.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ledger_rows[0].keys()) if ledger_rows else [])
        w.writeheader()
        w.writerows(ledger_rows)

    # ── renumber / keep_duplicate 候选 ──
    renum_rows = []
    for h in headings:
        d = decisions.get(h.ordinal)
        if d and d["decision"] in ("renumber", "keep_duplicate", "unresolved"):
            renum_rows.append({
                "ordinal": h.ordinal,
                "display_no": h.display_no if h.display_no is not None else "",
                "label": h.label[:50],
                "body_len": h.body_length(source),
                "auto_decision": d["decision"],
                "target_display": d["target"] if d["target"] else "",
                "auto_note": d["note"],
                "decision": "",
                "note": "",
            })
    with open(out / "renumber_candidates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(renum_rows[0].keys()) if renum_rows else [])
        w.writeheader()
        w.writerows(renum_rows)

    # ── gap_review.csv：缺号段（原始 + 决策后未解决） ──
    gaps = report["chapter_gap_manifest"]
    projected_gaps = project_gaps_after_renumber(gaps, decisions)
    gap_rows = []
    for seg in segment_gaps(gaps):
        gap_rows.append({**seg, "stage": "original", "decision": "", "note": ""})
    for seg in segment_gaps(projected_gaps):
        gap_rows.append({**seg, "stage": "projected", "decision": "", "note": ""})
    with open(out / "gap_review.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "stage", "gap_start", "gap_end", "missing_count", "decision", "note",
        ])
        w.writeheader()
        w.writerows(gap_rows)

    # ── 汇总 ──
    from collections import Counter
    stats = Counter(d["decision"] for d in decisions.values())
    snapshot = apply_decisions_snapshot(headings, decisions)

    # ── 阶段四第 0 步：最终决议快照（不可变）+ resolved 报告 ──
    resolved = build_resolved_snapshot(headings, decisions, source, report)
    resolved_path = out / "resolved_decision_ledger.json"
    resolved_path.write_text(
        json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    # 更新验收报告：应用决策后的最终状态
    report["decisions_hash"] = resolved["decisions_hash"]
    report["resolved_verdict"] = resolved["resolved_verdict"]
    report["ready_for_write"] = resolved["ready_for_write"]
    report["gap_resolution"] = resolved["gap_resolution"]
    json_path = out / "validation_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"异常账本: {out / 'anomaly_review.csv'} ({len(ledger_rows)} 个独立 ordinal)")
    print(f"编号候选: {out / 'renumber_candidates.csv'} ({len(renum_rows)} 项)")
    print(f"缺号清单: {out / 'gap_review.csv'} (原始 {len(gaps)} 章 / {len(segment_gaps(gaps))} 段 → 决策后 {len(projected_gaps)} 章 / {len(segment_gaps(projected_gaps))} 段)")
    print("决策统计(全部章节):", dict(stats))
    print("最终映射快照:", snapshot)
    print(f"最终决议快照: {resolved_path}")
    print(f"resolved_verdict={resolved['resolved_verdict']} | ready_for_write={resolved['ready_for_write']} | decisions_hash={resolved['decisions_hash'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
