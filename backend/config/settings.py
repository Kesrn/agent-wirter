"""应用配置

所有配置从环境变量读取，提供合理默认值。
不依赖 .env 文件存在即可启动（mock 模式）。
"""

import os
from dataclasses import dataclass, field


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
    EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))

    # --- 安全边界 ---
    MAX_SYSTEM_PROMPT_LENGTH: int = 2000
    MAX_TOKENS_LIMIT: int = 8192
    MAX_REVISION_COUNT: int = 3
    AGENT_RATE_LIMIT_PER_MINUTE: int = 10
    AUTH_RATE_LIMIT_PER_MINUTE: int = 10
    PROTECTED_WORKFLOW_POSITIONS: tuple = ("consistency_checker", "human_review")

    # --- LangGraph ---
    LANGGRAPH_CHECKPOINT_DB: bool = os.getenv("LANGGRAPH_CHECKPOINT_DB", "false").lower() == "true"

    # --- 应用 ---
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:3000")


settings = Settings()