"""数据库连接和会话管理"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    from models import project, chapter, document, expert, world_entry, character, character_relation, outline, hidden_thread, user, llm_config, chapter_version, document_version, generation_record  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
