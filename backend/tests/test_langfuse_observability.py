"""Langfuse observability integration tests."""

import sys
import types

from config.settings import settings
from observability.langfuse import (
    activate_langfuse_context,
    current_langfuse_metadata,
    current_langfuse_trace_id,
    finish_langfuse_context,
    get_langfuse_async_openai_class,
)


def _set_setting(name: str, value):
    old = getattr(settings, name)
    object.__setattr__(settings, name, value)
    return old


def test_langfuse_context_is_disabled_without_keys():
    old_enabled = _set_setting("LANGFUSE_ENABLED", False)
    try:
        ctx = activate_langfuse_context(name="generate", user_id="u1")
        assert ctx.trace_id is None
        assert current_langfuse_trace_id() is None
        assert current_langfuse_metadata({"api_key": "secret"}) is None
    finally:
        object.__setattr__(settings, "LANGFUSE_ENABLED", old_enabled)


def test_langfuse_context_sets_trace_metadata_and_filters_secrets():
    old_values = {
        "LANGFUSE_ENABLED": settings.LANGFUSE_ENABLED,
        "LANGFUSE_PUBLIC_KEY": settings.LANGFUSE_PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": settings.LANGFUSE_SECRET_KEY,
        "LANGFUSE_SAMPLE_RATE": settings.LANGFUSE_SAMPLE_RATE,
    }
    try:
        object.__setattr__(settings, "LANGFUSE_ENABLED", True)
        object.__setattr__(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
        object.__setattr__(settings, "LANGFUSE_SECRET_KEY", "sk-test")
        object.__setattr__(settings, "LANGFUSE_SAMPLE_RATE", 1.0)

        ctx = activate_langfuse_context(
            name="generate",
            user_id="user-1",
            session_id="project-1",
            tags=["novel", "full_pipeline"],
            metadata={"project_id": "project-1", "api_key": "must-not-leak"},
        )
        try:
            assert ctx.trace_id
            assert current_langfuse_trace_id() == ctx.trace_id
            metadata = current_langfuse_metadata({"llm_model": "mock", "secret": "hidden"})
            assert metadata["langfuse_trace_id"] == ctx.trace_id
            assert metadata["langfuse_user_id"] == "user-1"
            assert metadata["llm_model"] == "mock"
            assert metadata["langfuse_metadata"] == {"project_id": "project-1"}
            assert "secret" not in metadata
        finally:
            finish_langfuse_context(ctx)

        assert current_langfuse_trace_id() is None
    finally:
        for name, value in old_values.items():
            object.__setattr__(settings, name, value)


def test_langfuse_openai_wrapper_is_optional(monkeypatch):
    old_values = {
        "LANGFUSE_ENABLED": settings.LANGFUSE_ENABLED,
        "LANGFUSE_PUBLIC_KEY": settings.LANGFUSE_PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": settings.LANGFUSE_SECRET_KEY,
        "LANGFUSE_CAPTURE_PROMPTS": settings.LANGFUSE_CAPTURE_PROMPTS,
    }

    fake_langfuse = types.ModuleType("langfuse")
    fake_openai = types.ModuleType("langfuse.openai")

    class FakeAsyncOpenAI:
        pass

    fake_openai.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)
    monkeypatch.setitem(sys.modules, "langfuse.openai", fake_openai)

    try:
        object.__setattr__(settings, "LANGFUSE_ENABLED", True)
        object.__setattr__(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
        object.__setattr__(settings, "LANGFUSE_SECRET_KEY", "sk-test")
        object.__setattr__(settings, "LANGFUSE_CAPTURE_PROMPTS", True)

        assert get_langfuse_async_openai_class() is None

        ctx = activate_langfuse_context(name="generate", user_id="user-1")
        try:
            assert get_langfuse_async_openai_class() is FakeAsyncOpenAI
        finally:
            finish_langfuse_context(ctx)

        object.__setattr__(settings, "LANGFUSE_CAPTURE_PROMPTS", False)
        ctx = activate_langfuse_context(name="generate", user_id="user-1")
        try:
            assert get_langfuse_async_openai_class() is None
        finally:
            finish_langfuse_context(ctx)
    finally:
        for name, value in old_values.items():
            object.__setattr__(settings, name, value)
