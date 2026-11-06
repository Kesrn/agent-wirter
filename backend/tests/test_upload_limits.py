"""任务 0.6：请求体大小限制（边读边数）与上传内容校验。

覆盖任务单第四节验收条目 1-9。测试复用 test_smoke 已经搭好的 app 与内存数据库，
避免重复初始化（同 test_workflow_resume_after_restart.py 的做法）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import uuid

import httpx
import pytest

# test_smoke 在导入时设置 CHECKPOINTER_BACKEND=memory、打 SQLite 类型补丁并建好 app；
# setup_db 是 session 级 autouse fixture，导入即生效。
from test_smoke import app, client, setup_db, _auth_headers  # noqa: F401

from api.body_limit import (
    MULTIPART_OVERHEAD_BYTES,
    RequestBodyLimitMiddleware,
    resolve_body_limit,
)
from config.settings import settings

_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000154a24f6f000000"
    "0049454e44ae426082"
)

_MEDIA_HEADERS = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff\xe0",
    "image/gif": b"GIF89a",
    "image/webp": b"RIFF\x24\x00\x00\x00WEBP",
}


def _create_project(headers: dict[str, str], title: str = "上传限制测试") -> str:
    response = client.post("/api/projects", json={"title": title}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
def upload_dir(tmp_path):
    """把 UPLOAD_DIR 指到 tmp_path，避免写到真实 backend/uploads。"""
    original = settings.UPLOAD_DIR
    object.__setattr__(settings, "UPLOAD_DIR", str(tmp_path))
    try:
        yield tmp_path
    finally:
        object.__setattr__(settings, "UPLOAD_DIR", original)


@pytest.fixture
def small_limits():
    """把三个上限调小，便于用几十 KB 验证边界，不必真的造 50MB 文件。"""

    class _Restore:
        def __init__(self) -> None:
            self.saved: dict[str, int] = {}

        def set(self, **values: int) -> None:
            for name, value in values.items():
                self.saved.setdefault(name, getattr(settings, name))
                object.__setattr__(settings, name, value)

    restore = _Restore()
    try:
        yield restore
    finally:
        for name, value in restore.saved.items():
            object.__setattr__(settings, name, value)


# ---------------------------------------------------------------- 验收 1


def test_image_upload_over_content_length_returns_413_and_writes_no_file(upload_dir):
    headers = _auth_headers("bodylimit_a", "limitpass")
    project_id = _create_project(headers)
    body = _MEDIA_HEADERS["image/png"] + b"0" * (settings.MAX_UPLOAD_BYTES + 1 - 8)

    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=body,
        headers={**headers, "Content-Type": "image/png"},
    )

    assert response.status_code == 413
    assert not (upload_dir / "projects" / project_id / "images").exists()


# ---------------------------------------------------------------- 验收 2


def test_chunked_image_upload_stops_reading_after_limit(upload_dir):
    """不带 Content-Length 的 60MB 分块请求：413，且服务端只多读一个分块。"""
    headers = _auth_headers("bodylimit_b", "limitpass")
    project_id = _create_project(headers)
    chunk_size = 1024 * 1024
    limit, _ = resolve_body_limit("POST", f"/api/projects/{project_id}/assets/images")
    consumed = {"bytes": 0}

    async def body_stream():
        first = True
        sent = 0
        while sent < 60 * chunk_size:
            chunk = (_MEDIA_HEADERS["image/png"] if first else b"") + b"a" * (chunk_size - 8)
            first = False
            consumed["bytes"] += len(chunk)
            sent += chunk_size
            yield chunk

    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            return await ac.post(
                f"/api/projects/{project_id}/assets/images",
                content=body_stream(),
                headers={**headers, "Content-Type": "image/png"},
            )

    response = asyncio.run(send_request())

    assert response.status_code == 413
    assert consumed["bytes"] <= limit + chunk_size, (
        f"服务端读了 {consumed['bytes']} 字节，上限 {limit} + 一个分块 {chunk_size}"
    )
    assert consumed["bytes"] < 60 * chunk_size
    assert not (upload_dir / "projects" / project_id / "images").exists()


# ---------------------------------------------------------------- 验收 3


def test_image_upload_exactly_at_limit_is_ok_and_one_byte_over_is_413(upload_dir):
    headers = _auth_headers("bodylimit_c", "limitpass")
    project_id = _create_project(headers)
    exact = _MEDIA_HEADERS["image/png"] + b"0" * (settings.MAX_UPLOAD_BYTES - 8)

    ok = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=exact,
        headers={**headers, "Content-Type": "image/png"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["size"] == settings.MAX_UPLOAD_BYTES

    over = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=exact + b"0",
        headers={**headers, "Content-Type": "image/png"},
    )
    assert over.status_code == 413


# ---------------------------------------------------------------- 验收 4


def test_txt_import_over_limit_returns_413(small_limits):
    small_limits.set(MAX_TXT_IMPORT_BYTES=4096)
    headers = _auth_headers("bodylimit_d", "limitpass")

    response = client.post(
        "/api/projects/import-txt",
        headers=headers,
        data={"title": "超长文本"},
        files={"file": ("big.txt", b"a" * 5000, "text/plain")},
    )

    assert response.status_code == 413


def test_txt_import_within_limit_still_works(small_limits):
    small_limits.set(MAX_TXT_IMPORT_BYTES=4096)
    headers = _auth_headers("bodylimit_e", "limitpass")
    content = "第一章 开端\n林澈来到旧城。\n\n第二章 转折\n档案突然出现。".encode("utf-8")

    response = client.post(
        "/api/projects/import-txt",
        headers=headers,
        data={"title": "正常导入"},
        files={"file": ("ok.txt", content, "text/plain")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["import_meta"]["chapter_count"] == 2


def test_knowledge_upload_over_limit_returns_413(small_limits):
    small_limits.set(KNOWLEDGE_UPLOAD_MAX_BYTES=4096)
    headers = _auth_headers("bodylimit_f", "limitpass")
    project_id = _create_project(headers)

    response = client.post(
        f"/api/projects/{project_id}/knowledge/upload",
        headers=headers,
        files={"file": ("big.txt", b"a" * 5000, "text/plain")},
        data={"title": "超长资料"},
    )

    assert response.status_code == 413


def test_knowledge_upload_within_limit_still_works(small_limits):
    small_limits.set(KNOWLEDGE_UPLOAD_MAX_BYTES=4096)
    headers = _auth_headers("bodylimit_g", "limitpass")
    project_id = _create_project(headers)

    response = client.post(
        f"/api/projects/{project_id}/knowledge/upload",
        headers=headers,
        files={"file": ("ok.txt", "这是一段资料。".encode("utf-8"), "text/plain")},
        data={"title": "正常资料"},
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- 验收 5


def test_generic_endpoint_over_max_request_body_returns_413(small_limits):
    small_limits.set(MAX_REQUEST_BODY_BYTES=1024)
    headers = _auth_headers("bodylimit_h", "limitpass")

    response = client.post(
        "/api/projects",
        content=json.dumps({"title": "x" * 2000}),
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "请求体过大" in response.json()["detail"]


@pytest.mark.parametrize("content_length", ["abc", "-5", "1e9", ""])
def test_invalid_content_length_returns_400(content_length):
    """Content-Length 不是合法非负整数时直接 400（不经应用代码）。"""
    captured: list[dict] = []

    async def dummy_app(scope, receive, send):  # pragma: no cover - 不该被调用
        raise AssertionError("请求体非法时不应该进入应用")

    async def send(message):
        captured.append(message)

    async def receive():  # pragma: no cover
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/projects",
        "headers": [(b"content-length", content_length.encode())],
    }

    asyncio.run(RequestBodyLimitMiddleware(dummy_app)(scope, receive, send))

    assert captured[0]["status"] == 400
    body = json.loads(b"".join(m.get("body", b"") for m in captured[1:]))
    assert "Content-Length" in body["detail"]


# ---------------------------------------------------------------- 验收 6


def test_413_response_carries_cors_headers(upload_dir):
    headers = _auth_headers("bodylimit_i", "limitpass")
    project_id = _create_project(headers)
    body = _MEDIA_HEADERS["image/png"] + b"0" * (settings.MAX_UPLOAD_BYTES + 1 - 8)

    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=body,
        headers={
            **headers,
            "Content-Type": "image/png",
            "Origin": "http://localhost:5173",
        },
    )

    assert response.status_code == 413
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-credentials" in response.headers


# ---------------------------------------------------------------- 验收 7


def test_get_requests_pass_through_untouched():
    """GET（含 SSE）不经过任何包装：receive/send 原样传给应用。"""
    seen: dict[str, object] = {}

    async def dummy_app(scope, receive, send):
        seen["receive"] = receive
        seen["send"] = send

    async def receive():  # pragma: no cover
        return {"type": "http.request"}

    async def send(message):  # pragma: no cover
        return None

    scope = {"type": "http", "method": "GET", "path": "/api/health", "headers": []}
    middleware = RequestBodyLimitMiddleware(dummy_app)
    asyncio.run(middleware(scope, receive, send))

    assert seen["receive"] is receive
    assert seen["send"] is send


# ---------------------------------------------------------------- 验收 8


@pytest.mark.parametrize(
    "content_type,payload",
    [
        ("image/svg+xml", b"<svg onload=alert(1)></svg>"),
        ("text/html", b"<html><script>alert(1)</script></html>"),
        ("text/plain", b"just text pretending to be an image"),
        ("image/png", b"not really a png at all"),
        # 声明 JPEG，实际是 PNG 字节：扩展名与魔数不一致
        ("image/jpeg", _PNG_1X1),
    ],
)
def test_image_upload_rejects_bad_types_with_400(upload_dir, content_type, payload):
    headers = _auth_headers("bodylimit_j", "limitpass")
    project_id = _create_project(headers)

    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=payload,
        headers={**headers, "Content-Type": content_type},
    )

    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "content_type,magic",
    [
        ("image/png", _MEDIA_HEADERS["image/png"]),
        ("image/jpeg", _MEDIA_HEADERS["image/jpeg"]),
        ("image/gif", _MEDIA_HEADERS["image/gif"]),
        ("image/webp", _MEDIA_HEADERS["image/webp"]),
    ],
)
def test_image_upload_accepts_whitelisted_types(upload_dir, content_type, magic):
    headers = _auth_headers("bodylimit_k", "limitpass")
    project_id = _create_project(headers)
    expected_ext = "jpg" if content_type == "image/jpeg" else content_type.split("/")[1]

    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=magic + b"payload",
        headers={**headers, "Content-Type": content_type},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["url"] == f"/media/projects/{project_id}/images/{data['filename']}"
    assert data["filename"].endswith(f".{expected_ext}")
    assert set(data) == {"url", "filename", "content_type", "size"}


def test_uploaded_image_can_be_fetched_through_media_route(upload_dir):
    """F4：上传拿到的 url 再用 Bearer 取回，字节完全一致（0.5 的 /media 与上传格式对得上）。"""
    headers = _auth_headers("bodylimit_l", "limitpass")
    project_id = _create_project(headers)

    upload = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=_PNG_1X1,
        headers={**headers, "Content-Type": "image/png"},
    )
    assert upload.status_code == 200, upload.text
    url = upload.json()["url"]

    fetched = client.get(url, headers=headers)

    assert fetched.status_code == 200
    assert fetched.content == _PNG_1X1
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------- 验收 9


def test_upload_filename_is_server_generated_and_confined(upload_dir):
    headers = _auth_headers("bodylimit_m", "limitpass")
    project_id = _create_project(headers)

    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        content=_PNG_1X1,
        headers={
            **headers,
            "Content-Type": "image/png",
            "X-Filename": "../../../etc/passwd.png",
            "Content-Disposition": 'attachment; filename="../../x.png"',
        },
    )

    assert response.status_code == 200, response.text
    filename = response.json()["filename"]
    assert re.fullmatch(r"[0-9a-f]{32}\.png", filename), filename

    images_dir = upload_dir / "projects" / project_id / "images"
    assert (images_dir / filename).is_file()
    # 没有在 images/ 之外落任何文件
    outside = [p for p in upload_dir.rglob("*") if p.is_file() and p.parent != images_dir]
    assert outside == []
