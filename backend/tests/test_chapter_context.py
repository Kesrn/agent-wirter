"""ChapterContextService 测试 —— 复用 test_smoke 的测试基础架构"""
import asyncio

import pytest

# 显式导入 setup_db fixture 确保 SQLite 表在测试前创建
from test_smoke import client, test_session_factory, setup_db  # noqa: F401
from db.session import async_session as real_async_session
from services.chapter_context import (
    build_chapter_context,
    format_chapter_context_for_prompt,
    context_to_stats,
    apply_context_exclusions,
    ChapterContext,
    ContextStats,
    CharacterInfo,
    CharacterEventInfo,
    HiddenThreadInfo,
    WorldEntryInfo,
    ConfirmedMemoryInfo,
    PreviousChapterEndingInfo,
    StoryArcInfo,
    OutlineInfo,
)


def test_context_exclusion_filters_prompt_but_preserves_preview_item():
    context = ChapterContext(
        characters=[CharacterInfo(id="c1", name="程旋", role_type="protagonist", profile="侦探", faction="")],
        character_events=[CharacterEventInfo(
            id="e1", character_id="c1", character_name="程旋", chapter_sequence_number=2,
            event_summary="发现线索", appearance_type="appeared", state_change="起疑", importance=4,
        )],
        previous_chapter_ending=PreviousChapterEndingInfo("ch1", 1, "第一章", "门后传来脚步声"),
    )
    excluded = ["character:c1", "previous_chapter_ending"]
    summary = context.context_summary(excluded_context_keys=excluded)
    assert summary["excluded_context_keys"] == excluded
    assert summary["characters"][0]["key"] == "character:c1"
    assert summary["characters"][0]["excluded"] is True

    filtered = apply_context_exclusions(context, excluded)
    prompt = format_chapter_context_for_prompt(filtered)
    assert "程旋" not in prompt
    assert "发现线索" not in prompt
    assert "门后传来脚步声" not in prompt
    assert context.characters[0].name == "程旋"  # 原上下文未被修改

_AUTH_CACHE: dict[str, dict[str, str]] = {}


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    """确保 test_smoke 的 setup_db fixture 在当前文件中生效"""
    yield


def _auth(username: str = "ctxusr"):
    """获取 Authorization headers，避免污染 test_smoke 的 _auth_headers 共享缓存"""
    if username not in _AUTH_CACHE:
        client.post("/api/auth/register", json={"username": username, "password": "test"})
        resp = client.post("/api/auth/login", json={"username": username, "password": "test"})
        assert resp.status_code == 200, f"login failed: {resp.text}"
        token = resp.json()["access_token"]
        _AUTH_CACHE[username] = {"Authorization": f"Bearer {token}"}
    return _AUTH_CACHE[username]


def _create_project(title="章节上下文测试"):
    """通过 API 创建项目，返回 project_id"""
    resp = client.post("/api/projects", json={"title": title}, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_chapter(project_id, title, seq, content=""):
    """通过 API 创建章节（含可选正文内容）"""
    resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": title, "sequence_number": seq},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    chapter_id = resp.json()["id"]
    if content:
        # ChapterCreate 没有 content 字段，创建后用 PATCH 设置
        resp2 = client.patch(
            f"/api/projects/{project_id}/chapters/{seq}",
            json={"content": content},
            headers=_auth(),
        )
        assert resp2.status_code == 200, resp2.text
    return chapter_id


def _finalize_chapter(project_id, seq, content=None):
    """通过最后人工审核定稿接口固定章节版本。"""
    payload = {} if content is None else {"content": content}
    resp = client.post(
        f"/api/projects/{project_id}/chapters/{seq}/finalize",
        json=payload,
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_character(project_id, name, role_type="supporting", profile=""):
    """通过 API 创建角色"""
    resp = client.post(
        f"/api/projects/{project_id}/characters",
        json={"name": name, "role_type": role_type, "profile": profile},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_character_event(project_id, character_id, chapter_seq, event_summary, importance=3):
    """通过 API 创建/更新角色事件"""
    import math
    resp = client.put(
        f"/api/projects/{project_id}/characters/{character_id}/chapter-events/{chapter_seq}",
        json={
            "event_summary": event_summary,
            "importance": importance,
            "appeared": True,
        },
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_outline(project_id, seq, title, summary="", turning_point=""):
    """通过 API 创建大纲"""
    resp = client.post(
        f"/api/projects/{project_id}/outlines",
        json={
            "sequence_number": seq,
            "title": title,
            "summary": summary,
        },
        headers=_auth(),
    )
    # PATCH 设置 turning_point
    oid = resp.json()["id"]
    client.patch(
        f"/api/projects/{project_id}/outlines/{oid}",
        json={"turning_point": turning_point},
        headers=_auth(),
    )
    return oid


def _create_world_entry(project_id, title, category="general", scope_type="global", content=""):
    """通过 API 创建设定"""
    resp = client.post(
        f"/api/projects/{project_id}/world-entries",
        json={
            "title": title,
            "category": category,
            "scope_type": scope_type,
            "content": content,
        },
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_hidden_thread(project_id, name, description="", chapter_nums=None):
    """通过 API 创建暗线"""
    resp = client.post(
        f"/api/projects/{project_id}/hidden-threads",
        json={
            "name": name,
            "description": description,
            "chapter_nums": chapter_nums or [],
        },
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_source(project_id, title, source_type="upload", content="", always_inject=False):
    """通过 API 创建资料库条目"""
    resp = client.post(
        f"/api/projects/{project_id}/knowledge/sources",
        json={
            "title": title,
            "source_type": source_type,
            "content": content,
            "always_inject": always_inject,
        },
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ── tests ──


class TestChapterContextService:
    """build_chapter_context 集成测试"""

    def test_build_chapter_context_loads_character_events(self):
        """验证章节上下文能加载本章角色事件和相关角色"""
        pid = _create_project("事件加载测试")

        # 创建章节
        _create_chapter(pid, "第一章", 1, "这是第一章的内容。")

        # 创建角色和事件
        char_id = _create_character(pid, "主角", "protagonist", "勇敢的冒险者")
        char2_id = _create_character(pid, "配角", "supporting", "忠诚的伙伴")
        _create_character_event(pid, char_id, 1, "主角登场击败敌人", importance=5)
        _create_character_event(pid, char2_id, 1, "配角提供关键道具", importance=3)

        # 创建大纲
        _create_outline(pid, 1, "序章大纲", "主角登场", "初次遇敌")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())

        # 验证
        assert len(ctx.character_events) == 2
        assert ctx.stats.events == 2
        assert len(ctx.characters) == 2
        assert ctx.stats.characters == 2

        # 验证角色名反查
        names = {e.character_name for e in ctx.character_events}
        assert "主角" in names
        assert "配角" in names

        # 验证事件内容
        summaries = {e.event_summary for e in ctx.character_events}
        assert "主角登场击败敌人" in summaries
        assert "配角提供关键道具" in summaries

        # 验证本章大纲
        assert ctx.outline is not None
        assert ctx.outline.title == "序章大纲"
        assert ctx.outline.summary == "主角登场"
        assert ctx.outline.turning_point == "初次遇敌"

        # 验证当前章节
        assert ctx.chapter is not None
        assert ctx.chapter.title == "第一章"
        assert "第一章的内容" in ctx.chapter.content_snippet

    def test_context_summary_uses_frontend_preview_shape(self):
        """L-3 摘要保持统一 item 形状，并标记显式选择和来源。"""
        ctx = ChapterContext(
            outline=OutlineInfo("outline-1", 2, "第二章", "进入城镇", "发现线索"),
            selected_outlines=[OutlineInfo("outline-2", 1, "第一章", "前情", "离开村庄")],
            characters=[CharacterInfo("char-1", "主角", "protagonist", "谨慎", "旅团")],
            hidden_threads=[HiddenThreadInfo("thread-1", "黑匣子", "尚未揭晓", [2], status="ACTIVE")],
            world_entries=[WorldEntryInfo("world-1", "城镇规则", "规则", "global", "夜间禁行")],
            confirmed_memories=[ConfirmedMemoryInfo("memory-1", "event", "主角已受伤", "左臂受伤", chapter_sequence_number=1)],
            previous_chapter_ending=PreviousChapterEndingInfo("chapter-1", 1, "第一章", "门在身后关上。"),
            story_arcs=[StoryArcInfo("arc-1", "ARC", "逃亡线", summary="离开追捕", arc_position="BUILDUP")],
        )

        summary = ctx.context_summary(
            selected_outline_ids=["outline-2"],
            selected_character_ids=["char-1"],
            selected_world_entry_ids=[],
            selected_hidden_thread_ids=[],
        )

        assert summary["previous_chapter_ending"] == "门在身后关上。"
        assert {item["source_type"] for item in summary["outlines"]} == {"current_outline", "selected_outline"}
        assert next(item for item in summary["outlines"] if item["id"] == "outline-2")["selected"] is True
        assert summary["characters"][0] == {
            "key": "character:char-1",
            "id": "char-1",
            "label": "主角",
            "detail": "谨慎；旅团",
            "source_type": "character",
            "selected": True,
            "chapter_sequence_number": None,
            "excluded": False,
        }
        assert summary["hidden_threads"][0]["source_type"] == "hidden_thread"
        assert summary["world_entries"][0]["source_type"] == "world_entry"
        assert summary["confirmed_memories"][0]["chapter_sequence_number"] == 1
        assert summary["story_arcs"][0]["source_type"] == "story_arc:arc"

    def test_empty_chapter_does_not_crash(self):
        """无角色事件、无大纲、无暗线时不报错，返回空结构"""
        pid = _create_project("空章节测试")
        _create_chapter(pid, "空章节", 5)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 5)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())

        assert ctx.chapter is not None
        assert ctx.chapter.title == "空章节"
        assert ctx.outline is None
        assert ctx.character_events == []
        assert ctx.characters == []
        assert ctx.hidden_threads == []
        assert ctx.world_entries == []
        assert ctx.fanfic_rules == []
        assert ctx.retrieved_sources == []
        assert ctx.stats.characters == 0
        assert ctx.stats.events == 0
        assert ctx.stats.hidden_threads == 0
        assert ctx.stats.world_entries == 0
        assert ctx.stats.total == 0

    def test_selected_ids_append(self):
        """selected IDs 精确加载不替代自动聚合，而是追加"""
        pid = _create_project("selected IDs 测试")
        _create_chapter(pid, "第二章", 2)

        # 本章自动加载的角色
        auto_char_id = _create_character(pid, "本章角色", "protagonist")
        _create_character_event(pid, auto_char_id, 2, "本章事件")

        # 用户选中的角色（不同章节的角色）
        selected_char_id = _create_character(pid, "其他章角色", "supporting")

        # 创建暗线
        _create_hidden_thread(pid, "暗线A", "描述A", [2, 3])

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(
                    session,
                    pid,
                    2,
                    selected_character_ids=[selected_char_id],
                )
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())

        # 自动加载的角色
        assert len(ctx.characters) >= 2, f"expected >= 2 characters, got {len(ctx.characters)}"
        auto_names = {c.name for c in ctx.characters}
        assert "本章角色" in auto_names
        assert "其他章角色" in auto_names  # selected 追加

        # 自动加载的事件
        assert len(ctx.character_events) == 1

        # 暗线
        assert len(ctx.hidden_threads) == 1
        assert ctx.hidden_threads[0].name == "暗线A"

    def test_hidden_thread_filtering(self):
        """暗线只加载 chapter_nums 包含当前章节的"""
        pid = _create_project("暗线过滤测试")
        _create_chapter(pid, "第一章", 1)

        _create_hidden_thread(pid, "暗线A", "涉及第1章", [1, 3])
        _create_hidden_thread(pid, "暗线B", "涉及第2章", [2])

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())

        assert ctx.stats.hidden_threads == 1
        assert ctx.hidden_threads[0].name == "暗线A"

    def test_world_entries_global_and_chapter(self):
        """设定加载：全局 + 章节 scope_type 都加载"""
        pid = _create_project("设定加载测试")
        _create_chapter(pid, "第一章", 1)

        _create_world_entry(pid, "全局设定A", "geography", "global", "这是全局设定")
        _create_world_entry(pid, "章节设定A", "character", "chapter", "这是章节设定")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())

        assert ctx.stats.world_entries >= 2
        global_titles = {w.title for w in ctx.world_entries if w.scope_type == "global"}
        assert "全局设定A" in global_titles
        chapter_titles = {w.title for w in ctx.world_entries if w.scope_type == "chapter"}
        assert "章节设定A" in chapter_titles

    def test_previous_chapters_ordered(self):
        """前文只加载最近 3 章已定稿内容，按序号升序排列"""
        pid = _create_project("前文排序测试")
        _create_chapter(pid, "第一章", 1, "第1章内容")
        _create_chapter(pid, "第二章", 2, "第2章内容")
        _create_chapter(pid, "第三章", 3, "第3章内容")
        _create_chapter(pid, "第四章", 4, "第4章内容（当前章）")
        _finalize_chapter(pid, 1)
        _finalize_chapter(pid, 2)
        _finalize_chapter(pid, 3)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 4)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())

        # 前文应为第 1, 2, 3 章（第 4 章是当前章，不包含）
        assert len(ctx.previous_chapters) == 3
        seqs = [pc.sequence_number for pc in ctx.previous_chapters]
        assert seqs == [1, 2, 3]  # 升序
        assert ctx.previous_chapters[0].title == "第一章"

    def test_only_finalized_snapshot_is_used_as_previous_context(self):
        """草稿不能污染下一章；定稿后读取固定版本，二次编辑会解除定稿。"""
        pid = _create_project("定稿上下文测试")
        _create_chapter(pid, "第一章", 1, "第一版结尾：门后传来脚步声。")
        _create_chapter(pid, "第二章", 2)

        async def _build():
            async with test_session_factory() as session:
                return await build_chapter_context(session, pid, 2)

        before_final = asyncio.new_event_loop().run_until_complete(_build())
        assert before_final.previous_chapters == []
        assert before_final.previous_chapter_ending is None

        finalized = _finalize_chapter(pid, 1)
        assert finalized["status"] == "final"
        assert finalized["final_version_id"]
        finalized_again = _finalize_chapter(pid, 1)
        assert finalized_again["final_version_id"] == finalized["final_version_id"]
        versions = client.get(
            f"/api/projects/{pid}/chapters/1/versions",
            headers=_auth(),
        )
        assert versions.status_code == 200, versions.text
        assert len([version for version in versions.json() if version["source"] == "finalize"]) == 1

        after_final = asyncio.new_event_loop().run_until_complete(_build())
        assert [item.sequence_number for item in after_final.previous_chapters] == [1]
        assert after_final.previous_chapter_ending is not None
        assert after_final.previous_chapter_ending.ending_text == "第一版结尾：门后传来脚步声。"

        # 再次编辑即解除定稿，下一章不再使用未经确认的新文本。
        resp = client.patch(
            f"/api/projects/{pid}/chapters/1",
            json={"content": "未重新确认的改稿。"},
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "draft"
        assert resp.json()["final_version_id"] is None

        after_edit = asyncio.new_event_loop().run_until_complete(_build())
        assert after_edit.previous_chapters == []
        assert after_edit.previous_chapter_ending is None

    def test_selected_outlines_do_not_override_current_outline(self):
        """用户额外选中的其他章大纲只作为参考，不覆盖本章大纲"""
        pid = _create_project("参考大纲不覆盖测试")
        _create_chapter(pid, "第一章", 1)
        _create_chapter(pid, "第二章", 2)

        current_outline_id = _create_outline(pid, 1, "第一章大纲", "第一章概要", "第一章转折")
        other_outline_id = _create_outline(pid, 2, "第二章大纲", "第二章概要", "第二章转折")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(
                    session,
                    pid,
                    1,
                    selected_outline_ids=[current_outline_id, other_outline_id],
                )
                return ctx, format_chapter_context_for_prompt(ctx)

        ctx, text = asyncio.new_event_loop().run_until_complete(_run())

        assert ctx.outline is not None
        assert ctx.outline.title == "第一章大纲"
        # 跨章节大纲兜底过滤：第二章大纲不应进入第一章的生成上下文
        assert ctx.selected_outlines == []
        assert "## 本章大纲\n第1章 第一章大纲" in text
        assert "## 参考大纲" not in text
        assert "第二章大纲" not in text

    def test_cross_chapter_outlines_filtered_for_current_chapter(self):
        """生成第 2 章时，即便请求带了第 1 章大纲 id，也不应注入第 1 章作为参考大纲"""
        pid = _create_project("跨章大纲过滤测试")
        _create_chapter(pid, "第一章", 1)
        _create_chapter(pid, "第二章", 2)

        ch1_outline_id = _create_outline(pid, 1, "穿越到全职法师小说的世界", "第一章概要", "第一章转折")
        ch2_outline_id = _create_outline(pid, 2, "天澜魔法高中", "第二章概要", "第二章转折")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(
                    session,
                    pid,
                    2,
                    selected_outline_ids=[ch1_outline_id, ch2_outline_id],
                )
                return ctx, format_chapter_context_for_prompt(ctx)

        ctx, text = asyncio.new_event_loop().run_until_complete(_run())

        # 本章大纲是第 2 章
        assert ctx.outline is not None
        assert ctx.outline.sequence_number == 2
        assert ctx.outline.title == "天澜魔法高中"
        # selected_outlines 不应包含第 1 章
        assert all(o.sequence_number == 2 for o in ctx.selected_outlines)
        assert not any(o.sequence_number == 1 for o in ctx.selected_outlines)
        # prompt 不应出现第 1 章作为参考大纲
        assert "## 参考大纲" not in text
        assert "穿越到全职法师小说的世界" not in text
        assert "## 本章大纲\n第2章 天澜魔法高中" in text

    def test_project_sources_default_off(self):
        """资料库默认不进入 prompt，避免本章生成被资料库内容污染。"""
        pid = _create_project("同人规则测试")
        _create_chapter(pid, "第一章", 1, "第一章正文")
        _create_outline(pid, 1, "第一章概要", "第一章概要", "第一章转折")
        char_id = _create_character(pid, "主角", "protagonist")
        _create_character_event(pid, char_id, 1, "主角登场")

        _create_source(
            pid,
            title="平台同人规则",
            source_type="fanfic_rule",
            content="不可让主角突然性格崩坏。",
        )
        _create_source(
            pid,
            title="第一章资料",
            source_type="upload",
            content="这份资料会被章节标题命中。",
            always_inject=True,
        )

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1, user_query="主角")
                return ctx, context_to_stats(ctx), format_chapter_context_for_prompt(ctx)

        ctx, stats, text = asyncio.new_event_loop().run_until_complete(_run())

        assert ctx.fanfic_rules == []
        assert ctx.retrieved_sources == []
        assert stats["stats"]["fanfic_rules"] == 0
        assert stats["stats"]["sources"] == 0
        assert "## 同人规则" not in text
        assert "## 检索资料" not in text
        assert "平台同人规则" not in text
        assert "第一章资料" not in text

    def test_project_sources_opt_in_split_into_fanfic_rules_and_retrieved_sources(self):
        """显式开启资料库后，同人规则进入 fanfic_rules，其他资料继续走检索资料。"""
        pid = _create_project("同人规则开启测试")
        _create_chapter(pid, "第一章", 1, "第一章正文")
        _create_outline(pid, 1, "第一章概要", "第一章概要", "第一章转折")
        char_id = _create_character(pid, "主角", "protagonist")
        _create_character_event(pid, char_id, 1, "主角登场")

        _create_source(
            pid,
            title="平台同人规则",
            source_type="fanfic_rule",
            content="不可让主角突然性格崩坏。",
        )
        _create_source(
            pid,
            title="第一章资料",
            source_type="upload",
            content="这份资料会被章节标题命中。",
            always_inject=True,
        )

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(
                    session,
                    pid,
                    1,
                    user_query="主角",
                    include_knowledge_sources=True,
                )
                return ctx, context_to_stats(ctx), format_chapter_context_for_prompt(ctx)

        ctx, stats, text = asyncio.new_event_loop().run_until_complete(_run())

        assert [r.title for r in ctx.fanfic_rules] == ["平台同人规则"]
        assert any(s.title == "第一章资料" for s in ctx.retrieved_sources)
        assert stats["stats"]["fanfic_rules"] == 1
        assert stats["stats"]["sources"] == 1
        assert "## 同人规则" in text
        assert "## 检索资料" in text
        assert "平台同人规则" in text
        assert "第一章资料" in text


class TestFormatChapterContext:
    """format_chapter_context_for_prompt 测试"""

    def test_full_pipeline_excludes_local_article_material_but_keeps_chapter_materials(self):
        """完整章节生成只传本章资料和前章结尾，不把本地旧文/资料库/记忆送入 Prompt。"""
        pid = _create_project("完整生成上下文隔离")
        _create_chapter(
            pid,
            "第一章",
            1,
            "PREVIOUS_FULL_ARTICLE_MUST_NOT_ENTER_PROMPT。" + "A" * 600 + "上章定稿结尾锚点。",
        )
        _finalize_chapter(pid, 1)
        _create_chapter(pid, "第二章", 2, "CURRENT_DRAFT_MUST_NOT_ENTER_PROMPT")
        _create_outline(pid, 2, "第二章大纲", "按既定大纲生成完整第二章", "本章转折")
        char_id = _create_character(pid, "本章主角", "protagonist", "严格遵循人物设定")
        _create_character_event(pid, char_id, 2, "执行本章事件", importance=5)
        _create_world_entry(pid, "本章设定", "rule", "global", "不可违反的世界规则")
        _create_source(
            pid,
            title="SHOULD_NOT_INJECT_FANFIC_RULE",
            source_type="fanfic_rule",
            content="FANFIC_RULE_LOCAL_ARTICLE_MUST_NOT_ENTER_PROMPT",
            always_inject=True,
        )
        _create_source(
            pid,
            title="SHOULD_NOT_INJECT_SOURCE",
            source_type="upload",
            content="SOURCE_LOCAL_ARTICLE_MUST_NOT_ENTER_PROMPT",
            always_inject=True,
        )

        async def _run():
            from models.writing_memory_staging import WritingMemoryStaging
            from models.harness_enums import MemoryStagingStatus, MemoryType

            async with test_session_factory() as session:
                session.add(WritingMemoryStaging(
                    project_id=pid,
                    memory_type=MemoryType.PLOT_FACT,
                    title="SHOULD_NOT_INJECT_CONFIRMED_MEMORY",
                    payload={"content": "MEMORY_LOCAL_ARTICLE_MUST_NOT_ENTER_PROMPT"},
                    evidence="MEMORY_EVIDENCE_MUST_NOT_ENTER_PROMPT",
                    status=MemoryStagingStatus.CONFIRMED,
                    chapter_sequence_number=1,
                ))
                await session.commit()

            async with test_session_factory() as session:
                ctx = await build_chapter_context(
                    session,
                    pid,
                    2,
                    intent="full_pipeline",
                    user_query="本章主角",
                    include_knowledge_sources=True,
                )
                return ctx, format_chapter_context_for_prompt(ctx)

        ctx, text = asyncio.new_event_loop().run_until_complete(_run())

        assert ctx.chapter is not None
        assert ctx.chapter.content_snippet == ""
        assert ctx.previous_chapters == []
        assert ctx.fanfic_rules == []
        assert ctx.retrieved_sources == []
        assert ctx.confirmed_memories == []
        assert "CURRENT_DRAFT_MUST_NOT_ENTER_PROMPT" not in text
        assert "PREVIOUS_FULL_ARTICLE_MUST_NOT_ENTER_PROMPT" not in text
        assert "SOURCE_LOCAL_ARTICLE_MUST_NOT_ENTER_PROMPT" not in text
        assert "FANFIC_RULE_LOCAL_ARTICLE_MUST_NOT_ENTER_PROMPT" not in text
        assert "MEMORY_LOCAL_ARTICLE_MUST_NOT_ENTER_PROMPT" not in text
        assert "MEMORY_EVIDENCE_MUST_NOT_ENTER_PROMPT" not in text
        assert "SHOULD_NOT_INJECT" not in text
        assert "已有正文片段" not in text
        assert "## 前文摘要" not in text
        assert "## 同人规则" not in text
        assert "## 检索资料" not in text
        assert "## 已确认记忆" not in text
        assert "## 本章大纲" in text
        assert "按既定大纲生成完整第二章" in text
        assert "## 本章角色" in text
        assert "## 本章角色事件" in text
        assert "## 相关设定" in text
        assert "## 上章定稿结尾锚点" in text
        assert "上章定稿结尾锚点。" in text

    def test_continue_keeps_current_draft_snippet(self):
        """续写模式仍需读取当前正文，避免 full_pipeline 修复误伤续写。"""
        pid = _create_project("续写上下文保留")
        _create_chapter(pid, "第一章", 1, "CONTINUE_FROM_THIS_DRAFT")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1, intent="continue")
                return format_chapter_context_for_prompt(ctx)

        text = asyncio.new_event_loop().run_until_complete(_run())

        assert "已有正文片段" in text
        assert "CONTINUE_FROM_THIS_DRAFT" in text

    def test_format_includes_all_sections(self):
        """格式化输出包含所有 section"""
        pid = _create_project("格式化测试")
        _create_chapter(pid, "第一章", 1, "测试内容")

        char_id = _create_character(pid, "主角", "protagonist", "冒险者")
        _create_character_event(pid, char_id, 1, "击败怪物", importance=5)

        _create_outline(pid, 1, "大纲", "概要", "转折点")
        _create_hidden_thread(pid, "暗线A", "隐藏线索", [1])
        _create_world_entry(pid, "全局设定A", "geography", "global", "世界设定内容")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return format_chapter_context_for_prompt(ctx)

        text = asyncio.new_event_loop().run_until_complete(_run())

        assert "## 当前章节" in text
        assert "## 本章大纲" in text
        assert "## 明线推进" in text
        assert "## 本章角色" in text
        assert "## 本章角色事件" in text
        assert "## 暗线" in text
        assert "## 相关设定" in text
        assert "击败怪物" in text
        assert "主角" in text

    def test_format_empty_context(self):
        """空 context 不崩溃"""
        ctx = ChapterContext()
        text = format_chapter_context_for_prompt(ctx)
        assert text == "(暂无章节上下文)"


class TestContextStats:
    """context_to_stats 测试"""

    def test_context_to_stats_returns_structured(self):
        pid = _create_project("stats 测试")
        _create_chapter(pid, "第一章", 1)

        char_id = _create_character(pid, "主角", "protagonist")
        _create_character_event(pid, char_id, 1, "事件A")
        _create_outline(pid, 1, "大纲", "概要内容", "明线内容")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return context_to_stats(ctx)

        stats = asyncio.new_event_loop().run_until_complete(_run())

        assert "stats" in stats
        assert stats["stats"]["characters"] == 1
        assert stats["stats"]["events"] == 1
        assert "chapter_goal" in stats
        assert stats["chapter_goal"]["outline"] == "概要内容"
        assert stats["chapter_goal"]["light_line"] == "明线内容"

    def test_context_to_stats_empty(self):
        ctx = ChapterContext()
        stats = context_to_stats(ctx)
        assert stats["stats"]["characters"] == 0
        assert stats["stats"]["events"] == 0
        assert stats["chapter_goal"]["outline"] == ""
        assert stats["chapter_goal"]["light_line"] == ""


class TestOpeningAnchor:
    """K-1: opening_anchor 自动提取测试"""

    def test_previous_chapter_ending_uses_tail_text(self):
        """上章结尾锚点应取上一章正文末尾，不是开头。"""
        pid = _create_project("上章结尾锚点测试")
        _create_chapter(pid, "第一章", 1, "A" * 800)  # 800 字，取后 500
        _create_chapter(pid, "第二章", 2)
        _finalize_chapter(pid, 1)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 2)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert ctx.previous_chapter_ending is not None
        assert ctx.previous_chapter_ending.sequence_number == 1
        assert ctx.previous_chapter_ending.title == "第一章"
        # 应取末尾 500 字，不是开头
        assert len(ctx.previous_chapter_ending.ending_text) == 500
        assert ctx.previous_chapter_ending.ending_text.startswith("A" * 300)  # 800 - 500 = 300 offset

        prompt = format_chapter_context_for_prompt(ctx)
        assert "## 上章定稿结尾锚点" in prompt
        assert "第一章" in prompt

    def test_previous_chapter_ending_omitted_when_no_previous_content(self):
        """第一章没有上一章，不应出现上章结尾锚点。"""
        pid = _create_project("无上章锚点测试")
        _create_chapter(pid, "第一章", 1, "正文")

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert ctx.previous_chapter_ending is None

        prompt = format_chapter_context_for_prompt(ctx)
        assert "## 上章结尾锚点" not in prompt

    def test_previous_chapter_ending_omitted_when_no_previous_content_body(self):
        """上一章正文为空时，不应出现上章结尾锚点。"""
        pid = _create_project("上章空正文锚点测试")
        _create_chapter(pid, "第一章", 1, "")  # 空正文
        _create_chapter(pid, "第二章", 2)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 2)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert ctx.previous_chapter_ending is None

    def test_previous_chapter_ending_falls_back_to_latest_previous_chapter(self):
        """第 1 章无正文、第 2 章缺失时，应回退到最近有正文的前一章。"""
        pid = _create_project("回退锚点测试")
        _create_chapter(pid, "第1章", 1, "")      # 空正文
        _create_chapter(pid, "第3章", 3, "前文正文内容ABC")  # 第 2 章缺失
        _create_chapter(pid, "第4章", 4)
        _finalize_chapter(pid, 3)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 4)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        # 应回退到第 3 章（最近有正文的前一章）
        assert ctx.previous_chapter_ending is not None
        assert ctx.previous_chapter_ending.sequence_number == 3
        assert ctx.previous_chapter_ending.title == "第3章"
        assert ctx.previous_chapter_ending.ending_text == "前文正文内容ABC"

        prompt = format_chapter_context_for_prompt(ctx)
        assert "## 上章定稿结尾锚点" in prompt
        assert "第3章" in prompt


def _create_story_arc(project_id, *, arc_type="VOLUME", name="第一卷", summary="",
                      goal="", main_conflict="", start_chapter=None, end_chapter=None,
                      order_index=0):
    """通过 API 创建长线结构"""
    payload = {"arc_type": arc_type, "name": name, "order_index": order_index}
    if summary:
        payload["summary"] = summary
    if goal:
        payload["goal"] = goal
    if main_conflict:
        payload["main_conflict"] = main_conflict
    if start_chapter is not None:
        payload["start_chapter"] = start_chapter
    if end_chapter is not None:
        payload["end_chapter"] = end_chapter
    resp = client.post(
        f"/api/projects/{project_id}/story-arcs",
        json=payload,
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestStoryArc:
    """K-2: 长线结构 Context Builder 注入测试"""

    def test_arc_in_range_injected(self):
        """arc 覆盖当前章节时，应注入到 context。"""
        pid = _create_project("长线结构测试")
        _create_chapter(pid, "第三章", 3)
        _create_story_arc(pid, arc_type="VOLUME", name="第一卷：觉醒", goal="主角觉醒",
                          main_conflict="自我认知", start_chapter=1, end_chapter=5)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 3)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert len(ctx.story_arcs) == 1
        arc = ctx.story_arcs[0]
        assert arc.arc_type == "VOLUME"
        assert arc.name == "第一卷：觉醒"
        assert arc.goal == "主角觉醒"
        assert arc.main_conflict == "自我认知"
        assert arc.start_chapter == 1
        assert arc.end_chapter == 5

        prompt = format_chapter_context_for_prompt(ctx)
        assert "## 当前长线结构" in prompt
        assert "[VOLUME]" in prompt
        assert "第一卷：觉醒" in prompt
        assert "主角觉醒" in prompt

    def test_arc_out_of_range_not_injected(self):
        """arc 范围不覆盖当前章节时，不应注入。"""
        pid = _create_project("范围外测试")
        _create_chapter(pid, "第三章", 3)
        _create_story_arc(pid, name="第二卷", start_chapter=6, end_chapter=10)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 3)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert len(ctx.story_arcs) == 0

        prompt = format_chapter_context_for_prompt(ctx)
        assert "## 当前长线结构" not in prompt

    def test_arc_via_outline_story_arc_id(self):
        """通过 outline.story_arc_id 关联的 arc 应注入，且 arc_position 正确。"""
        pid = _create_project("大纲关联arc测试")
        _create_chapter(pid, "第一章", 1)
        arc_id = _create_story_arc(pid, arc_type="ARC", name="觉醒仪式",
                                    start_chapter=1, end_chapter=3)

        # 创建大纲并关联 arc
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={
                "sequence_number": 1,
                "title": "第一章",
                "story_arc_id": arc_id,
                "arc_position": "BUILDUP",
            },
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert len(ctx.story_arcs) == 1
        arc = ctx.story_arcs[0]
        assert arc.name == "觉醒仪式"
        assert arc.arc_position == "BUILDUP"

    def test_multiple_arcs_ordered(self):
        """多个 arc 覆盖同一章节时，按 order_index 排序。"""
        pid = _create_project("多arc排序测试")
        _create_chapter(pid, "第三章", 3)
        _create_story_arc(pid, arc_type="VOLUME", name="第二卷", order_index=2,
                          start_chapter=1, end_chapter=10)
        _create_story_arc(pid, arc_type="ARC", name="觉醒仪式", order_index=1,
                          start_chapter=1, end_chapter=5)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 3)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert len(ctx.story_arcs) == 2
        assert ctx.story_arcs[0].name == "觉醒仪式"
        assert ctx.story_arcs[1].name == "第二卷"

    def test_no_arcs_no_section(self):
        """无 arc 时不应注入 section。"""
        pid = _create_project("无arc测试")
        _create_chapter(pid, "第一章", 1)

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        assert len(ctx.story_arcs) == 0

        prompt = format_chapter_context_for_prompt(ctx)
        assert "## 当前长线结构" not in prompt

    def test_arc_without_range_not_injected_unless_outline_linked(self):
        """arc 无 start/end_chapter 时，不通过范围注入，但 outline 关联仍可注入。"""
        pid = _create_project("无范围arc测试")
        _create_chapter(pid, "第一章", 1)
        arc_id = _create_story_arc(pid, name="无范围arc")  # 无 start/end_chapter

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx = asyncio.new_event_loop().run_until_complete(_run())
        # 无范围且无 outline 关联 → 不注入
        assert len(ctx.story_arcs) == 0

        # 创建大纲关联
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={"sequence_number": 1, "title": "第一章", "story_arc_id": arc_id},
            headers=_auth(),
        )
        assert resp.status_code == 200

        async def _run2():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return ctx

        ctx2 = asyncio.new_event_loop().run_until_complete(_run2())
        assert len(ctx2.story_arcs) == 1
        assert ctx2.story_arcs[0].name == "无范围arc"


class TestOutlinePacing:
    """K-4: outline pacing 字段测试"""

    def test_create_outline_with_pacing_fields(self):
        """POST /outlines 创建时 pacing 字段持久化"""
        pid = _create_project("pacing测试")
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={
                "sequence_number": 1,
                "title": "第一章",
                "pacing": "BUILDUP",
                "tension_level": 3,
                "target_scene_count": 4,
            },
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        outline = resp.json()
        assert outline["pacing"] == "BUILDUP"
        assert outline["tension_level"] == 3
        assert outline["target_scene_count"] == 4

    def test_outline_pacing_in_prompt(self):
        """format_chapter_context_for_prompt 包含 pacing 信息"""
        pid = _create_project("pacing prompt测试")
        _create_chapter(pid, "第一章", 1)

        client.post(
            f"/api/projects/{pid}/outlines",
            json={
                "sequence_number": 1,
                "title": "第一章",
                "pacing": "CLIMAX",
                "tension_level": 5,
                "target_scene_count": 3,
            },
            headers=_auth(),
        )

        async def _run():
            async with test_session_factory() as session:
                ctx = await build_chapter_context(session, pid, 1)
                return format_chapter_context_for_prompt(ctx)

        prompt = asyncio.new_event_loop().run_until_complete(_run())
        assert "节奏：CLIMAX" in prompt
        assert "张力等级：5/5" in prompt
        assert "目标场景数：3" in prompt

    def test_get_character_arc(self):
        """GET /characters/{id}/arc 返回聚合数据"""
        pid = _create_project("arc测试")
        _create_chapter(pid, "第一章", 1)
        char_id = _create_character(pid, "主角", "protagonist")
        _create_character_event(pid, char_id, 1, "觉醒", importance=5)

        headers = _auth()
        resp = client.get(f"/api/projects/{pid}/characters/{char_id}/arc", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["character_name"] == "主角"
        assert len(data["items"]) >= 1
        assert data["items"][0]["source_type"] == "CharacterEvent"

    def test_get_character_arc_to_chapter_filter(self):
        """GET /characters/{id}/arc?to_chapter=N 过滤未来章节"""
        pid = _create_project("arc filter测试")
        _create_chapter(pid, "第一章", 1)
        _create_chapter(pid, "第五章", 5)
        char_id = _create_character(pid, "配角", "supporting")
        _create_character_event(pid, char_id, 1, "出场", importance=3)
        _create_character_event(pid, char_id, 5, "退场", importance=3)

        headers = _auth()
        resp = client.get(f"/api/projects/{pid}/characters/{char_id}/arc?to_chapter=1", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["chapter_sequence_number"] == 1
