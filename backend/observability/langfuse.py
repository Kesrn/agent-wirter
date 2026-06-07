"""Optional Langfuse tracing helpers.

The rest of the app should be able to run without Langfuse installed or
configured. This module keeps all SDK imports lazy and swallows observability
failures so tracing never breaks generation.
"""

from __future__ import annotations

import contextvars
import logging
import os
import random
import uuid
from dataclasses import dataclass
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("langfuse_trace_id", default=None)
_metadata_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("langfuse_metadata", default={})


@dataclass
class LangfuseContext:
    trace_id: str | None
    metadata_token: contextvars.Token | None = None
    trace_token: contextvars.Token | None = None


def langfuse_status() -> dict[str, Any]:
    """Return non-sensitive Langfuse runtime status for logs and diagnostics."""
    public_key_configured = bool(settings.LANGFUSE_PUBLIC_KEY)
    secret_key_configured = bool(settings.LANGFUSE_SECRET_KEY)
    sample_rate = max(0.0, min(1.0, settings.LANGFUSE_SAMPLE_RATE))

    reason = None
    if not settings.LANGFUSE_ENABLED:
        reason = "LANGFUSE_ENABLED=false"
    elif not public_key_configured or not secret_key_configured:
        reason = "missing public or secret key"
    elif sample_rate <= 0:
        reason = "LANGFUSE_SAMPLE_RATE=0"

    return {
        "enabled": settings.LANGFUSE_ENABLED,
        "ready": reason is None,
        "reason": reason,
        "host": settings.LANGFUSE_HOST,
        "sample_rate": sample_rate,
        "capture_prompts": settings.LANGFUSE_CAPTURE_PROMPTS,
        "public_key_configured": public_key_configured,
        "secret_key_configured": secret_key_configured,
    }


def log_langfuse_startup_status() -> None:
    status = langfuse_status()
    if not status["enabled"]:
        logger.info("Langfuse disabled: reason=%s", status["reason"])
        return
    if not status["ready"]:
        logger.warning(
            "Langfuse enabled but not ready: reason=%s host=%s sample_rate=%s capture_prompts=%s public_key_configured=%s secret_key_configured=%s",
            status["reason"],
            status["host"],
            status["sample_rate"],
            status["capture_prompts"],
            status["public_key_configured"],
            status["secret_key_configured"],
        )
        return
    logger.info(
        "Langfuse enabled: host=%s sample_rate=%s capture_prompts=%s",
        status["host"],
        status["sample_rate"],
        status["capture_prompts"],
    )


def _langfuse_keys_configured() -> bool:
    if not settings.LANGFUSE_ENABLED:
        return False
    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        return False
    return True


def langfuse_configured() -> bool:
    if not _langfuse_keys_configured():
        return False
    sample_rate = max(0.0, min(1.0, settings.LANGFUSE_SAMPLE_RATE))
    if sample_rate <= 0:
        return False
    return random.random() <= sample_rate


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        lowered = key.lower()
        if "api_key" in lowered or "authorization" in lowered or "secret" in lowered or "token" in lowered:
            continue
        if value is None:
            continue
        clean[key] = value
    return clean


def _trace_log_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    safe = _safe_metadata(metadata)
    allowed_keys = ("project_id", "chapter_id", "document_id", "mode", "action", "endpoint")
    return {key: safe[key] for key in allowed_keys if key in safe}


def activate_langfuse_context(
    *,
    name: str,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> LangfuseContext:
    """Create a request-level Langfuse context for downstream LLM calls."""
    if not langfuse_configured():
        return LangfuseContext(trace_id=None)

    trace_id = uuid.uuid4().hex
    langfuse_metadata: dict[str, Any] = {
        "langfuse_trace_id": trace_id,
        "langfuse_trace_name": name,
    }
    if user_id:
        langfuse_metadata["langfuse_user_id"] = str(user_id)
    if session_id:
        langfuse_metadata["langfuse_session_id"] = session_id
    if tags:
        langfuse_metadata["langfuse_tags"] = tags
    safe = _safe_metadata(metadata)
    if safe:
        langfuse_metadata["langfuse_metadata"] = safe

    logger.info(
        "Langfuse trace activated: trace_id=%s name=%s user_id=%s session_id=%s tags=%s metadata=%s",
        trace_id,
        name,
        user_id,
        session_id,
        tags or [],
        _trace_log_metadata(metadata),
    )

    return LangfuseContext(
        trace_id=trace_id,
        metadata_token=_metadata_var.set(langfuse_metadata),
        trace_token=_trace_id_var.set(trace_id),
    )


def finish_langfuse_context(context: LangfuseContext) -> None:
    if context.metadata_token is not None:
        _metadata_var.reset(context.metadata_token)
    if context.trace_token is not None:
        _trace_id_var.reset(context.trace_token)
    if context.trace_id:
        try:
            client = get_langfuse_client()
            if client and hasattr(client, "flush"):
                client.flush()
        except Exception:
            logger.debug("Langfuse flush failed", exc_info=True)


def current_langfuse_trace_id() -> str | None:
    return _trace_id_var.get()


def current_langfuse_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not settings.LANGFUSE_ENABLED:
        return None
    base = dict(_metadata_var.get() or {})
    if extra:
        base.update(_safe_metadata(extra))
    return base or None


def get_langfuse_client():
    """Return the Langfuse client when available, otherwise None."""
    if not settings.LANGFUSE_ENABLED:
        return None
    try:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.LANGFUSE_PUBLIC_KEY)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.LANGFUSE_SECRET_KEY)
        os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_HOST)
        from langfuse import get_client

        return get_client()
    except Exception:
        logger.debug("Langfuse client unavailable", exc_info=True)
        return None


def get_langfuse_async_openai_class():
    """Return Langfuse's AsyncOpenAI wrapper when configured and installed."""
    if not _langfuse_keys_configured() or not settings.LANGFUSE_CAPTURE_PROMPTS or not current_langfuse_trace_id():
        return None
    try:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.LANGFUSE_PUBLIC_KEY)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.LANGFUSE_SECRET_KEY)
        os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_HOST)
        from langfuse.openai import AsyncOpenAI

        return AsyncOpenAI
    except Exception:
        logger.warning("Langfuse OpenAI wrapper unavailable; continuing without tracing", exc_info=True)
        return None
