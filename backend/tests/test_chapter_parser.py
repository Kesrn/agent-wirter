"""P-1 章节解析器单测：标题解析 / 伪标题过滤 / 异常分类 / 哈希与区间。"""

import re

from services.chapter_parser import (
    ChapterHeading,
    build_validation_report,
    classify_anomalies,
    cn_to_int,
    parse_headings,
)

# ── 中文数字 ─────────────────────────────────────────────

def test_cn_to_int_basic():
    assert cn_to_int("一") == 1
    assert cn_to_int("十四") == 14
    assert cn_to_int("二十") == 20
    assert cn_to_int("一百二十三") == 123
    assert cn_to_int("二百三十五") == 235
    assert cn_to_int("一千二百") == 1200
    assert cn_to_int("三千零五") == 3005


def test_cn_to_int_invalid():
    assert cn_to_int("") is None
    assert cn_to_int("abc") is None
    assert cn_to_int("甲乙丙") is None


# ── 标题解析 ─────────────────────────────────────────────

def test_parse_headings_basic_arabic():
    src = "\n第1章 世界大变\n内容一。\n第2章 真实的阶级\n内容二。\n"
    hs = parse_headings(src)
    assert len(hs) == 2
    assert hs[0].display_no == 1 and hs[0].title == "世界大变"
    assert hs[1].display_no == 2 and hs[1].title == "真实的阶级"
    assert hs[0].ordinal == 1 and hs[1].ordinal == 2
    # 区间连续性：前章 end == 后章 start，且全文无丢失
    assert hs[0].end == hs[1].start
    assert hs[0].end <= len(src)


def test_parse_headings_chinese_numeric():
    src = "\n第一章 觉醒\n内容。\n第二章 测试\n内容。\n"
    hs = parse_headings(src)
    assert len(hs) == 2
    assert hs[0].display_no == 1 and hs[0].kind == "cn"
    assert hs[1].display_no == 2


def test_parse_headings_special():
    src = "\n楔子\n楔子内容。\n第1章 正文\n内容。\n"
    hs = parse_headings(src)
    assert hs[0].kind == "special" and hs[0].display_no is None
    assert hs[1].display_no == 1


def test_parse_headings_with_spaces_and_fullwidth():
    src = "\n第 613 章 肉丘障碍\n内容。\n第１章　全角\n内容。\n"
    hs = parse_headings(src)
    assert hs[0].display_no == 613
    assert hs[1].display_no == 1


# ── 伪标题过滤（P-1 阶段二） ─────────────────────────────

def test_update_note_detected():
    """作者更新说明（"第一章：中午12点"）应判为 false_heading。"""
    src = (
        "\n第1章 正常\n正文内容足够长，超过一百个字符以避免被误判为短残片。"
        "正文内容足够长，超过一百个字符以避免被误判为短残片。"
        "\n第一章：中午12点\n"
        "\n第2章 继续\n下一章正文内容也要足够长以免误判。"
        "下一章正文内容也要足够长以免误判。"
    )
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    cats = [a.category for a in anoms]
    assert "false_heading" in cats, f"更新说明应判 false_heading，实际 {cats}"


def test_page_artifact_detected():
    """翻页残片（"(第1/1页)"）应判为 page_artifact。"""
    src = (
        "\n第2074章 魔宰 (第1/1页)\n"
        "\n第2075章 正常章\n正文内容足够长超过一百字符以排除短残片误判。"
        "正文内容足够长超过一百字符以排除短残片误判。"
    )
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    assert any(a.category == "page_artifact" for a in anoms)


def test_watermark_tail_detected():
    """手打水印尾巴应判 false_heading（重复标题行之一）。"""
    src = (
        "\n第1426章 杀回去！\n正文内容足够长超过一百字符。正文内容足够长。"
        "\n第1426章 杀回去！是由【无*错】【小-说-网】会员手打，更多章节请到\n"
        "\n第1427章 继续\n正文内容足够长超过一百字符。正文内容足够长。"
    )
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    assert any(a.category == "false_heading" for a in anoms)
    assert any(a.category == "duplicate_label" for a in anoms)


def test_short_body_manual_review():
    """正文过短且无明确模式 → manual_review。"""
    src = "\n第1章 正常\n正文足够长超过一百字符避免误判。正文足够长超过一百字符避免误判。\n第2章 可疑\n短\n第3章 正常\n正文足够长超过一百字符避免误判。正文足够长超过一百字符避免误判。\n"
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    suspicious = [a for a in anoms if a.ordinal == 2]
    assert any(a.category == "manual_review" for a in suspicious)


# ── 异常分类 ─────────────────────────────────────────────

def test_duplicate_label_all_instances():
    src = "\n第1章 A\n正文一足够长超过一百字符。正文一足够长超过一百字符。\n第1章 B\n正文二足够长超过一百字符。正文二足够长超过一百字符。\n"
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    dups = [a for a in anoms if a.category == "duplicate_label"]
    assert len(dups) == 2, "重复章号的所有实例都应标记，而非只标第一条"


def test_misnumber_detected():
    src = "\n第5章 正常\n正文足够长超过一百字符避免误判。正文足够长。\n第2章 回跳\n正文足够长超过一百字符避免误判。正文足够长。\n"
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    assert any(a.category == "suspected_misnumber" for a in anoms)


def test_source_gap_detected():
    src = "\n第1章 A\n正文一足够长超过一百字符。正文一足够长。\n第4章 B\n正文二足够长超过一百字符。正文二足够长。\n"
    hs = parse_headings(src)
    anoms = classify_anomalies(src, hs)
    gaps = [a for a in anoms if a.category == "source_gap"]
    assert gaps, "缺号应生成 source_gap 异常"
    assert "2" in gaps[0].label and "3" in gaps[0].label


# ── 哈希与区间 ───────────────────────────────────────────

def test_full_sha256_hash():
    src = "\n第1章 A\n正文。\n"
    hs = parse_headings(src)
    assert len(hs[0].content_hash) == 64, "应保存完整 SHA-256（64 hex）"
    report = build_validation_report(src, hs)
    assert len(report["source_content_hash"]) == 64
    # 同一内容哈希稳定
    assert hs[0].content_hash == hs[0].content_hash


def test_interval_no_overlap_no_loss():
    src = "\n第1章 A\n正文一。\n第2章 B\n正文二。\n"
    hs = parse_headings(src)
    # 相邻区间首尾相接（解析保证），全文覆盖从首标题到末尾
    assert hs[0].end == hs[1].start
    assert hs[1].end == len(src)
    report = build_validation_report(src, hs)
    assert report["interval_check"]["ok"] is True
    assert report["tail_uncovered_len"] == 0


def test_anomaly_summary_in_report():
    src = "\n第1章 A\n正文一足够长超过一百字符。正文一足够长。\n第2章 B\n正文二足够长超过一百字符。正文二足够长。\n第2章 C (第1/1页)\n"
    hs = parse_headings(src)
    report = build_validation_report(src, hs)
    assert "anomaly_summary" in report
    assert report["anomaly_summary"].get("page_artifact", 0) >= 1


# ── 恢复规则（P-1 阶段四前置：真实标题格式恢复） ─────────

def test_range_heading_recovered_author_skip():
    """'第956~967章 三方聚头' 恢复为 display=967；跳号区间 957~966（956 已存在，不入决议）。"""
    src = (
        "\n第955章 前章\n正文足够长超过一百字符避免误判。正文足够长。"
        "\n第956章 正主\n正文足够长超过一百字符避免误判。正文足够长。"
        "\n第956~967章 三方聚头\n三方聚头正文足够长超过一百字符避免误判。"
        "\n第968章 后章\n正文足够长超过一百字符避免误判。正文足够长。"
    )
    hs = parse_headings(src)
    assert len(hs) == 4
    h = hs[2]
    assert h.kind == "range_recovered", f"应为 range_recovered，实际 {h.kind}"
    assert h.display_no == 967, f"应恢复为 967，实际 {h.display_no}"
    assert "author_numbering_skip:957~966" in h.warning, f"跳号应为 957~966，实际 {h.warning}"

    report = build_validation_report(src, hs)
    # 跳号 957~966 不是缺章；956 是已存在章节，不入决议
    for no in (956, 957, 966):
        assert no not in report["chapter_gap_manifest"], f"{no} 不应算缺章"
    assert report["author_numbering_skip_segments"] == [
        {"start": 957, "end": 966, "count": 10}
    ], f"跳号段应为 957~966 共 10 个，实际 {report['author_numbering_skip_segments']}"


def test_noisy_heading_recovered():
    """'第1313亮晶晶章 大雷爆' 恢复为 display=1313（format_recovered）。"""
    src = (
        "\n第1312章 前章\n正文足够长超过一百字符避免误判。正文足够长。"
        "\n第1313亮晶晶章 大雷爆\n大雷爆正文足够长超过一百字符避免误判。"
        "\n第1314章 后章\n正文足够长超过一百字符避免误判。正文足够长。"
    )
    hs = parse_headings(src)
    h = hs[1]
    assert h.kind == "noise_recovered", f"应为 noise_recovered，实际 {h.kind}"
    assert h.display_no == 1313, f"应恢复为 1313，实际 {h.display_no}"
    assert "format_recovered" in h.warning


def test_unparsed_scan_structured_and_full_count():
    """scan_unparsed_headings 返回结构化候选（行号/偏移/文本/原因），计数完整。"""
    from services.chapter_parser import scan_unparsed_headings

    src = (
        "第1章 正常\n正文足够长超过一百字符避免误判。\n"
        "第956~967章 三方聚头\n正文。\n"      # 已恢复，不算 unparsed
        "第X回 未知格式\n正文。\n"            # 疑似但无法解析 → unparsed
        "第一天第一节课上，班主任薛木生就给所有魔法学生们上了重要的一课：魔法的释放！\n"
    )
    all_c, samples = scan_unparsed_headings(src)
    assert len(all_c) == len(samples)  # 样本数 = 全量（未超 limit）
    texts = [c.text for c in all_c]
    assert any("第X回" in t for t in texts), f"第X回 应在 unparsed 中，实际 {texts}"
    assert not any("956~967" in t for t in texts), "已恢复的 range 标题不应算 unparsed"
    assert all(c.offset >= 0 and c.line_no >= 1 for c in all_c), "候选应含行号与偏移"


def test_unparsed_count_not_truncated_by_sample_limit():
    """真实计数不受 sample_limit 截断（limit 只影响 samples 展示）。"""
    from services.chapter_parser import scan_unparsed_headings

    src = "\n".join(f"第X回 未知格式{i}" for i in range(30)) + "\n"
    all_c, samples = scan_unparsed_headings(src, sample_limit=5)
    assert len(all_c) == 30, f"全量计数应为 30，实际 {len(all_c)}"
    assert len(samples) == 5, "samples 应截断为 5"
