"""集中日志配置

统一管理所有日志配置，支持：
- 从 settings 读取日志级别
- 控制台输出
- 可选的文件日志（按大小轮转）
- 抑制第三方库噪音
"""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from api.request_id import request_id_var


_SK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{8,}")
_BEARER_TOKEN_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+"
)
_QUOTED_SECRET_RE = re.compile(
    r"""(?ix)
    (["']?(?:api[_-]?key|access[_-]?token)["']?\s*[:=]\s*)
    (["'])
    .*?
    \2
    """
)


def redact_sensitive_text(value: object) -> str:
    """对日志文本中的常见 API Key/Authorization 值做兜底脱敏。"""
    text = str(value)
    text = _SK_TOKEN_RE.sub("[REDACTED]", text)
    text = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", text)
    text = _QUOTED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        text,
    )
    return text


class SensitiveDataFilter(logging.Filter):
    """在任何 handler 输出前，把格式化后的消息统一脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        return True


class RequestIdFilter(logging.Filter):
    """为每条日志填入当前请求号；非请求上下文使用 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


def setup_logging(log_level: str = "INFO", log_file: str = "", max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5, sql_echo: bool = False) -> None:
    """配置全局日志系统，应在应用启动时调用一次。

    Args:
        log_level: 日志级别，如 DEBUG / INFO / WARNING / ERROR
        log_file: 可选文件日志路径，为空则仅控制台输出
        max_bytes: 单个日志文件最大字节，默认 10MB
        backup_count: 日志文件备份数量
        sql_echo: 是否启用 SQLAlchemy engine 的 SQL 日志（对应 settings.SQL_ECHO）。
            为 False 时压到 WARNING 以减少噪音；为 True 时设为 INFO，SQL 语句才会输出。
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 避免重复添加 handler（防止热重载时重复输出）
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root_logger.handlers):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s [request_id=%(request_id)s] - %(message)s")
        )
        root_logger.addHandler(console_handler)

    # 可选文件日志（按大小轮转）
    if log_file and not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s [request_id=%(request_id)s] - %(message)s")
        )
        root_logger.addHandler(file_handler)

    for handler in root_logger.handlers:
        if not any(isinstance(item, RequestIdFilter) for item in handler.filters):
            handler.addFilter(RequestIdFilter())
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())

    # 抑制第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # openai DEBUG 会打印完整请求，aiosqlite DEBUG 会打印 checkpoint 二进制参数；
    # 两者都可能间接包含模型配置，必须无条件压到 WARNING。
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    # sqlalchemy.engine 随 SQL_ECHO：echo 开启时设 INFO 才能看到 SQL，否则压到 WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if sql_echo else logging.WARNING)
