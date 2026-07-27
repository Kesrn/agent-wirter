"""数据库连接和会话管理。

后端所有数据库访问都通过这里创建的 async SQLAlchemy engine/session。
FastAPI 路由使用 get_db 作为依赖注入，每个请求拿到一个独立 AsyncSession；
服务层函数只接收 session，不自己创建事务，这样调用方可以决定何时 commit/rollback。
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from config.settings import settings

# 全局 engine 在进程启动时创建。settings.DATABASE_URL 可以是 PostgreSQL asyncpg，
# 也可以是桌面端覆盖后的 SQLite aiosqlite。
_engine = create_async_engine(settings.DATABASE_URL, echo=settings.SQL_ECHO)
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
async_session = _session_factory


def get_engine():
    """返回当前全局 engine。

    测试和后台任务会用它判断数据库类型，例如 SQLite 内存库不适合跨 session 后台任务。
    """
    return _engine


def set_engine(new_engine):
    """替换默认 engine，主要供测试注入 SQLite 内存数据库。

    注意替换 engine 时必须同步刷新 session_factory 和 async_session，
    否则后续请求仍会连到旧数据库。
    """
    global _engine, _session_factory, async_session
    _engine = new_engine
    _session_factory = async_sessionmaker(new_engine, class_=AsyncSession, expire_on_commit=False)
    async_session = _session_factory


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库会话。

    每次请求进入路由时创建 session，请求结束后关闭。commit/rollback 不在这里做，
    因为不同接口可能需要多次 flush、SSE 流中间保存候选记录，必须由业务显式控制。
    """
    async with _session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """初始化数据库：创建所有表。

    Alembic 是正式迁移方案，但桌面端/演示环境需要“开箱即用”，所以这里会根据
    SQLAlchemy metadata 创建缺失表。SQLite 桌面端还会走 _ensure_incremental_columns，
    让已安装用户的数据文件能跟随小版本新增字段继续使用。
    """
    from models.base import Base
    from models import project, chapter, document, expert, world_entry, character, character_relation, character_event, outline, hidden_thread, user, llm_config, chapter_version, document_version, generation_record, chapter_review_note, evaluation, project_source, project_source_chunk, project_knowledge_fact, extraction_pipeline, structured_knowledge, knowledge_qa_session, knowledge_qa_message  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_incremental_columns(conn)
        await _validate_sqlite_schema(conn)


async def _ensure_incremental_columns(conn):
    """为桌面端 SQLite 做增量字段补齐。

    SQLite 没有像 Alembic 在线迁移那样完整的部署流程，老用户本地数据库可能缺字段。
    这里只做安全的 ADD COLUMN，不删除/重命名字段，避免破坏用户已有数据。
    """
    if conn.dialect.name != "sqlite":
        return

    async def columns(table_name: str) -> set[str]:
        result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
        return {row[1] for row in result.fetchall()}

    async def add_missing_columns(table_name: str, additions: dict[str, str]) -> None:
        existing = await columns(table_name)
        for name, definition in additions.items():
            if name not in existing:
                await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"))

    await add_missing_columns("projects", {
        "overall_outline": "TEXT",
    })
    await add_missing_columns("characters", {
        "scope_type": "VARCHAR(20) DEFAULT 'recurring'",
    })
    await add_missing_columns("world_entries", {
        "scope_type": "VARCHAR(20) DEFAULT 'global'",
    })
    await add_missing_columns("chapters", {
        "final_version_id": "CHAR(36)",
    })

    # 015 / 023：旧项目的专家表没有技能目录和专家系统 v2 字段。缺列不仅会让
    # 专家管理页失败，也会在生成工作流加载专家时触发 500。
    await add_missing_columns("experts", {
        "skill_dir": "VARCHAR(100)",
        "expert_key": "VARCHAR(80)",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "deprecated": "BOOLEAN NOT NULL DEFAULT 0",
        "input_schema": "JSON",
        "output_schema": "JSON",
    })

    # 021：版本审计与生成记录关联字段。保留历史行的 NULL，不回填或改写既有版本。
    await add_missing_columns("chapter_versions", {
        "project_id": "CHAR(36)",
        "run_id": "CHAR(36)",
        "parent_version_id": "CHAR(36)",
        "rollback_from_version_id": "CHAR(36)",
        "diff_from_parent": "JSON",
    })
    await add_missing_columns("generation_records", {
        "run_id": "CHAR(36)",
    })

    # 长线与节奏功能（024 / 026）会给已有的大纲表追加字段。桌面端用户的
    # SQLite 文件可能早于这些迁移；create_all 不会改动已存在的表，因此必须在
    # 启动时补齐，否则读取 Outline 就会直接因缺列而报错。
    await add_missing_columns("outlines", {
        "story_arc_id": "CHAR(36)",
        "arc_position": "VARCHAR(30)",
        "pacing": "VARCHAR(30)",
        "tension_level": "INTEGER",
        "target_scene_count": "INTEGER",
    })

    # 伏笔生命周期（025）同样需要兼容旧的 hidden_threads 表。status 使用默认值，
    # 既保证旧记录可读，也让后续新写入遵循模型的非空约束。
    await add_missing_columns("hidden_threads", {
        "status": "VARCHAR(20) NOT NULL DEFAULT 'PLANNED'",
        "thread_type": "VARCHAR(30)",
        "planted_chapter": "INTEGER",
        "reveal_chapter": "INTEGER",
        "resolved_chapter": "INTEGER",
        "payoff_summary": "TEXT",
        "risk_level": "VARCHAR(20)",
    })

    # 与 Alembic 迁移保持一致；IF NOT EXISTS 使桌面端重复启动保持幂等。
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_outlines_story_arc_id ON outlines (story_arc_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_outlines_pacing ON outlines (pacing)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hidden_threads_status ON hidden_threads (status)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hidden_threads_planted_chapter ON hidden_threads (planted_chapter)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hidden_threads_reveal_chapter ON hidden_threads (reveal_chapter)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_experts_expert_key ON experts (expert_key)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chapter_versions_project_id ON chapter_versions (project_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chapter_versions_run_id ON chapter_versions (run_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_generation_records_run_id ON generation_records (run_id)"))


async def _missing_sqlite_model_columns(conn, table_names: set[str] | None = None) -> dict[str, list[str]]:
    """返回 ORM 模型与 SQLite 物理表之间仍不一致的列。

    这是桌面端迁移的最后一道防线：已知的安全增量迁移会在前面自动执行；如果未来
    新增字段却忘记补迁移，服务会在启动阶段明确报错，而不是在用户操作中返回 500。
    """
    if conn.dialect.name != "sqlite":
        return {}

    from models.base import Base

    missing: dict[str, list[str]] = {}
    for table in Base.metadata.sorted_tables:
        if table_names is not None and table.name not in table_names:
            continue
        result = await conn.execute(text(f"PRAGMA table_info({table.name})"))
        actual = {row[1] for row in result.fetchall()}
        absent = [column.name for column in table.columns if column.name not in actual]
        if absent:
            missing[table.name] = absent
    return missing


async def _validate_sqlite_schema(conn) -> None:
    """确保桌面端启动后所有 ORM 查询都有对应的物理列。"""
    missing = await _missing_sqlite_model_columns(conn)
    if missing:
        details = "; ".join(
            f"{table}: {', '.join(columns)}" for table, columns in sorted(missing.items())
        )
        raise RuntimeError(f"桌面端 SQLite 数据库迁移不完整，缺少字段：{details}")
