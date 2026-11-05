#!/usr/bin/env python3
"""Export applied human edits as JSONL through a read-only SQLite connection."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from models.chapter import Chapter
from models.chapter_version import ChapterVersion
from models.generation_record import GenerationRecord


def _readonly_sqlite_url(database_url: str) -> URL:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise ValueError("导出脚本只支持文件形式的本地 SQLite 数据库")
    database = url.database
    if database.startswith("file:"):
        database = database[5:].split("?", 1)[0]
    path = Path(database).expanduser().resolve()
    return URL.create(
        "sqlite+aiosqlite",
        database=f"file:{path.as_posix()}",
        query={"mode": "ro", "uri": "true"},
    )


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed


async def export_edit_pairs(
    *,
    database_url: str,
    project_id: str,
    out_path: str | Path,
    since: datetime | None = None,
) -> int:
    """Write eligible cached human-edit pairs without opening a writable DB."""
    engine = create_async_engine(_readonly_sqlite_url(database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            query = select(GenerationRecord).where(
                GenerationRecord.project_id == project_id,
                GenerationRecord.status == "applied",
            )
            if since is not None:
                query = query.where(GenerationRecord.updated_at >= since)
            query = query.order_by(GenerationRecord.updated_at.desc(), GenerationRecord.id.asc())
            records = list((await db.execute(query)).scalars().all())
            versions_needed: set[str] = set()
            eligible = []
            for record in records:
                metrics = record.human_edit_metrics
                if not isinstance(metrics, dict) or metrics.get("edited") is not True:
                    continue
                anchor_id = metrics.get("anchor_version_id")
                human_id = metrics.get("human_version_id")
                if not anchor_id or not human_id or str(record.accepted_version_id) != str(anchor_id):
                    continue
                versions_needed.update((str(anchor_id), str(human_id)))
                eligible.append((record, str(anchor_id), str(human_id), metrics))

            versions = {}
            if versions_needed:
                version_result = await db.execute(
                    select(
                        ChapterVersion.id,
                        ChapterVersion.chapter_id,
                        ChapterVersion.content,
                        Chapter.sequence_number,
                    )
                    .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
                    .where(
                        ChapterVersion.id.in_(versions_needed),
                        Chapter.project_id == project_id,
                    )
                )
                versions = {
                    str(row.id): (
                        str(row.chapter_id),
                        row.content or "",
                        row.sequence_number,
                    )
                    for row in version_result
                }

            rows = []
            for record, anchor_id, human_id, metrics in eligible:
                before = versions.get(anchor_id)
                after = versions.get(human_id)
                if before is None or after is None or before[0] != after[0]:
                    continue
                try:
                    edit_ratio = float(metrics["edit_ratio"])
                except (KeyError, TypeError, ValueError):
                    continue
                rows.append({
                    "before": before[1],
                    "after": after[1],
                    "edit_ratio": edit_ratio,
                    "chapter_sequence_number": before[2],
                    "generation_record_id": str(record.id),
                    "reason_tag": "",
                })

        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return len(rows)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="只读导出项目人工修改前后版本")
    parser.add_argument("--project", required=True, help="项目 UUID")
    parser.add_argument("--out", required=True, help="输出 JSONL 文件")
    parser.add_argument("--since", help="只导出此 ISO 时间之后应用的记录")
    args = parser.parse_args()
    count = asyncio.run(export_edit_pairs(
        database_url=settings.DATABASE_URL,
        project_id=args.project,
        out_path=args.out,
        since=_parse_since(args.since),
    ))
    print(f"已只读导出 {count} 条人工修改示例到 {args.out}")


if __name__ == "__main__":
    main()
