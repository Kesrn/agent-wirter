"""P-1 决策账本单测：伪标题判定 / 编号推断 / 撞号保护 / 缺号判定。

对应评审七项验收中的：道歉/晚上不误判、drop 后重算、撞号标记、
1863-1866 高置信、ord 1/2/3 keep、gap 清单。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.chapter_decision_ledger import (
    build_numbering_decisions,
    is_drop_candidate,
)
from services.chapter_parser import parse_headings

# 构造原文辅助：章节列表 [(display_no, title, body)]
def make_src(chapters: list[tuple[int | None, str, str]]) -> str:
    parts = []
    for no, title, body in chapters:
        label = f"第{no}章 {title}" if no is not None else title
        parts.append(f"{label}\n{body}\n")
    return "\n".join(parts)


def decisions_of(src: str, gap_nos: set[int] | None = None) -> dict:
    headings = parse_headings(src)
    return build_numbering_decisions(headings, src, gap_nos or set())


# ── 验收 1：正常标题含"道歉/晚上"不得判伪标题 ────────────

def test_normal_titles_with_apology_and_evening_not_dropped():
    src = make_src([
        (1124, "议长秘书的道歉", "正文内容足够长超过一百字符以避免被误判为短残片。" * 2),
        (1125, "晚上是禽兽", "正文内容足够长超过一百字符以避免被误判为短残片。" * 2),
    ])
    hs = parse_headings(src)
    assert not is_drop_candidate(hs[0], src), "含'道歉'的正常标题不得判伪标题"
    assert not is_drop_candidate(hs[1], src), "含'晚上'的正常标题不得判伪标题"


def test_update_note_with_colon_time_still_dropped():
    """作者更新说明格式（"第一章：中午12点"）仍应判伪标题。"""
    src = make_src([
        (1, "正常", "正文内容足够长超过一百字符以避免被误判为短残片。" * 2),
        (1, "：中午12点", ""),
    ])
    hs = parse_headings(src)
    assert is_drop_candidate(hs[1], src), "冒号+时间格式的更新说明应判伪标题"


# ── 验收 2/4/5：drop 后重算、ord 1/2/3 keep、1863-1866 高置信 ──

def test_ord1_2_3_keep_after_fake_headings_dropped():
    """伪标题（第1/2/3章重复实例）drop 后，正主第 1/2/3 章必须 keep。"""
    src = make_src([
        (1, "世界大变", "正文一足够长超过一百字符避免误判。" * 2),
        (2, "真实的阶级", "正文二足够长超过一百字符避免误判。" * 2),
        (3, "开学大典", "正文三足够长超过一百字符避免误判。" * 2),
        (1, "：中午12点", ""),
        (2, "：傍晚18点", ""),
        (3, "：晚上24点", ""),
        (4, "法系觉醒", "正文四足够长超过一百字符避免误判。" * 2),
    ])
    d = decisions_of(src)
    for ordinal in (1, 2, 3):
        assert d[ordinal]["decision"] == "keep", f"ord {ordinal} 应 keep，实际 {d[ordinal]}"
    assert d[7]["decision"] == "keep", f"ord 7（法系觉醒）应 keep，实际 {d[7]}"
    for ordinal in (4, 5, 6):
        assert d[ordinal]["decision"] == "drop", f"伪标题 ord {ordinal} 应 drop，实际 {d[ordinal]}"


def test_regression_segment_1863_high_confidence_when_filling_gap():
    """回退段（标 1683-1686）目标 1863-1866 在缺号清单 → 全部 renumber。"""
    src = make_src([
        (1861, "A", "正文足够长超过一百字符避免误判。" * 2),
        (1862, "B", "正文足够长超过一百字符避免误判。" * 2),
        (1683, "就是暴脾气", "正文足够长超过一百字符避免误判。" * 2),
        (1684, "墙头草", "正文足够长超过一百字符避免误判。" * 2),
        (1685, "垃圾城", "正文足够长超过一百字符避免误判。" * 2),
        (1686, "噬主邪鸟", "正文足够长超过一百字符避免误判。" * 2),
        (1867, "C", "正文足够长超过一百字符避免误判。" * 2),
    ])
    # 缺号清单：1863-1866
    gap = set(range(1863, 1867))
    d = decisions_of(src, gap_nos=gap)
    for ordinal, expected in ((3, 1863), (4, 1864), (5, 1865), (6, 1866)):
        dec = d[ordinal]
        assert dec["decision"] == "renumber", f"ord {ordinal} 应 renumber，实际 {dec}"
        assert dec["target"] == expected, f"ord {ordinal} 目标应 {expected}，实际 {dec['target']}"


# ── 验收 3：renumber 撞号 → keep_duplicate / unresolved ──

def test_renumber_collision_becomes_keep_duplicate():
    """非正主推断目标已被保留章节占用（726 存在）→ keep_duplicate，不得 renumber。"""
    src = make_src([
        (724, "A", "正文足够长超过一百字符避免误判。" * 2),
        (725, "令人发指的食物（下）", "正文足够长超过一百字符避免误判。" * 2),
        (725, "复仇法师！", "正文足够长超过一百字符避免误判。" * 2),
        (726, "B", "正文足够长超过一百字符避免误判。" * 2),
    ])
    d = decisions_of(src)
    # ord 2 与前一保留章（724）连续 → 正主 keep；ord 3 推断 726 但被占用 → keep_duplicate
    assert d[2]["decision"] == "keep", f"正主应 keep，实际 {d[2]}"
    assert d[3]["decision"] == "keep_duplicate", f"撞号应 keep_duplicate，实际 {d[3]}"


def test_renumber_filling_gap_high_confidence():
    """非正主推断目标在缺号清单且未被占用（459 缺号）→ renumber。"""
    src = make_src([
        (457, "A", "正文足够长超过一百字符避免误判。" * 2),
        (458, "敦煌之地", "正文足够长超过一百字符避免误判。" * 2),
        (458, "沙啸虎！", "正文足够长超过一百字符避免误判。" * 2),
        (460, "B", "正文足够长超过一百字符避免误判。" * 2),
    ])
    d = decisions_of(src, gap_nos={459})
    assert d[3]["decision"] == "renumber", f"填补缺号应 renumber，实际 {d[3]}"
    assert d[3]["target"] == 459


# ── 验收 6：gap 清单段合并 ──────────────────────────────

def test_gap_segments_are_contiguous_ranges():
    """缺号连续段应合并为段（957-967 一段，不是 11 条）。"""
    from scripts.chapter_decision_ledger import segment_gaps

    segs = segment_gaps(list(range(957, 968)) + [134, 135, 677])
    by_start = {s["gap_start"]: s for s in segs}
    assert by_start[957]["gap_end"] == 967 and by_start[957]["missing_count"] == 11
    assert by_start[134]["gap_end"] == 135 and by_start[134]["missing_count"] == 2
    assert by_start[677]["missing_count"] == 1


def test_gap_projection_after_renumber():
    """renumber 填补的缺号应从 projected 清单移除。"""
    from scripts.chapter_decision_ledger import project_gaps_after_renumber

    gaps = list(range(1863, 1867)) + [459, 2156, 2400, 100]
    decisions = {
        1: {"decision": "renumber", "target": 1863},
        2: {"decision": "renumber", "target": 1864},
        3: {"decision": "renumber", "target": 1865},
        4: {"decision": "renumber", "target": 1866},
        5: {"decision": "renumber", "target": 459},
        6: {"decision": "renumber", "target": 2156},
        7: {"decision": "renumber", "target": 2400},
    }
    projected = project_gaps_after_renumber(gaps, decisions)
    assert projected == [100], f"已填补缺号应移除，剩 {projected}"


# ── 闭环 1：2401 组逐步应用 effective 映射 ──────────────

def test_2401_group_recomputed_after_effective_update():
    """ord 2394 改 2400 后，ord 2395 应正常保留 2401（不再 keep_duplicate）。"""
    src = make_src([
        (2399, "前章", "正文足够长超过一百字符避免误判。" * 2),
        (2401, "制定规则的人", "正文足够长超过一百字符避免误判。" * 2),
        (2401, "我缺条狗", "正文足够长超过一百字符避免误判。" * 2),
        (2402, "后章", "正文足够长超过一百字符避免误判。" * 2),
    ])
    d = decisions_of(src, gap_nos={2400})
    assert d[2]["decision"] == "renumber" and d[2]["target"] == 2400, f"ord2 应 renumber→2400，实际 {d[2]}"
    assert d[3]["decision"] == "keep", f"ord3 应 keep（2401 与改号后的前章连续），实际 {d[3]}"


# ── 最终映射不变量（对应实际应用全部决策后的统计） ───────

def _build_realistic_src() -> str:
    """模拟真实错标结构：回退段 + 重复组 + 伪标题 + 缺号。"""
    chs: list[tuple[int | None, str, str]] = []
    body = "正文足够长超过一百字符避免被误判为短残片。" * 2
    # 正常 1..5
    for n in range(1, 6):
        chs.append((n, f"章{n}", body))
    # 伪标题（drop）
    chs.append((1, "：中午12点", ""))
    # 正常 6..8
    for n in range(6, 9):
        chs.append((n, f"章{n}", body))
    # 重复组 458
    chs.append((457, "前", body))
    chs.append((458, "敦煌之地", body))
    chs.append((458, "沙啸虎！", body))
    chs.append((460, "后", body))
    # 回退段（标 1683-1685，应为 1863-1865）
    chs.append((1862, "锚", body))
    chs.append((1683, "就是暴脾气", body))
    chs.append((1684, "墙头草", body))
    chs.append((1685, "垃圾城", body))
    chs.append((1866, "尾", body))
    return make_src(chs)


def test_final_mapping_invariants():
    """应用全部决策后：无回跳、重复仅剩 keep_duplicate 组、缺号被填补。"""
    from scripts.chapter_decision_ledger import (
        apply_decisions_snapshot,
        build_numbering_decisions,
        project_gaps_after_renumber,
    )

    src = _build_realistic_src()
    headings = parse_headings(src)
    gap_nos = {459, 1863, 1864, 1865, 2400}
    decisions = build_numbering_decisions(headings, src, gap_nos)
    snapshot = apply_decisions_snapshot(headings, decisions)

    # 不变量：应用后无回跳
    assert snapshot["regression_count"] == 0, f"应用决策后应无回跳，实际 {snapshot}"
    # 重复组只剩 keep_duplicate 保留的组
    keep_dup_groups = {
        h.display_no for h in headings
        if decisions.get(h.ordinal, {}).get("decision") == "keep_duplicate"
    }
    assert snapshot["duplicate_groups"] == len(keep_dup_groups), (
        f"最终重复组数应等于 keep_duplicate 组数，实际 {snapshot} vs {len(keep_dup_groups)}"
    )
    # 缺号被 renumber 填补
    projected = project_gaps_after_renumber(list(gap_nos), decisions)
    renumbered = {d["target"] for d in decisions.values() if d["decision"] == "renumber"}
    assert all(t not in projected for t in renumbered), "renumber 目标不应留在 projected 缺号中"


def test_drop_entire_span_semantics_note():
    """drop 的 note 必须声明整段排除（drop_entire_span），不并入前章。"""
    src = make_src([
        (1, "正常", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (1, "：中午12点", ""),
    ])
    d = decisions_of(src)
    assert "drop_entire_span" in d[2]["note"], f"drop 应声明整段排除，实际 {d[2]}"


# ── ready_for_write 硬门槛（六条件 AND，每个条件一个失败测试） ──

def _resolve(src: str, gap_nos: set[int] | None = None) -> dict:
    """便捷：解析 → 决策 → 决议快照。"""
    from scripts.chapter_decision_ledger import (
        build_numbering_decisions,
        build_resolved_snapshot,
    )
    from services.chapter_parser import build_validation_report

    headings = parse_headings(src)
    report = build_validation_report(src, headings)
    decisions = build_numbering_decisions(headings, src, gap_nos or set())
    return build_resolved_snapshot(headings, decisions, src, report)


def test_gate_unresolved_zero():
    """门槛：unresolved 存在 → ready_for_write=False。"""
    src = make_src([
        (1, "A", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (1, "B", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (1, "C", "正文足够长超过一百字符避免被误判为短残片。" * 2),
    ])
    r = _resolve(src)
    assert r["ready_for_write"] is False
    assert r["ready_gate"]["unresolved_zero"] is False


def test_gate_landmarks_pass():
    """门槛：地标失败（铁笼不在 613）→ ready_for_write=False。"""
    src = make_src([
        (1, "普通开头", "正文足够长超过一百字符避免被误判为短残片。" * 2),
    ])
    r = _resolve(src)
    assert r["ready_for_write"] is False
    assert r["ready_gate"]["landmarks_pass"] is False


def test_gate_interval_ok():
    """门槛：区间完整性失败 → ready_for_write=False。"""
    src = make_src([
        (613, "铁笼", "正文足够长超过一百字符避免被误判为短残片。铁笼很大。" * 2),
        (3195, "梵葵陷阱", "正文足够长超过一百字符避免被误判为短残片。梵葵。" * 2),
        (3233, "人间", "正文足够长超过一百字符避免被误判为短残片。" * 2),
    ])
    headings = parse_headings(src)
    from services.chapter_parser import build_validation_report
    report = build_validation_report(src, headings)
    report["interval_check"]["ok"] = False  # 人为破坏区间完整性
    from scripts.chapter_decision_ledger import (
        build_numbering_decisions,
        build_resolved_snapshot,
    )
    decisions = build_numbering_decisions(headings, src, set())
    r = build_resolved_snapshot(headings, decisions, src, report)
    assert r["ready_for_write"] is False
    assert r["ready_gate"]["interval_ok"] is False


def test_gate_pending_unparsed_zero():
    """门槛：存在 pending_human_review 的 unparsed 候选 → ready_for_write=False。"""
    src = make_src([
        (613, "铁笼", "正文足够长超过一百字符避免被误判为短残片。铁笼很大。" * 2),
        (3195, "梵葵陷阱", "正文足够长超过一百字符避免被误判为短残片。梵葵。" * 2),
        (3233, "人间", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        # 无标点疑似标题 → pending_human_review
        (None, "", ""),
    ])
    # 追加一行"第X回 无标点"（无句子标点 → pending）
    src += "第X回 无标点\n"
    r = _resolve(src)
    assert r["ready_for_write"] is False
    assert r["ready_gate"]["pending_unparsed_zero"] is False


def test_gate_regression_zero():
    """门槛：应用决策后仍有回跳 → ready_for_write=False。"""
    src = make_src([
        (5, "A", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (2, "B", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (3, "C", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (613, "铁笼", "正文足够长超过一百字符避免被误判为短残片。铁笼很大。" * 2),
        (3195, "梵葵陷阱", "正文足够长超过一百字符避免被误判为短残片。梵葵。" * 2),
        (3233, "人间", "正文足够长超过一百字符避免被误判为短残片。" * 2),
    ])
    r = _resolve(src)
    assert r["ready_for_write"] is False
    assert r["ready_gate"]["regression_zero"] is False


def test_gate_duplicate_groups_match():
    """门槛：实际重复组数 != keep_duplicate 组数 → ready_for_write=False。"""
    # 一个重复组但无法改号（目标号被占用）→ keep_duplicate；另一个重复组
    # 无法归入任何决策 → 组数不匹配
    src = make_src([
        (1, "A", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (1, "B", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (613, "铁笼", "正文足够长超过一百字符避免被误判为短残片。铁笼很大。" * 2),
        (3195, "梵葵陷阱", "正文足够长超过一百字符避免被误判为短残片。梵葵。" * 2),
        (3233, "人间", "正文足够长超过一百字符避免被误判为短残片。" * 2),
    ])
    r = _resolve(src)
    assert r["ready_for_write"] is False
    assert r["ready_gate"]["duplicate_groups_match"] is False


def test_gate_all_pass_when_clean():
    """门槛：全部条件满足 → ready_for_write=True（真实基线的缩小版）。"""
    src = make_src([
        (612, "前", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (613, "铁笼", "正文足够长超过一百字符避免被误判为短残片。铁笼很大。" * 2),
        (3194, "前", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (3195, "梵葵陷阱", "正文足够长超过一百字符避免被误判为短残片。梵葵。" * 2),
        (3232, "前", "正文足够长超过一百字符避免被误判为短残片。" * 2),
        (3233, "人间，惹不起了", "正文足够长超过一百字符避免被误判为短残片。" * 2),
    ])
    r = _resolve(src)
    assert r["ready_for_write"] is True, f"干净基线应通过，实际 {r['ready_gate']}"
    assert r["resolved_verdict"] == "PASS"
