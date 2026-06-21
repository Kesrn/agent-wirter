"""Chapter review notes API tests."""

import asyncio

from sqlalchemy import func, select

from models import ChapterReviewNote
from models.base import Base
from test_smoke import client, _auth_headers, setup_db, test_session_factory  # noqa: F401


def _create_project_with_chapter(username: str = "review_notes_user"):
    headers = _auth_headers(username, "reviewpass")
    project_resp = client.post("/api/projects", json={"title": "审阅备注项目"}, headers=headers)
    assert project_resp.status_code == 200, project_resp.text
    project_id = project_resp.json()["id"]

    chapter_resp = client.post(
        f"/api/projects/{project_id}/chapters",
        json={"title": "第一章", "sequence_number": 1},
        headers=headers,
    )
    assert chapter_resp.status_code == 200, chapter_resp.text
    return project_id, chapter_resp.json(), headers


def test_chapter_review_notes_create_list_patch_and_delete():
    project_id, chapter, headers = _create_project_with_chapter("review_notes_crud")

    create_resp = client.post(
        f"/api/projects/{project_id}/chapters/1/review-notes",
        json={
            "source_type": "agent",
            "severity": "warning",
            "content": "这里的人物动机需要补一笔。",
            "metadata": {"expert": "consistency_checker"},
        },
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    note = create_resp.json()
    assert note["project_id"] == project_id
    assert note["chapter_id"] == chapter["id"]
    assert note["chapter_sequence_number"] == 1
    assert note["source_type"] == "agent"
    assert note["severity"] == "warning"
    assert note["resolved"] is False
    assert note["metadata"] == {"expert": "consistency_checker"}

    list_resp = client.get(f"/api/projects/{project_id}/chapters/1/review-notes", headers=headers)
    assert list_resp.status_code == 200
    assert [item["id"] for item in list_resp.json()] == [note["id"]]

    patch_resp = client.patch(
        f"/api/projects/{project_id}/chapters/1/review-notes/{note['id']}",
        json={"resolved": True},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["resolved"] is True
    assert patch_resp.json()["content"] == "这里的人物动机需要补一笔。"

    unresolved_resp = client.get(
        f"/api/projects/{project_id}/chapters/1/review-notes?resolved=false",
        headers=headers,
    )
    assert unresolved_resp.status_code == 200
    assert unresolved_resp.json() == []

    delete_resp = client.delete(
        f"/api/projects/{project_id}/chapters/1/review-notes/{note['id']}",
        headers=headers,
    )
    assert delete_resp.status_code == 204
    assert client.get(f"/api/projects/{project_id}/chapters/1/review-notes", headers=headers).json() == []


def test_chapter_review_note_invalid_enum_rejected():
    project_id, _, headers = _create_project_with_chapter("review_notes_enum")

    resp = client.post(
        f"/api/projects/{project_id}/chapters/1/review-notes",
        json={"source_type": "bad-source", "severity": "warning", "content": "来源格式非法"},
        headers=headers,
    )
    assert resp.status_code == 422

    resp2 = client.patch(
        f"/api/projects/{project_id}/chapters/1/review-notes/00000000-0000-0000-0000-000000000000",
        json={"severity": "critical"},
        headers=headers,
    )
    assert resp2.status_code == 404

    resp3 = client.post(
        f"/api/projects/{project_id}/chapters/1/review-notes",
        json={"source_type": "critic_output", "severity": "error", "content": "严重度非法"},
        headers=headers,
    )
    assert resp3.status_code == 422


def test_chapter_review_note_owner_isolation_and_missing_chapter():
    project_id, _, headers = _create_project_with_chapter("review_notes_owner_a")
    other_headers = _auth_headers("review_notes_owner_b", "reviewpass")

    forbidden_resp = client.get(f"/api/projects/{project_id}/chapters/1/review-notes", headers=other_headers)
    assert forbidden_resp.status_code == 404

    missing_resp = client.post(
        f"/api/projects/{project_id}/chapters/99/review-notes",
        json={"content": "不存在的章节"},
        headers=headers,
    )
    assert missing_resp.status_code == 404
    assert missing_resp.json()["detail"] == "章节不存在"


def test_chapter_review_notes_registered_and_project_delete_cleans_rows():
    assert "chapter_review_notes" in Base.metadata.tables
    project_id, _, headers = _create_project_with_chapter("review_notes_delete")

    create_resp = client.post(
        f"/api/projects/{project_id}/chapters/1/review-notes",
        json={"content": "删除项目时应被清理"},
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text

    async def _count_notes():
        async with test_session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(ChapterReviewNote).where(ChapterReviewNote.project_id == project_id)
            )
            return result.scalar_one()

    assert asyncio.run(_count_notes()) == 1

    delete_resp = client.delete(f"/api/projects/{project_id}", headers=headers)
    assert delete_resp.status_code == 204
    assert asyncio.run(_count_notes()) == 0
