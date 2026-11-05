"""人工作动指标纯计算、版本链解析和缓存刷新测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from models import Base, Chapter, ChapterVersion, GenerationRecord, Project


@pytest.fixture
def db_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metrics.sqlite3'}", poolclass=NullPool)

    async def initialize():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(initialize())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    asyncio.run(engine.dispose())


def _compute(original, final, **kwargs):
    from services.edit_metrics_service import compute_edit_metrics

    return compute_edit_metrics(original, final, anchor_version_id="anchor-id", **kwargs)


def test_compute_unchanged_is_zero_and_not_edited():
    result = _compute("原稿 文字。", "原稿 文字。", human_version_id=None)
    assert result["edited"] is False
    assert result["original_chars"] == 5
    assert result["final_chars"] == 5
    assert result["changed_chars"] == 0
    assert result["changed_original_chars"] == 0
    assert result["inserted_chars"] == 0
    assert result["edit_ratio"] == 0


def test_compute_about_ten_percent_change_is_in_range():
    original = "".join(chr(0x4E00 + index) for index in range(1000))
    final = "".join(chr(0x6000 + index) for index in range(100)) + original[100:]
    result = _compute(original, final, human_version_id="human-id")
    assert 0.09 <= result["edit_ratio"] <= 0.11
    assert result["edited"] is True


def test_compute_full_rewrite_is_near_one_and_capped():
    result = _compute("甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉", human_version_id="human-id")
    assert result["edit_ratio"] == 1.0
    assert result["edit_ratio"] <= 1.0


def test_compute_append_counts_insertions_only():
    result = _compute("甲乙丙丁", "甲乙丙丁戊", human_version_id="human-id")
    assert result["inserted_chars"] == 1
    assert result["changed_original_chars"] == 0
    assert result["changed_chars"] == 1


def test_compute_whitespace_differences_do_not_count():
    result = _compute("甲 乙\n丙", "甲乙 丙", human_version_id=None)
    assert result["original_chars"] == result["final_chars"] == 3
    assert result["changed_chars"] == 0
    assert result["edit_ratio"] == 0


def test_compute_uses_paragraph_fast_path_for_thirty_thousand_chars():
    from services.edit_metrics_service import compute_edit_metrics
    import time

    paragraphs = [f"段落{i:03d}" + ("甲乙丙丁戊己庚辛壬癸" * 10) for i in range(300)]
    original = "\n".join(paragraphs)
    edited = list(paragraphs)
    replacement = "子丑寅卯辰巳午未申酉戌" * 10
    for index in range(0, 300, 10):
        edited[index] = edited[index][:5] + replacement
    final = "\n".join(edited)
    started = time.perf_counter()
    result = compute_edit_metrics(original, final, anchor_version_id="a", human_version_id="h")
    elapsed = time.perf_counter() - started
    assert len(original.replace("\n", "")) >= 30_000
    assert 0 < result["edit_ratio"] < 0.11
    assert elapsed < 2.0


def _version(chapter_id, number, content, source="manual", *, run_id=None, rollback_from=None):
    return ChapterVersion(
        chapter_id=chapter_id,
        version_number=number,
        content=content,
        word_count=len(content),
        source=source,
        run_id=run_id,
        rollback_from_version_id=rollback_from,
    )


async def _chapter(db, title="测试章节"):
    project = Project(title="人工改动统计", owner_id="owner")
    db.add(project)
    await db.flush()
    chapter = Chapter(project_id=project.id, title=title, sequence_number=1, content="")
    db.add(chapter)
    await db.flush()
    return project, chapter


def _record(project, chapter, anchor, *, status="applied", metrics=None):
    return GenerationRecord(
        project_id=project.id,
        chapter_id=chapter.id,
        mode="full_pipeline",
        content=anchor.content or "",
        status=status,
        accepted_version_id=anchor.id if anchor else None,
        human_edit_metrics=metrics,
    )


def test_resolve_no_manual_edit_returns_none(db_factory):
    from services.edit_metrics_service import resolve_human_version

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            anchor = _version(chapter.id, 1, "AI原稿", "ai_approve", run_id="11111111-1111-1111-1111-111111111111")
            db.add(anchor)
            await db.flush()
            record = _record(project, chapter, anchor)
            db.add(record)
            await db.flush()
            resolved_anchor, human = await resolve_human_version(db, record)
            assert resolved_anchor.id == anchor.id
            assert human is None

    asyncio.run(run())


def test_resolve_multiple_manual_edits_uses_last_and_finalize_is_manual(db_factory):
    from services.edit_metrics_service import resolve_human_version

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            versions = [
                _version(chapter.id, 1, "AI原稿", "ai_enhance"),
                _version(chapter.id, 2, "第一次手改", "manual"),
                _version(chapter.id, 3, "第二次定稿", "finalize"),
            ]
            db.add_all(versions)
            await db.flush()
            record = _record(project, chapter, versions[0])
            db.add(record)
            await db.flush()
            anchor, human = await resolve_human_version(db, record)
            assert anchor.id == versions[0].id
            assert human.id == versions[2].id

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["ai", "rollback", "next_anchor"])
def test_resolve_stops_before_boundary_and_later_manual_edits(db_factory, boundary):
    from services.edit_metrics_service import resolve_human_version

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            anchor = _version(chapter.id, 1, "AI原稿", "ai_approve", run_id="11111111-1111-1111-1111-111111111111")
            first_manual = _version(chapter.id, 2, "手改一", "manual")
            if boundary == "ai":
                stop = _version(chapter.id, 3, "新AI稿", "ai_approve", run_id="22222222-2222-2222-2222-222222222222")
            elif boundary == "rollback":
                stop = _version(chapter.id, 3, "回滚内容", "manual", rollback_from="33333333-3333-3333-3333-333333333333")
            else:
                stop = _version(chapter.id, 3, "第二次采用", "manual")
            last_manual = _version(chapter.id, 4, "边界后的手改", "manual")
            db.add_all([anchor, first_manual, stop, last_manual])
            await db.flush()
            record = _record(project, chapter, anchor)
            db.add(record)
            await db.flush()
            if boundary == "next_anchor":
                next_record = _record(project, chapter, stop)
                db.add(next_record)
                await db.flush()
            _, human = await resolve_human_version(db, record)
            assert human.id == first_manual.id

    asyncio.run(run())


def test_resolve_null_anchor_and_conversation_ai_enhance_anchor(db_factory):
    from services.edit_metrics_service import resolve_human_version

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            conversation_anchor = _version(chapter.id, 1, "对话采用稿", "ai_enhance")
            manual = _version(chapter.id, 2, "对话稿手改", "manual")
            db.add_all([conversation_anchor, manual])
            await db.flush()
            record = _record(project, chapter, conversation_anchor)
            db.add(record)
            no_anchor = GenerationRecord(
                project_id=project.id, chapter_id=chapter.id, mode="full_pipeline",
                content="旧记录", status="applied", accepted_version_id=None,
            )
            db.add(no_anchor)
            await db.flush()
            anchor, human = await resolve_human_version(db, record)
            assert anchor.id == conversation_anchor.id
            assert human.id == manual.id
            assert await resolve_human_version(db, no_anchor) == (None, None)

    asyncio.run(run())


def test_ensure_fresh_metrics_refreshes_human_version_and_preserves_applied_at(db_factory):
    from services.edit_metrics_service import ensure_fresh_metrics

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            anchor = _version(chapter.id, 1, "原稿甲乙丙", "ai_approve", run_id="11111111-1111-1111-1111-111111111111")
            old_human = _version(chapter.id, 2, "旧手改甲乙丁", "manual")
            new_human = _version(chapter.id, 3, "新手改甲戊丁", "finalize")
            db.add_all([anchor, old_human, new_human])
            await db.flush()
            applied_at = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
            cached = {"method": "difflib-v1", "anchor_version_id": str(anchor.id), "human_version_id": str(old_human.id)}
            record = _record(project, chapter, anchor, metrics=cached)
            record.updated_at = applied_at
            db.add(record)
            await db.flush()
            await db.refresh(record)

            await ensure_fresh_metrics(db, [record])

            assert record.human_edit_metrics["human_version_id"] == str(new_human.id)
            assert record.human_edit_metrics["edited"] is True
            await db.commit()
            await db.refresh(record)
            assert record.updated_at.replace(tzinfo=timezone.utc) == applied_at

    asyncio.run(run())


def test_ensure_fresh_metrics_respects_max_recompute(db_factory):
    from services.edit_metrics_service import ensure_fresh_metrics

    async def run():
        async with db_factory() as db:
            records = []
            for index in range(2):
                project, chapter = await _chapter(db, title=f"章节{index}")
                anchor = _version(chapter.id, 1, f"AI稿{index}", "ai_approve", run_id=f"11111111-1111-1111-1111-11111111111{index}")
                manual = _version(chapter.id, 2, f"手改稿{index}", "manual")
                db.add_all([anchor, manual])
                await db.flush()
                record = _record(project, chapter, anchor)
                db.add(record)
                records.append(record)
            await db.flush()
            await ensure_fresh_metrics(db, records, max_recompute=1)
            assert records[0].human_edit_metrics is not None
            assert records[1].human_edit_metrics is None

    asyncio.run(run())


def test_ensure_fresh_metrics_computation_failure_keeps_old_cache(db_factory, monkeypatch):
    from services import edit_metrics_service

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            anchor = _version(chapter.id, 1, "原稿", "ai_approve", run_id="11111111-1111-1111-1111-111111111111")
            manual = _version(chapter.id, 2, "手改", "manual")
            db.add_all([anchor, manual])
            await db.flush()
            old = {"method": "difflib-v1", "human_version_id": "stale"}
            record = _record(project, chapter, anchor, metrics=old)
            db.add(record)
            await db.flush()

            def explode(*args, **kwargs):
                raise RuntimeError("simulated diff failure")

            monkeypatch.setattr(edit_metrics_service, "compute_edit_metrics", explode)
            await edit_metrics_service.ensure_fresh_metrics(db, [record])
            assert record.human_edit_metrics == old

    asyncio.run(run())


def test_pruning_keeps_accepted_anchor_and_metrics_still_compute(db_factory):
    from services.edit_metrics_service import ensure_fresh_metrics
    from services.version_service import MAX_VERSIONS_PER_CHAPTER, create_version

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            anchor = await create_version(db, chapter.id, "永不删除的AI锚点", source="ai_approve", run_id="11111111-1111-1111-1111-111111111111")
            record = _record(project, chapter, anchor)
            db.add(record)
            await db.flush()
            for index in range(MAX_VERSIONS_PER_CHAPTER + 2):
                await create_version(db, chapter.id, f"后续手改版本{index}", source="manual")
            retained = await db.get(ChapterVersion, anchor.id)
            assert retained is not None
            await ensure_fresh_metrics(db, [record])
            assert record.human_edit_metrics is not None
            assert record.human_edit_metrics["anchor_version_id"] == str(anchor.id)

    asyncio.run(run())


def test_mark_applied_updates_updated_at_for_applied_timestamp(db_factory):
    from services.generation_record_service import mark_applied_for_run

    async def run():
        async with db_factory() as db:
            project, chapter = await _chapter(db)
            run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            anchor = _version(chapter.id, 1, "AI原稿", "ai_approve", run_id=run_id)
            db.add(anchor)
            await db.flush()
            old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
            record = GenerationRecord(
                project_id=project.id,
                chapter_id=chapter.id,
                mode="full_pipeline",
                content="AI原稿",
                word_count=3,
                status="candidate",
                run_id=run_id,
                updated_at=old_time,
            )
            db.add(record)
            await db.flush()
            assert await mark_applied_for_run(
                db, run_id=run_id, accepted_version_id=anchor.id, content="AI原稿",
            )
            await db.refresh(record)
            assert record.status == "applied"
            assert record.updated_at.replace(tzinfo=timezone.utc) > old_time

    asyncio.run(run())
