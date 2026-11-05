from __future__ import annotations

import asyncio
import logging
import re

import pytest


async def _invoke_asgi(wrap, *, headers=None, method="GET", path="/"):
    messages = []
    observed = {}

    async def inner(scope, receive, send):
        from api.request_id import request_id_var

        observed["request_id"] = request_id_var.get()
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "http_version": "1.1",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    await wrap(inner)(scope, receive, send)
    return messages, observed


def _response_header(messages, name):
    response = next(item for item in messages if item["type"] == "http.response.start")
    return next(value.decode() for key, value in response["headers"] if key.lower() == name.lower())


@pytest.mark.parametrize("request_id", ["req-123", "a.b_c-9", "A" * 64])
def test_valid_request_id_is_echoed_unchanged(request_id):
    from api.request_id import RequestIdMiddleware, request_id_var

    messages, observed = asyncio.run(_invoke_asgi(
        RequestIdMiddleware, headers=[(b"x-request-id", request_id.encode())],
    ))

    assert _response_header(messages, b"x-request-id") == request_id
    assert observed["request_id"] == request_id
    assert request_id_var.get() is None


@pytest.mark.parametrize("value", ["bad id", "bad!", "A" * 65, "请求号"])
def test_invalid_request_id_is_replaced(value):
    from api.request_id import RequestIdMiddleware

    messages, observed = asyncio.run(_invoke_asgi(
        RequestIdMiddleware, headers=[(b"x-request-id", value.encode())],
    ))
    response_id = _response_header(messages, b"x-request-id")
    assert response_id == observed["request_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", response_id)


def test_missing_request_id_is_generated_and_401_keeps_header():
    from api.request_id import RequestIdMiddleware

    messages, observed = asyncio.run(_invoke_asgi(RequestIdMiddleware))
    response_id = _response_header(messages, b"x-request-id")
    assert re.fullmatch(r"[0-9a-f]{32}", response_id)
    assert response_id == observed["request_id"]
    response = next(item for item in messages if item["type"] == "http.response.start")
    assert response["status"] == 401


def test_request_id_survives_early_413(monkeypatch):
    import api.body_limit as body_limit
    from api.body_limit import RequestBodyLimitMiddleware
    from api.request_id import RequestIdMiddleware

    monkeypatch.setattr(body_limit, "resolve_body_limit", lambda method, path: (1, "1B"))
    called = []

    async def should_not_run(*args):
        called.append(True)

    messages, _ = asyncio.run(_invoke_asgi(
        lambda inner: RequestIdMiddleware(RequestBodyLimitMiddleware(should_not_run)),
        headers=[(b"content-length", b"2"), (b"x-request-id", b"before-limit")],
        method="POST",
        path="/not-a-route",
    ))
    response = next(item for item in messages if item["type"] == "http.response.start")
    assert response["status"] == 413
    assert _response_header(messages, b"x-request-id") == "before-limit"
    assert called == []


def test_request_id_context_is_copied_to_asyncio_task():
    from api.request_id import request_id_var

    async def run():
        token = request_id_var.set("inherited-request")
        try:
            child = asyncio.create_task(asyncio.sleep(0, result=request_id_var.get()))
            return await child
        finally:
            request_id_var.reset(token)

    assert asyncio.run(run()) == "inherited-request"


def test_request_id_logging_filter_sets_value_and_fallback():
    from api.request_id import request_id_var
    from config.logging_config import RequestIdFilter

    record = logging.LogRecord("test", logging.INFO, "test.py", 1, "hello", (), None)
    token = request_id_var.set("log-request")
    try:
        assert RequestIdFilter().filter(record)
        assert record.request_id == "log-request"
    finally:
        request_id_var.reset(token)

    fallback = logging.LogRecord("test", logging.INFO, "test.py", 1, "hello", (), None)
    assert RequestIdFilter().filter(fallback)
    assert fallback.request_id == "-"


def test_main_middleware_order_and_cors_exposure(monkeypatch, tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).parents[1]
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    env.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "APP_ENV": "development",
        "JWT_SECRET": "aicp-test-fake-jwt-secret-not-real",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'main.sqlite3'}",
        "UPLOAD_DIR": str(tmp_path / "uploads"),
    })
    script = (
        "import json; from main import app; "
        "print(json.dumps({'classes':[m.cls.__name__ for m in app.user_middleware],"
        "'expose':next(m.kwargs.get('expose_headers',[]) for m in app.user_middleware "
        "if m.cls.__name__=='CORSMiddleware')}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=backend, env=env,
        capture_output=True, text=True, check=True, timeout=60,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["classes"] == ["CORSMiddleware", "RequestIdMiddleware", "RequestBodyLimitMiddleware"]
    assert "X-Request-ID" in payload["expose"]
