"""章节质量检查模块单元测试。"""

import pytest
from services.chapter_quality import (
    QualityIssue,
    QualityReport,
    EditEffectivenessReport,
    check_length,
    check_forbidden_metadata,
    check_heading,
    check_ending,
    check_repetition,
    validate_draft_quality,
    validate_editor_effectiveness,
    validate_edited_draft_quality,
    validate_final_quality,
    clean_generated_content,
    count_non_space_chars,
    normalize_for_comparison,
    calculate_edit_change_metrics,
    critique_requires_revision,
    build_retry_prompt_errors,
)


# ── 字数统计 ──


def test_count_non_space_chars_normal():
    assert count_non_space_chars("hello world") == 10
    assert count_non_space_chars("你好 世界") == 4
    assert count_non_space_chars("   ") == 0
    assert count_non_space_chars("") == 0
    assert count_non_space_chars("a\nb\tc") == 3


def test_count_non_space_chars_cjk():
    text = "这是测试文本，包含中文标点和英文 mixed 在一起的情况。"
    clean = text.replace(" ", "").replace("\n", "").replace("\t", "")
    assert count_non_space_chars(text) == len(clean)


# ── 内容清理 ──


def test_clean_generated_content_removes_headings():
    text = "# 第一章 出发\n正文内容..."
    result = clean_generated_content(text)
    assert "第一章" not in result or not result.startswith("#")
    assert "正文内容" in result


def test_clean_generated_content_removes_metadata_tags():
    text = "【环境与状态铺垫 — 增强开始】\n正文章节内容。\n【增强结束】\n结尾。"
    result = clean_generated_content(text)
    assert "增强开始" not in result
    assert "增强结束" not in result
    assert "正文章节内容" in result
    assert "结尾" in result


def test_clean_generated_content_removes_scene_headings():
    text = "## 场景 1：训练\n正文内容\n## 写作说明\n不应该出现"
    result = clean_generated_content(text)
    assert "场景 1" not in result
    assert "写作说明" not in result
    assert "正文内容" in result
    assert "不应该出现" in result


# ── normalize_for_comparison ──


def test_normalize_for_comparison_whitespace():
    text1 = "你好世界这是测试。"
    text2 = "你好 世界 这是测试。"
    assert normalize_for_comparison(text1) == normalize_for_comparison(text2)


# ── 1. 字数硬约束 ──


def test_length_pass():
    # 目标 2000，实际 2100 → 通过
    text = "a" * 2100
    passed, issues, actual, min_c, max_c = check_length(text, 2000)
    assert passed
    assert len(issues) == 0
    assert actual == 2100
    assert min_c == 1600
    assert max_c == 2400


def test_length_too_long_is_non_blocking_warning():
    # 目标 2000，实际 3999 → 保留候选稿，交给人工决定
    text = "a" * 3999
    passed, issues, actual, min_c, max_c = check_length(text, 2000)
    assert passed
    assert len(issues) == 1
    assert issues[0].code == "CONTENT_TOO_LONG"
    assert issues[0].severity == "WARNING"
    assert "人工审核" in issues[0].message


def test_length_too_short_is_non_blocking_warning():
    text = "a" * 500
    passed, issues, _, _, _ = check_length(text, 2000)
    assert passed
    assert any(i.code == "CONTENT_TOO_SHORT" for i in issues)
    assert all(i.severity == "WARNING" for i in issues)


def test_length_skip_when_target_zero():
    passed, issues, actual, _, _ = check_length("any text", 0)
    assert passed
    assert len(issues) == 0


# ── 2. 内部元信息禁止 ──


def test_forbidden_metadata_enhance_tag():
    text = "正文内容\n【环境与状态铺垫 — 增强开始】\n后续内容"
    passed, issues = check_forbidden_metadata(text)
    assert not passed
    assert any(i.code == "METADATA_LEAK" for i in issues)


def test_forbidden_metadata_end_tag():
    text = "正文内容\n【增强结束】\n结尾。"
    passed, issues = check_forbidden_metadata(text)
    assert not passed


def test_forbidden_metadata_scene_heading():
    text = "正文。\n## 场景 3：决斗\n内容"
    passed, issues = check_forbidden_metadata(text)
    assert not passed
    assert any(i.code == "METADATA_HEADING" for i in issues)


def test_forbidden_metadata_instruction_line():
    text = "正文。\n场景目标：展示主角成长\n"
    passed, issues = check_forbidden_metadata(text)
    assert not passed
    assert any(i.code == "METADATA_INSTRUCTION" for i in issues)


def test_forbidden_metadata_clean_text():
    text = "这是干净的小说正文，没有任何标记。主角走出了房间。"
    passed, issues = check_forbidden_metadata(text)
    assert passed
    assert len(issues) == 0


# ── 3. Markdown 标题 ──


def test_heading_removed():
    text = "# 第一章 黎明\n这是正文内容。"
    had, issues, cleaned = check_heading(text)
    assert had or len(issues) > 0
    assert "第一章" not in cleaned or cleaned.startswith("这是")


def test_heading_no_heading():
    text = "正文直接开始，没有标题。"
    had, issues, _ = check_heading(text)
    assert not had


# ── 4. 完整结尾 ──


def test_ending_valid_period():
    passed, issues = check_ending("故事在这里结束了。")
    assert passed
    assert len(issues) == 0


def test_ending_valid_exclamation():
    passed, issues = check_ending("非常精彩！")
    assert passed


def test_ending_valid_ascii_dialogue_quote():
    passed, issues = check_ending('“我别无选择。”'.replace('”', '"'))
    assert passed
    assert issues == []


def test_ending_comma():
    passed, issues = check_ending("故事还没结束，")
    assert not passed
    assert any("逗号" in i.message for i in issues)


def test_ending_empty():
    passed, issues = check_ending("")
    assert not passed
    assert any(i.code == "EMPTY_CONTENT" for i in issues)


def test_ending_colon():
    passed, issues = check_ending("接下来是：")
    assert not passed


def test_ending_without_terminal_punctuation_is_error():
    passed, issues = check_ending("故事在这里突然中断")
    assert not passed
    assert any(i.code == "INCOMPLETE_ENDING" and i.severity == "ERROR" for i in issues)


# ── 5. 重复检测 ──


def test_repetition_clean():
    issues = check_repetition("这是一个干净的段落。每个段落都不同。内容丰富多样。")
    # 应该没有或极少重复
    assert all(i.severity == "WARNING" for i in issues)


# ── 6. validate_draft_quality ──


def test_draft_quality_pass():
    # 构造一个恰好 2100 字符的合理中文文本
    segment = "他走在清晨的街道上，四周弥漫着薄雾。远处的钟楼传来缓慢的钟声。"
    text = ("第一章正文内容。" + segment * 100)[:2149] + "。"  # ~2100 chars, complete ending
    report = validate_draft_quality(text, 2000)
    errors = [i for i in report.issues if i.severity == "ERROR"]
    assert len(errors) == 0


def test_draft_too_long_reaches_human_review_with_warning():
    # 精彩的小说正文。 = 8 chars, * 400 = 3200 > 2400
    text = "精彩的小说正文。" * 400
    report = validate_draft_quality(text, 2000)
    assert report.passed
    assert any(i.code == "CONTENT_TOO_LONG" for i in report.issues)
    assert not any(i.severity == "ERROR" for i in report.issues)


def test_draft_with_metadata_fails():
    text = ("正文内容小说。" * 70)[:2100] + "\n【增强开始】\n正文。"
    report = validate_draft_quality(text, 2000)
    assert not report.passed
    assert any(i.code == "METADATA_LEAK" for i in report.issues)


# ── 7. Editor 有效性 ──


def test_editor_effectiveness_no_revision_required():
    # Critic 判定无需修改
    eff = validate_editor_effectiveness("原文内容。", "原文内容。", {"requires_revision": False})
    assert eff.requires_revision is False
    assert eff.passed is True
    assert eff.similarity == 1.0


def test_editor_effectiveness_similarity_too_high():
    # Critic 要求修改，但编辑稿与初稿完全相同 → 失败
    eff = validate_editor_effectiveness("原文不变的内容。", "原文不变的内容。", {"requires_revision": True})
    assert eff.requires_revision is True
    assert eff.passed is False
    assert eff.similarity == 1.0


def test_editor_effectiveness_changed():
    # Critic 要求修改，编辑稿确实改动
    eff = validate_editor_effectiveness(
        "原始初稿内容包含大量文字。",
        "修改后的编辑稿内容不一样。",  # 不同
        {"requires_revision": True},
    )
    assert eff.requires_revision is True
    # 由于两个文本确实不同，similarity 应该 < 0.98
    assert eff.similarity < 1.0


def test_editor_effectiveness_only_tag_removal():
    # 只删除增强标签，其余正文完全相同 → 不算有效编辑
    initial = "【增强开始】\n正文内容完全相同。\n【增强结束】"
    edited = "正文内容完全相同。"  # 只删了标签
    eff = validate_editor_effectiveness(initial, edited, {"requires_revision": True})
    # 标签被 clean_generated_content 清理后，内容完全相同
    assert eff.passed is False
    assert eff.similarity == 1.0
    assert eff.changed_chars == 0


def test_equal_length_substantive_rewrite_counts_changed_characters():
    initial = "甲" * 200
    edited = "乙" * 200
    similarity, changed, changed_original, inserted = calculate_edit_change_metrics(initial, edited)
    assert similarity == 0.0
    assert changed == 200
    assert changed_original == 200
    assert inserted == 0

    eff = validate_editor_effectiveness(initial, edited, {"requires_revision": True})
    assert eff.passed is True
    assert eff.changed_chars == 200


def test_appended_filler_does_not_count_as_effective_edit():
    initial = "甲乙丙丁" * 100
    edited = initial + ("新增" * 50)
    eff = validate_editor_effectiveness(initial, edited, {"requires_revision": True})
    assert eff.changed_chars == 100
    assert eff.changed_original_chars == 0
    assert eff.inserted_chars == 100
    assert eff.passed is False


def test_exact_copy_has_zero_changed_characters():
    text = "正文内容" * 100
    eff = validate_editor_effectiveness(text, text, {"requires_revision": True})
    assert eff.similarity == 1.0
    assert eff.changed_chars == 0
    assert eff.changed_original_chars == 0
    assert eff.passed is False


def test_p1_and_actionable_instructions_require_revision():
    assert critique_requires_revision({"p1": ["节奏拖沓"]}) is True
    assert critique_requires_revision({"p2": ["措辞重复"]}) is True
    assert critique_requires_revision({"edit_instructions": {"rewrite": ["重写结尾"]}}) is True
    assert critique_requires_revision({"edit_instructions": {"keep": ["保留开头"]}}) is False


def test_missing_writer_baseline_cannot_bypass_required_revision():
    eff = validate_editor_effectiveness("", "编辑后的完整正文。" * 100, {"p1": ["需要修改"]})
    assert eff.requires_revision is True
    assert eff.passed is False


# ── 8. validate_edited_draft_quality ──


def test_edited_draft_quality_with_effective_edit():
    initial = "初稿内容需要修改一些表达方式。"
    edited = "修改后的编辑稿，完全重写了表达方式。"
    report = validate_edited_draft_quality(initial, edited, 20, {"requires_revision": True})
    assert report.stage == "edited"


# ── 9. validate_final_quality ──


def test_final_quality_pass():
    segment = "故事讲述了主角踏上旅途的种种经历，沿途风景美不胜收。"
    text = segment * 100  # ~2100 chars
    report = validate_final_quality(text[:2099] + "。", 2000)
    errors = [i for i in report.issues if i.severity == "ERROR"]
    assert len(errors) == 0


def test_final_quality_empty_fails():
    report = validate_final_quality("", 2000)
    assert not report.passed


def test_final_quality_with_failed_editor():
    text = "正文内容。"
    eff = EditEffectivenessReport(requires_revision=True, similarity=1.0, changed_chars=0, passed=False)
    report = validate_final_quality(text, 20, edit_effectiveness=eff)
    assert not report.passed
    assert any(i.code == "EDIT_EFFECTIVENESS_FAILED" for i in report.issues)


def test_final_quality_carries_editor_metrics_for_approval_modal():
    text = "完整正文内容，包含自然结束标点。" * 10
    eff = EditEffectivenessReport(
        requires_revision=True,
        similarity=0.75,
        changed_chars=120,
        changed_original_chars=90,
        inserted_chars=30,
        passed=True,
    )
    report = validate_final_quality(text, count_non_space_chars(text), edit_effectiveness=eff)
    assert report.similarity == 0.75
    assert report.changed_chars == 120
    assert report.requires_revision is True
    assert report.metrics["changed_original_chars"] == 90


# ── 10. build_retry_prompt_errors ──


def test_build_retry_prompt_with_errors():
    report = QualityReport(
        passed=False, stage="draft", target_chars=2000, actual_chars=3999,
        min_chars=1600, max_chars=2400,
        issues=[
            QualityIssue(code="CONTENT_TOO_LONG", severity="ERROR", message="字数超标：实际 3999 字"),
            QualityIssue(code="METADATA_LEAK", severity="ERROR", message="正文包含内部标记"),
        ],
    )
    prompt = build_retry_prompt_errors(report, 2000)
    assert "3999" in prompt
    assert "1600" in prompt
    assert "2400" in prompt
    assert "METADATA_LEAK" in prompt or "内部标记" in prompt


def test_build_retry_prompt_no_errors():
    report = QualityReport(passed=True, stage="draft", target_chars=2000, actual_chars=2100,
                          min_chars=1600, max_chars=2400, issues=[])
    prompt = build_retry_prompt_errors(report, 2000)
    assert prompt == ""


# ── 11. 报告序列化 ──


def test_quality_report_to_dict():
    report = QualityReport(
        passed=True, stage="draft", target_chars=2000, actual_chars=2100,
        min_chars=1600, max_chars=2400,
        issues=[QualityIssue(code="TEST", severity="WARNING", message="test")],
    )
    d = report.to_dict()
    assert d["passed"] is True
    assert d["stage"] == "draft"
    assert len(d["issues"]) == 1
    assert d["issues"][0]["code"] == "TEST"


def test_edit_effectiveness_report_to_dict():
    eff = EditEffectivenessReport(
        requires_revision=True, similarity=0.91, changed_chars=312, passed=True,
        instruction_results=[{"id": "E1", "applied": True}],
    )
    d = eff.to_dict()
    assert d["passed"] is True
    assert d["similarity"] == 0.91
    assert d["changed_chars"] == 312
