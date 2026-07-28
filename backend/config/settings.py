"""应用配置。

所有配置从环境变量读取，提供合理默认值。
不依赖 .env 文件存在即可启动（mock 模式）。

这里集中管理“运行环境差异”：Web 开发、本地 Docker、Electron 桌面端、
生产部署都可以通过环境变量覆盖同一套 Settings。业务代码只依赖 settings，
不直接散落读取 os.getenv，便于排查配置来源。
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))


def _load_env_files() -> None:
    """加载本地 .env 文件。

    直接 `python main.py` 或 `uvicorn main:app` 启动时，工作目录可能在项目根，
    也可能在 backend 目录；这里两个位置都尝试加载。override=False 表示环境变量
    已经由 shell/部署平台注入时，.env 不会覆盖外部配置。
    """
    for env_file in (os.path.join(PROJECT_ROOT, ".env"), os.path.join(BASE_DIR, ".env")):
        if os.path.exists(env_file):
            load_dotenv(env_file, override=False)


_load_env_files()


@dataclass(frozen=True)
class Settings:
    """不可变配置对象。

    frozen=True 可以避免运行过程中被业务代码误改配置。需要切换 provider、
    数据库或日志级别时，应通过环境变量重启服务，或者走用户级 LLMConfig。
    """
    # --- 数据库 ---
    # Web/服务端默认使用 PostgreSQL asyncpg；Electron 桌面端会在 desktop_server.py
    # 启动前覆盖为 sqlite+aiosqlite:///<用户数据目录>/ai_creative.sqlite3。
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_creative")

    # --- LLM ---
    # 全局默认 LLM 配置。用户在设置页保存的 LLMConfig 优先级更高；
    # 没有用户配置时才使用这里的默认 provider/base_url/model/api_key。
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")  # openai | deepseek | siliconflow | zhipu | moonshot | qwen | yi | minimax | custom
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    # 生产默认禁用 Mock；自动化测试可显式开启，避免真实网络和 token 消耗。
    ALLOW_MOCK_PROVIDER: bool = os.getenv("ALLOW_MOCK_PROVIDER", "false").lower() == "true"

    # --- JWT ---
    # JWT_SECRET 同时用于签发登录 token，以及派生 Fernet key 加密用户 API Key。
    # 生产环境必须固定配置，否则重启会导致 token 失效，API Key 也可能无法解密。
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

    # --- RAG / Embedding ---
    # Embedding 用于素材/知识库相似检索。维度需和模型输出维度一致；
    # PostgreSQL + pgvector 场景下，维度不一致会导致写入或距离计算失败。
    EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "openai")  # openai | huggingface
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    EMBEDDING_DIMENSION: int = int(os.getenv("EMBEDDING_DIMENSION", "1536"))

    # --- 日志 ---
    # DEBUG=true 时默认 DEBUG 日志，便于本地追踪 SSE、LLM 和抽取状态机；
    # 生产环境建议 DEBUG=false，并配置 LOG_FILE 做滚动日志。
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG" if os.getenv("DEBUG", "true").lower() == "true" else "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "")  # 可选文件日志路径，为空则仅控制台输出
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 单个日志文件最大字节，默认 10MB
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))  # 日志文件备份数量

    # --- 安全边界 ---
    # 这些限制用于约束用户可配置 Agent，防止超长 system prompt、
    # 无限制 token 输出或用户覆盖 human_review / consistency_checker 等保护节点。
    MAX_SYSTEM_PROMPT_LENGTH: int = 2000
    MAX_TOKENS_LIMIT: int = 8192
    MAX_REVISION_COUNT: int = 3
    AGENT_RATE_LIMIT_PER_MINUTE: int = 10
    AUTH_RATE_LIMIT_PER_MINUTE: int = 10
    PROTECTED_WORKFLOW_POSITIONS: tuple = ("consistency_checker", "human_review")

    # --- LangGraph ---
    # 目前主要使用内存 checkpointer；该开关预留给后续数据库 checkpoint。
    # 使用内存 checkpoint 时，服务重启后等待人工审核的 thread 状态会丢失。
    LANGGRAPH_CHECKPOINT_DB: bool = os.getenv("LANGGRAPH_CHECKPOINT_DB", "false").lower() == "true"

    # --- Langfuse / Observability ---
    # Langfuse 是可选观测能力。代码里所有 Langfuse 调用都做了降级处理，
    # 未配置时不影响生成，只是不记录 trace。
    LANGFUSE_ENABLED: bool = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
    LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    LANGFUSE_SAMPLE_RATE: float = float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))
    LANGFUSE_CAPTURE_PROMPTS: bool = os.getenv("LANGFUSE_CAPTURE_PROMPTS", "true").lower() == "true"

    # --- 应用 ---
    # CORS_ORIGINS 包含 null 是为了兼容 Electron/file origin；纯 Web 部署时可收紧。
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
