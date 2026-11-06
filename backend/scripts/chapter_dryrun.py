"""P-1 阶段二：章节切分 dry-run 报告（非破坏性，不写数据库）。

从真实桌面库（只读）读取原文 txt，用 chapter_parser 重新解析章节，输出：
  1. chapters CSV（source_ordinal / display_chapter_no / original_label /
     title / start_offset / end_offset / content_hash / warning）
  2. validation report JSON（解析统计 / 缺号清单 / 重复 / 逆序 / 区间校验 /
     异常分类汇总 / 全文哈希）
  3. anomaly review CSV（六类异常清单，人工只审可疑项）
  4. 分级验收状态：地标通过 ≠ 验收通过，异常未清零不得进入写库阶段

用法：
  venv/bin/python scripts/chapter_dryrun.py <sqlite备份> <project_id> <source_id> <输出目录>
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.chapter_parser import (
    build_validation_report,
    classify_anomalies,
    parse_headings,
    scan_unparsed_headings,
)


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    db_path, project_id, source_id, out_dir = sys.argv[1:5]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 只读连接：杜绝 dry-run 意外写库
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT content FROM project_sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    ).fetchone()
    if not row:
        print(f"source not found: {source_id}")
        return 1
    source = row[0]
    print(f"原文长度: {len(source)} 字符")

    headings = parse_headings(source)
    report = build_validation_report(source, headings)
    unparsed_all, _unparsed_samples = scan_unparsed_headings(source)
    anomalies = classify_anomalies(source, headings)

    # ── CSV ──
    csv_path = out / "chapters_dryrun.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "source_ordinal", "display_chapter_no", "kind",
            "original_label", "title",
            "start_offset", "end_offset", "content_hash", "warning",
        ])
        for h in headings:
            w.writerow([
                h.ordinal,
                h.display_no if h.display_no is not None else "",
                h.kind,
                h.label,
                h.title,
                h.start,
                h.end,
                h.content_hash,
                h.warning,
            ])
    print(f"CSV: {csv_path} ({len(headings)} 行)")

    # ── 异常复核清单 ──
    anomaly_path = out / "anomaly_review.csv"
    with open(anomaly_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "category", "ordinal", "display_chapter_no", "label",
            "body_len", "prev_label", "next_label", "head_tail", "suggestion",
        ])
        for a in anomalies:
            w.writerow([
                a.category,
                a.ordinal or "",
                a.display_no if a.display_no is not None else "",
                a.label, a.body_len, a.prev_label, a.next_label,
                a.head_tail, a.suggestion,
            ])
    print(f"异常清单: {anomaly_path} ({len(anomalies)} 项)")

    # ── 地标验收（重复章号时列出全部候选，选内容含关键词者，否则标 ambiguous）──
    def candidates_with_no(no: int):
        return [h for h in headings if h.display_no == no]

    def check_landmark(no: int, keyword: str | None):
        cands = candidates_with_no(no)
        if not cands:
            return False, "未找到", []
        if keyword is None:
            return True, f"ordinal={cands[-1].ordinal} ({cands[-1].title})", [(h.ordinal, h.title) for h in cands]
        hit = [h for h in cands if keyword in h.content(source)]
        if hit:
            return True, f"ordinal={hit[0].ordinal} ({hit[0].title})", [(h.ordinal, h.title) for h in cands]
        return False, f"存在 {len(cands)} 个候选但均不含「{keyword}」", [(h.ordinal, h.title) for h in cands]

    checks = []
    ok, detail, cands = check_landmark(613, "铁笼")
    checks.append(("铁笼→613", ok, detail, cands))
    ok, detail, cands = check_landmark(3195, "梵葵")
    checks.append(("梵葵陷阱→3195", ok, detail, cands))
    ok, detail, cands = check_landmark(3233, None)
    ok = ok and bool(cands) and cands[-1][0] == report["total_headings"]
    checks.append(("末章→3233(末位)", ok, detail, cands))

    print("\n=== 地标验收 ===")
    for name, ok_, detail, cands in checks:
        mark = "PASS" if ok_ else "FAIL"
        extra = f"（候选 {len(cands)} 处）" if len(cands) > 1 else ""
        print(f"  [{mark}] {name}  {detail}{extra}")

    print("\n=== 报告摘要 ===")
    print(f"  总标题: {report['total_headings']} (阿拉伯 {report['arabic']} / 中文数字 {report['cn']} / 特殊 {report['special']})")
    print(f"  末章号: {report['last_display_no']} | 缺号: {report.get('missing_count', 0)}")
    print(f"  重复章号: {len(report['duplicate_display_nos'])} | 逆序: {len(report['out_of_order'])}")
    print(f"  全文哈希: {report['source_content_hash'][:16]}...")
    print(f"  异常分类: {report['anomaly_summary']}")

    # ── 分级验收状态 ──
    landmarks_pass = all(ok_ for _, ok_, _, _ in checks)
    total_anomalies = len(anomalies)
    if not landmarks_pass:
        verdict = "FAIL：地标未通过，不得进入写库阶段"
        code = 1
    elif total_anomalies > 0:
        verdict = f"地标通过，但存在 {total_anomalies} 项异常待人工复核——尚未验收通过，不得进入写库阶段"
        code = 1
    else:
        verdict = "PASS：地标通过且无异常"
        code = 0

    # ── JSON 报告（全部检查完成后写入，含地标与验收状态） ──
    report["landmark_checks"] = [
        {"name": name, "pass": ok_, "detail": detail, "candidates": cands}
        for name, ok_, detail, cands in checks
    ]
    report["landmarks_pass"] = landmarks_pass
    report["verdict"] = verdict
    report["unparsed_resolution"] = [
        {
            "line_no": c.line_no,
            "offset": c.offset,
            "text": c.text,
            "reason": c.reason,
            # 含句末/句中标点 → 正文句子特征，固化 false_positive_body_text 候选
            "resolution": (
                "false_positive_body_text" if any(p in c.text for p in ("。", "，", "；", "："))
                else "pending_human_review"
            ),
        }
        for c in unparsed_all
    ]
    json_path = out / "validation_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {json_path} (含 landmark_checks/landmarks_pass/verdict/unparsed_resolution)")

    print(f"\n验收状态: {verdict}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
