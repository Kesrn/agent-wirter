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

# --- 运行环境 ---
# APP_ENV 决定“安全相关默认值”的松紧程度：
#   development（默认）：本地/桌面端，允许首次登录建号、允许注册、允许自动生成密钥；
#   production：服务端部署，密钥缺失即启动失败，注册与首次登录建号默认关闭。
# 只认这两个值（dev/prod 视为别名），拼错不会静默按开发环境放开安全开关。
_APP_ENV_ALIASES = {
    "development": "development",
    "dev": "development",
    "production": "production",
    "prod": "production",
}


def _resolve_app_env() -> str:
    """解析 APP_ENV，非法取值直接报错而不是猜测。空值按未配置处理。"""
    raw = os.getenv("APP_ENV", "").strip().lower()
    if not raw:
        return "development"
    resolved = _APP_ENV_ALIASES.get(raw)
    if resolved is None:
        raise RuntimeError(
            f"APP_ENV 取值非法：{raw!r}，只支持 development 或 production"
        )
    return resolved


def _env_flag(name: str, default: bool) -> bool:
    """读取布尔型环境变量；未配置时用按环境推导的默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() == "true"


def _load_env_files() -> None:
    """加载本地 .env 文件。

    直接 `python main.py` 或 `uvicorn main:app` 启动时，工作目录可能在项目根，
    也可能在 backend 目录；这里两个位置都尝试加载。override=False 表示环境变量
    已经由 shell/部署平台注入时，.env 不会覆盖外部配置。
    """
    for env_file in (os.path.join(PROJECT_ROOT, ".env"), os.path.join(BASE_DIR, ".env")):
        if os.path.exists(env_file):
            load_dotenv(env_file, override=False)


# 必须先加载 .env，再读 APP_ENV：否则 .env 里写的 APP_ENV=production 会被忽略，
# 进程静默按开发环境运行（安全开关全开），属于 fail-open。顺序不能调换。
_load_env_files()

_APP_ENV = _resolve_app_env()
_IS_PRODUCTION = _APP_ENV == "production"


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

    # --- 运行环境与安全开关 ---
    # APP_ENV 见文件头部说明。生产环境默认拒绝自助注册、拒绝首次登录自动建号，
    # 账号只能由 `python scripts/create_admin.py` 创建。
    APP_ENV: str = _APP_ENV
    IS_PRODUCTION: bool = _IS_PRODUCTION
    ALLOW_REGISTRATION: bool = _env_flag("ALLOW_REGISTRATION", not _IS_PRODUCTION)
    ALLOW_FIRST_USER_BOOTSTRAP: bool = _env_flag("ALLOW_FIRST_USER_BOOTSTRAP", not _IS_PRODUCTION)

    # --- JWT ---
    # JWT_SECRET 只用于签发/校验登录 token。
    # 生产环境必须显式配置且长度 >= 32（启动时校验）；开发环境未配置时会自动生成，
    # 但重启后登录态失效。
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

    # --- 数据加密 ---
    # ENCRYPTION_KEY 专门用于加密用户 API Key（标准 Fernet 密钥，Fernet.generate_key()
    # 的输出）。与 JWT_SECRET 分离：轮换登录密钥不会连带使历史密文不可读。
    # 生产环境必须显式配置（启动时校验）；开发环境未配置时回退到“由 JWT_SECRET 派生”
    # 并打 warning，见 utils/crypto.py。
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")

    # --- RAG / Embedding ---
    # Embedding 用于素材/知识库相似检索。维度需和模型输出维度一致；
    # PostgreSQL + pgvector 场景下，维度不一致会导致写入或距离计算失败。
    EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "openai")  # openai | huggingface
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    EMBEDDING_DIMENSION: int = int(os.getenv("EMBEDDING_DIMENSION", "1536"))
    # embedding 端点独立于 LLM 对话端点（如 SiliconFlow 与对话 base_url 不同）；
    # 为空时回退 LLM_BASE_URL，再回退 OpenAI 官方。
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL", "")

    # --- 检索模式 ---
    # 知识问答检索链路（services/knowledge_source.search_project_knowledge）的检索器选择，
    # 由阶段一词法检索（LexicalRetriever，评测 recall@5 0.132 → 0.816）启用。
    # 可选值：
    #   ilike   （默认）原 ILIKE 关键词检索，行为完全不变；
    #   lexical 词法检索（LexicalRetriever），要求项目已构建检索索引（retrieval_chunks）；
    #   hybrid  lexical + ILIKE 合并去重（lexical 优先，按 chunk_id 去重）。
    # lexical/hybrid 下自动降级为 ILIKE 的场景：项目无检索索引、检索抛异常、
    # 或 lexical 无命中且检索策略 on_no_result == "fallback"（均记日志，不阻断问答）。
    # 策略参数（source_kinds/entity_first）按 services.retrieval_policy.get_retrieval_policy(intent)
    # 集中映射（D5）；其他未识别值按 ilike 处理。
    RETRIEVAL_MODE: str = os.getenv("RETRIEVAL_MODE", "ilike")

    # --- 日志 ---
    # DEBUG 默认关闭：会打开 DEBUG 级日志，生产环境不该默认开。
    # 本地需要追踪 SSE/LLM/抽取状态机时显式设置 DEBUG=true。
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG" if os.getenv("DEBUG", "false").lower() == "true" else "INFO")
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
    # 认证端点的第二个限流维度：同一 IP 对 /auth/login + /auth/register 的每分钟总次数。
    # 只按用户名限流时，换用户名就能绕过，撞库依然有效，所以这里独立按 IP 再限一层。
    AUTH_IP_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("AUTH_IP_RATE_LIMIT_PER_MINUTE", "20"))
    # 反向代理层数。0（默认）= 不信任 X-Forwarded-For，只用 TCP 连接的对端地址；
    # 部署在 N 层代理后面时设为 N，取 X-Forwarded-For 从右往左第 N 个地址。
    TRUSTED_PROXY_COUNT: int = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
    PROTECTED_WORKFLOW_POSITIONS: tuple = ("consistency_checker", "human_review")

    # --- LangGraph checkpointer ---
    # 默认 sqlite 持久化，保证重启后 WAITING_HUMAN 状态可被 resume 找回。
    # 可选值：sqlite | postgres | memory。memory 仅用于无文件 IO 的极端场景，
    # 重启即丢失等待中的 thread 状态。生产环境 setup 失败应 fail-fast。
    CHECKPOINTER_BACKEND: str = os.getenv("CHECKPOINTER_BACKEND", "sqlite")
    CHECKPOINTER_PATH: str = os.getenv("CHECKPOINTER_PATH", "")

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


def validate_security_config(cfg: Settings | None = None) -> None:
    """校验与密钥相关的启动配置，不合格直接抛错让进程退出。

    生产环境“缺密钥就自动生成”会把一次配置遗漏变成一次静默的数据不可解密事故，
    因此这里选择 fail-fast：JWT_SECRET 和 ENCRYPTION_KEY 都必须显式配置。
    开发/桌面端保留自动生成路径，只是打 warning，避免第一次启动就打不开。
    """
    cfg = cfg or settings

    # 配置了 ENCRYPTION_KEY 就必须是合法的 Fernet 密钥，否则加密写入会带病运行。
    configured_key = (cfg.ENCRYPTION_KEY or "").strip()
    if configured_key:
        from cryptography.fernet import Fernet

        try:
            Fernet(configured_key.encode())
        except Exception as exc:  # noqa: BLE001 - 统一转成可读的配置错误
            raise RuntimeError(
                "ENCRYPTION_KEY 不是合法的 Fernet 密钥（应为 Fernet.generate_key() 的输出）"
            ) from exc

    if not cfg.IS_PRODUCTION:
        return

    problems: list[str] = []
    if not cfg.JWT_SECRET or len(cfg.JWT_SECRET.strip()) < 32:
        problems.append("JWT_SECRET 未配置或长度小于 32")
    if not configured_key:
        problems.append("ENCRYPTION_KEY 未配置")
    if problems:
        raise RuntimeError(
            "生产环境安全配置不完整：" + "；".join(problems)
            + "。生产环境不再自动生成密钥，请显式配置环境变量后重启。"
        )


validate_security_config()
