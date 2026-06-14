"""数据库连接和会话管理"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from config.settings import settings

_engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
async_session = _session_factory


def get_engine():
    return _engine


def set_engine(new_engine):
    """Replace the default engine (used by tests to inject SQLite)."""
    global _engine, _session_factory, async_session
    _engine = new_engine
    _session_factory = async_sessionmaker(new_engine, class_=AsyncSession, expire_on_commit=False)
    async_session = _session_factory


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库会话"""
    async with _session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """初始化数据库：创建所有表"""
    from models.base import Base
    from models import project, chapter, document, expert, world_entry, character, character_relation, character_event, outline, hidden_thread, user, llm_config, chapter_version, document_version, generation_record, evaluation  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_incremental_columns(conn)


async def _ensure_incremental_columns(conn):
    """Keep desktop SQLite databases compatible with additive schema changes."""
    if conn.dialect.name != "sqlite":
        return

    async def columns(table_name: str) -> set[str]:
        result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
        return {row[1] for row in result.fetchall()}

    project_columns = await columns("projects")
    if "overall_outline" not in project_columns:
        await conn.execute(text("ALTER TABLE projects ADD COLUMN overall_outline TEXT"))

    character_columns = await columns("characters")
    if "scope_type" not in character_columns:
        await conn.execute(text("ALTER TABLE characters ADD COLUMN scope_type VARCHAR(20) DEFAULT 'recurring'"))

    world_columns = await columns("world_entries")
    if "scope_type" not in world_columns:
        await conn.execute(text("ALTER TABLE world_entries ADD COLUMN scope_type VARCHAR(20) DEFAULT 'global'"))
