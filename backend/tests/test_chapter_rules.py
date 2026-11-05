"""确定性章节规则校验器的单元测试。所有文本均为本地编写。"""

from __future__ import annotations

import json

import pytest


def _validate(text="", task_card=None, characters=None, **config):
    from services.chapter_rules import RulesConfig, validate_task_card_compliance

    return validate_task_card_compliance(
        text,
        task_card,
        characters or [],
        config=RulesConfig(**config) if config else None,
    )


def _codes(issues):
    return {issue.code for issue in issues}


def _card(**overrides):
    value = {
        "scenes": [],
        "forbidden": [],
        "information_rules": {"forbidden": []},
    }
    value.update(overrides)
    return value


def test_must_include_full_phrase_passes():
    issues = _validate("夜色降临，星门开启。", _card(scenes=[{"must_include": ["星门开启"]}]))
    assert "RULE_MUST_INCLUDE_MISSING" not in _codes(issues)


def test_must_include_low_coverage_is_error_with_evidence():
    issues = _validate("ab◎◎xy", _card(scenes=[{"must_include": ["abcdefghijk"]}]))
    issue = next(item for item in issues if item.code == "RULE_MUST_INCLUDE_MISSING")
    assert issue.severity == "ERROR"
    assert "abcdefghijk" in issue.evidence
    assert "0.10" in issue.evidence


def test_must_include_exact_point_three_is_warning():
    issues = _validate("ab◎◎de◎◎gh", _card(scenes=[{"must_include": ["abcdefghijk"]}]))
    issue = next(item for item in issues if item.code == "RULE_MUST_INCLUDE_MISSING")
    assert issue.severity == "WARNING"
    assert "0.30" in issue.evidence


def test_must_include_exact_point_six_passes():
    issues = _validate("ab◎◎de◎◎fg◎◎hi◎◎jk◎◎cd", _card(scenes=[{"must_include": ["abcdefghijk"]}]))
    assert "RULE_MUST_INCLUDE_MISSING" not in _codes(issues)


def test_short_must_include_uses_whole_string_match():
    issues = _validate("她终于抵达。", _card(scenes=[{"must_include": ["抵达"]}]))
    assert "RULE_MUST_INCLUDE_MISSING" not in _codes(issues)


def test_one_character_must_include_uses_whole_string_match():
    issues = _validate("天色渐暗，灯火亮起。", _card(scenes=[{"must_include": ["灯"]}]))
    assert "RULE_MUST_INCLUDE_MISSING" not in _codes(issues)


def test_empty_must_include_and_text_do_not_emit_issue():
    issues = _validate("", _card(scenes=[{"must_include": [""]}]))
    assert "RULE_MUST_INCLUDE_MISSING" not in _codes(issues)


def test_short_forbidden_literal_hit_is_error():
    issues = _validate("他打开了密室。", _card(scenes=[{"must_not_include": ["密室"]}]))
    assert any(i.code == "RULE_FORBIDDEN_PRESENT" and i.severity == "ERROR" for i in issues)


def test_forbidden_literal_length_twelve_is_checked():
    phrase = "禁止逐字命中"  # 6 chars, padded below to exactly 12
    phrase = phrase + "不得出现"
    phrase = phrase + "甲乙"
    assert len(phrase) == 12
    issues = _validate(phrase, _card(forbidden=[phrase]))
    assert "RULE_FORBIDDEN_PRESENT" in _codes(issues)


def test_forbidden_literal_over_twelve_is_skipped():
    phrase = "这是一段超过十二个字符的描述性剧情限制"
    assert len(phrase) > 12
    issues = _validate(phrase, _card(forbidden=[phrase]))
    assert "RULE_FORBIDDEN_PRESENT" not in _codes(issues)


def test_forbidden_nonmatching_literal_is_clean():
    issues = _validate("晴空下两人并肩前行。", _card(forbidden=["密室"]))
    assert "RULE_FORBIDDEN_PRESENT" not in _codes(issues)


def test_forbidden_empty_input_is_clean():
    issues = _validate("", _card(forbidden=["密室"]))
    assert "RULE_FORBIDDEN_PRESENT" not in _codes(issues)


def test_forbidden_information_rule_is_checked():
    issues = _validate("他把密信交给了守卫。", _card(information_rules={"forbidden": ["密信"]}))
    assert "RULE_FORBIDDEN_PRESENT" in _codes(issues)


def test_forbidden_character_name_is_separate_error():
    issues = _validate(
        "林澈走进房间。",
        _card(forbidden=["林澈"]),
        [{"name": "林澈", "aliases": ["阿澈"]}],
    )
    assert "RULE_CHARACTER_FORBIDDEN" in _codes(issues)


def test_forbidden_character_alias_is_detected():
    issues = _validate(
        "阿澈推开门。",
        _card(scenes=[{"must_not_include": ["阿澈"]}]),
        [{"name": "林澈", "aliases": ["阿澈"]}],
    )
    assert "RULE_CHARACTER_FORBIDDEN" in _codes(issues)


def test_forbidden_character_absent_is_clean():
    issues = _validate("空屋里只有风声。", _card(forbidden=["林澈"]), [{"name": "林澈", "aliases": []}])
    assert "RULE_CHARACTER_FORBIDDEN" not in _codes(issues)


def test_character_forbidden_requires_exact_forbidden_name():
    issues = _validate("林澈走过长廊。", _card(forbidden=["林澈的旧居"]), [{"name": "林澈", "aliases": []}])
    assert "RULE_CHARACTER_FORBIDDEN" not in _codes(issues)


def test_required_character_missing_is_warning():
    issues = _validate("长廊空无一人。", _card(scenes=[{"characters": ["林澈"]}]), [{"name": "林澈", "aliases": []}])
    issue = next(i for i in issues if i.code == "RULE_CHARACTER_ABSENT")
    assert issue.severity == "WARNING"


def test_required_character_name_present_passes():
    issues = _validate("林澈推门而入。", _card(scenes=[{"characters": ["林澈"]}]), [{"name": "林澈", "aliases": []}])
    assert "RULE_CHARACTER_ABSENT" not in _codes(issues)


def test_required_character_alias_present_passes():
    issues = _validate("阿澈推门而入。", _card(scenes=[{"characters": ["林澈"]}]), [{"name": "林澈", "aliases": ["阿澈"]}])
    assert "RULE_CHARACTER_ABSENT" not in _codes(issues)


def test_required_character_listed_by_alias_accepts_canonical_name():
    issues = _validate("林澈推门而入。", _card(scenes=[{"characters": ["阿澈"]}]), [{"name": "林澈", "aliases": ["阿澈"]}])
    assert "RULE_CHARACTER_ABSENT" not in _codes(issues)


def test_empty_required_character_and_text_are_clean():
    issues = _validate("", _card(scenes=[{"characters": [""]}]))
    assert "RULE_CHARACTER_ABSENT" not in _codes(issues)


@pytest.mark.parametrize("prose", [
    "清晨的雾气散开，村民在田埂上整理工具。",
    "雨停以后，石阶留下深色水痕，远处传来鸟鸣。",
    "木门吱呀一声打开，屋里亮起暖黄色的灯。",
    "他把地图折好放进包里，沿着河岸继续赶路。",
    "孩子们围着桌子画画，窗外的树叶轻轻摇动。",
    "船只靠岸后，旅人向码头管理员询问路线。",
    "她检查了三遍行李，确认药瓶和信件都在。",
    "院子里晾着刚洗好的衣物，风把袖口吹起。",
    "钟声响过两次，街上的店铺陆续关门。",
    "老人坐在长椅上读报，身旁放着一杯茶。",
])
def test_ten_original_prose_samples_have_no_name_variant(prose):
    issues = _validate(prose, characters=[{"name": "柳星河", "aliases": []}])
    assert "RULE_NAME_VARIANT" not in _codes(issues)


@pytest.mark.parametrize("prose", [
    "柳星荷走进院子。柳星荷把伞放在门边。",
    "柳星荷望向窗外。柳星荷听见远处的钟声。",
    "柳星荷取出地图。柳星荷沿着小路向北走。",
])
def test_three_repeated_single_character_name_variants_warn(prose):
    issues = _validate(prose, characters=[{"name": "柳星河", "aliases": []}])
    assert any(i.code == "RULE_NAME_VARIANT" and i.severity == "WARNING" for i in issues)


def test_name_variant_single_occurrence_does_not_warn():
    issues = _validate("柳星荷走进院子，然后继续赶路。", characters=[{"name": "柳星河", "aliases": []}])
    assert "RULE_NAME_VARIANT" not in _codes(issues)


def test_name_variant_matching_other_character_is_excluded():
    issues = _validate(
        "柳星荷走进院子。柳星荷把伞放好。",
        characters=[{"name": "柳星河", "aliases": []}, {"name": "柳星荷", "aliases": []}],
    )
    assert "RULE_NAME_VARIANT" not in _codes(issues)


def test_name_variant_short_names_and_empty_text_are_ignored():
    issues = _validate("", characters=[{"name": "林澈", "aliases": []}])
    assert "RULE_NAME_VARIANT" not in _codes(issues)


def test_banned_phrase_count_above_threshold_warns():
    text = "嘴角勾起一抹" * 3 + "甲" * (1000 - len("嘴角勾起一抹") * 3)
    issues = _validate(text)
    issue = next(i for i in issues if i.code == "RULE_BANNED_PHRASE")
    assert issue.severity == "WARNING"
    assert "嘴角勾起一抹" in issue.evidence


def test_banned_phrase_exact_threshold_does_not_warn():
    text = "嘴角勾起一抹" * 2 + "甲" * (1000 - len("嘴角勾起一抹") * 2)
    issues = _validate(text)
    assert "RULE_BANNED_PHRASE" not in _codes(issues)


def test_banned_phrase_custom_config_and_small_text():
    issues = _validate("旧式套话旧式套话", banned_phrases=("旧式套话",), max_per_1000_chars=0)
    assert "RULE_BANNED_PHRASE" in _codes(issues)


def test_banned_phrase_empty_text_does_not_divide_by_zero():
    issues = _validate("")
    assert "RULE_BANNED_PHRASE" not in _codes(issues)


def test_banned_phrase_config_file_has_description_and_terms():
    from pathlib import Path

    data = json.loads((Path(__file__).parents[1] / "config" / "banned_phrases.json").read_text())
    assert data["description"]
    assert 10 <= len(data["phrases"]) <= 20


def test_no_task_card_skips_task_card_rules_but_keeps_independent_rules():
    issues = _validate("嘴角勾起一抹" * 3 + "甲" * 1000, task_card=None)
    assert not _codes(issues) & {
        "RULE_MUST_INCLUDE_MISSING",
        "RULE_FORBIDDEN_PRESENT",
        "RULE_CHARACTER_FORBIDDEN",
        "RULE_CHARACTER_ABSENT",
    }


def test_empty_card_and_empty_characters_are_safe():
    assert _validate("", _card(), []) == []


def test_nested_task_card_shape_is_supported():
    issues = _validate(
        "星门开启。",
        {"ChapterTaskCard": {"scenes": [{"must_include": ["星门开启"]}]}},
    )
    assert "RULE_MUST_INCLUDE_MISSING" not in _codes(issues)
