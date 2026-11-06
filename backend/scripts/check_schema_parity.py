#!/usr/bin/env python3
"""Compare an Alembic-upgraded PostgreSQL schema with ORM metadata.

Known legacy drift is stored in ``backend/alembic/known_drift.json``. New drift
fails the check; drift that has disappeared is reported without failing. The
baseline is only written when ``--update-baseline`` is explicitly supplied.
"""

import argparse
import asyncio
import json
import pprint
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
BASELINE_PATH = BACKEND_DIR / "alembic" / "known_drift.json"
sys.path.insert(0, str(BACKEND_DIR))

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from config.settings import settings  # noqa: E402
from models.base import Base  # noqa: E402
import models  # noqa: E402,F401
import models.retrieval_index  # noqa: E402,F401
import models.retrieval_index_job  # noqa: E402,F401
import models.retrieval_vector  # noqa: E402,F401


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


async def _assert_empty_postgres() -> None:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            if conn.dialect.name != "postgresql":
                raise RuntimeError("schema parity 仅支持 PostgreSQL 临时数据库")
            result = await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version' ORDER BY table_name"
            ))
            existing = [row[0] for row in result.fetchall()]
            if existing:
                raise RuntimeError(
                    "目标数据库不是空库；为避免覆盖已有数据，拒绝运行。"
                )
    finally:
        await engine.dispose()


async def _compare_models_to_database() -> list:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn, opts={
                        "compare_type": _compare_database_type,
                        "include_object": _include_schema_object,
                    }),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()


def _include_schema_object(obj, name, object_type, reflected, compare_to):
    # These tables/columns are maintained with explicit PostgreSQL FTS DDL and do
    # not have a full ORM representation.
    if object_type == "table" and name == "retrieval_chunks_fts":
        return False
    if object_type == "column" and name == "search_tsv":
        table = getattr(obj, "table", None)
        if getattr(table, "name", None) in {"retrieval_chunks", "retrieval_chunks_fts"}:
            return False
    return True


def _compare_database_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """Ignore Python wrapper differences when both sides compile to the same SQL type."""
    if inspected_type.compile(dialect=context.dialect) == metadata_type.compile(dialect=context.dialect):
        return False
    return None


def _unwrap_difference(difference):
    while isinstance(difference, list) and len(difference) == 1:
        difference = difference[0]
    return difference


def _type_label(value) -> str:
    return f"{type(value).__name__}({value})"


def _difference_to_entry(difference) -> dict[str, str]:
    """Convert SQLAlchemy autogenerate output into a stable, categorized record."""
    difference = _unwrap_difference(difference)
    operation = difference[0]

    if operation == "modify_nullable":
        _, _, table_name, column_name, _, actual, expected = difference
        return {
            "type": "nullability",
            "table": table_name,
            "object": column_name,
            "description": f"database nullable={actual}; ORM nullable={expected}",
        }

    if operation == "modify_type":
        _, _, table_name, column_name, _, actual, expected = difference
        type_names = f"{type(actual).__name__} {actual} {type(expected).__name__} {expected}".lower()
        if "json" in type_names:
            category = "json_jsonb"
        elif any(token in type_names for token in ("guid", "uuid", "char(36)", "varchar(36)")):
            category = "uuid_string"
        else:
            category = "type"
        return {
            "type": category,
            "table": table_name,
            "object": column_name,
            "description": f"database {_type_label(actual)}; ORM {_type_label(expected)}",
        }

    if operation in {"add_index", "remove_index"}:
        index = difference[1]
        columns = ",".join(index.columns.keys())
        return {
            "type": "old_index",
            "table": index.table.name,
            "object": index.name,
            "description": f"{operation}; columns={columns}; unique={index.unique}",
        }

    if operation in {"add_fk", "remove_fk", "add_constraint", "remove_constraint"}:
        constraint = difference[1]
        table_name = constraint.table.name
        name = constraint.name or ",".join(column.name for column in constraint.columns)
        if "foreignkey" in type(constraint).__name__.lower():
            category = "foreign_key"
            target = ",".join(
                f"{element.column.table.name}.{element.column.name}"
                for element in getattr(constraint, "elements", ())
            )
            description = f"{operation}; target={target or 'unknown'}"
        else:
            # Existing uniqueness/index-like constraints are part of the legacy
            # index drift set; this record still names the constraint explicitly.
            category = "old_index"
            description = f"{operation}; constraint={type(constraint).__name__}"
        return {
            "type": category,
            "table": table_name,
            "object": name,
            "description": description,
        }

    if operation in {"add_column", "remove_column"}:
        if len(difference) == 4:
            _, _, table_name, column = difference
        else:
            _, table_name, column = difference
        return {
            "type": "column",
            "table": table_name,
            "object": column.name,
            "description": f"{operation}; {_type_label(column.type)}; nullable={column.nullable}",
        }

    if operation in {"add_table", "remove_table"}:
        table = difference[1]
        return {
            "type": "table",
            "table": table.name,
            "object": table.name,
            "description": operation,
        }

    return {
        "type": "other",
        "table": "",
        "object": operation,
        "description": pprint.pformat(difference, sort_dicts=True, compact=True),
    }


def _entry_key(entry: dict[str, str]) -> tuple[str, str, str, str]:
    return (entry["type"], entry["table"], entry["object"], entry["description"])


def _sorted_entries(differences) -> list[dict[str, str]]:
    entries = [_difference_to_entry(difference) for difference in differences]
    return sorted(entries, key=_entry_key)


def compare_drift(current: list[dict[str, str]], baseline: list[dict[str, str]]):
    """Return new drift and baseline drift that has disappeared."""
    current_by_key = {_entry_key(item): item for item in current}
    baseline_by_key = {_entry_key(item): item for item in baseline}
    new = [current_by_key[key] for key in sorted(current_by_key.keys() - baseline_by_key.keys())]
    disappeared = [baseline_by_key[key] for key in sorted(baseline_by_key.keys() - current_by_key.keys())]
    return new, disappeared


def evaluate_drift(current: list[dict[str, str]], baseline: list[dict[str, str]]):
    """Return new entries, resolved entries, and the corresponding process status."""
    new, disappeared = compare_drift(current, baseline)
    return new, disappeared, 1 if new else 0


def format_resolved_notice(entries: list[dict[str, str]]) -> str:
    if not entries:
        return ""
    lines = [f"Known drift no longer present ({len(entries)}):"]
    lines.extend(
        f"{entry['type']}: {entry['table']}.{entry['object']} — {entry['description']}"
        for entry in entries
    )
    return "\n".join(lines)


def _read_baseline(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    differences = payload.get("differences")
    if not isinstance(differences, list):
        raise ValueError(f"无效的漂移基线格式: {path}")
    return differences


def _write_baseline(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "differences": sorted(entries, key=_entry_key)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help=f"显式更新已知漂移基线: {BASELINE_PATH}",
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(_assert_empty_postgres())
        command.upgrade(_alembic_config(), "head")
        differences = asyncio.run(_compare_models_to_database())
        entries = _sorted_entries(differences)
        if args.update_baseline:
            _write_baseline(BASELINE_PATH, entries)
            print(f"Known drift baseline updated: {len(entries)} entries -> {BASELINE_PATH}")
            return 0

        baseline = _read_baseline(BASELINE_PATH)
        new, disappeared, status = evaluate_drift(entries, baseline)
    except Exception as exc:
        print(f"schema parity failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if disappeared:
        print(format_resolved_notice(disappeared))
    if new:
        print(f"New schema differences ({len(new)}):")
        for entry in new:
            print(f"{entry['type']}: {entry['table']}.{entry['object']} — {entry['description']}")
        return 1

    print(f"Schema parity OK: no new differences ({len(entries)} known, {len(disappeared)} resolved).")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
