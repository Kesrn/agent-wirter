"""FastAPI 应用入口。

这个文件负责把后端服务真正“装配”起来：
- 先读取 settings 并初始化日志系统；
- 在 FastAPI lifespan 里初始化数据库表结构；
- 注册跨域中间件、静态资源目录和业务路由；
- 暴露 /health 健康检查，供前端、桌面端或部署探针判断服务是否可用。

业务逻辑本身不放在这里，避免入口文件越来越臃肿。具体接口在 api/routes.py、
鉴权在 api/auth.py、LLM 设置在 api/llm_settings.py。
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from config.logging_config import setup_logging
import db.session as db_session
from db.session import get_engine, init_db
from harness.run_manager import discard_incomplete_runs
from api.routes import router
from api.auth import router as auth_router
from api.body_limit import RequestBodyLimitMiddleware
from api.request_id import RequestIdMiddleware
from api.media import router as media_router
from api.llm_settings import router as llm_settings_router
from observability.langfuse import log_langfuse_startup_status
from agents.checkpointer import setup_checkpointer, teardown_checkpointer
from services.background_tasks import shutdown as shutdown_background_tasks

setup_logging(
    log_level=settings.LOG_LEVEL,
    log_file=settings.LOG_FILE or "",
    max_bytes=settings.LOG_MAX_BYTES,
    backup_count=settings.LOG_BACKUP_COUNT,
    sql_echo=settings.SQL_ECHO,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    FastAPI 启动时会先执行 yield 前的代码，关闭时执行 yield 后的代码。
    这里的 init_db 会按 SQLAlchemy model 创建表，并为桌面端 SQLite 做增量字段兼容。
    数据库初始化必须成功。继续运行一个数据库不完整的服务只会把启动问题延迟成
    用户操作时的 500，因此这里采用 fail-fast，便于桌面端及时重启和排查。
    """
    logger.info("🚀 AI 小说创作平台启动")
    log_langfuse_startup_status()
    app.state.database_ready = False
    app.state.checkpointer_ready = False
    from agents.workflow import _CHECKPOINTER
    try:
        await init_db()
        # 持久化 checkpointer setup：sqlite/postgres 建表。失败 fail-fast，
        # 不静默降级到 memory（否则 WAITING_HUMAN 状态重启后无法恢复）。
        await setup_checkpointer(_CHECKPOINTER)
        app.state.checkpointer_ready = True

        # 进程被强制关闭时，SSE 请求没有机会执行异常处理。启动后清理上次遗留的
        # CREATED/RUNNING run，防止半截任务、步骤和提示词审计记录长期留在数据库。
        # 通过模块读取当前 session factory，确保测试注入或桌面端运行时切换
        # engine 后不会继续使用 import 时绑定的旧连接池。
        async with db_session.async_session() as db:
            stale_thread_ids = await discard_incomplete_runs(db)
            await db.commit()
        if stale_thread_ids:
            for thread_id in stale_thread_ids:
                try:
                    await _CHECKPOINTER.adelete_thread(thread_id)
                except Exception:
                    logger.warning("清理 stale thread checkpoint 失败 thread_id=%s", thread_id, exc_info=True)
            logger.info("已清理 %d 个异常中断的生成任务", len(stale_thread_ids))
        app.state.database_ready = True
        logger.info("✅ 数据库初始化完成")
    except Exception:
        logger.exception("数据库初始化失败，服务不会以不完整状态启动")
        raise
    yield
    logger.info("🛑 AI 小说创作平台关闭")
    await shutdown_background_tasks()
    await teardown_checkpointer(_CHECKPOINTER)


app = FastAPI(
    title="AI 小说创作平台",
    description="多智能体小说创作平台 API，支持 LangGraph 工作流、RAG 上下文、自定义 Agent",
    version="0.1.0",
    lifespan=lifespan,
)

# 中间件顺序很关键：Starlette 里「后添加的在最外层」。
# 实际顺序为 CORS → 请求 ID → 请求体限制：CORS 可暴露请求 ID 响应头，
# 请求 ID 位于请求体限制外侧，因此 413 等提前返回也会带 X-Request-ID。
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(RequestIdMiddleware)

app.add_middleware(
    CORSMiddleware,
    # CORS_ORIGINS 来自环境变量，桌面端会包含 null / 127.0.0.1，
    # Web 开发模式通常是 localhost:5173。
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# 上传的图片等文件统一放到 UPLOAD_DIR。这里不再用 StaticFiles 直接挂载目录：
# /media 下的文件是项目私有资源，改由 api/media.py 提供，需要登录 + 校验项目所有者。
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

# 主业务路由、认证路由、用户级 LLM 设置路由分开注册，降低单个 router 的职责。
app.include_router(router)
app.include_router(auth_router)
app.include_router(media_router)
app.include_router(llm_settings_router)


@app.get("/health/live")
async def health_live():
    """进程存活探针，不访问数据库或其它外部服务。"""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """检查应用、数据库连接与 checkpointer 初始化状态，不暴露内部异常。"""
    database_initialized = bool(getattr(app.state, "database_ready", False))
    checkpointer_initialized = bool(getattr(app.state, "checkpointer_ready", False))
    database_connected = False
    try:
        async def _ping_database():
            async with get_engine().connect() as connection:
                await connection.execute(text("SELECT 1"))

        await asyncio.wait_for(_ping_database(), timeout=2.0)
        database_connected = True
    except Exception:
        # Health probes intentionally avoid returning connection strings or exception text.
        database_connected = False

    checks = {
        "database_initialized": database_initialized,
        "database_connected": database_connected,
        "checkpointer_initialized": checkpointer_initialized,
    }
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "not_ready", "checks": checks},
    )


@app.get("/health")
async def health_check():
    """健康检查接口。

    返回当前 LLM/Embedding provider，方便排查“为什么本地没有调真实模型”
    或“桌面端是否仍处于 mock/openai provider 配置”的问题。
    """
    return {
        "status": "ok",
        "service": "AI 小说创作平台",
        "version": "0.1.0",
        "llm_provider": settings.LLM_PROVIDER,
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "database_dialect": get_engine().dialect.name,
        "database_ready": bool(getattr(app.state, "database_ready", False)),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
