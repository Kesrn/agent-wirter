"""任务 0.7：出站 SSRF 过滤（url_guard）与接入点回归测试。

不发任何真实网络请求：
- DNS 解析用 monkeypatch 替换 `utils.url_guard._resolve_host`；
- HTTP 请求用 `httpx.MockTransport`；
- 模型测试连接用假的 AsyncOpenAI。

覆盖任务单第四节验收条目 1-9。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from config.settings import settings
from utils import url_guard
from utils.url_guard import (
    UnsafeURLError,
    guarded_get,
    is_ip_allowed,
    validate_url,
)

# 复用 test_smoke 的 app / 内存库（同一进程只初始化一次）
from test_smoke import app, client, setup_db, _auth_headers  # noqa: F401


@contextmanager
def settings_override(**changes):
    original = {name: getattr(settings, name) for name in changes}
    try:
        for name, value in changes.items():
            object.__setattr__(settings, name, value)
        yield
    finally:
        for name, value in original.items():
            object.__setattr__(settings, name, value)


def _fake_dns(mapping: dict[str, list[str]]):
    """返回一个假的 _resolve_host：按映射查地址，未登记的主机名视为解析失败。"""

    async def _resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise OSError(f"cannot resolve {host}")
        return list(mapping[host])

    return _resolve


def _url_for(host: str, port: int | None = None, scheme: str = "http") -> str:
    """按主机名/IP 拼 URL，IPv6 字面量要加方括号。"""
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{netloc}{f':{port}' if port else ''}/"


# ---------------------------------------------------------------- 验收 1

_BLOCKED_ADDRESSES = [
    "127.0.0.1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "0.0.0.0",
    "100.64.0.1",
    "::1",
    "fc00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
]


@pytest.mark.parametrize("address", _BLOCKED_ADDRESSES)
def test_private_and_special_addresses_are_rejected(address):
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url(_url_for(address), allow_private=False))


def test_public_address_is_allowed():
    resolved = asyncio.run(validate_url(_url_for("93.184.216.34"), allow_private=False))
    assert resolved.addresses == ("93.184.216.34",)


@pytest.mark.parametrize("address", _BLOCKED_ADDRESSES)
def test_is_ip_allowed_matches_validate_url(address):
    assert is_ip_allowed(address, allow_private=False) is False


def test_ipv4_mapped_ipv6_is_normalized():
    """::ffff:127.0.0.1 必须按 IPv4 判断（否则会被当成全局地址放过）。"""
    assert is_ip_allowed("::ffff:127.0.0.1") is False
    assert is_ip_allowed("::ffff:93.184.216.34") is True


# ---------------------------------------------------------------- 验收 2


def test_domain_resolving_to_private_is_rejected(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"evil.example": ["10.1.2.3"]}))

    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url("http://evil.example/", allow_private=False))


def test_domain_with_mixed_public_and_private_is_rejected(monkeypatch):
    monkeypatch.setattr(
        url_guard,
        "_resolve_host",
        _fake_dns({"mixed.example": ["93.184.216.34", "127.0.0.1"]}),
    )

    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url("http://mixed.example/", allow_private=False))


def test_public_domain_passes(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"ok.example": ["93.184.216.34"]}))

    resolved = asyncio.run(validate_url("http://ok.example/", allow_private=False))
    assert resolved.hostname == "ok.example"
    assert resolved.addresses == ("93.184.216.34",)


@pytest.mark.parametrize("host", ["localhost", "localhost."])
def test_localhost_is_rejected(host):
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url(f"http://{host}/", allow_private=False))


# ---------------------------------------------------------------- 验收 3


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://127.0.0.1:6379/_GET",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url(url, allow_private=False))


@pytest.mark.parametrize(
    "url",
    [
        "http://good.com@127.0.0.1/",
        "http://127.0.0.1:80@evil.com/",
        "http://user:pass@example.com/",
    ],
)
def test_urls_with_userinfo_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url(url, allow_private=False))


def test_allowed_ports_are_enforced():
    with pytest.raises(UnsafeURLError):
        asyncio.run(
            validate_url("http://93.184.216.34:8080/", allow_private=False, allowed_ports={80, 443})
        )
    resolved = asyncio.run(
        validate_url("http://93.184.216.34/", allow_private=False, allowed_ports={80, 443})
    )
    assert resolved.port == 80


def test_error_message_does_not_leak_addresses():
    try:
        asyncio.run(validate_url("http://10.1.2.3/", allow_private=False))
    except UnsafeURLError as exc:
        text = str(exc)
    else:  # pragma: no cover
        pytest.fail("应当被拒绝")
    assert "10.1.2.3" not in text
    assert "http://" not in text


# ---------------------------------------------------------------- 验收 4 / 5


def _mock_transport(routes: dict[str, httpx.Response], counter: dict | None = None) -> httpx.MockTransport:
    """按 Host 头路由到预设响应，模拟第三方服务。"""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        if counter is not None:
            counter["requests"] = counter.get("requests", 0) + 1
            counter.setdefault("hosts", []).append(host)
        if host not in routes:
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"missing")
        response = routes[host]
        response.request = request
        return response

    return httpx.MockTransport(handler)


def test_redirect_to_metadata_address_is_rejected(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"start.example": ["93.184.216.34"]}))
    transport = _mock_transport(
        {
            "start.example": httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        }
    )

    with pytest.raises(UnsafeURLError):
        asyncio.run(
            guarded_get(
                "http://start.example/",
                allow_private=False,
                max_bytes=1024,
                max_redirects=3,
                transport=transport,
            )
        )


def test_too_many_redirects_is_rejected(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"loop.example": ["93.184.216.34"]}))
    transport = _mock_transport(
        {"loop.example": httpx.Response(302, headers={"location": "/again"})}
    )

    with pytest.raises(UnsafeURLError):
        asyncio.run(
            guarded_get(
                "http://loop.example/",
                allow_private=False,
                max_bytes=1024,
                max_redirects=2,
                transport=transport,
            )
        )


def test_relative_redirect_is_followed(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"site.example": ["93.184.216.34"]}))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>ok</html>"
        )

    response = asyncio.run(
        guarded_get(
            "http://site.example/start",
            allow_private=False,
            max_bytes=1024,
            max_redirects=3,
            allowed_content_types={"text/html"},
            transport=httpx.MockTransport(handler),
        )
    )

    assert response.status_code == 200
    assert "ok" in response.text


class _CountingStream(httpx.AsyncByteStream):
    """按块吐数据并计数，用来验证“超限就没再读完”。"""

    def __init__(self, total_chunks: int, chunk_size: int, counter: dict):
        self.total_chunks = total_chunks
        self.chunk_size = chunk_size
        self.counter = counter

    async def __aiter__(self):
        for _ in range(self.total_chunks):
            self.counter["read"] += self.chunk_size
            yield b"a" * self.chunk_size


def test_oversized_response_stops_reading(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"big.example": ["93.184.216.34"]}))
    counter = {"read": 0}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=_CountingStream(50, 1024, counter),
        )
    )

    with pytest.raises(UnsafeURLError):
        asyncio.run(
            guarded_get(
                "http://big.example/",
                allow_private=False,
                max_bytes=4096,
                allowed_content_types={"text/html"},
                transport=transport,
            )
        )

    assert counter["read"] <= 4096 + 1024, f"实际读了 {counter['read']} 字节"


def test_content_type_outside_whitelist_is_rejected(monkeypatch):
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"api.example": ["93.184.216.34"]}))
    transport = _mock_transport(
        {"api.example": httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")}
    )

    with pytest.raises(UnsafeURLError):
        asyncio.run(
            guarded_get(
                "http://api.example/",
                allow_private=False,
                max_bytes=1024,
                allowed_content_types={"text/html", "application/json"},
                transport=transport,
            )
        )


def test_request_is_pinned_to_validated_ip_with_host_header(monkeypatch):
    """连接用的是解析出的 IP，Host 头仍是原主机名。"""
    monkeypatch.setattr(url_guard, "_resolve_host", _fake_dns({"pinned.example": ["93.184.216.34"]}))
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"ok")

    asyncio.run(
        guarded_get(
            "https://pinned.example/page?x=1",
            allow_private=False,
            max_bytes=1024,
            allowed_content_types={"text/html"},
            transport=httpx.MockTransport(handler),
        )
    )

    assert seen["url"] == "https://93.184.216.34/page?x=1"
    assert seen["host"] == "pinned.example"
    assert seen["sni"] == "pinned.example"


# ---------------------------------------------------------------- 验收 6


def test_allow_private_permits_loopback_but_not_metadata():
    resolved = asyncio.run(
        validate_url("http://127.0.0.1:11434/v1", allow_private=True)
    )
    assert resolved.port == 11434

    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url("http://169.254.169.254/v1", allow_private=True))

    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_url("file:///etc/passwd", allow_private=True))


# ---------------------------------------------------------------- 验收 7


def test_page_fetch_never_allows_private_even_when_setting_is_true(monkeypatch):
    """网页正文抓取固定 allow_private=False，不受 ALLOW_PRIVATE_LLM_ENDPOINTS 影响。"""
    from services import web_search

    captured: dict = {}

    async def fake_guarded_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        raise UnsafeURLError("目标地址不被允许")

    monkeypatch.setattr(web_search, "guarded_get", fake_guarded_get)
    item = {"url": "http://127.0.0.1:8080/admin", "title": "内网页", "snippet": ""}

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=True):
        result = asyncio.run(web_search._fetch_page_snippet(item, "关键词"))

    assert captured["allow_private"] is False
    assert captured["allowed_ports"] == {80, 443}
    # 被拒绝只跳过该页，原 item 不变、也不会被标成 fetched
    assert result == item
    assert "fetched" not in result


# ---------------------------------------------------------------- 验收 8

_MODELS_URL = "/api/llm-settings/models"
_CONFIGS_URL = "/api/llm-settings/configs"


def _create_project(headers: dict[str, str], title: str = "SSRF 测试项目") -> str:
    response = client.post("/api/projects", json={"title": title}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("base_url", ["http://169.254.169.254/v1", "http://127.0.0.1:11434/v1"])
def test_save_config_rejects_private_base_url_in_production(base_url):
    headers = _auth_headers("ssrf_save_a", "ssrfpass")

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=False):
        response = client.post(
            _CONFIGS_URL,
            json={"name": "私网地址", "provider": "openai", "api_key": "sk-test", "base_url": base_url},
            headers=headers,
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "模型服务地址不被允许"}


def test_save_config_allows_private_base_url_when_enabled():
    headers = _auth_headers("ssrf_save_b", "ssrfpass")

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=True):
        response = client.post(
            _CONFIGS_URL,
            json={
                "name": "本机 Ollama",
                "provider": "openai",
                "api_key": "sk-test",
                "base_url": "http://127.0.0.1:11434/v1",
            },
            headers=headers,
        )

    assert response.status_code == 201, response.text


@pytest.mark.parametrize("base_url", ["http://169.254.169.254/v1", "http://127.0.0.1:11434/v1"])
def test_test_connection_rejects_private_base_url_in_production(base_url):
    headers = _auth_headers("ssrf_conn_a", "ssrfpass")

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=False):
        response = client.post(
            _MODELS_URL,
            json={"provider": "openai", "api_key": "sk-test", "base_url": base_url},
            headers=headers,
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "模型服务地址不被允许"}


def test_test_connection_allows_private_when_enabled_and_uses_no_redirect_client(monkeypatch):
    """ALLOW_PRIVATE_LLM_ENDPOINTS=true 时通过校验；SDK 必须拿到关闭重定向的 http_client。"""
    import openai

    captured: dict = {}

    class _FakeModels:
        async def list(self):
            return SimpleNamespace(data=[SimpleNamespace(id="llama3", owned_by="local")])

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.models = _FakeModels()

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)
    headers = _auth_headers("ssrf_conn_b", "ssrfpass")

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=True):
        response = client.post(
            _MODELS_URL,
            json={
                "provider": "openai",
                "api_key": "sk-test",
                "base_url": "http://127.0.0.1:11434/v1",
            },
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert response.json() == [{"id": "llama3", "owned_by": "local"}]
    assert str(captured["base_url"]) == "http://127.0.0.1:11434/v1"
    assert captured["timeout"] == 5.0
    assert captured["http_client"].follow_redirects is False
    assert captured.get("closed") is True


# ---------------------------------------------------------------- 验收 9


def test_knowledge_ask_rejects_private_web_base_url():
    headers = _auth_headers("ssrf_ask", "ssrfpass")
    project_id = _create_project(headers)

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=False):
        response = client.post(
            f"/api/projects/{project_id}/knowledge/ask",
            json={"question": "随便问问", "web_base_url": "http://169.254.169.254/search"},
            headers=headers,
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "搜索服务地址不被允许"}


def test_knowledge_ask_accepts_public_web_base_url(monkeypatch):
    """公网地址通过校验（后续问答链路被替换，不发真实请求）。"""
    from services import knowledge_source

    # 不查真实 DNS：把公网域名解析到固定公网地址
    monkeypatch.setattr(
        url_guard, "_resolve_host", _fake_dns({"search.example.com": ["93.184.216.34"]})
    )

    async def fake_ask(*args, **kwargs):
        return {"answer": "ok", "citations": [], "web_base_url": kwargs.get("web_base_url")}

    monkeypatch.setattr(knowledge_source, "ask_knowledge_question", fake_ask)
    headers = _auth_headers("ssrf_ask_ok", "ssrfpass")
    project_id = _create_project(headers)

    with settings_override(ALLOW_PRIVATE_LLM_ENDPOINTS=False):
        response = client.post(
            f"/api/projects/{project_id}/knowledge/ask",
            json={"question": "随便问问", "web_base_url": "https://search.example.com/api"},
            headers=headers,
        )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- 桌面端开关


def test_desktop_server_enables_private_llm_endpoints(monkeypatch, tmp_path):
    from desktop_server import configure_desktop_environment

    saved = {key: os.environ.get(key) for key in ("ALLOW_PRIVATE_LLM_ENDPOINTS", "JWT_SECRET")}
    monkeypatch.setenv("AI_CREATIVE_DESKTOP_DATA_DIR", str(tmp_path / "desktop-data"))
    os.environ.pop("ALLOW_PRIVATE_LLM_ENDPOINTS", None)
    try:
        configure_desktop_environment()
        assert os.environ["ALLOW_PRIVATE_LLM_ENDPOINTS"] == "true"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
