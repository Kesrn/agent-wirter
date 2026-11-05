from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models import Base, Chapter, ChapterVersion, GenerationRecord, Project
from services.edit_examples import (
    EditPair,
    build_edit_pairs,
    load_edit_examples,
    render_edit_examples_block,
)


@pytest_asyncio.fixture
async def edit_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fewshot.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _add_edit_record(
    db,
    *,
    project,
    owner_id,
    edit_ratio=0.1,
    status="applied",
    edited=True,
    mode="full_pipeline",
    applied_at=None,
    missing_human=False,
    deleted_human=False,
    missing_anchor=False,
    original="这是足够长的旧段落内容，描述一段故事变化。",
    final="这是经过人工调整后的段落内容，细节更加清楚。",
):
    chapter = Chapter(project_id=project.id, title="测试章节", sequence_number=3)
    db.add(chapter)
    await db.flush()
    anchor = ChapterVersion(
        chapter_id=chapter.id,
        project_id=project.id,
        content=original,
        version_number=1,
        source="generation",
        run_id=str(uuid.uuid4()),
    )
    db.add(anchor)
    await db.flush()
    human = None
    if not missing_human:
        human = ChapterVersion(
            chapter_id=chapter.id,
            project_id=project.id,
            content=final,
            version_number=2,
            source="manual",
        )
        db.add(human)
        await db.flush()
    record = GenerationRecord(
        project_id=project.id,
        chapter_id=chapter.id,
        mode=mode,
        content="候选稿字段不作为人工示例正文来源。",
        status=status,
        accepted_version_id=(uuid.uuid4() if missing_anchor else anchor.id),
        human_edit_metrics={
            "method": "difflib-v1",
            "edited": edited,
            "anchor_version_id": str(anchor.id),
            "human_version_id": str(human.id) if human else None,
            "edit_ratio": edit_ratio,
        },
        updated_at=applied_at or datetime.now(timezone.utc),
    )
    db.add(record)
    await db.flush()
    if deleted_human and human is not None:
        await db.delete(human)
        await db.flush()
    return record, anchor, human


def test_build_edit_pairs_aligns_paragraph_insertions_replacements_and_deletions():
    original = "开头保持不变\n需要删掉的旧段落内容\n中间保持不变\n这是旧版本中需要整体改写的段落内容\n结尾保持不变"
    final = "开头保持不变\n中间保持不变\n新的写法在这里完整替换了原先的段落结构\n新增一段用于说明的内容\n结尾保持不变"

    pairs = build_edit_pairs(original, final)

    assert len(pairs) == 3
    assert pairs[0].before == "需要删掉的旧段落内容"
    assert pairs[0].after == ""
    assert pairs[1].before == "这是旧版本中需要整体改写的段落内容"
    assert pairs[1].after == "新的写法在这里完整替换了原先的段落结构"
    assert pairs[2].before == ""
    assert pairs[2].after == "新增一段用于说明的内容"
    assert pairs == build_edit_pairs(original, final)


def test_build_edit_pairs_skips_whitespace_short_and_unchanged_differences():
    assert build_edit_pairs("甲 乙\n丙丁", "甲乙\n丙丁") == []
    assert build_edit_pairs("abcdefghij", "abcdefghiX") == []
    assert build_edit_pairs("正文内容完全相同", "正文内容完全相同") == []


def test_build_edit_pairs_truncates_each_side_around_long_change():
    original = "前文" + "旧" * 1000 + "后文"
    final = "前文" + "新" * 1000 + "后文"

    pair = build_edit_pairs(original, final, max_snippet_chars=40)[0]

    assert len(pair.before) <= 40
    assert len(pair.after) <= 40
    assert "旧" in pair.before
    assert "新" in pair.after


def test_render_edit_examples_block_drops_from_end_and_returns_empty_for_no_pairs():
    pairs = [
        EditPair("前稿一" * 10, "改稿一" * 10),
        EditPair("前稿二" * 10, "改稿二" * 10),
    ]
    block = render_edit_examples_block(pairs, max_total_chars=150)

    assert len(block) <= 150
    assert "示例1" in block
    assert "修改前：前稿一" in block
    assert "示例2" not in block
    assert render_edit_examples_block([], max_total_chars=1200) == ""


@pytest.mark.asyncio
async def test_load_edit_examples_enforces_owner_project_status_edited_ratio_versions_and_limit(edit_db):
    async with edit_db() as db:
        owner_project = Project(title="owner project", owner_id="owner-a")
        other_project = Project(title="other project", owner_id="owner-a")
        other_owner_project = Project(title="other owner", owner_id="owner-b")
        db.add_all([owner_project, other_project, other_owner_project])
        await db.flush()

        now = datetime.now(timezone.utc)
        valid_low, _, _ = await _add_edit_record(
            db, project=owner_project, owner_id="owner-a", edit_ratio=0.03,
            applied_at=now - timedelta(minutes=1),
        )
        valid_high, _, _ = await _add_edit_record(
            db, project=owner_project, owner_id="owner-a", edit_ratio=0.6,
            applied_at=now,
        )
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", edit_ratio=0.0299)
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", edit_ratio=0.6001)
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", status="candidate")
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", edited=False)
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", missing_human=True)
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", deleted_human=True)
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", missing_anchor=True)
        await _add_edit_record(db, project=owner_project, owner_id="owner-a", mode="enhance")
        await _add_edit_record(db, project=other_project, owner_id="owner-a")
        await _add_edit_record(db, project=other_owner_project, owner_id="owner-b")
        await db.commit()

        result = await load_edit_examples(
            db,
            owner_id="owner-a",
            project_id=str(owner_project.id),
            mode="full_pipeline",
            query_text="人工调整后的段落细节",
            limit=1,
        )

        assert len(result) == 1
        assert result[0].generation_record_id == str(valid_high.id)
        assert result[0].edit_ratio == 0.6
        assert result[0].pairs
        assert result[0].pairs[0].before == "这是足够长的旧段落内容，描述一段故事变化。"
        assert result[0].pairs[0].after == "这是经过人工调整后的段落内容，细节更加清楚。"

        all_valid = await load_edit_examples(
            db,
            owner_id="owner-a",
            project_id=str(owner_project.id),
            mode="full_pipeline",
            query_text="",
            limit=10,
        )
        assert {item.generation_record_id for item in all_valid} == {
            str(valid_low.id), str(valid_high.id),
        }
        since_recent = await load_edit_examples(
            db,
            owner_id="owner-a",
            project_id=str(owner_project.id),
            mode="full_pipeline",
            query_text="",
            limit=10,
            since=now - timedelta(seconds=30),
        )
        assert [item.generation_record_id for item in since_recent] == [str(valid_high.id)]


@pytest.mark.asyncio
async def test_export_edit_pairs_is_read_only_and_writes_reason_tag(edit_db, tmp_path):
    from scripts.export_edit_pairs import export_edit_pairs

    db_path = tmp_path / "readonly.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        project = Project(title="export", owner_id="owner-a")
        db.add(project)
        await db.flush()
        record, _, _ = await _add_edit_record(db, project=project, owner_id="owner-a")
        await db.commit()
        project_id = str(project.id)
        record_id = str(record.id)
    await engine.dispose()

    before = db_path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    before_mtime = db_path.stat().st_mtime_ns
    output = tmp_path / "pairs.jsonl"
    await export_edit_pairs(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        project_id=project_id,
        out_path=output,
    )
    lines = [json.loads(line) for line in output.read_text().splitlines()]

    assert len(lines) == 1
    assert lines[0]["generation_record_id"] == record_id
    assert lines[0]["before"] == "这是足够长的旧段落内容，描述一段故事变化。"
    assert lines[0]["after"] == "这是经过人工调整后的段落内容，细节更加清楚。"
    assert lines[0]["chapter_sequence_number"] == 3
    assert lines[0]["reason_tag"] == ""
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash
    assert db_path.stat().st_mtime_ns == before_mtime
    future_output = tmp_path / "future-pairs.jsonl"
    assert await export_edit_pairs(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        project_id=project_id,
        out_path=future_output,
        since=datetime.now(timezone.utc) + timedelta(days=1),
    ) == 0
    assert future_output.read_text() == ""
