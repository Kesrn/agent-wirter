"""应用配置

所有配置从环境变量读取，提供合理默认值。
不依赖 .env 文件存在即可启动（mock 模式）。
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))


def _load_env_files() -> None:
    """Load local env files for direct `python main.py` startup."""
    for env_file in (os.path.join(PROJECT_ROOT, ".env"), os.path.join(BASE_DIR, ".env")):
        if os.path.exists(env_file):
            load_dotenv(env_file, override=False)


_load_env_files()


@dataclass(frozen=True)
class Settings:
    # --- 数据库 ---
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_creative")

    # --- LLM ---
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "mock")  # mock | openai | deepseek | siliconflow | zhipu | moonshot | qwen | yi | minimax | custom
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")

    # --- JWT ---
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

    # --- RAG / Embedding ---
    EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "mock")  # mock | openai | huggingface
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    EMBEDDING_DIMENSION: int = int(os.getenv("EMBEDDING_DIMENSION", "1536"))

    # --- 日志 ---
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG" if os.getenv("DEBUG", "true").lower() == "true" else "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "")  # 可选文件日志路径，为空则仅控制台输出
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 单个日志文件最大字节，默认 10MB
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))  # 日志文件备份数量

    # --- 安全边界 ---
    MAX_SYSTEM_PROMPT_LENGTH: int = 2000
    MAX_TOKENS_LIMIT: int = 8192
    MAX_REVISION_COUNT: int = 3
    AGENT_RATE_LIMIT_PER_MINUTE: int = 10
    AUTH_RATE_LIMIT_PER_MINUTE: int = 10
    PROTECTED_WORKFLOW_POSITIONS: tuple = ("consistency_checker", "human_review")

    # --- LangGraph ---
    LANGGRAPH_CHECKPOINT_DB: bool = os.getenv("LANGGRAPH_CHECKPOINT_DB", "false").lower() == "true"

    # --- Langfuse / Observability ---
    LANGFUSE_ENABLED: bool = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
    LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    LANGFUSE_SAMPLE_RATE: float = float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))
    LANGFUSE_CAPTURE_PROMPTS: bool = os.getenv("LANGFUSE_CAPTURE_PROMPTS", "true").lower() == "true"

    # --- 应用 ---
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"
    SQL_ECHO: bool = os.getenv("SQL_ECHO", "false").lower() == "true"
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:3000,null")
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
    MAX_TXT_IMPORT_BYTES: int = int(os.getenv("MAX_TXT_IMPORT_BYTES", str(50 * 1024 * 1024)))
    KNOWLEDGE_UPLOAD_MAX_BYTES: int = int(os.getenv("KNOWLEDGE_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
    WEB_SEARCH_ENABLED: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"
    WEB_SEARCH_PROVIDER: str = os.getenv("WEB_SEARCH_PROVIDER", "duckduckgo")
    WEB_SEARCH_API_KEY: str = os.getenv("WEB_SEARCH_API_KEY", "")
    WEB_SEARCH_BASE_URL: str = os.getenv("WEB_SEARCH_BASE_URL", "")
    WEB_SEARCH_MAX_RESULTS: int = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
    WEB_SEARCH_TIMEOUT_SECONDS: float = float(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "8"))


settings = Settings()
