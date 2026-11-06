"""日志与模型配置快照的密钥保护回归测试。"""

import logging
import sqlite3

from agents.checkpointer import migrate_sqlite_checkpoint_secrets
from config.logging_config import (
    SensitiveDataFilter,
    redact_sensitive_text,
    setup_logging,
)
from harness.run_manager import sanitize_model_config_snapshot
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from utils.crypto import decrypt_api_key


def test_sensitive_log_text_is_redacted():
    text = (
        "api_key='sk-example-secret-1234567890' "
        'Authorization: Bearer bearer-secret-value '
        '"api_key": "custom-secret-value"'
    )

    redacted = redact_sensitive_text(text)

    assert "sk-example-secret" not in redacted
    assert "bearer-secret-value" not in redacted
    assert "custom-secret-value" not in redacted
    assert redacted.count("[REDACTED]") >= 3


def test_sensitive_filter_redacts_formatted_log_record():
    record = logging.LogRecord(
        "openai._base_client",
        logging.DEBUG,
        __file__,
        1,
        "request config=%s",
        ({"api_key": "sk-filter-secret-123456"},),
        None,
    )

    assert SensitiveDataFilter().filter(record) is True
    assert "sk-filter-secret" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_run_model_snapshot_never_contains_key_material():
    snapshot = sanitize_model_config_snapshot(
        {
            "config_id": "cfg-1",
            "config_name": "deepseek",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-plain-secret",
            "encrypted_api_key": "fernet-ciphertext",
        }
    )

    assert snapshot == {
        "config_id": "cfg-1",
        "config_name": "deepseek",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": "https://example.invalid/v1",
        "api_key_configured": True,
    }


def test_sensitive_third_party_debug_loggers_are_forced_to_warning():
    setup_logging(log_level="DEBUG")

    assert logging.getLogger("openai").level == logging.WARNING
    assert logging.getLogger("openai._base_client").level == logging.WARNING
    assert logging.getLogger("aiosqlite").level == logging.WARNING


def test_legacy_checkpoint_key_is_encrypted_without_losing_progress(tmp_path):
    path = tmp_path / "checkpoints.sqlite"
    serde = JsonPlusSerializer()
    checkpoint = {
        "channel_values": {
            "llm_config": {
                "provider": "deepseek",
                "api_key": "sk-legacy-checkpoint-secret",
                "model": "deepseek-test",
            },
            "draft": "已经生成且等待人工审核的章节正文",
        },
        "model_config_snapshot": {
            "provider": "deepseek",
            "api_key": "sk-snapshot-secret",
            "model": "deepseek-test",
        },
    }
    value_type, blob = serde.dumps_typed(checkpoint)

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE checkpoints ("
        "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)"
    )
    conn.execute(
        "CREATE TABLE writes ("
        "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT, "
        "idx INTEGER, channel TEXT, type TEXT, value BLOB)"
    )
    conn.execute(
        "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("thread-1", "", "checkpoint-1", None, value_type, blob, b"{}"),
    )
    conn.commit()
    conn.close()

    assert migrate_sqlite_checkpoint_secrets(path) == 1
    assert migrate_sqlite_checkpoint_secrets(path) == 0

    conn = sqlite3.connect(path)
    stored_type, stored_blob = conn.execute(
        "SELECT type, checkpoint FROM checkpoints"
    ).fetchone()
    restored = serde.loads_typed((stored_type, stored_blob))
    conn.close()

    llm_config = restored["channel_values"]["llm_config"]
    assert "api_key" not in llm_config
    assert decrypt_api_key(llm_config["encrypted_api_key"]) == (
        "sk-legacy-checkpoint-secret"
    )
    assert restored["channel_values"]["draft"] == (
        "已经生成且等待人工审核的章节正文"
    )
    assert restored["model_config_snapshot"] == {
        "provider": "deepseek",
        "model": "deepseek-test",
        "api_key_configured": True,
    }
