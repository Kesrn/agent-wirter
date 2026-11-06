"""人工改动统计 API 的字段、过滤和所有权测试。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from test_smoke import client, test_session_factory, setup_db, _auth_headers  # noqa: F401
from models import Chapter, ChapterVersion, GenerationRecord, Project, User


@pytest.fixture(autouse=True)
def _ensure_db(setup_db):
    yield


def _project(username: str, title: str):
    headers = _auth_headers(username, "testpass")
    response = client.post("/api/projects", json={"title": title}, headers=headers)
    assert response.status_code == 200, response.text
    project_id = response.json()["id"]
    chapters = []
    for sequence in (1, 2):
        created = client.post(
            f"/api/projects/{project_id}/chapters",
            json={"title": f"第{sequence}章", "sequence_number": sequence},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        chapters.append(created.json()["id"])
    return headers, project_id, chapters


def _insert_records(project_id: str, chapter_ids: list[str], ratios: list[float], applied_dates=None):
    dates = applied_dates or [datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(days=i) for i in range(len(ratios))]

    async def run():
        async with test_session_factory() as db:
            project = await db.get(Project, uuid.UUID(project_id))
            records = []
            next_version_number: dict[str, int] = {}
            original = "甲乙丙丁戊己庚辛壬癸"
            for index, ratio in enumerate(ratios):
                chapter_id = chapter_ids[index % len(chapter_ids)]
                version_number = next_version_number.get(chapter_id, 1)
                final = original
                if ratio == 0.1:
                    final = "子" + original[1:]
                elif ratio == 0.2:
                    final = "子乙丑" + original[3:]
                anchor = ChapterVersion(
                    chapter_id=uuid.UUID(chapter_id), project_id=project.id,
                    version_number=version_number, content=original, word_count=len(original), source="ai_approve",
                )
                db.add(anchor)
                await db.flush()
                human = ChapterVersion(
                    chapter_id=uuid.UUID(chapter_id), project_id=project.id,
                    version_number=version_number + 1, content=final, word_count=len(final), source="manual",
                )
                db.add(human)
                await db.flush()
                record = GenerationRecord(
                    project_id=project.id,
                    chapter_id=uuid.UUID(chapter_id),
                    mode="full_pipeline",
                    content=original,
                    word_count=len(original),
                    status="applied",
                    accepted_version_id=anchor.id,
                    updated_at=dates[index],
                )
                db.add(record)
                records.append((record, anchor, human))
                next_version_number[chapter_id] = version_number + 2
            await db.commit()
            return [(str(record.id), str(anchor.id), str(human.id)) for record, anchor, human in records]

    return asyncio.run(run())


def test_list_detail_original_and_refresh_return_metrics():
    headers, project_id, chapter_ids = _project("edit_metrics_owner1", "指标列表详情")
    record_ids = _insert_records(project_id, chapter_ids, [0.1])
    record_id, anchor_id, first_human_id = record_ids[0]

    listed = client.get(f"/api/projects/{project_id}/chapters/1/generations", headers=headers)
    assert listed.status_code == 200, listed.text
    item = next(value for value in listed.json() if value["id"] == record_id)
    assert item["human_edit_metrics"]["human_version_id"] == first_human_id
    assert "content" not in item

    detail = client.get(f"/api/projects/{project_id}/generations/{record_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["human_edit_metrics"]["anchor_version_id"] == anchor_id

    original = client.get(f"/api/generation-records/{record_id}/original", headers=headers)
    assert original.status_code == 200, original.text
    assert original.json() == {"id": record_id, "content": "甲乙丙丁戊己庚辛壬癸"}

    async def add_latest_manual():
        async with test_session_factory() as db:
            latest = ChapterVersion(
                chapter_id=uuid.UUID(chapter_ids[0]),
                version_number=3,
                content="子乙丑丁戊己庚辛壬癸",
                word_count=10,
                source="finalize",
            )
            db.add(latest)
            await db.commit()
            return str(latest.id)

    latest_human_id = asyncio.run(add_latest_manual())
    refreshed = client.post(f"/api/generation-records/{record_id}/refresh-edit-metrics", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["human_edit_metrics"]["human_version_id"] == latest_human_id


def test_stats_summary_pagination_chapter_and_time_filters():
    headers, project_id, chapter_ids = _project("edit_metrics_owner2", "统计过滤")
    dates = [
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 2, tzinfo=timezone.utc),
        datetime(2026, 11, 3, tzinfo=timezone.utc),
    ]
    records = _insert_records(project_id, chapter_ids, [0.1, 0.2, 0.0], dates)

    response = client.get(f"/api/projects/{project_id}/edit-stats", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"] == {
        "count": 3,
        "edited_count": 2,
        "mean_edit_ratio": 0.1,
        "median_edit_ratio": 0.1,
        "p90_edit_ratio": 0.2,
        "total_original_chars": 30,
        "total_changed_chars": 3,
    }
    assert [item["generation_record_id"] for item in data["items"]] == [records[2][0], records[1][0], records[0][0]]

    page = client.get(f"/api/projects/{project_id}/edit-stats?limit=1&offset=1", headers=headers)
    assert page.status_code == 200
    assert page.json()["summary"]["count"] == 3
    assert len(page.json()["items"]) == 1
    assert page.json()["items"][0]["generation_record_id"] == records[1][0]

    chapter_filter = client.get(
        f"/api/projects/{project_id}/edit-stats?chapter_id={chapter_ids[0]}", headers=headers,
    )
    assert chapter_filter.status_code == 200
    assert chapter_filter.json()["summary"]["count"] == 2

    time_filter = client.get(
        f"/api/projects/{project_id}/edit-stats?since=2026-11-02T00:00:00Z&until=2026-11-02T23:59:59Z",
        headers=headers,
    )
    assert time_filter.status_code == 200
    assert time_filter.json()["summary"]["count"] == 1
    assert time_filter.json()["items"][0]["generation_record_id"] == records[1][0]


def test_owner_checks_empty_stats_and_null_anchor_fallback():
    owner_headers, project_id, chapter_ids = _project("edit_metrics_owner3", "空统计")
    other_headers = _auth_headers("edit_metrics_other3", "testpass")
    empty = client.get(f"/api/projects/{project_id}/edit-stats", headers=owner_headers)
    assert empty.status_code == 200
    assert empty.json()["summary"]["count"] == 0
    assert empty.json()["summary"]["mean_edit_ratio"] is None
    assert empty.json()["summary"]["median_edit_ratio"] is None
    assert empty.json()["summary"]["p90_edit_ratio"] is None

    no_anchor_record = _insert_records(project_id, chapter_ids, [0.0])[0][0]

    async def remove_anchor(record_id):
        async with test_session_factory() as db:
            record = await db.get(GenerationRecord, uuid.UUID(record_id))
            record.accepted_version_id = None
            record.human_edit_metrics = None
            await db.commit()

    asyncio.run(remove_anchor(no_anchor_record))
    original = client.get(f"/api/generation-records/{no_anchor_record}/original", headers=owner_headers)
    assert original.status_code == 200
    assert original.json()["content"] == "甲乙丙丁戊己庚辛壬癸"
    refreshed = client.post(
        f"/api/generation-records/{no_anchor_record}/refresh-edit-metrics", headers=owner_headers,
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["human_edit_metrics"] is None

    assert client.get(f"/api/projects/{project_id}/edit-stats", headers=other_headers).status_code == 404
    assert client.get(
        f"/api/generation-records/{no_anchor_record}/original", headers=other_headers,
    ).status_code == 404
    assert client.post(
        f"/api/generation-records/{no_anchor_record}/refresh-edit-metrics", headers=other_headers,
    ).status_code == 404


def test_no_accepted_anchor_stays_null_and_list_models_include_field():
    headers, project_id, chapter_ids = _project("edit_metrics_owner4", "无锚点")
    record_id = _insert_records(project_id, chapter_ids, [0.0])[0][0]

    async def clear_anchor():
        async with test_session_factory() as db:
            record = await db.get(GenerationRecord, uuid.UUID(record_id))
            record.accepted_version_id = None
            await db.commit()

    asyncio.run(clear_anchor())
    response = client.get(f"/api/projects/{project_id}/chapters/1/generations", headers=headers)
    assert response.status_code == 200
    row = next(value for value in response.json() if value["id"] == record_id)
    assert row["human_edit_metrics"] is None


def test_stats_items_approximate_flag():
    headers, project_id, chapter_ids = _project("edit_metrics_owner5", "近似标记测试")

    async def insert_approx_records():
        async with test_session_factory() as db:
            project = await db.get(Project, uuid.UUID(project_id))
            r1 = GenerationRecord(
                project_id=project.id,
                chapter_id=uuid.UUID(chapter_ids[0]),
                mode="full_pipeline",
                content="测试1",
                word_count=3,
                status="applied",
                accepted_version_id=uuid.uuid4(),
                updated_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
                human_edit_metrics={
                    "original_chars": 10,
                    "final_chars": 12,
                    "changed_chars": 2,
                    "edit_ratio": 0.2,
                    "similarity": 0.8,
                    "edited": True,
                    "approximate": True,
                },
            )
            r2 = GenerationRecord(
                project_id=project.id,
                chapter_id=uuid.UUID(chapter_ids[1]),
                mode="full_pipeline",
                content="测试2",
                word_count=3,
                status="applied",
                accepted_version_id=uuid.uuid4(),
                updated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                human_edit_metrics={
                    "original_chars": 10,
                    "final_chars": 10,
                    "changed_chars": 0,
                    "edit_ratio": 0.0,
                    "similarity": 1.0,
                    "edited": False,
                    # simulating legacy cached record without "approximate"
                },
            )
            db.add_all([r1, r2])
            await db.commit()
            return str(r1.id), str(r2.id)

    r1_id, r2_id = asyncio.run(insert_approx_records())
    response = client.get(f"/api/projects/{project_id}/edit-stats", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    by_id = {item["generation_record_id"]: item for item in items}
    assert by_id[r1_id]["approximate"] is True
    assert by_id[r2_id]["approximate"] is False

