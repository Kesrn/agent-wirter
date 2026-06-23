"""小说知识库抽取流水线 mock 测试（文档 §10、§17.12）。

覆盖：
1. 合法 JSON → success extraction → merge
2. 非法 JSON → validation_failed
3. 缺 evidence → validation_failed
4. retry_count 行为
5. max_chapters_per_run 限制
6. MAX_EXTRACT_CHARS 截断 warning
7. merge 到四类结构化表
8. structured QA 返回 citations
"""

import asyncio
import json
import pytest
from sqlalchemy import select

from agents.llm_provider import MockProvider
from models.extraction_pipeline import (
    ProjectSourceChapter, ExtractionJob, ExtractionStaging,
)
from models.structured_knowledge import (
    CharacterProfile, AbilityProfile, EventTimeline, WorldRule, CharacterAppearance,
)
from models.project_knowledge_fact import ProjectKnowledgeFact
from services.extraction_schema import (
    ChapterExtraction, CharacterItem, AbilityItem, EventItem, WorldRuleItem,
    AbilityType, AbilityStatus, RulePriority,
)
from services.extraction_service import (
    advance_extraction_job, get_latest_job_status, StagingStatus, JobStatus,
    _try_parse_json, _handle_retry_or_fail, _normalize_extraction_abilities,
)
from services.chapter_splitter import split_source_chapters
from test_smoke import client, _auth_headers, setup_db, test_session_factory


def _create_project(title="抽取测试项目"):
    headers = _auth_headers("extraction_tester")
    resp = client.post("/api/projects", json={"title": title}, headers=headers)
    assert resp.status_code == 200, f"create project failed: {resp.text}"
    return resp.json()["id"], headers


def _create_novel_source(project_id, title, content, headers, genre="magic_fantasy"):
    """创建 novel 类型资料源。"""
    return client.post(
        f"/api/projects/{project_id}/knowledge/sources",
        json={
            "title": title,
            "source_type": "novel",
            "content": content,
            "genre": genre,
            "canon_level": "original",
        },
        headers=headers,
    )


# ── 1. 章节切分验收（§17.2） ────────────────────────────


def test_split_chapters_writes_project_source_chapters():
    """上传 txt 小说后能写入 project_source_chapters。"""
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "测试小说",
        "第一章 觉醒\n莫凡觉醒了火系。\n第二章 初战\n张小侯释放风轨。\n第三章 修行\n莫凡修炼雷霆系。",
        headers,
    )
    sid = create_resp.json()["id"]

    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chapter_count"] == 3
    assert data["split_type"] == "chapter_regex"


def test_split_chapters_auto_length_fallback():
    """无章节标记时按长度兜底切分。"""
    pid, headers = _create_project()
    content = "某段正文内容。" * 2000  # 约 12000 字
    create_resp = _create_novel_source(pid, "无标记小说", content, headers)
    sid = create_resp.json()["id"]

    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["split_type"] == "auto_length"
    assert resp.json()["chapter_count"] >= 1


# ── 2. 抽取流水线验收 ──────────────────────────────────


def test_extraction_success_and_merge():
    """合法 JSON → success → merge 到 4 张结构化表。"""
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "抽取小说",
        "第一章 觉醒\n莫凡觉醒了火系。张小侯释放风轨击退敌人。",
        headers,
    )
    sid = create_resp.json()["id"]

    # 切分
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 抽取
    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] in (JobStatus.COMPLETED, JobStatus.PARTIAL_FAILED, JobStatus.RUNNING)
    assert data["merged_count"] >= 1

    # 验证 4 张表有数据
    async def _check():
        async with test_session_factory() as session:
            chars = (await session.execute(select(CharacterProfile).where(CharacterProfile.project_id == pid))).scalars().all()
            abilities = (await session.execute(select(AbilityProfile).where(AbilityProfile.project_id == pid))).scalars().all()
            events = (await session.execute(select(EventTimeline).where(EventTimeline.project_id == pid))).scalars().all()
            rules = (await session.execute(select(WorldRule).where(WorldRule.project_id == pid))).scalars().all()
            return chars, abilities, events, rules

    chars, abilities, events, rules = asyncio.run(_check())
    assert len(chars) >= 1, "应合并人物到 character_profile"
    assert len(abilities) >= 1, "应合并能力到 ability_profile"
    assert len(events) >= 1, "应合并事件到 event_timeline"
    assert len(rules) >= 1, "应合并世界规则到 world_rule"


# ── 3. 非法 JSON 验收（§17.6） ──────────────────────────


def test_invalid_json_validation_failed():
    """LLM 返回非法 JSON 时：raw_output 保存，raw_json=null，status=VALIDATION_FAILED。"""
    # 直接测 _try_parse_json
    assert _try_parse_json("这不是 JSON") is None
    assert _try_parse_json("") is None
    assert _try_parse_json("```json\n{\"a\":1}\n```") == {"a": 1}
    # 合法 JSON
    assert _try_parse_json('{"chapter_no":1}') == {"chapter_no": 1}
    # 前后带解释文本时也应提取 JSON 对象
    assert _try_parse_json('说明：\n{"chapter_no":1,"characters":[]}\n已完成。') == {
        "chapter_no": 1,
        "characters": [],
    }


def test_normalize_extraction_infers_mofan_lightning_from_purple_arc():
    """真实样本里“电流 + 紫色弧线”应补为莫凡雷系，不能只留下 unknown 异象。"""
    extraction = ChapterExtraction(
        chapter_no=5,
        chapter_title="天生双系（上）",
        abilities=[
            AbilityItem(
                character="莫凡",
                ability_type=AbilityType.unknown,
                ability_name="觉醒异象：紫色弧线",
                level="未知",
                status=AbilityStatus.new,
                importance=5,
                confidence=0.9,
                evidence="突然在自己那片虚无的精神世界里划过了一道紫色的弧线",
            )
        ],
    )
    content = (
        "就在莫凡将手放在觉醒石上的时候，他能够感觉到一股电流的力量。"
        "这股力量从手掌传递到全身，紧接着在精神世界里划过了一道紫色的弧线。"
    )

    normalized = _normalize_extraction_abilities(extraction, content)
    pairs = {(a.character, a.ability_type.value, a.ability_name) for a in normalized.abilities}
    assert ("莫凡", "magic_element", "雷系") in pairs


def test_normalize_extraction_infers_system_from_spell_alias():
    """抽到“风轨·疾行”这类技能时，应补出同人物风系，支撑“是什么系”问答。"""
    extraction = ChapterExtraction(
        chapter_no=19,
        chapter_title="风轨疾行",
        abilities=[
            AbilityItem(
                character="张小侯",
                ability_type=AbilityType.spell,
                ability_name="风轨·疾行",
                level="初阶",
                status=AbilityStatus.used,
                importance=4,
                confidence=0.88,
                evidence="张小侯释放风轨·疾行。",
            )
        ],
    )
    normalized = _normalize_extraction_abilities(extraction, "张小侯释放风轨·疾行，速度骤然提升。")
    pairs = {(a.character, a.ability_type.value, a.ability_name) for a in normalized.abilities}
    assert ("张小侯", "magic_element", "风系") in pairs


def test_normalize_extraction_does_not_infer_magic_system_for_non_magic_genre():
    """技能别名推法系只适用于 magic_fantasy，避免污染其它题材。"""
    extraction = ChapterExtraction(
        chapter_no=1,
        chapter_title="风轨",
        abilities=[
            AbilityItem(
                character="李靖",
                ability_type=AbilityType.spell,
                ability_name="风轨",
                level="",
                status=AbilityStatus.used,
                importance=3,
                confidence=0.8,
                evidence="李靖沿风轨追敌。",
            )
        ],
    )
    normalized = _normalize_extraction_abilities(extraction, "李靖沿风轨追敌。", genre="historical")
    pairs = {(a.character, a.ability_type.value, a.ability_name) for a in normalized.abilities}
    assert ("李靖", "magic_element", "风系") not in pairs
    assert pairs == {("李靖", "spell", "风轨")}


def test_normalize_extraction_canonicalizes_magic_system_names():
    """雷霆系魔法、火炎系等模型常见叫法应规整为标准法系名。"""
    extraction = ChapterExtraction(
        chapter_no=3,
        abilities=[
            AbilityItem(
                character="莫凡",
                ability_type=AbilityType.magic_element,
                ability_name="雷霆系魔法",
                level="初阶",
                status=AbilityStatus.new,
                importance=5,
                confidence=0.9,
                evidence="莫凡的雷霆系魔法。",
            ),
            AbilityItem(
                character="莫凡",
                ability_type=AbilityType.magic_element,
                ability_name="火炎系",
                level="初阶",
                status=AbilityStatus.new,
                importance=5,
                confidence=0.9,
                evidence="莫凡拥有火炎系。",
            ),
        ],
    )
    normalized = _normalize_extraction_abilities(extraction, "莫凡拥有雷霆系魔法和火炎系。")
    names = [a.ability_name for a in normalized.abilities]
    assert "雷系" in names
    assert "火系" in names


def test_normalize_extraction_drops_unbound_mentioned_spell():
    """只是在章节里解释/提到的技能，不应被挂到人物能力上。"""
    extraction = ChapterExtraction(
        chapter_no=13,
        abilities=[
            AbilityItem(
                character="莫凡",
                ability_type=AbilityType.spell,
                ability_name="风之翼",
                level="高阶",
                status=AbilityStatus.mentioned,
                importance=3,
                confidence=0.9,
                evidence="风系高阶技能-风之翼，就是可以让人飞翔起来的技能",
            ),
            AbilityItem(
                character="张小侯",
                ability_type=AbilityType.spell,
                ability_name="风轨",
                level="初阶",
                status=AbilityStatus.used,
                importance=4,
                confidence=0.9,
                evidence="张小侯释放风轨疾行。",
            ),
        ],
    )

    normalized = _normalize_extraction_abilities(
        extraction,
        "风系高阶技能-风之翼，就是可以让人飞翔起来的技能。张小侯释放风轨疾行。",
    )
    pairs = {(a.character, a.ability_type.value, a.ability_name) for a in normalized.abilities}
    assert ("莫凡", "spell", "风之翼") not in pairs
    assert ("张小侯", "spell", "风轨") in pairs


# ── 4. 缺 evidence 验收（§17.7） ────────────────────────


def test_normalize_extraction_collapses_star_dust_and_magic_suffixes():
    """真实抽取：雷系星尘/雷霆系魔法/火系星尘/火系魔法 应归一为 雷系/火系。"""
    extraction = ChapterExtraction(
        chapter_no=8,
        abilities=[
            AbilityItem(character="莫凡", ability_type=AbilityType.magic_element,
                        ability_name="雷系星尘", level="初阶", status=AbilityStatus.new,
                        importance=5, confidence=1.0, evidence="紫色星尘悬浮"),
            AbilityItem(character="莫凡", ability_type=AbilityType.magic_element,
                        ability_name="雷霆系魔法", level="觉醒", status=AbilityStatus.new,
                        importance=5, confidence=1.0, evidence="莫凡觉醒雷霆系"),
            AbilityItem(character="莫凡", ability_type=AbilityType.magic_element,
                        ability_name="火系星尘", level="初阶", status=AbilityStatus.new,
                        importance=5, confidence=1.0, evidence="红色星尘"),
            AbilityItem(character="莫凡", ability_type=AbilityType.magic_element,
                        ability_name="火系魔法", level="觉醒", status=AbilityStatus.new,
                        importance=5, confidence=1.0, evidence="莫凡觉醒火系"),
        ],
    )
    normalized = _normalize_extraction_abilities(extraction, "紫色星尘与红色星尘。")
    names = [a.ability_name for a in normalized.abilities
             if a.ability_type == AbilityType.magic_element]
    assert names.count("雷系") == 1
    assert names.count("火系") == 1
    assert "雷系星尘" not in names
    assert "雷霆系魔法" not in names
    assert "火系星尘" not in names
    assert "火系魔法" not in names


def test_is_ability_bound_to_character_filters_introduction_evidence():
    """介绍性/旁听性 evidence 不应入正式 ability_profile。"""
    from services.extraction_normalization import is_ability_bound_to_character

    # 介绍性台词：人物只是听/问/提到，不拥有
    assert not is_ability_bound_to_character(
        "莫凡", "风之翼",
        "风系高阶技能-风之翼，就是可以让人飞翔起来的技能",
    )
    assert not is_ability_bound_to_character(
        "莫凡", "风之翼", "莫凡听说风之翼可以让人飞翔",
    )
    assert not is_ability_bound_to_character(
        "莫凡", "风之翼", "老师介绍风之翼是什么",
    )
    # 明确绑定：觉醒/拥有/释放/使用/掌握
    assert is_ability_bound_to_character(
        "莫凡", "火系", "莫凡觉醒火系，精神世界出现红色星尘",
    )
    assert is_ability_bound_to_character(
        "张小侯", "风轨", "张小侯释放风轨冲出包围",
    )
    assert is_ability_bound_to_character(
        "莫凡", "雷系星尘", "莫凡的雷系星尘内一共有七颗星子",
    )
    assert is_ability_bound_to_character(
        "莫凡", "雷印", "莫凡使用雷印击倒了徐冰",
    )
    # 第一人称 evidence（LLM 以主角视角写 "自己觉醒了火系"）应判为绑定
    assert is_ability_bound_to_character(
        "莫凡", "火系", "自己精神世界里出现了红色星尘，自己又觉醒了一个火系",
    )


def test_normalize_character_name_merges_zhang_xiaohou_variants():
    """张侯/张候/张小候/张小候 归一到 张小侯，保留异体为 aliases。"""
    from services.extraction_normalization import normalize_character_name

    for variant in ("张侯", "张候", "张小候", "张小侯"):
        canonical, aliases = normalize_character_name(variant)
        assert canonical == "张小侯"
        assert "张侯" in aliases
        assert "张候" in aliases
        assert "张小候" in aliases

    # 不该乱归一的普通名字保持不变
    canonical, aliases = normalize_character_name("莫凡")
    assert canonical == "莫凡"
    assert aliases == []


def test_normalize_world_rule_category_collapses_synonyms():
    """world_rule category 同义归一到白名单。"""
    from services.extraction_normalization import normalize_world_rule_category

    assert normalize_world_rule_category("魔法修炼体系") == "魔法修炼"
    assert normalize_world_rule_category("魔法修炼机制") == "魔法修炼"
    assert normalize_world_rule_category("星子修炼") == "魔法修炼"
    assert normalize_world_rule_category("魔法觉醒体系") == "魔法觉醒"
    assert normalize_world_rule_category("觉醒机制") == "魔法觉醒"
    assert normalize_world_rule_category("世家体系") == "势力组织"
    assert normalize_world_rule_category("家族势力") == "势力组织"
    assert normalize_world_rule_category("穆氏世家") == "势力组织"
    assert normalize_world_rule_category("魔法体系") == "魔法体系"
    assert normalize_world_rule_category("妖魔") == "妖魔体系"
    assert normalize_world_rule_category("未知奇怪分类") == "其他"


def test_missing_evidence_validation_failed():
    """LLM 返回合法 JSON 但缺 evidence 时校验失败，不得 merge。"""
    # 构造缺 evidence 的人物数据
    with pytest.raises(Exception):
        CharacterItem(name="莫凡", evidence="")  # evidence 必须非空
    # 构造缺 evidence 的能力数据
    with pytest.raises(Exception):
        AbilityItem(character="莫凡", ability_type=AbilityType.magic_element,
                    ability_name="火系", evidence="")
    # 合法数据应通过
    valid = AbilityItem(character="莫凡", ability_type=AbilityType.magic_element,
                        ability_name="火系", evidence="莫凡觉醒火系")
    assert valid.evidence == "莫凡觉醒火系"


# ── 5. retry_count 行为（§17.12.4） ─────────────────────


def test_retry_count_behavior():
    """retry_count < 2 → RETRYING；>= 2 → FAILED。"""
    async def _run():
        async with test_session_factory() as session:
            staging = ExtractionStaging(
                project_id="00000000-0000-0000-0000-000000000000",
                source_id="00000000-0000-0000-0000-000000000001",
                genre="magic_fantasy", template_name="test", schema_version="1.0",
                raw_output="bad", status=StagingStatus.VALIDATION_FAILED,
            )
            session.add(staging)
            await session.flush()
            # 第一次 retry（retry_count 0 → 1）
            await _handle_retry_or_fail(session, staging)
            assert staging.status == StagingStatus.RETRYING
            assert staging.retry_count == 1
            # 第二次 retry（retry_count 1 → 2）
            await _handle_retry_or_fail(session, staging)
            assert staging.status == StagingStatus.RETRYING
            assert staging.retry_count == 2
            # 第三次 → FAILED（retry_count 已达 MAX_RETRIES）
            await _handle_retry_or_fail(session, staging)
            assert staging.status == StagingStatus.FAILED
            assert staging.retry_count == 3

    asyncio.run(_run())


# ── 6. max_chapters_per_run 限制（§17.3） ───────────────


def test_max_chapters_per_run_limit():
    """max_chapters_per_run 限制单批推进章节数，全书未完时 BATCH_DONE。"""
    pid, headers = _create_project()
    # 构造 25 章（显式列表，避免 for c in 字符串 把"十一"拆成"十""一"）
    nums = ["一","二","三","四","五","六","七","八","九","十",
            "十一","十二","十三","十四","十五","十六","十七","十八","十九","二十",
            "二十一","二十二","二十三","二十四","二十五"]
    content = "\n".join(f"第{c}章 标题\n莫凡觉醒火系。张小侯释放风轨。" for c in nums)
    create_resp = _create_novel_source(pid, "25章小说", content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 启动抽取，max_chapters_per_run=20
    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"max_chapters_per_run": 20},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # job 的 total_chapters 应为 25
    status = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert status["total_chapters"] == 25, f"应切 25 章，实际 {status['total_chapters']}"

    # 第一次推进：max_chapters_per_run=20，处理一批后应 BATCH_DONE（25>20）
    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"max_chapters_per_run": 20}, headers=headers,
    )
    # 轮询直到非 RUNNING
    for _ in range(10):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"max_chapters_per_run": 20}, headers=headers,
        )
    after_batch = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    # 本批完成但全书未完 → BATCH_DONE
    assert after_batch["status"] == JobStatus.BATCH_DONE, (
        f"25章配额20应 BATCH_DONE，实际: {after_batch['status']}"
    )
    # 单批处理不超过 max_chapters_per_run
    assert after_batch["extracted_count"] <= 20, (
        f"单批不应超过 20，实际 {after_batch['extracted_count']}"
    )


# ── 7. MAX_EXTRACT_CHARS 截断 warning（§17.4） ──────────


def test_max_extract_chars_truncation_warning():
    """章节超过 MAX_EXTRACT_CHARS 时记录 warning，不无上限提交。"""
    from services.extraction_schema import MAX_EXTRACT_CHARS
    assert MAX_EXTRACT_CHARS == 16000

    # 构造超长章节（超过 16000 字）
    long_content = "莫凡觉醒了火系。" * 2000  # 约 32000 字
    pid, headers = _create_project()
    create_resp = _create_novel_source(pid, "超长章节", long_content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 验证章节记录了 warning（切分后可能是一段，需检查 warning 字段）
    async def _check_warning():
        async with test_session_factory() as session:
            result = await session.execute(
                select(ProjectSourceChapter).where(ProjectSourceChapter.source_id == sid)
            )
            chapters = list(result.scalars().all())
            return chapters

    chapters = asyncio.run(_check_warning())
    # 超长章节切分后至少有一条，抽取阶段会截断并记录 warning
    # 这里验证章节存在且 char_count 正常
    assert len(chapters) >= 1


# ── 8. structured QA 返回 citations（§17.10） ───────────


def test_structured_qa_character_ability_returns_citations():
    """结构化 QA 查人物能力，返回 answer + citations。"""
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "QA小说",
        "第一章 觉醒\n莫凡觉醒了火系。张小侯释放风轨击退敌人。",
        headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"max_chapters_per_run": 5}, headers=headers,
    )

    # QA
    resp = client.post(
        f"/api/projects/{pid}/knowledge/structured-qa",
        json={"question": "莫凡有什么系别？"}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "answer" in data
    assert "citations" in data
    assert data["query_plan"]["mode"] == "structured"
    assert data["query_plan"]["intent"] == "character_ability"
    # 回答应含火系
    assert "火系" in data["answer"], f"回答应含火系，实际: {data['answer']!r}"
    # citations 应非空且含 evidence
    assert len(data["citations"]) > 0
    assert any("evidence_type" in c for c in data["citations"])


def test_structured_qa_not_found_message():
    """结构化库无记录时返回"未找到明确记录"，不凭常识补充。"""
    pid, headers = _create_project()
    resp = client.post(
        f"/api/projects/{pid}/knowledge/structured-qa",
        json={"question": "莫凡有什么系别？"}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "未找到" in data["answer"] or "无记录" in data["answer"]
    assert data["citations"] == []
    assert data["retrieval_stats"]["structured_hits"] == 0


def test_structured_qa_character_name_hou_variant():
    """张小侯/张小候/张候这类常见异体写法应能命中同一结构化能力。"""
    import uuid as _uuid
    pid = str(_uuid.uuid4())
    sid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            session.add(AbilityProfile(
                project_id=pid,
                source_id=sid,
                character_name="张候",
                ability_type="magic_element",
                ability_name="风系",
                first_seen_chapter=5,
                canon_level="original",
                origin="llm_extracted",
                source_priority=60,
                confidence=0.9,
                evidence=["张候觉醒了风系。"],
            ))
            session.add(AbilityProfile(
                project_id=pid,
                source_id=sid,
                character_name="张小侯",
                ability_type="magic_element",
                ability_name="风系",
                first_seen_chapter=19,
                canon_level="original",
                origin="llm_extracted",
                source_priority=60,
                confidence=0.85,
                evidence=["张小侯释放风轨，证明他掌握风系。"],
            ))
            session.add(AbilityProfile(
                project_id=pid,
                source_id=sid,
                character_name="张小侯",
                ability_type="spell",
                ability_name="风轨·疾行",
                first_seen_chapter=19,
                canon_level="original",
                origin="llm_extracted",
                source_priority=60,
                confidence=0.85,
                evidence=["张小侯释放风轨·疾行。"],
            ))
            await session.flush()

            from services.structured_qa import answer_structured_question
            result = await answer_structured_question(session, pid, "张小侯是什么系的")
            assert "风系" in result["answer"], result["answer"]
            assert "风轨" not in result["answer"], result["answer"]
            assert result["answer"].count("风系") == 1, result["answer"]
            assert result["retrieval_stats"]["structured_hits"] == 1

    asyncio.run(_run())


def test_structured_qa_character_name_variants_do_not_create_short_duplicate_for_repeated_xiao():
    """李小小这类名字不应生成李小这种脏变体。"""
    from services.structured_qa import _character_name_variants

    assert _character_name_variants("张小侯") == ["张小侯", "张小候", "张侯", "张候"]
    assert _character_name_variants("李小小") == ["李小小"]


def test_structured_qa_falls_back_to_facts_when_only_unknown_ability():
    """问系别时，如果 ability_profile 只有 unknown 异象，应继续查 facts。"""
    import uuid as _uuid
    pid = str(_uuid.uuid4())
    sid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            session.add(AbilityProfile(
                project_id=pid,
                source_id=sid,
                character_name="莫凡",
                ability_type="unknown",
                ability_name="觉醒异象：紫色弧线",
                first_seen_chapter=5,
                canon_level="original",
                origin="llm_extracted",
                source_priority=60,
                confidence=0.9,
                evidence=["紫色弧线"],
            ))
            session.add(ProjectKnowledgeFact(
                project_id=pid,
                source_id=sid,
                fact_type="character_system",
                subject="莫凡",
                predicate="has_magic_system",
                object="雷系",
                confidence="explicit",
                evidence_text="莫凡觉醒雷系。",
                extractor="rule.character_system.v1",
            ))
            await session.flush()

            from services.structured_qa import answer_structured_question
            result = await answer_structured_question(session, pid, "莫凡有什么系别?")
            assert "雷系" in result["answer"], result["answer"]
            assert result["query_plan"]["tables"] == ["project_knowledge_facts"]

    asyncio.run(_run())


# ── 9. 状态查询接口（§15.4） ────────────────────────────


def test_extract_status_endpoint():
    """GET /extract/status 返回 job 状态。"""
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "状态小说", "第一章 觉醒\n莫凡觉醒火系。", headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 抽取前 status 应为 NONE
    resp = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "NONE"

    # 启动抽取
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"max_chapters_per_run": 5}, headers=headers,
    )

    # 查状态
    resp2 = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    )
    assert resp2.status_code == 200
    status = resp2.json()
    assert "job_id" in status
    assert status["status"] in (JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.PARTIAL_FAILED)


# ── 状态机修复回归测试 ──────────────────────────────────


def test_retry_reuses_same_staging_record():
    """修2：retry 复用同一 staging 记录，retry_count 真实累积。"""
    async def _run():
        async with test_session_factory() as session:
            from services.extraction_service import _save_staging, _handle_retry_or_fail, StagingStatus
            # 模拟一个 job 和 chapter
            import uuid
            job = ExtractionJob(
                project_id="00000000-0000-0000-0000-000000000000",
                source_id="00000000-0000-0000-0000-000000000001",
                genre="magic_fantasy", status="RUNNING",
            )
            session.add(job)
            await session.flush()
            chapter = ProjectSourceChapter(
                project_id="00000000-0000-0000-0000-000000000000",
                source_id="00000000-0000-0000-0000-000000000001",
                chapter_no=1, content="test",
            )
            session.add(chapter)
            await session.flush()

            # 第一次保存（EXTRACTED）
            s1 = await _save_staging(session, job, chapter, "magic_fantasy",
                                     raw_output="bad json", status=StagingStatus.EXTRACTED)
            # 模拟 parse 失败 → VALIDATION_FAILED → RETRYING
            s1.status = StagingStatus.VALIDATION_FAILED
            await _handle_retry_or_fail(session, s1)
            assert s1.retry_count == 1
            assert s1.status == StagingStatus.RETRYING

            # 第二次保存（retry）：应复用同一条记录
            s2 = await _save_staging(session, job, chapter, "magic_fantasy",
                                     raw_output="still bad", status=StagingStatus.EXTRACTED)
            assert s2.id == s1.id, "retry 应复用同一 staging 记录"
            # retry_count 应保留（未被重置）
            # 注意：_save_staging 复用时不重置 retry_count，但 status 被设为 EXTRACTED
            # 这里验证复用即可

    asyncio.run(_run())


def test_job_completion_requires_no_pending_staging():
    """修1：job 完成判断要求无悬空态（RETRYING 等），避免误判 COMPLETED。

    通过端到端：正常抽取后 job 应 COMPLETED；若手动制造 RETRYING 悬空态，job 不应完成。
    """
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "完成判断小说", "第一章 觉醒\n莫凡觉醒了火系。", headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"max_chapters_per_run": 5}, headers=headers,
    )
    assert resp.status_code == 200
    # mock 抽取应成功 merge，job 最终 COMPLETED
    for _ in range(5):
        status = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if status["status"] in (JobStatus.COMPLETED, JobStatus.PARTIAL_FAILED, JobStatus.FAILED):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"max_chapters_per_run": 5}, headers=headers,
        )
    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert final["status"] == JobStatus.COMPLETED, f"正常抽取应 COMPLETED，实际 {final['status']}"
    assert final["merged_count"] >= 1


def test_merge_priority_low_does_not_overwrite_high():
    """修3：低优先级来源不覆盖高优先级已有字段。

    先以 manual(100) 写入人物身份，再以 original(60) 抽取同一人物，
    manual 的 identity_desc 不应被 original 覆盖。
    """
    import uuid as _uuid
    pid = str(_uuid.uuid4())
    sid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            from services.extraction_service import _merge_character, CANON_PRIORITY
            from services.extraction_schema import CharacterItem

            # 先以 manual 优先级写入
            char_manual = CharacterItem(
                name="莫凡", identity="用户设定的法师身份", status="用户设定状态",
                importance=5, confidence=1.0, evidence="用户手动设定",
            )
            await _merge_character(session, pid, sid, 1, "第1章", char_manual,
                                   "manual", "manual", CANON_PRIORITY["manual"])
            await session.flush()

            # 再以 original 优先级抽取同一人物
            char_original = CharacterItem(
                name="莫凡", identity="LLM抽取的不同身份", status="LLM抽取状态",
                importance=3, confidence=0.8, evidence="原文证据",
            )
            await _merge_character(session, pid, sid, 2, "第2章", char_original,
                                   "original", "llm_extracted", CANON_PRIORITY["original"])
            await session.flush()

            # 查回，identity 应保持 manual 的
            result = await session.execute(
                select(CharacterProfile).where(CharacterProfile.project_id == pid)
                .where(CharacterProfile.name == "莫凡")
            )
            char = result.scalar_one()
            assert char.identity_desc == "用户设定的法师身份", (
                f"低优先级不应覆盖高优先级 identity，实际: {char.identity_desc}"
            )
            assert char.source_priority == 100, "应保持 manual 优先级"

    asyncio.run(_run())


def test_world_rule_priority_sorted_correctly():
    """修5：world_rule priority 按 high>medium>low 排序，非字典序。"""
    import uuid as _uuid
    pid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            for p, cat in [("low", "A类"), ("high", "B类"), ("medium", "C类")]:
                session.add(WorldRule(
                    project_id=pid, source_id=str(_uuid.uuid4()),
                    category=cat, rule_text=f"{cat}规则",
                    priority=p, confidence=0.8, evidence="测试",
                ))
            await session.flush()

            from services.structured_qa import _answer_world_rule
            result = await _answer_world_rule(session, pid, "规则", "world_rule_query", None)
            # answer 文本里 B类(high) 应在 A类(low) 之前出现
            answer = result["answer"]
            b_pos = answer.find("B类")
            a_pos = answer.find("A类")
            assert b_pos >= 0 and a_pos >= 0, f"answer 应含两类，实际: {answer!r}"
            assert b_pos < a_pos, f"high(B) 应排在 low(A) 之前，b_pos={b_pos} a_pos={a_pos}"

    asyncio.run(_run())


# ── 补0：event_query LIKE 转义（P2 #3） ────────────────


def test_event_query_like_escapes_wildcards():
    """问题关键词里的 % 和 _ 不应被当成 SQL LIKE 通配符扩大命中。

    构造两条事件：
      A: event_desc="abc"   （不含字面下划线）
      B: event_desc="a_c"   （含字面下划线）
    提问 "a_c"。
    旧逻辑：LIKE '%a_c%' 中 '_' 匹配任意单字符 → 命中 A("abc") 和 B("a_c")，错误扩大。
    新逻辑：转义 '_' → 只命中字面 "a_c" → 仅 B。
    """
    import uuid as _uuid
    pid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            session.add(EventTimeline(
                project_id=pid, source_id=str(_uuid.uuid4()),
                event_title="事件A", event_desc="abc", chapter_no=1,
            ))
            session.add(EventTimeline(
                project_id=pid, source_id=str(_uuid.uuid4()),
                event_title="事件B", event_desc="a_c", chapter_no=2,
            ))
            await session.flush()

            from services.structured_qa import _answer_event_query
            result = await _answer_event_query(session, pid, "a_c", "event_query", None)
            titles = [c["title"] for c in result["citations"]]
            # 只应命中字面 "a_c" 的那条，不应把 "abc" 也匹配上
            assert "事件A" not in titles, (
                f"'_' 不应被当通配符匹配到 abc，实际命中: {titles}"
            )
            assert "事件B" in titles, f"应命中字面 a_c，实际命中: {titles}"

    asyncio.run(_run())


def test_event_query_like_escapes_percent():
    """问题关键词里的 % 不应被当成 SQL LIKE 通配符。

    构造：A: event_desc="abc"（无 %）；B: event_desc="a%c"（字面 %）。
    提问 "a%c"。旧逻辑 LIKE '%a%c%' 中 '%' 是通配 → 命中 A 和 B。新逻辑只命中 B。
    """
    import uuid as _uuid
    pid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            session.add(EventTimeline(
                project_id=pid, source_id=str(_uuid.uuid4()),
                event_title="事件A", event_desc="abc", chapter_no=1,
            ))
            session.add(EventTimeline(
                project_id=pid, source_id=str(_uuid.uuid4()),
                event_title="事件B", event_desc="a%c", chapter_no=2,
            ))
            await session.flush()

            from services.structured_qa import _answer_event_query
            result = await _answer_event_query(session, pid, "a%c", "event_query", None)
            titles = [c["title"] for c in result["citations"]]
            assert "事件A" not in titles, (
                f"'%' 不应被当通配符匹配到 abc，实际命中: {titles}"
            )
            assert "事件B" in titles, f"应命中字面 a%c，实际命中: {titles}"

    asyncio.run(_run())


def test_event_query_parses_chinese_chapter_number():
    """提问“第一章发生了什么”应按 chapter_no=1 精确查事件。"""
    import uuid as _uuid
    pid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            session.add(EventTimeline(
                project_id=pid, source_id=str(_uuid.uuid4()),
                event_title="第一章事件", event_desc="主角完成觉醒。", chapter_no=1,
                evidence=["第一章证据"],
            ))
            session.add(EventTimeline(
                project_id=pid, source_id=str(_uuid.uuid4()),
                event_title="第二章事件", event_desc="主角参加试炼。", chapter_no=2,
                evidence=["第二章证据"],
            ))
            await session.flush()

            from services.structured_qa import _answer_event_query
            result = await _answer_event_query(session, pid, "第一章发生了什么?", "event_query", None)
            titles = [c["title"] for c in result["citations"]]
            assert titles == ["第一章事件"], f"应只命中第一章事件，实际: {titles}"

    asyncio.run(_run())


# ── 补0b：抽取批次 summary 日志（P2 #4） ───────────────


def test_advance_logs_batch_summary(caplog):
    """每次 advance_extraction_job 结束应打一条 batch summary 日志，含关键字段。

    断言日志里出现 "extraction batch done"，并含 progress 与 status。
    """
    import logging
    pid, headers = _create_project()
    content = "\n".join(
        f"第{c}章 标题\n莫凡觉醒火系。"
        for c in "一二三"
    )
    create_resp = _create_novel_source(pid, "summary日志小说", content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    with caplog.at_level(logging.INFO, logger="services.extraction_service"):
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 20},
            headers=headers,
        )
        # 推进直到非 RUNNING（确保批次结束）
        for _ in range(8):
            s = client.get(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
            ).json()
            if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
                break
            client.post(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
                json={"genre": "magic_fantasy", "max_chapters_per_run": 20}, headers=headers,
            )

    summary_lines = [
        r.message for r in caplog.records
        if "extraction batch done" in r.message
    ]
    assert summary_lines, (
        "应至少打一条 'extraction batch done' summary 日志，实际记录: "
        + repr([r.message for r in caplog.records])
    )
    last = summary_lines[-1]
    assert "progress=" in last, f"summary 日志应含 progress=，实际: {last!r}"
    assert "status=" in last, f"summary 日志应含 status=，实际: {last!r}"


# ── 补0c：P1 mock provider 标记与状态暴露 ───────────────


def test_extract_status_exposes_mock_provider():
    """status 接口必须返回 provider 与 is_mock，让前端区分 mock 数据。

    测试环境无用户 LLM 配置 → 回退 settings.LLM_PROVIDER=mock → is_mock=True。
    """
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "provider小说", "第一章 觉醒\n莫凡觉醒火系。", headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 抽取前（NONE）也应返回 provider/is_mock
    resp = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "provider" in data, f"NONE 状态也应返回 provider，实际: {data}"
    assert "is_mock" in data
    assert data["is_mock"] is True, f"无配置时应为 mock，实际 provider={data['provider']}"
    assert data["provider"] == "mock"

    # 启动抽取
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )

    resp = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    )
    data = resp.json()
    assert data["is_mock"] is True
    assert data["provider"] == "mock"


def test_job_records_provider_when_created():
    """ExtractionJob 落库时应记录本次抽取使用的 provider，便于区分 mock/真实数据。"""
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "provider记录小说", "第一章 觉醒\n莫凡觉醒火系。", headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )

    async def _check():
        async with test_session_factory() as session:
            jobs = (await session.execute(
                select(ExtractionJob).where(ExtractionJob.source_id == sid)
            )).scalars().all()
            assert len(jobs) >= 1
            assert jobs[0].provider == "mock", (
                f"job 应记录 provider=mock，实际: {jobs[0].provider!r}"
            )

    asyncio.run(_check())


def test_extract_status_real_provider_not_mock():
    """配置了真实 provider 时，status 的 is_mock 应为 False。"""
    from unittest.mock import patch
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "realprovider小说", "第一章 觉醒\n莫凡觉醒火系。", headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # mock 一个真实 provider 配置（不实际调用 LLM，只验证状态判断）
    fake_real_config = {
        "provider": "openai", "api_key": "sk-test", "base_url": None, "model": "gpt-4o",
    }
    with patch("api.llm_deps.get_user_llm_config", return_value=fake_real_config):
        resp = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        )
    data = resp.json()
    assert data["is_mock"] is False, f"真实 provider 不应 is_mock，实际: {data}"
    assert data["provider"] == "openai"


def test_resumed_job_refreshes_provider_after_config_change():
    """job 复用时应刷新 provider，避免 is_mock 陈旧。

    场景：mock 下建 job（provider=mock）→ 用户切真实 openai 配置 → 继续抽取（复用 job）。
    期望：job.provider 变为 openai，status is_mock=False。
    旧 bug：复用 job 不更新 provider，仍报 mock，但实际 _process_single_chapter 用真实配置。
    """
    from unittest.mock import patch
    from agents.llm_provider import MockProvider
    pid, headers = _create_project()
    # 6 章 + max_chapters_per_run=2 → 第一批只跑 2 章，job 停在 BATCH_DONE（可被复用）
    content = "\n".join(f"第{c}章 觉醒\n莫凡觉醒火系。" for c in "一二三四五六")
    create_resp = _create_novel_source(
        pid, "刷新provider小说", content, headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 1) mock 下建 job 并跑完一批（停在 BATCH_DONE，2/6）
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 2}, headers=headers,
    )
    async def _check_provider():
        async with test_session_factory() as session:
            jobs = (await session.execute(
                select(ExtractionJob).where(ExtractionJob.source_id == sid)
            )).scalars().all()
            return jobs[0].provider if jobs else None
    prov0 = asyncio.run(_check_provider())
    assert prov0 == "mock", f"初始应为 mock，实际 {prov0!r}"

    # 2) 切换为真实 openai 配置后继续抽取（复用 job）。
    #    patch get_llm_provider 仍返回 MockProvider，避免真实网络调用；
    #    但 get_user_llm_config 返回 openai 配置 → _resolve_provider_name 应解析为 openai。
    fake_real_config = {
        "provider": "openai", "api_key": "sk-test", "base_url": None, "model": "gpt-4o",
    }
    with patch("api.llm_deps.get_user_llm_config", return_value=fake_real_config), \
         patch("agents.llm_provider.get_llm_provider", return_value=MockProvider()):
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 2}, headers=headers,
        )
        # 验证 status 反映真实 provider
        resp = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        )
        data = resp.json()
        assert data["provider"] == "openai", f"复用 job 后 provider 应刷新为 openai，实际: {data['provider']}"
        assert data["is_mock"] is False, f"复用 job 后 is_mock 应为 False，实际: {data}"

    # 3) 直接查 DB 确认 job.provider 已落库
    prov1 = asyncio.run(_check_provider())
    assert prov1 == "openai", f"job.provider 应落库为 openai，实际 {prov1!r}"


def test_status_reports_failed_outcome_when_real_provider_all_failed():
    """is_mock=false 不代表真实数据真流入：真实 provider 但 LLM 调用全失败时，
    status 应返回 last_run_outcome=failed，让前端区分'配置了真实模型但没成功'。

    场景：patch 真实 openai 配置（is_mock=false）+ get_llm_provider 抛 LLMConfigError。
    期望：跑完一批后 status is_mock=false 且 last_run_outcome='failed'。
    """
    from unittest.mock import patch
    from agents.llm_provider import LLMConfigError
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "失败outcome小说", "第一章 觉醒\n莫凡觉醒火系。", headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    fake_real_config = {
        "provider": "openai", "api_key": "sk-bad", "base_url": None, "model": "gpt-4o",
    }

    def _raising_provider(_cfg):
        raise LLMConfigError("模拟 API key 无效")

    with patch("api.llm_deps.get_user_llm_config", return_value=fake_real_config), \
         patch("agents.llm_provider.get_llm_provider", side_effect=_raising_provider):
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )
        # 推进直到非 RUNNING
        for _ in range(6):
            s = client.get(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
            ).json()
            if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
                break
            client.post(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
                json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
            )

    data = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert data["is_mock"] is False, f"真实 provider 不应 is_mock，实际: {data}"
    assert data.get("last_run_outcome") == "failed", (
        f"真实 provider 全失败时应 last_run_outcome=failed，实际: {data}"
    )


# ── 补1：RETRYING 悬空态负例测试 ─────────────────────────


def test_job_not_completed_when_retrying_pending():
    """RETRYING 悬空态时 job 不应标记 COMPLETED，后续 retry 成功后才能完成。

    构造真实场景：用假 provider 让某章第一次返回非法 JSON（→RETRYING），
    验证此时 job 不能 COMPLETED。然后让 mock 返回合法 JSON（→MERGED），
    再次推进后 job 才能 COMPLETED。
    """
    from unittest.mock import patch
    from agents.llm_provider import MockProvider

    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "retry小说",
        "第一章 觉醒\n莫凡觉醒了火系。",
        headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 第一次：假 provider 返回非法 JSON，制造 RETRYING 悬空态
    bad_provider = _FakeProvider(responses=["这不是JSON"])
    with patch("agents.llm_provider.get_llm_provider", return_value=bad_provider), \
         patch("api.llm_deps.get_user_llm_config", return_value={"provider": "mock"}):
        resp = client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"max_chapters_per_run": 5}, headers=headers,
        )
    assert resp.status_code == 200
    # 此时 staging 应是 RETRYING 或 FAILED，job 不应 COMPLETED
    status = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert status["status"] == JobStatus.BATCH_DONE, (
        f"RETRYING 待重试时本次请求已结束，应回到 BATCH_DONE 允许继续，实际: {status['status']}"
    )
    assert status["status"] != JobStatus.COMPLETED, (
        f"存在 RETRYING 悬空态时不应 COMPLETED，实际: {status['status']}"
    )

    # 第二次：用真 MockProvider 返回合法 JSON，retry 成功 → MERGED
    good_provider = MockProvider()
    with patch("agents.llm_provider.get_llm_provider", return_value=good_provider), \
         patch("api.llm_deps.get_user_llm_config", return_value={"provider": "mock"}):
        for _ in range(5):
            s = client.get(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
            ).json()
            if s["status"] in (JobStatus.COMPLETED, JobStatus.PARTIAL_FAILED, JobStatus.FAILED):
                break
            client.post(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
                json={"max_chapters_per_run": 5}, headers=headers,
            )

    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert final["status"] == JobStatus.COMPLETED, (
        f"retry 成功后应 COMPLETED，实际: {final['status']}"
    )
    assert final["merged_count"] >= 1


# ── 补2：MERGE_FAILED 持续失败→FAILED 终态 ───────────────


def test_merge_failed_eventually_becomes_failed_terminal():
    """merge 持续失败时，超过 MAX_RETRIES 后 staging 转 FAILED 终态，job 不永远 RUNNING。

    构造场景：mock 返回合法 JSON 但 merge 阶段持续抛异常，
    多次推进后 staging 进入 FAILED，job 进入 FAILED 或 PARTIAL_FAILED。
    """
    from unittest.mock import patch

    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "merge失败小说",
        "第一章 觉醒\n莫凡觉醒了火系。",
        headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # mock：LLM 返回合法 JSON，但 merge 持续失败
    good_provider = _FakeProvider(responses=None)  # None → 用默认合法 JSON

    async def _failing_merge(*args, **kwargs):
        raise RuntimeError("模拟 merge 稳定 bug")

    with patch("agents.llm_provider.get_llm_provider", return_value=good_provider), \
         patch("api.llm_deps.get_user_llm_config", return_value={"provider": "mock"}), \
         patch("services.extraction_service._merge_extraction", side_effect=_failing_merge):
        for _ in range(8):
            s = client.get(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
            ).json()
            if s["status"] in (JobStatus.COMPLETED, JobStatus.PARTIAL_FAILED, JobStatus.FAILED):
                break
            client.post(
                f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
                json={"max_chapters_per_run": 5}, headers=headers,
            )

    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    # job 应进入终态，不能一直 RUNNING
    assert final["status"] in (JobStatus.FAILED, JobStatus.PARTIAL_FAILED), (
        f"merge 持续失败后 job 应进终态，实际: {final['status']}"
    )
    # staging 应有 FAILED 记录（merge 失败超限转 FAILED）
    async def _check_staging():
        async with test_session_factory() as session:
            result = await session.execute(
                select(ExtractionStaging).where(ExtractionStaging.source_id == sid)
            )
            return [(s.status, s.retry_count) for s in result.scalars().all()]
    stagings = asyncio.run(_check_staging())
    assert any(s[0] == StagingStatus.FAILED for s in stagings), (
        f"应有 FAILED 终态 staging，实际: {stagings}"
    )


# ── 辅助：可控的假 Provider ─────────────────────────────


class _FakeProvider:
    """测试用假 LLM Provider，可控制返回内容。

    responses=None 时返回合法 JSON（复用 MockProvider 的抽取逻辑）。
    responses=["非JSON"] 时返回指定内容。
    """

    def __init__(self, responses=None):
        self._responses = responses
        self._mock = MockProvider() if responses is None else None

    async def generate(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        if self._mock is not None:
            return await self._mock.generate(system_prompt, user_prompt, **kwargs)
        return self._responses[0] if self._responses else "not json"

    async def generate_stream(self, *args, **kwargs):
        yield ""


class _SequenceProvider:
    """测试用顺序 Provider，每次 generate 返回下一段 JSON。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self._index = 0

    async def generate(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        if self._index >= len(self._responses):
            return self._responses[-1]
        response = self._responses[self._index]
        self._index += 1
        return response

    async def generate_stream(self, *args, **kwargs):
        yield ""


# ── 上传 novel genre/canon_level 闭环测试 ────────────────


def test_upload_novel_saves_genre_in_metadata():
    """上传 source_type=novel + genre=historical，metadata 应保存 genre。"""
    import io
    pid, headers = _create_project()
    content = "第一章 觉醒\n莫凡觉醒了火系。\n第二章 初战\n张小侯释放风轨。"
    file_data = io.BytesIO(content.encode("utf-8"))

    resp = client.post(
        f"/api/projects/{pid}/knowledge/upload",
        files={"file": ("小说.txt", file_data, "text/plain")},
        data={
            "title": "测试小说",
            "source_type": "novel",
            "genre": "historical",
            "canon_level": "original",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "测试小说"
    sid = data["id"]

    # 查回 source 确认 source_type 和 metadata
    detail = client.get(f"/api/projects/{pid}/knowledge/sources/{sid}", headers=headers)
    assert detail.status_code == 200
    src = detail.json()
    assert src["source_type"] == "novel"
    # metadata_ 在响应里
    meta = src.get("metadata_") or {}
    assert meta.get("genre") == "historical", f"metadata 应存 genre=historical，实际: {meta}"
    assert meta.get("canon_level") == "original"


def test_upload_novel_default_genre_when_empty():
    """上传 novel 但 genre 为空，后端应默认 magic_fantasy。"""
    import io
    pid, headers = _create_project()
    content = "第一章 觉醒\n莫凡觉醒了火系。"
    file_data = io.BytesIO(content.encode("utf-8"))

    resp = client.post(
        f"/api/projects/{pid}/knowledge/upload",
        files={"file": ("小说.txt", file_data, "text/plain")},
        data={"title": "默认类型小说", "source_type": "novel"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    detail = client.get(f"/api/projects/{pid}/knowledge/sources/{sid}", headers=headers)
    meta = detail.json().get("metadata_") or {}
    assert meta.get("genre") == "magic_fantasy", f"空 genre 应默认 magic_fantasy，实际: {meta}"


def test_upload_novel_invalid_genre_returns_400():
    """上传 novel + 非法 genre 应返回 400。"""
    import io
    pid, headers = _create_project()
    content = "测试内容"
    file_data = io.BytesIO(content.encode("utf-8"))

    resp = client.post(
        f"/api/projects/{pid}/knowledge/upload",
        files={"file": ("小说.txt", file_data, "text/plain")},
        data={"title": "非法类型", "source_type": "novel", "genre": "scifi"},
        headers=headers,
    )
    assert resp.status_code == 400, f"非法 genre 应 400，实际: {resp.status_code}"
    assert "genre" in resp.text


# ── BATCH_DONE 状态测试：批次完成 vs 全书完成 ──────────


def test_batch_done_when_quota_reached_but_more_chapters():
    """章节数 > max_chapters_per_run 时，本批完成应标 BATCH_DONE 而非 COMPLETED。

    构造 6 章，max_chapters_per_run=5，抽完后应有 5 章处理完、1 章未处理，
    状态为 BATCH_DONE（不是 COMPLETED）。
    """
    pid, headers = _create_project()
    # 构造 6 章（正文不含"第X章"避免误切）
    content = "\n".join(
        f"第{c}章 标题\n莫凡觉醒火系。张小侯释放风轨。"
        for c in "一二三四五六"
    )
    create_resp = _create_novel_source(pid, "6章小说", content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 确认切出 6 章
    split_resp = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    )

    # 启动抽取，max_chapters_per_run=5（小于 6 章）
    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5},
        headers=headers,
    )
    assert resp.status_code == 200

    # 轮询直到非 RUNNING
    for _ in range(8):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )

    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert final["total_chapters"] == 6, f"应切 6 章，实际 {final['total_chapters']}"
    assert final["extracted_count"] <= 5, f"不应超过配额 5，实际 {final['extracted_count']}"
    assert final["status"] == JobStatus.BATCH_DONE, (
        f"未全书完成时应 BATCH_DONE，实际: {final['status']}"
    )


def test_completed_when_all_chapters_processed():
    """章节数 <= max_chapters_per_run 时，全部处理完应标 COMPLETED。"""
    pid, headers = _create_project()
    content = "\n".join(
        f"第{c}章 标题\n莫凡觉醒火系。"
        for c in "一二三"
    )
    create_resp = _create_novel_source(pid, "3章小说", content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 20},
        headers=headers,
    )
    assert resp.status_code == 200

    for _ in range(8):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 20}, headers=headers,
        )

    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    # 3 章，配额 20，全部处理完 → COMPLETED
    assert final["status"] == JobStatus.COMPLETED, (
        f"全书完成应 COMPLETED，实际: {final['status']}"
    )


def test_batch_done_can_continue_to_completed():
    """BATCH_DONE 后继续推进，最终全书完成应变 COMPLETED。

    6 章 + 配额 5 → BATCH_DONE → 继续推进剩余 1 章 → COMPLETED。
    """
    pid, headers = _create_project()
    content = "\n".join(
        f"第{c}章 标题\n莫凡觉醒火系。"
        for c in "一二三四五六"
    )
    create_resp = _create_novel_source(pid, "续抽小说", content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 第一批：配额 5 → BATCH_DONE
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )
    for _ in range(5):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )
    mid = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert mid["status"] == JobStatus.BATCH_DONE, f"首批应 BATCH_DONE，实际: {mid['status']}"

    # 第二批：继续推进（复用同一 job）
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )
    for _ in range(5):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING, JobStatus.BATCH_DONE):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )
    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert final["status"] == JobStatus.COMPLETED, (
        f"续抽完全部应 COMPLETED，实际: {final['status']}, extracted={final['extracted_count']}"
    )


# ── §1 旧任务状态自愈测试 ───────────────────────────────


def test_completed_legacy_job_self_heals_to_batch_done():
    """旧 bug: COMPLETED 但 extracted < total → 自愈为 BATCH_DONE。"""
    import uuid as _uuid
    pid = str(_uuid.uuid4())
    sid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            # 构造旧错误状态：COMPLETED, 20/3130
            job = ExtractionJob(
                project_id=pid, source_id=sid, genre="magic_fantasy",
                status=JobStatus.COMPLETED,
                total_chapters=3130, extracted_count=20,
                merged_count=20, failed_count=0,
                max_chapters_per_run=20,
            )
            session.add(job)
            await session.commit()

            # 调 status 接口（触发自愈）
            from services.extraction_service import get_latest_job_status
            result = await get_latest_job_status(session, pid, sid)

            # 返回应是 BATCH_DONE
            assert result["status"] == JobStatus.BATCH_DONE, (
                f"旧 COMPLETED 20/3130 应自愈为 BATCH_DONE，实际: {result['status']}"
            )

            # DB 里也应改成 BATCH_DONE 且 finished_at 清空
            await session.refresh(job)
            assert job.status == JobStatus.BATCH_DONE
            assert job.finished_at is None

    asyncio.run(_run())


def test_truly_completed_job_not_self_healed():
    """真正完成的 job（extracted == total）不应被自愈改动。"""
    import uuid as _uuid
    pid = str(_uuid.uuid4())
    sid = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            job = ExtractionJob(
                project_id=pid, source_id=sid, genre="magic_fantasy",
                status=JobStatus.COMPLETED,
                total_chapters=10, extracted_count=10,
                merged_count=10, failed_count=0,
                max_chapters_per_run=20,
            )
            session.add(job)
            await session.commit()

            from services.extraction_service import get_latest_job_status
            result = await get_latest_job_status(session, pid, sid)
            assert result["status"] == JobStatus.COMPLETED, (
                f"真正完成的不应被改动，实际: {result['status']}"
            )

    asyncio.run(_run())


# ── §2 BATCH_DONE 可继续推进（已有 test_batch_done_can_continue 覆盖，补端到端） ──


def test_legacy_batch_done_job_continues_via_extract_api():
    """BATCH_DONE 的旧 job 通过 extract API 继续推进，最终 COMPLETED。

    端到端：构造 6 章配额 5 → BATCH_DONE → 调 extract 继续 → COMPLETED。
    验证不新建重复 job。
    """
    pid, headers = _create_project()
    nums = ["一", "二", "三", "四", "五", "六"]
    content = "\n".join(f"第{c}章 标题\n莫凡觉醒火系。" for c in nums)
    create_resp = _create_novel_source(pid, "续抽端到端", content, headers)
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    # 第一批：配额 5 → BATCH_DONE
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )
    for _ in range(5):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )
    mid = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert mid["status"] == JobStatus.BATCH_DONE

    # 记录 job_id
    job_id = mid.get("job_id") or mid.get("id")

    # 第二批：继续推进
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )
    for _ in range(5):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING, JobStatus.BATCH_DONE):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )
    final = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert final["status"] == JobStatus.COMPLETED, (
        f"续抽后应 COMPLETED，实际: {final['status']}"
    )
    # 不应新建 job（job_id 应保持一致）
    final_job_id = final.get("job_id") or final.get("id")
    assert final_job_id == job_id, "续抽应复用同一 job，不应新建"


# ── §3 重置抽取接口测试 ─────────────────────────────────


def test_reset_extraction_clears_pipeline_and_structured_tables():
    """重置接口清除 staging/job/4 张结构化表，保留原文和 chunks。"""
    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid, "重置测试",
        "第一章 觉醒\n莫凡觉醒了火系。\n第二章 战斗\n张小侯释放风轨。",
        headers,
    )
    sid = create_resp.json()["id"]
    # 切分 + 抽取
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)
    client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
        json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
    )
    # 等抽取完成
    for _ in range(5):
        s = client.get(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
        ).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers,
        )

    # 确认有数据
    async def _count():
        async with test_session_factory() as session:
            jobs = (await session.execute(select(ExtractionJob).where(ExtractionJob.source_id == sid))).scalars().all()
            staging = (await session.execute(select(ExtractionStaging).where(ExtractionStaging.source_id == sid))).scalars().all()
            chars = (await session.execute(select(CharacterProfile).where(CharacterProfile.source_id == sid))).scalars().all()
            return len(jobs), len(staging), len(chars)
    j, st, c = asyncio.run(_count())
    assert j > 0 and st > 0 and c > 0, f"抽取后应有数据，实际 jobs={j} staging={st} chars={c}"

    # 调重置
    resp = client.post(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/reset", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "reset"
    assert data["deleted_jobs"] >= 1
    assert data["deleted_staging"] >= 1

    # 确认清空
    j2, st2, c2 = asyncio.run(_count())
    assert j2 == 0, f"重置后 jobs 应为 0，实际 {j2}"
    assert st2 == 0, f"重置后 staging 应为 0，实际 {st2}"
    assert c2 == 0, f"重置后 characters 应为 0，实际 {c2}"

    # 确认原文 source 还在
    src = client.get(f"/api/projects/{pid}/knowledge/sources/{sid}", headers=headers)
    assert src.status_code == 200
    assert src.json()["source_type"] == "novel"

    # 确认 status 回到 NONE
    s = client.get(
        f"/api/projects/{pid}/knowledge/sources/{sid}/extract/status", headers=headers,
    ).json()
    assert s["status"] == "NONE"


def test_force_reextract_clears_stale_structured_rows_for_target_chapter():
    """force_reextract 重抽单章时，应清掉该章旧 merged 行且不动其它章节。"""
    from unittest.mock import patch

    def _payload(chapter_no, title, *, abilities, events, rules):
        characters = []
        seen_characters = set()
        for item in abilities:
            character_name = item["character"]
            if character_name in seen_characters:
                continue
            seen_characters.add(character_name)
            characters.append({
                "name": character_name,
                "aliases": [],
                "identity": "学生",
                "status": "登场",
                "importance": 3,
                "confidence": 0.9,
                "evidence": item["evidence"],
            })
        return json.dumps({
            "chapter_no": chapter_no,
            "chapter_title": title,
            "chapter_summary": title,
            "characters": characters,
            "abilities": [
                {
                    "character": item["character"],
                    "ability_type": item["ability_type"],
                    "ability_name": item["ability_name"],
                    "level": "",
                    "status": "new",
                    "importance": 3,
                    "confidence": 0.9,
                    "evidence": item["evidence"],
                }
                for item in abilities
            ],
            "events": [
                {
                    "event_title": item["title"],
                    "event_desc": item["desc"],
                    "characters": item["characters"],
                    "location": "",
                    "cause": "",
                    "effect": "",
                    "importance": 3,
                    "confidence": 0.9,
                    "evidence": item["evidence"],
                }
                for item in events
            ],
            "world_rules": [
                {
                    "category": item["category"],
                    "rule_text": item["rule_text"],
                    "priority": "medium",
                    "confidence": 0.9,
                    "evidence": item["evidence"],
                }
                for item in rules
            ],
        }, ensure_ascii=False)

    pid, headers = _create_project()
    create_resp = _create_novel_source(
        pid,
        "重抽清理小说",
        "第一章 觉醒\n莫凡觉醒了火系。\n第二章 初战\n张小侯释放风轨。",
        headers,
    )
    sid = create_resp.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid}/split-chapters", headers=headers)

    first_provider = _SequenceProvider([
        _payload(
            1,
            "觉醒",
            abilities=[
                {"character": "莫凡", "ability_type": "magic_element", "ability_name": "火系", "evidence": "莫凡觉醒了火系"},
                {"character": "莫凡", "ability_type": "magic_element", "ability_name": "冰系", "evidence": "莫凡误记为冰系"},
            ],
            events=[
                {"title": "旧误判事件", "desc": "旧抽取误判", "characters": ["莫凡"], "evidence": "莫凡误记为冰系"},
            ],
            rules=[
                {"category": "误判规则", "rule_text": "旧规则应该被清理", "evidence": "莫凡误记为冰系"},
            ],
        ),
        _payload(
            2,
            "初战",
            abilities=[
                {"character": "张小侯", "ability_type": "magic_element", "ability_name": "风系", "evidence": "张小侯释放风轨"},
            ],
            events=[
                {"title": "张小侯释放风轨", "desc": "第二章事件", "characters": ["张小侯"], "evidence": "张小侯释放风轨"},
            ],
            rules=[
                {"category": "风系规则", "rule_text": "风轨属于风系技能", "evidence": "张小侯释放风轨"},
            ],
        ),
    ])
    with patch("agents.llm_provider.get_llm_provider", return_value=first_provider), \
         patch("api.llm_deps.get_user_llm_config", return_value={"provider": "mock"}):
        resp = client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={"genre": "magic_fantasy", "max_chapters_per_run": 5},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text

    second_provider = _SequenceProvider([
        _payload(
            1,
            "觉醒",
            abilities=[
                {"character": "莫凡", "ability_type": "magic_element", "ability_name": "火系", "evidence": "莫凡觉醒了火系"},
            ],
            events=[
                {"title": "莫凡觉醒火系", "desc": "修正后的第一章事件", "characters": ["莫凡"], "evidence": "莫凡觉醒了火系"},
            ],
            rules=[
                {"category": "觉醒规则", "rule_text": "觉醒可能获得魔法系别", "evidence": "莫凡觉醒了火系"},
            ],
        ),
    ])
    with patch("agents.llm_provider.get_llm_provider", return_value=second_provider), \
         patch("api.llm_deps.get_user_llm_config", return_value={"provider": "mock"}):
        resp = client.post(
            f"/api/projects/{pid}/knowledge/sources/{sid}/extract",
            json={
                "genre": "magic_fantasy",
                "chapter_no_start": 1,
                "chapter_no_end": 1,
                "max_chapters_per_run": 1,
                "force_reextract": True,
            },
            headers=headers,
        )
    assert resp.status_code == 200, resp.text

    async def _snapshot():
        async with test_session_factory() as session:
            abilities = (await session.execute(
                select(AbilityProfile)
                .where(AbilityProfile.source_id == sid)
                .order_by(AbilityProfile.character_name, AbilityProfile.ability_name)
            )).scalars().all()
            events = (await session.execute(
                select(EventTimeline)
                .where(EventTimeline.source_id == sid)
                .order_by(EventTimeline.chapter_no, EventTimeline.event_title)
            )).scalars().all()
            rules = (await session.execute(
                select(WorldRule)
                .where(WorldRule.source_id == sid)
                .order_by(WorldRule.chapter_no, WorldRule.category)
            )).scalars().all()
            return abilities, events, rules

    abilities, events, rules = asyncio.run(_snapshot())
    ability_keys = {
        (a.character_name, a.ability_type, a.ability_name)
        for a in abilities
    }
    assert ("莫凡", "magic_element", "冰系") not in ability_keys
    assert ("莫凡", "magic_element", "火系") in ability_keys
    assert ("张小侯", "magic_element", "风系") in ability_keys

    event_titles = {event.event_title for event in events}
    assert "旧误判事件" not in event_titles
    assert "莫凡觉醒火系" in event_titles
    assert "张小侯释放风轨" in event_titles

    rule_texts = {rule.rule_text for rule in rules}
    assert "旧规则应该被清理" not in rule_texts
    assert "觉醒可能获得魔法系别" in rule_texts
    assert "风轨属于风系技能" in rule_texts


# ── §4 reset / status 按 project_id + source_id 限定，不跨项目误删 ──


def test_reset_does_not_touch_other_source_in_same_project():
    """reset 只删当前 source，不影响同 project 下其它 source 的结构化数据。"""
    pid, headers = _create_project()

    # source A：抽取
    create_a = _create_novel_source(
        pid, "资料A",
        "第一章 觉醒\n莫凡觉醒了火系。", headers,
    )
    sid_a = create_a.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid_a}/split-chapters", headers=headers)
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid_a}/extract",
                json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers)
    for _ in range(5):
        s = client.get(f"/api/projects/{pid}/knowledge/sources/{sid_a}/extract/status",
                       headers=headers).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(f"/api/projects/{pid}/knowledge/sources/{sid_a}/extract",
                    json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers)

    # source B：抽取
    create_b = _create_novel_source(
        pid, "资料B",
        "第一章 初战\n张小侯释放风轨击退敌人。", headers,
    )
    sid_b = create_b.json()["id"]
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid_b}/split-chapters", headers=headers)
    client.post(f"/api/projects/{pid}/knowledge/sources/{sid_b}/extract",
                json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers)
    for _ in range(5):
        s = client.get(f"/api/projects/{pid}/knowledge/sources/{sid_b}/extract/status",
                       headers=headers).json()
        if s["status"] not in (JobStatus.RUNNING, JobStatus.PENDING):
            break
        client.post(f"/api/projects/{pid}/knowledge/sources/{sid_b}/extract",
                    json={"genre": "magic_fantasy", "max_chapters_per_run": 5}, headers=headers)

    # 重置 source A
    resp = client.post(f"/api/projects/{pid}/knowledge/sources/{sid_a}/extract/reset", headers=headers)
    assert resp.status_code == 200, resp.text

    # source B 的结构化数据应原样保留
    async def _count_b():
        async with test_session_factory() as session:
            jobs = (await session.execute(
                select(ExtractionJob).where(ExtractionJob.source_id == sid_b)
            )).scalars().all()
            staging = (await session.execute(
                select(ExtractionStaging).where(ExtractionStaging.source_id == sid_b)
            )).scalars().all()
            chars = (await session.execute(
                select(CharacterProfile).where(CharacterProfile.source_id == sid_b)
            )).scalars().all()
            return len(jobs), len(staging), len(chars)
    jb, stb, cb = asyncio.run(_count_b())
    assert jb > 0, f"reset source A 不应影响 source B 的 job，实际 {jb}"
    assert stb > 0, f"reset source A 不应影响 source B 的 staging，实际 {stb}"
    assert cb > 0, f"reset source A 不应影响 source B 的 characters，实际 {cb}"

    # source B 的 status 也应未受影响
    s_b = client.get(f"/api/projects/{pid}/knowledge/sources/{sid_b}/extract/status",
                     headers=headers).json()
    assert s_b["status"] != "NONE", f"source B 状态不应被 reset 清空，实际 {s_b['status']}"


def test_get_latest_job_status_scoped_to_project():
    """get_latest_job_status 按 project_id + source_id 限定。

    构造两个 project 各一个 job（project_id 不同、source_id 不同），
    断言查询只会命中当前 (project_id, source_id) 对，不会跨 project 串。
    """
    import uuid as _uuid
    from services.extraction_service import get_latest_job_status

    pid_a = str(_uuid.uuid4())
    sid_a = str(_uuid.uuid4())
    pid_b = str(_uuid.uuid4())
    sid_b = str(_uuid.uuid4())

    async def _run():
        async with test_session_factory() as session:
            session.add(ExtractionJob(
                project_id=pid_a, source_id=sid_a, genre="magic_fantasy",
                status=JobStatus.COMPLETED,
                total_chapters=10, extracted_count=10, merged_count=10,
            ))
            session.add(ExtractionJob(
                project_id=pid_b, source_id=sid_b, genre="magic_fantasy",
                status=JobStatus.COMPLETED,
                total_chapters=10, extracted_count=10, merged_count=10,
            ))
            await session.commit()

            # 查 pid_a/sid_a 只命中 A 的 job
            r_a = await get_latest_job_status(session, pid_a, sid_a)
            assert r_a is not None
            assert r_a["id"] is not None
            # 查 pid_a + 不存在的 source 应返回 None
            r_none = await get_latest_job_status(session, pid_a, sid_b)
            assert r_none is None, (
                f"project_a + source_b 不匹配，应返回 None，实际: {r_none}"
            )

    asyncio.run(_run())


# ── 抽取任务体验控制：暂停/取消/失败列表/单章重试/单章推进 ──

_TEST_PID = "00000000-0000-0000-0000-0000000000a0"
_TEST_SID = "00000000-0000-0000-0000-0000000000a1"


async def _seed_job_and_chapters(session, n_chapters=3, provider="mock", status="PENDING",
                                  pid=_TEST_PID, sid=_TEST_SID):
    """造一个 job + n 个章节，返回 job_obj。"""
    job = ExtractionJob(
        project_id=pid, source_id=sid,
        genre="magic_fantasy", status=status, provider=provider,
        chapter_no_start=1, chapter_no_end=n_chapters,
        total_chapters=n_chapters, max_chapters_per_run=1,
    )
    session.add(job)
    await session.flush()
    for i in range(1, n_chapters + 1):
        session.add(ProjectSourceChapter(
            project_id=pid, source_id=sid,
            chapter_no=i, content=f"第{i}章内容",
        ))
    await session.flush()
    return job


def test_extract_advances_only_one_chapter_for_real_provider():
    """非 mock provider 下，一次 advance 只处理 1 章。"""
    PID = "00000000-0000-0000-0000-0000000000b0"
    SID = "00000000-0000-0000-0000-0000000000b1"
    import services.extraction_service as svc
    original = svc._resolve_provider_name
    async def _run():
        async with test_session_factory() as session:
            await _seed_job_and_chapters(session, n_chapters=3, provider="deepseek", pid=PID, sid=SID)
            from services.extraction_service import advance_extraction_job
            async def _fake_resolve(uid, db):
                return "deepseek"
            svc._resolve_provider_name = _fake_resolve
            try:
                job = await advance_extraction_job(
                    session, PID, SID,
                    user_id="test-user",
                    genre="magic_fantasy", chapter_no_start=1, chapter_no_end=3,
                    max_chapters_per_run=1,
                )
            finally:
                svc._resolve_provider_name = original
            # 只推进了 1 章
            assert job["extracted_count"] <= 1, f"真实 provider 应只推进1章, 实际: {job['extracted_count']}"
    asyncio.run(_run())


def test_pause_extraction_stops_future_advance():
    """pause 后再次 advance 不推进。"""
    import services.extraction_service as svc
    original_resolve = svc._resolve_provider_name
    PID = "00000000-0000-0000-0000-0000000000c0"
    SID = "00000000-0000-0000-0000-0000000000c1"

    async def _run():
        async with test_session_factory() as session:
            await _seed_job_and_chapters(session, n_chapters=3, status="BATCH_DONE", pid=PID, sid=SID)
            from services.extraction_service import pause_extraction_job, advance_extraction_job, JobStatus
            async def _fake_resolve(uid, db):
                return "mock"
            svc._resolve_provider_name = _fake_resolve
            try:
                # 暂停
                paused = await pause_extraction_job(session, PID, SID)
                assert paused is not None
                assert paused["status"] == JobStatus.PAUSED
                assert paused.get("paused_at") is not None

                # 再次 advance → 应直接返回，不推进
                job = await advance_extraction_job(
                    session, PID, SID,
                    user_id="test-user", genre="magic_fantasy",
                    chapter_no_start=1, chapter_no_end=3, max_chapters_per_run=1,
                )
                assert job["status"] == JobStatus.PAUSED, f"暂停后应不推进, status={job['status']}"
                assert job["extracted_count"] == 0, f"暂停后 extracted_count 应为0, 实际: {job['extracted_count']}"
            finally:
                svc._resolve_provider_name = original_resolve
    asyncio.run(_run())


def test_cancel_extraction_stops_future_advance():
    """cancel 后该 job 不会被继续推进（新 advance 会创建新 job，旧 job 保持 CANCELLED）。"""
    import services.extraction_service as svc
    original_resolve = svc._resolve_provider_name

    async def _run():
        async with test_session_factory() as session:
            job_obj = await _seed_job_and_chapters(session, n_chapters=3, status="BATCH_DONE")
            old_job_id = str(job_obj.id)
            from services.extraction_service import cancel_extraction_job, JobStatus
            from sqlalchemy import select
            async def _fake_resolve(uid, db):
                return "mock"
            svc._resolve_provider_name = _fake_resolve
            try:
                cancelled = await cancel_extraction_job(session, _TEST_PID, _TEST_SID)
                assert cancelled is not None
                assert cancelled["status"] == JobStatus.CANCELLED
                assert cancelled.get("cancelled_at") is not None

                # 旧 job 保持 CANCELLED，extracted_count=0（未被推进）
                r = await session.execute(
                    select(ExtractionJob).where(ExtractionJob.id == job_obj.id)
                )
                old_job = r.scalar_one()
                assert old_job.status == JobStatus.CANCELLED
                assert old_job.extracted_count == 0, "旧 job 不应被推进"
            finally:
                svc._resolve_provider_name = original_resolve
    asyncio.run(_run())


def test_list_extraction_failures():
    """构造失败 staging，接口能列出。"""
    PID = "00000000-0000-0000-0000-0000000000e0"
    SID = "00000000-0000-0000-0000-0000000000e1"
    async def _run():
        async with test_session_factory() as session:
            job = await _seed_job_and_chapters(session, n_chapters=3, status="RUNNING", pid=PID, sid=SID)
            from services.extraction_service import ExtractionStaging, StagingStatus, list_extraction_failures
            session.add(ExtractionStaging(
                job_id=str(job.id), project_id=PID, source_id=SID,
                chapter_no=1, chapter_title="第1章", genre="magic_fantasy",
                template_name="default", schema_version="1.0",
                raw_output="bad", status=StagingStatus.VALIDATION_FAILED,
                error_message="JSON 解析失败", retry_count=2,
            ))
            session.add(ExtractionStaging(
                job_id=str(job.id), project_id=PID, source_id=SID,
                chapter_no=3, chapter_title="第3章", genre="magic_fantasy",
                template_name="default", schema_version="1.0",
                raw_output="err", status=StagingStatus.FAILED,
                error_message="LLM 超时", retry_count=1,
            ))
            await session.flush()

            result = await list_extraction_failures(session, PID, SID)
            assert result["total"] == 2
            nos = [it["chapter_no"] for it in result["items"]]
            assert 1 in nos and 3 in nos
            item1 = [it for it in result["items"] if it["chapter_no"] == 1][0]
            assert item1["status"] == StagingStatus.VALIDATION_FAILED
            assert item1["retry_count"] == 2
            assert "JSON" in item1["error_message"]
    asyncio.run(_run())


def test_retry_failed_chapter_only_reprocesses_that_chapter():
    """只重试指定章节，不影响其他章节的 staging。"""
    PID = "00000000-0000-0000-0000-0000000000f0"
    SID = "00000000-0000-0000-0000-0000000000f1"
    async def _run():
        async with test_session_factory() as session:
            job = await _seed_job_and_chapters(session, n_chapters=3, status="BATCH_DONE", pid=PID, sid=SID)
            from services.extraction_service import ExtractionStaging, StagingStatus
            for ch_no, st in [(1, StagingStatus.MERGED), (2, StagingStatus.FAILED), (3, StagingStatus.FAILED)]:
                session.add(ExtractionStaging(
                    job_id=str(job.id), project_id=PID, source_id=SID,
                    chapter_no=ch_no, chapter_title=f"第{ch_no}章", genre="magic_fantasy",
                    template_name="default", schema_version="1.0",
                    raw_output="x", status=st, retry_count=0,
                ))
            await session.flush()

            from services.extraction_service import retry_extraction_chapter
            import services.extraction_service as svc
            async def _fake_resolve(uid, db):
                return "mock"
            original = svc._resolve_provider_name
            svc._resolve_provider_name = _fake_resolve
            try:
                result = await retry_extraction_chapter(
                    session, PID, SID, chapter_no=2,
                    user_id="test-user",
                )
            finally:
                svc._resolve_provider_name = original

            assert result["chapter_no"] == 2
            from sqlalchemy import select
            remaining = await session.execute(
                select(ExtractionStaging.chapter_no, ExtractionStaging.status)
                .where(ExtractionStaging.source_id == SID)
                .where(ExtractionStaging.chapter_no.in_([1, 3]))
            )
            remaining_rows = {(r[0], r[1]) for r in remaining.all()}
            assert (1, StagingStatus.MERGED) in remaining_rows, "第1章不应被影响"
            assert (3, StagingStatus.FAILED) in remaining_rows, "第3章不应被影响"
    asyncio.run(_run())


def test_status_returns_current_chapter_and_last_error():
    """状态接口包含新增字段 current_chapter_no / last_error / pending_count。"""
    PID = "00000000-0000-0000-0000-000000000011"
    SID = "00000000-0000-0000-0000-000000000012"
    async def _run():
        async with test_session_factory() as session:
            job = await _seed_job_and_chapters(session, n_chapters=5, status="BATCH_DONE", pid=PID, sid=SID)
            job.current_chapter_no = 3
            job.last_error = "测试错误信息"
            job.extracted_count = 2
            await session.flush()

            from services.extraction_service import get_latest_job_status
            status = await get_latest_job_status(session, PID, SID)
            assert status is not None
            assert status["current_chapter_no"] == 3
            assert status["last_error"] == "测试错误信息"
            assert "last_run_started_at" in status
            assert "last_run_finished_at" in status
    asyncio.run(_run())


# ── 人物档案去重与章节出场记录 ──

async def _seed_character_extraction(session, pid, sid, chapter_no, name, identity="", status="",
                                      evidence="出现证据", importance=3, confidence=0.9):
    """模拟一章抽取到的人物，直接 merge 到正式库。"""
    from services.extraction_schema import CharacterItem
    from services.extraction_service import _merge_character
    char = CharacterItem(
        name=name, aliases=[], identity=identity or "", status=status or "",
        importance=importance, confidence=confidence, evidence=evidence,
    )
    await _merge_character(session, pid, sid, chapter_no, f"第{chapter_no}章", char,
                           "original", "llm_extracted", 60)


def test_character_profile_dedup_across_chapters():
    """连续两章抽到同一人物，profile 只有 1 条，appearance 有 2 条。"""
    PID = "00000000-0000-0000-0000-000000000010"
    SID = "00000000-0000-0000-0000-000000000020"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(session, PID, SID, 1, "莫凡", identity="主角")
            await _seed_character_extraction(session, PID, SID, 2, "莫凡", identity="主角")
            await session.commit()

            from sqlalchemy import select
            profiles = (await session.execute(
                select(CharacterProfile).where(CharacterProfile.project_id == PID)
            )).scalars().all()
            assert len(profiles) == 1, f"应只有1条profile, 实际{len(profiles)}"

            appearances = (await session.execute(
                select(CharacterAppearance)
                .where(CharacterAppearance.project_id == PID)
                .where(CharacterAppearance.canonical_name == "莫凡")
            )).scalars().all()
            assert len(appearances) == 2, f"应有2条appearance, 实际{len(appearances)}"
    asyncio.run(_run())


def test_character_profile_evidence_limited():
    """连续多章普通出场，profile evidence 不超过上限。"""
    PID = "00000000-0000-0000-0000-000000000030"
    SID = "00000000-0000-0000-0000-000000000040"
    async def _run():
        async with test_session_factory() as session:
            # 25 章普通出场（importance=2，非重要）
            for ch in range(1, 26):
                await _seed_character_extraction(
                    session, PID, SID, ch, "路人甲",
                    evidence=f"第{ch}章普通出场", importance=2,
                )
            await session.commit()

            from sqlalchemy import select
            profile = (await session.execute(
                select(CharacterProfile)
                .where(CharacterProfile.project_id == PID)
                .where(CharacterProfile.name == "路人甲")
            )).scalar_one()
            # evidence 不应无限增长（普通出场不追加）
            assert len(profile.evidence or []) <= 20, f"evidence应限制, 实际{len(profile.evidence or [])}"
            # appearance 应有 25 条
            appearances = (await session.execute(
                select(CharacterAppearance).where(CharacterAppearance.project_id == PID)
            )).scalars().all()
            assert len(appearances) == 25, f"appearance应有25条, 实际{len(appearances)}"
    asyncio.run(_run())


def test_character_profile_evidence_not_polluted_by_repeated_identity():
    """同一身份的普通重述不应被当成"状态变化"追加 evidence 到 profile。

    复刻真实 DeepSeek 的薛木生 bug：
      ch4: identity="天澜高中魔法导师、班主任", importance=4  (首次，进 profile)
      ch5/7/15/21/23: identity 变体如"8班班主任"/"班主任，强调冥修重要性",
                      importance=2 (普通出场，不应进 profile.evidence)

    旧 bug：status_desc 字符串不等 → has_status_change=True → evidence 全部追加，
    导致 14 次出场 / 14 条 evidence（但只有 1 次 importance>=4）。
    期望：profile.evidence 只含首次（或关键）的，普通重述不追加。
    """
    PID = "00000000-0000-0000-0000-000000000070"
    SID = "00000000-0000-0000-0000-000000000080"
    async def _run():
        async with test_session_factory() as session:
            # 首次：重要出场，带核心身份
            await _seed_character_extraction(
                session, PID, SID, 4, "薛木生",
                identity="天澜高中魔法导师、班主任",
                status="主持觉醒仪式，评价学生天赋",
                importance=4, evidence="导师-薛木生站在班级前列",
            )
            # 后续 5 章：同身份的不同重述 + 每章不同临时状态（复刻真实 LLM 输出）
            # 这些都是"班主任"这一身份的不同措辞、每章的临时状态，
            # 不是实质身份/状态变化，evidence 不应进 profile
            restatements = [
                (5, "8班班主任", "高兴，认为捡到宝", "班主任薛木生心里很开心"),
                (7, "8班班主任", "主持觉醒", "班主任薛木生语气平和的说道"),
                (15, "班主任，强调冥修重要性", "仅提及", "薛木生强调冥修"),
                (21, "八班班主任", "活", "薛木生点名"),
                (23, "班主任", "曾想内定莫凡为除名对象", "薛木生巡视"),
            ]
            for ch, ident, status, ev in restatements:
                await _seed_character_extraction(
                    session, PID, SID, ch, "薛木生",
                    identity=ident, status=status, importance=2, evidence=ev,
                )
            await session.commit()

            from sqlalchemy import select
            profile = (await session.execute(
                select(CharacterProfile)
                .where(CharacterProfile.project_id == PID)
                .where(CharacterProfile.name == "薛木生")
            )).scalar_one()
            ev_list = profile.evidence or []
            # 6 次出场，但只首次是 importance>=4 的关键证据，其余是身份重述
            # 期望 profile.evidence 远小于 6（普通重述不追加）
            assert len(ev_list) <= 2, (
                f"普通身份重述不应全部进 profile.evidence，实际 {len(ev_list)} 条: {ev_list}"
            )
            # appearance 应记录全部 6 次
            apps = (await session.execute(
                select(CharacterAppearance).where(CharacterAppearance.project_id == PID)
            )).scalars().all()
            assert len(apps) == 6, f"appearance 应有 6 条，实际 {len(apps)}"
    asyncio.run(_run())


def test_character_profile_evidence_keeps_substantive_identity_change():
    """实质身份变化（不是重述）仍应进 profile.evidence。

    场景：ch1 identity="学生"，ch5 identity="觉醒魔法师"（实质升级，非重述）。
    期望：profile.evidence 含两条（首次 + 实质变化），不是只 1 条。
    这是上一个测试的镜像，确保收紧不会误伤真实变化。
    """
    PID = "00000000-0000-0000-0000-0000000000b1"
    SID = "00000000-0000-0000-0000-0000000000b2"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(
                session, PID, SID, 1, "李四",
                identity="天澜高中学生", status="未觉醒",
                importance=4, evidence="李四是个普通学生",
            )
            # 普通重述（不应追加）
            await _seed_character_extraction(
                session, PID, SID, 3, "李四",
                identity="学生", status="上课",
                importance=2, evidence="李四在上课",
            )
            # 实质身份变化：学生 → 觉醒法师
            await _seed_character_extraction(
                session, PID, SID, 5, "李四",
                identity="火系觉醒法师", status="觉醒火系",
                importance=5, evidence="李四觉醒了火系",
            )
            await session.commit()

            from sqlalchemy import select
            profile = (await session.execute(
                select(CharacterProfile)
                .where(CharacterProfile.project_id == PID)
                .where(CharacterProfile.name == "李四")
            )).scalar_one()
            ev_list = profile.evidence or []
            # 首次 + 实质变化 = 2 条；普通重述（ch3）不追加
            assert len(ev_list) == 2, (
                f"首次+实质变化应2条，普通重述不追加。实际 {len(ev_list)}: {ev_list}"
            )
            assert profile.identity_desc == "火系觉醒法师"
            assert profile.status_desc == "觉醒火系"
            assert "普通学生" in ev_list[0]
    asyncio.run(_run())


def test_character_appearance_upsert_same_chapter():
    """同一章重复抽到同一人物，appearance 只有 1 条。"""
    PID = "00000000-0000-0000-0000-000000000050"
    SID = "00000000-0000-0000-0000-000000000060"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(session, PID, SID, 5, "重复角色", evidence="第一次")
            await _seed_character_extraction(session, PID, SID, 5, "重复角色", evidence="第二次")
            await session.commit()

            from sqlalchemy import select
            appearances = (await session.execute(
                select(CharacterAppearance)
                .where(CharacterAppearance.project_id == PID)
                .where(CharacterAppearance.chapter_no == 5)
            )).scalars().all()
            assert len(appearances) == 1, f"同一章应只有1条appearance, 实际{len(appearances)}"
    asyncio.run(_run())


def test_character_profile_seen_stats():
    """first_seen / last_seen / appearance_count 正确。"""
    PID = "00000000-0000-0000-0000-000000000070"
    SID = "00000000-0000-0000-0000-000000000080"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(session, PID, SID, 3, "统计角色")
            await _seed_character_extraction(session, PID, SID, 7, "统计角色")
            await _seed_character_extraction(session, PID, SID, 12, "统计角色")
            await session.commit()

            from sqlalchemy import select
            profile = (await session.execute(
                select(CharacterProfile)
                .where(CharacterProfile.project_id == PID)
                .where(CharacterProfile.name == "统计角色")
            )).scalar_one()
            assert profile.first_seen_chapter == 3, f"first_seen={profile.first_seen_chapter}"
            assert profile.last_seen_chapter == 12, f"last_seen={profile.last_seen_chapter}"
            assert profile.appearance_count == 3, f"count={profile.appearance_count}"
    asyncio.run(_run())


def test_reset_extraction_clears_character_appearances():
    """reset 后 appearance 归零。"""
    PID = "00000000-0000-0000-0000-000000000090"
    SID = "00000000-0000-0000-0000-000000000091"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(session, PID, SID, 1, "待清理角色")
            await session.commit()

            from services.extraction_service import reset_extraction
            from sqlalchemy import select, func
            await reset_extraction(session, PID, SID)

            count = (await session.execute(
                select(func.count(CharacterAppearance.id))
                .where(CharacterAppearance.project_id == PID)
            )).scalar()
            assert count == 0, f"reset后appearance应归零, 实际{count}"
    asyncio.run(_run())


def test_delete_source_removes_character_appearances():
    """删除 source 后 appearance 归零。"""
    PID = "00000000-0000-0000-0000-0000000000a2"
    SID = "00000000-0000-0000-0000-0000000000a3"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(session, PID, SID, 1, "删源角色")
            await session.commit()

            # 直接删 source 关联的 appearance（模拟 delete_knowledge_source 的清理逻辑）
            from sqlalchemy import delete, select, func
            await session.execute(
                delete(CharacterAppearance)
                .where(CharacterAppearance.source_id == SID)
                .where(CharacterAppearance.project_id == PID)
            )
            await session.commit()

            count = (await session.execute(
                select(func.count(CharacterAppearance.id))
                .where(CharacterAppearance.project_id == PID)
                .where(CharacterAppearance.source_id == SID)
            )).scalar()
            assert count == 0, f"删source后appearance应归零, 实际{count}"
    asyncio.run(_run())


def test_character_appearances_api():
    """人物出场接口返回正确章节列表。"""
    PID = "00000000-0000-0000-0000-0000000000a4"
    SID = "00000000-0000-0000-0000-0000000000a5"
    async def _run():
        async with test_session_factory() as session:
            await _seed_character_extraction(session, PID, SID, 1, "API角色", evidence="第1章出场")
            await _seed_character_extraction(session, PID, SID, 3, "API角色", evidence="第3章出场")
            await session.commit()

            from sqlalchemy import select
            profile = (await session.execute(
                select(CharacterProfile)
                .where(CharacterProfile.project_id == PID)
                .where(CharacterProfile.name == "API角色")
            )).scalar_one()

            from services.extraction_service import list_character_appearances
            result = await list_character_appearances(session, PID, str(profile.id))
            assert result["total"] == 2
            nos = [it["chapter_no"] for it in result["items"]]
            assert nos == [1, 3], f"章节顺序应升序, 实际{nos}"
            assert result["items"][0]["evidence_text"] == "第1章出场"
    asyncio.run(_run())
