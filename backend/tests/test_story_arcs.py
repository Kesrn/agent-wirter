"""StoryArc (长线结构) CRUD 测试 —— K-2a

复用 test_smoke 的测试基础架构。
"""
import pytest

from test_smoke import client, setup_db  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


_AUTH_CACHE: dict[str, dict[str, str]] = {}


def _auth(username: str = "arcusr"):
    if username not in _AUTH_CACHE:
        client.post("/api/auth/register", json={"username": username, "password": "testpass"})
        resp = client.post("/api/auth/login", json={"username": username, "password": "testpass"})
        assert resp.status_code == 200, f"login failed: {resp.text}"
        token = resp.json()["access_token"]
        _AUTH_CACHE[username] = {"Authorization": f"Bearer {token}"}
    return _AUTH_CACHE[username]


def _create_project(title="长线结构测试", headers=None):
    resp = client.post("/api/projects", json={"title": title}, headers=headers or _auth())
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_arc(project_id, *, arc_type="VOLUME", name="第一卷", summary="", goal="",
                main_conflict="", parent_arc_id=None, start_chapter=None, end_chapter=None,
                order_index=0, status="PLANNED", headers=None):
    payload = {
        "arc_type": arc_type,
        "name": name,
        "order_index": order_index,
        "status": status,
    }
    if summary:
        payload["summary"] = summary
    if goal:
        payload["goal"] = goal
    if main_conflict:
        payload["main_conflict"] = main_conflict
    if parent_arc_id:
        payload["parent_arc_id"] = parent_arc_id
    if start_chapter is not None:
        payload["start_chapter"] = start_chapter
    if end_chapter is not None:
        payload["end_chapter"] = end_chapter
    resp = client.post(
        f"/api/projects/{project_id}/story-arcs",
        json=payload,
        headers=headers or _auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_outline(project_id, seq, title, summary="", headers=None):
    resp = client.post(
        f"/api/projects/{project_id}/outlines",
        json={"sequence_number": seq, "title": title, "summary": summary},
        headers=headers or _auth(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ── CRUD ──────────────────────────────────────────────


class TestStoryArcCRUD:

    def test_create_and_list(self):
        pid = _create_project()
        arc = _create_arc(pid, arc_type="VOLUME", name="第一卷：觉醒", goal="主角觉醒",
                          main_conflict="自我认知", start_chapter=1, end_chapter=10)
        assert arc["arc_type"] == "VOLUME"
        assert arc["name"] == "第一卷：觉醒"
        assert arc["goal"] == "主角觉醒"
        assert arc["start_chapter"] == 1
        assert arc["end_chapter"] == 10
        assert arc["status"] == "PLANNED"

        resp = client.get(f"/api/projects/{pid}/story-arcs", headers=_auth())
        assert resp.status_code == 200
        arcs = resp.json()
        assert len(arcs) == 1
        assert arcs[0]["id"] == arc["id"]

    def test_list_ordered_by_order_index(self):
        pid = _create_project()
        _create_arc(pid, name="第二卷", order_index=2)
        _create_arc(pid, name="第一卷", order_index=1)
        resp = client.get(f"/api/projects/{pid}/story-arcs", headers=_auth())
        arcs = resp.json()
        assert arcs[0]["name"] == "第一卷"
        assert arcs[1]["name"] == "第二卷"

    def test_update(self):
        pid = _create_project()
        arc = _create_arc(pid, name="初始名", goal="")
        resp = client.patch(
            f"/api/projects/{pid}/story-arcs/{arc['id']}",
            json={"name": "更新名", "goal": "新目标", "status": "ACTIVE"},
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "更新名"
        assert resp.json()["goal"] == "新目标"
        assert resp.json()["status"] == "ACTIVE"

    def test_delete(self):
        pid = _create_project()
        arc = _create_arc(pid, name="待删除")
        resp = client.delete(f"/api/projects/{pid}/story-arcs/{arc['id']}", headers=_auth())
        assert resp.status_code == 204
        # 验证已删除
        resp = client.get(f"/api/projects/{pid}/story-arcs", headers=_auth())
        assert len(resp.json()) == 0

    def test_delete_not_found(self):
        pid = _create_project()
        import uuid
        fake_id = str(uuid.uuid4())
        resp = client.delete(f"/api/projects/{pid}/story-arcs/{fake_id}", headers=_auth())
        assert resp.status_code == 404


# ── 校验 ──────────────────────────────────────────────


class TestStoryArcValidation:

    def test_invalid_arc_type(self):
        pid = _create_project()
        resp = client.post(
            f"/api/projects/{pid}/story-arcs",
            json={"arc_type": "INVALID", "name": "测试"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_invalid_status(self):
        pid = _create_project()
        resp = client.post(
            f"/api/projects/{pid}/story-arcs",
            json={"arc_type": "VOLUME", "name": "测试", "status": "BOGUS"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_update_not_found(self):
        pid = _create_project()
        import uuid
        resp = client.patch(
            f"/api/projects/{pid}/story-arcs/{uuid.uuid4()}",
            json={"name": "不存在"},
            headers=_auth(),
        )
        assert resp.status_code == 404


# ── 层级 ──────────────────────────────────────────────


class TestStoryArcHierarchy:

    def test_parent_child(self):
        pid = _create_project()
        volume = _create_arc(pid, arc_type="VOLUME", name="第一卷", order_index=1)
        arc = _create_arc(
            pid, arc_type="ARC", name="觉醒仪式", parent_arc_id=volume["id"], order_index=2,
        )
        assert arc["parent_arc_id"] == volume["id"]

        resp = client.get(f"/api/projects/{pid}/story-arcs", headers=_auth())
        assert len(resp.json()) == 2

    def test_invalid_parent_returns_400(self):
        pid = _create_project()
        import uuid
        resp = client.post(
            f"/api/projects/{pid}/story-arcs",
            json={"arc_type": "ARC", "name": "孤儿", "parent_arc_id": str(uuid.uuid4())},
            headers=_auth(),
        )
        assert resp.status_code == 400
        assert "父级" in resp.json()["detail"]

    def test_delete_with_children_returns_400(self):
        pid = _create_project()
        volume = _create_arc(pid, arc_type="VOLUME", name="第一卷")
        _create_arc(pid, arc_type="ARC", name="子弧", parent_arc_id=volume["id"])
        resp = client.delete(f"/api/projects/{pid}/story-arcs/{volume['id']}", headers=_auth())
        assert resp.status_code == 400
        assert "子级" in resp.json()["detail"]

    def test_update_parent_to_self_returns_400(self):
        """更新 parent_arc_id 指向自身应返回 400"""
        pid = _create_project()
        arc = _create_arc(pid, arc_type="VOLUME", name="第一卷")
        resp = client.patch(
            f"/api/projects/{pid}/story-arcs/{arc['id']}",
            json={"parent_arc_id": arc["id"]},
            headers=_auth(),
        )
        assert resp.status_code == 400, resp.text
        assert "不能指向自身" in resp.text

    def test_update_parent_to_unknown_returns_400(self):
        """更新 parent_arc_id 到不存在的 arc 应返回 400"""
        pid = _create_project()
        arc = _create_arc(pid, arc_type="VOLUME", name="第一卷")
        fake_parent_id = "00000000-0000-0000-0000-000000000003"
        resp = client.patch(
            f"/api/projects/{pid}/story-arcs/{arc['id']}",
            json={"parent_arc_id": fake_parent_id},
            headers=_auth(),
        )
        assert resp.status_code == 400, resp.text
        assert "不存在或不属于该项目" in resp.text

    def test_update_parent_to_cross_project_returns_400(self):
        """更新 parent_arc_id 到其他项目的 arc 应返回 400"""
        pid1 = _create_project("项目1", _auth("user1"))
        pid2 = _create_project("项目2", _auth("user2"))
        arc_in_pid1 = _create_arc(pid1, name="项目1的卷", headers=_auth("user1"))
        arc_in_pid2 = _create_arc(pid2, name="项目2的卷", headers=_auth("user2"))

        # user2 尝试把 arc_in_pid2 的父级设为 arc_in_pid1
        resp = client.patch(
            f"/api/projects/{pid2}/story-arcs/{arc_in_pid2['id']}",
            json={"parent_arc_id": arc_in_pid1["id"]},
            headers=_auth("user2"),
        )
        assert resp.status_code == 400, resp.text
        assert "不存在或不属于该项目" in resp.text


# ── 跨用户隔离 ────────────────────────────────────────


class TestStoryArcCrossUser:

    def test_cross_user_forbidden(self):
        pid = _create_project()
        other_headers = _auth("arcother")
        # 其他用户访问 → 404（_verify_project_owner 对非 owner 返回 404）
        resp = client.get(f"/api/projects/{pid}/story-arcs", headers=other_headers)
        assert resp.status_code == 404

    def test_cross_user_create_forbidden(self):
        pid = _create_project()
        other_headers = _auth("arcother2")
        resp = client.post(
            f"/api/projects/{pid}/story-arcs",
            json={"arc_type": "VOLUME", "name": "越权"},
            headers=other_headers,
        )
        assert resp.status_code == 404


# ── Outline 关联 ─────────────────────────────────────


class TestOutlineStoryArcLink:

    def test_outline_create_with_story_arc_id(self):
        pid = _create_project()
        arc = _create_arc(pid, arc_type="VOLUME", name="第一卷")
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={
                "sequence_number": 1,
                "title": "第一章",
                "story_arc_id": arc["id"],
                "arc_position": "SETUP",
            },
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        outline = resp.json()
        assert outline["story_arc_id"] == arc["id"]
        assert outline["arc_position"] == "SETUP"

    def test_outline_update_story_arc_id(self):
        pid = _create_project()
        oid = _create_outline(pid, 1, "第一章")
        arc = _create_arc(pid, arc_type="VOLUME", name="第一卷")

        resp = client.patch(
            f"/api/projects/{pid}/outlines/{oid}",
            json={"story_arc_id": arc["id"], "arc_position": "CLIMAX"},
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["story_arc_id"] == arc["id"]
        assert resp.json()["arc_position"] == "CLIMAX"

    def test_outline_clear_story_arc_id(self):
        pid = _create_project()
        arc = _create_arc(pid, arc_type="VOLUME", name="第一卷")
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={"sequence_number": 1, "title": "第一章", "story_arc_id": arc["id"]},
            headers=_auth(),
        )
        oid = resp.json()["id"]
        # 清空
        resp = client.patch(
            f"/api/projects/{pid}/outlines/{oid}",
            json={"story_arc_id": None},
            headers=_auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["story_arc_id"] is None

    def test_outline_invalid_arc_position(self):
        pid = _create_project()
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={"sequence_number": 1, "title": "第一章", "arc_position": "BOGUS"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_outline_create_with_unknown_story_arc_id(self):
        """创建大纲时，story_arc_id 不存在应返回 400"""
        pid = _create_project()
        fake_arc_id = "00000000-0000-0000-0000-000000000001"
        resp = client.post(
            f"/api/projects/{pid}/outlines",
            json={
                "sequence_number": 1,
                "title": "第一章",
                "story_arc_id": fake_arc_id,
            },
            headers=_auth(),
        )
        assert resp.status_code == 400, resp.text
        assert "不存在或不属于该项目" in resp.text

    def test_outline_create_with_cross_project_story_arc_id(self):
        """创建大纲时，story_arc_id 属于其他项目应返回 400"""
        pid1 = _create_project("项目1", _auth("user1"))
        pid2 = _create_project("项目2", _auth("user2"))
        arc_in_pid1 = _create_arc(pid1, name="项目1的卷", headers=_auth("user1"))

        # user2 尝试在 pid2 的大纲中使用 pid1 的 arc
        resp = client.post(
            f"/api/projects/{pid2}/outlines",
            json={
                "sequence_number": 1,
                "title": "第一章",
                "story_arc_id": arc_in_pid1["id"],
            },
            headers=_auth("user2"),
        )
        assert resp.status_code == 400, resp.text
        assert "不存在或不属于该项目" in resp.text

    def test_outline_update_with_invalid_story_arc_id(self):
        """更新大纲时，story_arc_id 无效应返回 400"""
        pid = _create_project()
        oid = _create_outline(pid, 1, "第一章")
        fake_arc_id = "00000000-0000-0000-0000-000000000002"

        resp = client.patch(
            f"/api/projects/{pid}/outlines/{oid}",
            json={"story_arc_id": fake_arc_id},
            headers=_auth(),
        )
        assert resp.status_code == 400, resp.text
        assert "不存在或不属于该项目" in resp.text
