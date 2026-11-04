"""章节恢复历史版本语义测试（P1）。

评审要求：
1. 恢复历史版本后，章节状态改回 draft（等待用户再次定稿）；
2. final_version_id 保留原定稿快照（可信索引继续指向原定稿版本，不被恢复操作破坏）；
3. 未再次定稿前，检索索引不变（恢复不触发索引操作）；
4. 再次定稿后，final_version_id 更新为新版本并触发索引同步。
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from test_smoke import client, test_session_factory, setup_db, _auth_headers  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


# ── 辅助 ──


def _create_project_chapter(username: str):
    headers = _auth_headers(username, username)
    resp = client.post("/api/projects", json={"title": f"{username} 测试", "mode": "novel"}, headers=headers)
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]
    resp = client.post(f"/api/projects/{pid}/chapters",
                       json={"title": "第一章", "sequence_number": 1}, headers=headers)
    assert resp.status_code == 200, resp.text
    return headers, pid


def _query_chapter(pid, seq=1):
    async def _q():
        from models.chapter import Chapter
        async with test_session_factory() as s:
            res = await s.execute(select(Chapter).where(
                Chapter.project_id == uuid.UUID(pid),
                Chapter.sequence_number == seq,
            ))
            return res.scalar_one_or_none()
    return asyncio.new_event_loop().run_until_complete(_q())


def _query_versions(chapter_id):
    async def _q():
        from models.chapter_version import ChapterVersion
        async with test_session_factory() as s:
            chapter_uuid = chapter_id if isinstance(chapter_id, uuid.UUID) else uuid.UUID(chapter_id)
            res = await s.execute(
                select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_uuid)
                .order_by(ChapterVersion.version_number)
            )
            return list(res.scalars().all())
    return asyncio.new_event_loop().run_until_complete(_q())


def _setup_finalized_chapter(username: str, final_content: str = "第二版内容"):
    """草稿两版内容 → 定稿 → 返回 (headers, pid, 定稿版本 id, 第一版版本 id)。"""
    headers, pid = _create_project_chapter(username)
    r1 = client.patch(f"/api/projects/{pid}/chapters/1",
                      json={"content": "第一版内容"}, headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = client.patch(f"/api/projects/{pid}/chapters/1",
                      json={"content": final_content}, headers=headers)
    assert r2.status_code == 200, r2.text
    rf = client.post(f"/api/projects/{pid}/chapters/1/finalize",
                     json={"content": final_content}, headers=headers)
    assert rf.status_code == 200, rf.text
    assert rf.json()["status"] == "final"
    final_vid = rf.json()["final_version_id"]
    assert final_vid is not None

    chapter = _query_chapter(pid)
    assert chapter.status == "final"
    assert str(chapter.final_version_id) == str(final_vid)

    versions = _query_versions(chapter.id)
    v1 = next(v for v in versions if v.version_number == 1)
    assert v1.content == "第一版内容"
    return headers, pid, final_vid, v1.id


# ── 测试 1: 定稿 → 恢复历史版本 → status=draft、final_version_id 保留原定稿快照 ──


def test_restore_sets_draft_and_preserves_final_snapshot():
    headers, pid, final_vid, v1_id = _setup_finalized_chapter("rst_preserve")

    # 恢复历史版本 v1
    rr = client.post(f"/api/projects/{pid}/chapters/1/versions/{v1_id}/restore", headers=headers)
    assert rr.status_code == 200, rr.text
    data = rr.json()
    assert data["content"] == "第一版内容"
    # 恢复 ≠ 定稿：状态必须改回 draft
    assert data["status"] == "draft"
    # 可信快照保留：final_version_id 仍指向原定稿版本
    assert data["final_version_id"] == final_vid

    # 数据库层复核
    chapter_after = _query_chapter(pid)
    assert chapter_after.status == "draft"
    assert str(chapter_after.final_version_id) == str(final_vid)
    assert chapter_after.content == "第一版内容"

    # 恢复不覆盖历史：用旧版本内容创建一条 source=rollback 的新版本
    versions_after = _query_versions(chapter_after.id)
    latest = versions_after[-1]
    assert latest.source == "rollback"
    assert str(latest.rollback_from_version_id) == str(v1_id)
    assert latest.content == "第一版内容"


# ── 测试 2: 恢复后再定稿 → final_version_id 更新为新版本，且只有定稿触发索引 ──


def test_refinalize_updates_final_snapshot_and_triggers_index(monkeypatch):
    # 记录检索索引调用：恢复不得触发；再次定稿必须触发一次并指向新定稿版本
    import services.retrieval_indexer as ri_mod
    index_calls: list[tuple[str, str]] = []

    async def _spy(db, project_id, version_id):
        index_calls.append((str(project_id), str(version_id)))
        return None

    # finalize 接入点为 request_chapter_index（P0 拆分：SQLite 同步构建 / PG outbox）
    monkeypatch.setattr(ri_mod, "request_chapter_index", _spy)

    headers, pid, final_vid, v1_id = _setup_finalized_chapter("rst_refin")
    # 清掉 setup 阶段首次定稿触发的索引调用，只统计「恢复 / 再次定稿」阶段的触发
    index_calls.clear()

    # 恢复历史版本：不触发任何检索索引操作
    rr = client.post(f"/api/projects/{pid}/chapters/1/versions/{v1_id}/restore", headers=headers)
    assert rr.status_code == 200, rr.text
    assert rr.json()["status"] == "draft"
    assert rr.json()["final_version_id"] == final_vid
    assert index_calls == [], f"恢复操作不应触发检索索引: {index_calls}"

    # 再次定稿：final_version_id 更新为新版本，并触发索引同步
    rf2 = client.post(f"/api/projects/{pid}/chapters/1/finalize",
                      json={"content": "第一版内容（重新定稿）"}, headers=headers)
    assert rf2.status_code == 200, rf2.text
    data = rf2.json()
    assert data["status"] == "final"
    new_final_vid = data["final_version_id"]
    assert new_final_vid is not None
    assert str(new_final_vid) != str(final_vid), "再次定稿必须更新 final_version_id"

    # 索引只被再次定稿触发一次，且指向新定稿版本
    assert len(index_calls) == 1, f"期望恰好一次索引触发，实际 {index_calls}"
    assert index_calls[0][0] == pid
    assert index_calls[0][1] == str(new_final_vid)

    # 数据库层复核
    chapter_after = _query_chapter(pid)
    assert chapter_after.status == "final"
    assert str(chapter_after.final_version_id) == str(new_final_vid)
