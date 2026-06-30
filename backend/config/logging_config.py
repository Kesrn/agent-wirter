"""集中日志配置

统一管理所有日志配置，支持：
- 从 settings 读取日志级别
- 控制台输出
- 可选的文件日志（按大小轮转）
- 抑制第三方库噪音
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


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
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
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
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
        )
        root_logger.addHandler(file_handler)

    # 抑制第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # sqlalchemy.engine 随 SQL_ECHO：echo 开启时设 INFO 才能看到 SQL，否则压到 WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if sql_echo else logging.WARNING)
