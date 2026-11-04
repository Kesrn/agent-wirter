"""角色删除清理测试

验证：
1. 删除角色会级联清理其章节事件与角色关系，不留下悬空引用
   （character_events / character_relations 的角色 id 是裸 GUID 列，无外键级联）
2. 历史遗留的孤儿事件（角色已删除）仍然可以直接删除
"""

import asyncio
import uuid

import pytest
from sqlalchemy import insert

from test_smoke import setup_db, test_engine  # noqa: F401
from test_smoke import client, _auth_headers


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


def _create_project(headers: dict) -> str:
    resp = client.post("/api/projects", json={"title": "事件清理测试项目", "mode": "novel"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_character(headers: dict, project_id: str, name: str) -> str:
    resp = client.post(f"/api/projects/{project_id}/characters", json={"name": name}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _upsert_event(headers: dict, project_id: str, character_id: str, seq: int = 1):
    resp = client.put(
        f"/api/projects/{project_id}/characters/{character_id}/chapter-events/{seq}",
        json={"appearance_type": "appeared", "event_summary": "测试事件", "importance": 3},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _insert_orphan_event(project_id: str, character_id: str, seq: int = 1):
    """直接写库模拟旧版本删除角色后遗留的悬空事件（角色 id 不存在）。"""
    from models.character_event import CharacterEvent

    async def _insert():
        async with test_engine.begin() as conn:
            await conn.execute(
                insert(CharacterEvent).values(
                    project_id=uuid.UUID(project_id),
                    character_id=uuid.UUID(character_id),
                    chapter_sequence_number=seq,
                    appearance_type="appeared",
                    appeared=True,
                    importance=3,
                )
            )

    asyncio.run(_insert())


class TestDeleteCharacterCascades:
    def test_deleting_character_removes_its_events_and_relations(self):
        headers = _auth_headers("cleanupuser")
        pid = _create_project(headers)
        char_a = _create_character(headers, pid, "角色甲")
        char_b = _create_character(headers, pid, "角色乙")
        _upsert_event(headers, pid, char_a, 1)
        _upsert_event(headers, pid, char_b, 1)
        resp = client.post(
            f"/api/projects/{pid}/character-relations",
            json={"source_character_id": char_a, "target_character_id": char_b, "description": "朋友"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        resp = client.delete(f"/api/projects/{pid}/characters/{char_a}", headers=headers)
        assert resp.status_code == 204

        events = client.get(f"/api/projects/{pid}/character-events", headers=headers).json()
        assert events, "角色乙的事件应保留"
        assert all(e["character_id"] != char_a for e in events), "角色甲的事件应被级联删除"
        assert any(e["character_id"] == char_b for e in events)

        relations = client.get(f"/api/projects/{pid}/character-relations", headers=headers).json()
        assert all(
            r["source_character_id"] != char_a and r["target_character_id"] != char_a
            for r in relations
        ), "涉及角色甲的关系应被级联删除"


class TestDeleteOrphanEvent:
    def test_orphan_event_can_be_deleted_when_character_is_gone(self):
        headers = _auth_headers("orphanuser")
        pid = _create_project(headers)
        ghost_character_id = str(uuid.uuid4())
        _insert_orphan_event(pid, ghost_character_id, 1)

        # 孤儿事件仍然可见（前端显示“未知角色”）
        events = client.get(f"/api/projects/{pid}/character-events", headers=headers).json()
        assert len(events) == 1
        assert events[0]["character_id"] == ghost_character_id

        # 删除不再要求角色存在
        resp = client.delete(
            f"/api/projects/{pid}/characters/{ghost_character_id}/chapter-events/1",
            headers=headers,
        )
        assert resp.status_code == 204

        events = client.get(f"/api/projects/{pid}/character-events", headers=headers).json()
        assert events == []
