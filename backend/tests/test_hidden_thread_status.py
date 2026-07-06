"""HiddenThread 伏笔状态追踪测试 — K-3"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from models.hidden_thread import HiddenThread, STATUS_PRIORITY
from services.chapter_context import HiddenThreadInfo, ChapterContext, format_chapter_context_for_prompt, ContextStats, build_chapter_context

from test_smoke import client, test_session_factory, setup_db  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


_AUTH_CACHE: dict[str, dict[str, str]] = {}


def _auth(username: str = "htusr"):
    if username not in _AUTH_CACHE:
        client.post("/api/auth/register", json={"username": username, "password": "testpass"})
        resp = client.post("/api/auth/login", json={"username": username, "password": "testpass"})
        assert resp.status_code == 200, f"login failed: {resp.text}"
        token = resp.json()["access_token"]
        _AUTH_CACHE[username] = {"Authorization": f"Bearer {token}"}
    return _AUTH_CACHE[username]


def _create_project(title="伏笔测试"):
    resp = client.post("/api/projects", json={"title": title}, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_chapter(project_id, title, seq):
    resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": title, "sequence_number": seq},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_hidden_thread(project_id, name, **kwargs):
    payload = {"name": name, **kwargs}
    resp = client.post(
        f"/api/projects/{project_id}/hidden-threads",
        json=payload,
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_outline(project_id, seq, title):
    resp = client.post(
        f"/api/projects/{project_id}/outlines",
        json={"sequence_number": seq, "title": title},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestStatusPriority:

    def test_status_priority_order(self):
        """状态优先级数值正确"""
        assert STATUS_PRIORITY["PLANNED"] == 0
        assert STATUS_PRIORITY["PLANTED"] == 1
        assert STATUS_PRIORITY["ACTIVE"] == 2
        assert STATUS_PRIORITY["REVEALED"] == 3
        assert STATUS_PRIORITY["RESOLVED"] == 4
        assert STATUS_PRIORITY["DROPPED"] == 99
        assert STATUS_PRIORITY["RESOLVED"] > STATUS_PRIORITY["PLANTED"]

    def test_planted_beats_planned(self):
        """PLANTED 优先级高于 PLANNED"""
        assert STATUS_PRIORITY.get("PLANTED", 0) > STATUS_PRIORITY.get("PLANNED", 0)

    def test_resolved_beats_planted(self):
        """RESOLVED 优先级高于 PLANTED"""
        assert STATUS_PRIORITY.get("RESOLVED", 0) > STATUS_PRIORITY.get("PLANTED", 0)


class TestHiddenThreadInfo:

    def test_hidden_thread_info_has_new_fields(self):
        """HiddenThreadInfo 包含 K-3 新增字段"""
        info = HiddenThreadInfo(
            id="test-id",
            name="测试暗线",
            description="描述",
            chapter_nums=[1, 2],
            status="PLANTED",
            planted_chapter=1,
            reveal_chapter=5,
            resolved_chapter=None,
        )
        assert info.status == "PLANTED"
        assert info.planted_chapter == 1
        assert info.reveal_chapter == 5
        assert info.resolved_chapter is None

    def test_hidden_thread_info_defaults(self):
        """HiddenThreadInfo 默认值"""
        info = HiddenThreadInfo(
            id="test", name="测试", description="", chapter_nums=[],
        )
        assert info.status == "PLANNED"
        assert info.planted_chapter is None


class TestMemoryCuratorForeshadowing:

    def test_foreshadowing_new_has_planted_delta(self):
        """memory_curator 将 foreshadowing_new 转为 FORESHADOWING + status_delta=PLANTED"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_new": [
                {"title": "主角身世之谜", "description": "暗示主角不是人类", "evidence": "他的手...", "thread_name": None},
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=3)
        foreshadow_facts = [f for f in facts if f["memory_type"] == "FORESHADOWING"]
        assert len(foreshadow_facts) == 1
        assert foreshadow_facts[0]["title"] == "主角身世之谜"
        assert foreshadow_facts[0]["payload"]["status_delta"] == "PLANTED"
        assert 3 in foreshadow_facts[0]["payload"]["chapter_nums"]

    def test_foreshadowing_resolved_uses_thread_name_first(self):
        """foreshadowing_resolved 优先使用 thread_name 匹配原伏笔"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_resolved": [
                {
                    "thread_name": "主角身世之谜",
                    "title": "回收：身世",
                    "description": "主角得知自己是外星人",
                    "evidence": "真相大白",
                },
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=5)
        foreshadow_facts = [f for f in facts if f["memory_type"] == "FORESHADOWING"]
        assert len(foreshadow_facts) == 1
        assert foreshadow_facts[0]["title"] == "主角身世之谜"
        assert foreshadow_facts[0]["payload"]["status_delta"] == "RESOLVED"

    def test_foreshadowing_resolved_falls_back_to_title(self):
        """foreshadowing_resolved 无 thread_name 时 fallback 到 title"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_resolved": [
                {"title": "信号来源", "description": "信号来自地心", "evidence": "源头"},
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=5)
        foreshadow_facts = [f for f in facts if f["memory_type"] == "FORESHADOWING"]
        assert len(foreshadow_facts) == 1
        assert foreshadow_facts[0]["title"] == "信号来源"

    def test_foreshadowing_resolved_not_plot_fact(self):
        """K-3: foreshadowing_resolved 不再写为 PLOT_FACT"""
        from agents.memory_curator import story_record_to_facts

        record = {
            "foreshadowing_resolved": [
                {"title": "测试伏笔", "description": "回收", "evidence": ""},
            ],
        }
        facts = story_record_to_facts(record, chapter_seq=5)
        plot_facts = [f for f in facts if f["memory_type"] == "PLOT_FACT"]
        assert len(plot_facts) == 0


class TestContextBuilderHiddenThreads:

    def test_dropped_thread_format_ok(self):
        """DROPPED 伏笔在 format 中正常渲染（过滤由 _load_hidden_threads 负责）"""
        ctx = ChapterContext()
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="1", name="废弃暗线", description="不应出现",
                chapter_nums=[1], status="DROPPED",
            )
        )
        prompt = format_chapter_context_for_prompt(ctx)
        # format 不负责过滤 — 只是验证不崩溃
        assert "## 暗线" in prompt
        assert "DROPPED" in prompt

    def test_prompt_includes_status_label(self):
        """prompt 中包含伏笔状态标签"""
        ctx = ChapterContext()
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="1", name="身世伏笔", description="主角身份存疑",
                chapter_nums=[1, 2], status="PLANTED", planted_chapter=1,
                reveal_chapter=5,
            )
        )
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="2", name="旧伏笔", description="已被揭示",
                chapter_nums=[3], status="RESOLVED", resolved_chapter=3,
            )
        )
        prompt = format_chapter_context_for_prompt(ctx)
        assert "PLANTED" in prompt
        assert "RESOLVED" in prompt
        assert "## 暗线" in prompt

    def test_resolved_shows_chapter(self):
        """RESOLVED 状态显示回收章节"""
        ctx = ChapterContext()
        ctx.hidden_threads.append(
            HiddenThreadInfo(
                id="1", name="回收线", description="已回收",
                chapter_nums=[5], status="RESOLVED", resolved_chapter=5,
            )
        )
        prompt = format_chapter_context_for_prompt(ctx)
        assert "RESOLVED" in prompt


class TestHiddenThreadDB:

    def test_create_with_k3_fields(self):
        """API 创建暗线时写入 K-3 新字段"""
        pid = _create_project()
        ht = _create_hidden_thread(
            pid, "测试暗线",
            status="PLANTED", thread_type="FORESHADOWING",
            planted_chapter=3, reveal_chapter=8, chapter_nums=[3],
        )
        assert ht["status"] == "PLANTED"
        assert ht["thread_type"] == "FORESHADOWING"
        assert ht["planted_chapter"] == 3
        assert ht["reveal_chapter"] == 8

    def test_confirm_merges_chapter_nums(self):
        """_confirm_hidden_thread 合并 chapter_nums 而非覆盖"""
        async def _run():
            async with test_session_factory() as session:
                from services.memory_staging_service import _confirm_hidden_thread
                from models.writing_memory_staging import WritingMemoryStaging
                from models.harness_enums import MemoryStagingStatus

                pid = _create_project()
                staging1 = WritingMemoryStaging(
                    project_id=pid, title="合并测试", memory_type="FORESHADOWING",
                    status=MemoryStagingStatus.GENERATED, payload={},
                )
                staging2 = WritingMemoryStaging(
                    project_id=pid, title="合并测试", memory_type="FORESHADOWING",
                    status=MemoryStagingStatus.GENERATED, payload={},
                )
                session.add_all([staging1, staging2])
                await session.flush()

                await _confirm_hidden_thread(session, pid, staging1, {"description": "第一次", "chapter_nums": [1, 2]}, 1)
                await session.commit()
                await _confirm_hidden_thread(session, pid, staging2, {"description": "第二次", "chapter_nums": [2, 3]}, 3)
                await session.commit()

                from models.hidden_thread import HiddenThread as HT
                result = await session.execute(
                    select(HT).where(HT.project_id == pid, HT.name == "合并测试")
                )
                thread = result.scalar_one_or_none()
                assert thread is not None
                assert sorted(thread.chapter_nums or []) == [1, 2, 3]

        asyncio.new_event_loop().run_until_complete(_run())

    def test_status_not_downgraded(self):
        """RESOLVED 不被后续 PLANTED 倒退"""
        async def _run():
            async with test_session_factory() as session:
                from services.memory_staging_service import _confirm_hidden_thread
                from models.writing_memory_staging import WritingMemoryStaging
                from models.harness_enums import MemoryStagingStatus

                pid = _create_project()
                staging_resolved = WritingMemoryStaging(
                    project_id=pid, title="不倒退", memory_type="FORESHADOWING",
                    status=MemoryStagingStatus.GENERATED, payload={},
                )
                staging_planted = WritingMemoryStaging(
                    project_id=pid, title="不倒退", memory_type="FORESHADOWING",
                    status=MemoryStagingStatus.GENERATED, payload={},
                )
                session.add_all([staging_resolved, staging_planted])
                await session.flush()

                # 先确认 RESOLVED
                await _confirm_hidden_thread(
                    session, pid, staging_resolved,
                    {"description": "回收", "status_delta": "RESOLVED", "chapter_nums": [5]}, 5,
                )
                await session.commit()

                # 再尝试确认 PLANTED（更低优先级）
                await _confirm_hidden_thread(
                    session, pid, staging_planted,
                    {"description": "埋设", "status_delta": "PLANTED", "chapter_nums": [1]}, 1,
                )
                await session.commit()

                from models.hidden_thread import HiddenThread as HT
                result = await session.execute(
                    select(HT).where(HT.project_id == pid, HT.name == "不倒退")
                )
                thread = result.scalar_one_or_none()
                assert thread is not None
                assert thread.status == "RESOLVED"

        asyncio.new_event_loop().run_until_complete(_run())

    def test_dropped_not_in_context(self):
        """DROPPED 状态的伏笔不注入 build_chapter_context"""
        async def _run():
            async with test_session_factory() as session:
                pid = _create_project()
                _create_chapter(pid, "第一章", 1)
                _create_outline(pid, 1, "第一章大纲")

                # 创建一个 DROPPED 伏笔，chapter_nums 包含第一章
                _create_hidden_thread(
                    pid, "废弃伏笔", status="DROPPED", chapter_nums=[1],
                )
                # 创建一个 PLANTED 伏笔
                _create_hidden_thread(
                    pid, "活跃伏笔", status="PLANTED", chapter_nums=[1],
                )

                ctx = await build_chapter_context(session, pid, 1)
                names = [t.name for t in ctx.hidden_threads]
                assert "活跃伏笔" in names
                assert "废弃伏笔" not in names

        asyncio.new_event_loop().run_until_complete(_run())
