"""FastAPI 应用入口"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from db.session import init_db
from api.routes import router
from api.auth import router as auth_router
from api.llm_settings import router as llm_settings_router
from observability.langfuse import log_langfuse_startup_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 AI 小说创作平台启动")
    log_langfuse_startup_status()
    try:
        await init_db()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.warning(f"⚠️ 数据库初始化失败（将使用无 DB 模式）: {e}")
    yield
    logger.info("🛑 AI 小说创作平台关闭")


app = FastAPI(
    title="AI 小说创作平台",
    description="多智能体小说创作平台 API，支持 LangGraph 工作流、RAG 上下文、自定义 Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.UPLOAD_DIR), name="media")

app.include_router(router)
app.include_router(auth_router)
app.include_router(llm_settings_router)


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "AI 小说创作平台",
        "version": "0.1.0",
        "llm_provider": settings.LLM_PROVIDER,
        "embedding_provider": settings.EMBEDDING_PROVIDER,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
