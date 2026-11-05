#!/usr/bin/env python3
"""Upgrade an empty PostgreSQL database to Alembic head and compare ORM schema."""

import asyncio
import pprint
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
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
    # This generated full-text-search table is maintained explicitly by migration 031;
    # it intentionally has no ORM model.
    return not (object_type == "table" and name == "retrieval_chunks_fts")


def _compare_database_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """Ignore Python wrapper differences when both sides compile to the same SQL type."""
    if inspected_type.compile(dialect=context.dialect) == metadata_type.compile(dialect=context.dialect):
        return False
    return None


def _describe_difference(difference) -> str:
    if isinstance(difference, list) and len(difference) == 1:
        return _describe_difference(difference[0])
    operation = difference[0]
    if operation in {"add_table", "remove_table"}:
        return f"{operation}: {difference[1].name}"
    if operation in {"add_index", "remove_index"}:
        index = difference[1]
        columns = ",".join(index.columns.keys())
        return f"{operation}: {index.table.name}.{index.name} ({columns}, unique={index.unique})"
    if operation in {"add_column", "remove_column"}:
        _, table_name, column = difference
        return f"{operation}: {table_name}.{column.name} {column.type} nullable={column.nullable}"
    if operation == "modify_nullable":
        _, _, table_name, column_name, _, actual, expected = difference
        return f"modify_nullable: {table_name}.{column_name} nullable {actual} -> {expected}"
    if operation == "modify_type":
        _, _, table_name, column_name, _, actual, expected = difference
        return f"modify_type: {table_name}.{column_name} {actual} -> {expected}"
    if operation in {"add_fk", "remove_fk", "add_constraint", "remove_constraint"}:
        constraint = difference[1]
        return f"{operation}: {constraint.table.name}.{constraint.name} ({constraint})"
    return pprint.pformat(difference, sort_dicts=True, compact=True)


def main() -> int:
    try:
        asyncio.run(_assert_empty_postgres())
        command.upgrade(_alembic_config(), "head")
        differences = asyncio.run(_compare_models_to_database())
    except Exception as exc:
        print(f"schema parity failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if differences:
        print("Schema differences (ORM metadata vs Alembic-upgraded PostgreSQL):")
        for difference in differences:
            print(_describe_difference(difference))
        return 1

    print("Schema parity OK: no differences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
