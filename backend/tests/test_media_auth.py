"""任务 0.5：/media 鉴权回归测试。

覆盖任务单第四节验收条目：
1. 未登录请求存在的图片 → 401；
2. 用户 B 请求用户 A 项目的图片 → 404；
3. 用户 A 带 media_token Cookie → 200 + 正确 Content-Type + 三个响应头；
4. 用户 A 带 Authorization: Bearer → 200（桌面端 file:// 场景）；
5. 路径穿越/软链到目录外/非 images 子目录/目录本身 → 404；
6. .svg / .html 等非白名单类型 → 404；
7. media_token 不能调 /api/auth/me（Cookie 或 Bearer 两种方式都 401）；
8. Set-Cookie 属性正确，logout 后 Cookie 被清除；
9. 项目删除后 → 404。

上传目录一律用 tmp_path，不写真实 backend/uploads。
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.auth import router as auth_router
from api.media import router as media_router
from config.settings import settings
from db.session import get_db
from models.project import Project
from models.user import User

# 一张最小 PNG（内容不重要，FileResponse 只负责原样流式返回）
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000154a24f6f000000"
    "0049454e44ae426082"
)


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


class MediaEnv:
    """一个测试用的最小环境：两个用户、一个项目、一张图、若干测试客户端。"""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.uploads = tmp_path / "uploads"
        self.db_path = tmp_path / "media.sqlite3"
        self.url = f"sqlite+aiosqlite:///{self.db_path}"
        self.engine = None
        self.factory = None
        self.client = None
        self.app = None
        self.project_id: str = ""
        self.image_rel: str = ""
        self.token_a: str = ""
        self.token_b: str = ""

    # --- 环境搭建 ---
    def setup(self) -> "MediaEnv":
        async def _create() -> None:
            engine = create_async_engine(self.url)
            async with engine.begin() as conn:
                await conn.run_sync(User.__table__.create, checkfirst=True)
                await conn.run_sync(Project.__table__.create, checkfirst=True)
            await engine.dispose()

        asyncio.run(_create())
        self.engine = create_async_engine(self.url)
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        self.app = FastAPI()
        self.app.include_router(auth_router)
        self.app.include_router(media_router)

        factory = self.factory

        async def _override_db():
            async with factory() as session:
                yield session

        self.app.dependency_overrides[get_db] = _override_db

        self.client = TestClient(self.app, client=("203.0.113.200", 5000))

        # 两个用户：A 上传统建项目，B 只用来验证越权
        self.token_a, id_a = self._register_and_login("owner-a", "owner-a-pw", "203.0.113.201")
        self.token_b, _ = self._register_and_login("other-b", "other-b-pw", "203.0.113.202")

        self.project_id = str(uuid.uuid4())
        self.image_rel = "images/pic.png"
        self._insert_project(self.project_id, id_a)
        self._write_file(self.image_rel, _PNG_BYTES)
        return self

    def _register_and_login(self, username: str, password: str, client_ip: str) -> tuple[str, str]:
        client = TestClient(self.app, client=(client_ip, 5000))
        assert client.post(
            "/api/auth/register", json={"username": username, "password": password}
        ).status_code == 200
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        return response.json()["access_token"], response.json()["user"]["id"]

    def _insert_project(self, project_id: str, owner_id: str) -> None:
        async def _do() -> None:
            async with self.factory() as db:
                db.add(
                    Project(
                        id=uuid.UUID(project_id),
                        title="媒体鉴权测试项目",
                        owner_id=owner_id,
                    )
                )
                await db.commit()

        asyncio.run(_do())

    def _write_file(self, relative: str, data: bytes) -> Path:
        path = self.uploads / "projects" / self.project_id / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    # --- 便捷方法 ---
    def media_url(self, subpath: str) -> str:
        return f"/media/projects/{self.project_id}/{subpath}"

    def owner_bearer_client(self, client_ip: str = "203.0.113.210") -> TestClient:
        """只带 Authorization 头、没有 Cookie 的客户端（模拟桌面端）。"""
        return TestClient(self.app, client=(client_ip, 5000))

    def owner_cookie_client(self, client_ip: str = "203.0.113.211") -> TestClient:
        """用 Cookie 通过登录、之后只靠 Cookie 访问 /media 的客户端（模拟网页 <img>）。"""
        client = TestClient(self.app, client=(client_ip, 5000))
        response = client.post(
            "/api/auth/login", json={"username": "owner-a", "password": "owner-a-pw"}
        )
        assert response.status_code == 200, response.text
        client.headers.pop("Authorization", None)
        return client

    def delete_project_row(self) -> None:
        async def _do() -> None:
            async with self.factory() as db:
                await db.execute(delete(Project).where(Project.id == uuid.UUID(self.project_id)))
                await db.commit()

        asyncio.run(_do())


@pytest.fixture
def media_env(tmp_path):
    env = MediaEnv(tmp_path)
    with settings_override(UPLOAD_DIR=str(tmp_path / "uploads"), COOKIE_SECURE=False):
        env.setup()
        try:
            yield env
        finally:
            asyncio.run(env.engine.dispose())


def _count_users(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("select count(*) from users").fetchone()[0]


# ---------------------------------------------------------------- 验收 1


def test_anonymous_request_gets_401(media_env):
    anonymous = TestClient(media_env.app, client=("203.0.113.220", 5000))

    response = anonymous.get(media_env.media_url(media_env.image_rel))

    assert response.status_code == 401


# ---------------------------------------------------------------- 验收 2


def test_other_user_gets_404(media_env):
    other = media_env.owner_bearer_client("203.0.113.221")

    response = other.get(
        media_env.media_url(media_env.image_rel),
        headers={"Authorization": f"Bearer {media_env.token_b}"},
    )

    assert response.status_code == 404


# ---------------------------------------------------------------- 验收 3


def test_owner_with_cookie_gets_image_and_headers(media_env):
    client = media_env.owner_cookie_client()
    assert "media_token" in client.cookies

    response = client.get(media_env.media_url(media_env.image_rel))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.content == _PNG_BYTES


# ---------------------------------------------------------------- 验收 4


def test_owner_with_bearer_gets_image(media_env):
    """桌面端 file:// 场景：没有 Cookie，只有 Authorization 头。"""
    client = media_env.owner_bearer_client()

    response = client.get(
        media_env.media_url(media_env.image_rel),
        headers={"Authorization": f"Bearer {media_env.token_a}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_bearer_token_for_deleted_user_is_401(media_env):
    """Bearer 路径也要确认用户仍然存在（与 get_current_user 一致）。"""
    token = media_env.token_a

    async def _delete_user() -> None:
        async with media_env.factory() as db:
            await db.execute(delete(User))
            await db.commit()

    asyncio.run(_delete_user())

    response = media_env.owner_bearer_client().get(
        media_env.media_url(media_env.image_rel),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------- 验收 5


@pytest.mark.parametrize(
    "subpath",
    [
        "images/../pic.png",
        "images/../../../../etc/passwd",
        "images/..%2Fpic.png",
        "images/%2e%2e%2ficon.png",
        "images/....//pic.png",
        "images/.hidden.png",
        "images/sub/pic.png",
        "images",
        "other/pic.png",
        "",
    ],
)
def test_traversal_and_layout_violations_get_404(media_env, subpath):
    client = media_env.owner_bearer_client("203.0.113.230")
    headers = {"Authorization": f"Bearer {media_env.token_a}"}
    url = f"/media/projects/{media_env.project_id}/{subpath}"

    response = client.get(url, headers=headers)

    assert response.status_code == 404, f"{subpath!r} 应该 404，实际 {response.status_code}"


def test_symlink_pointing_outside_upload_dir_gets_404(media_env):
    outside = media_env.tmp_path / "outside-secret.png"
    outside.write_bytes(_PNG_BYTES)
    link = media_env.uploads / "projects" / media_env.project_id / "images" / "link.png"
    link.symlink_to(outside)

    response = media_env.owner_bearer_client("203.0.113.231").get(
        media_env.media_url("images/link.png"),
        headers={"Authorization": f"Bearer {media_env.token_a}"},
    )

    assert response.status_code == 404


def test_symlink_inside_project_dir_still_served(media_env):
    """指向项目目录内的软链接不算越界，避免把正常部署误杀。"""
    target = media_env._write_file("images/real.png", _PNG_BYTES)
    link = media_env.uploads / "projects" / media_env.project_id / "images" / "alias.png"
    link.symlink_to(target)

    response = media_env.owner_bearer_client("203.0.113.232").get(
        media_env.media_url("images/alias.png"),
        headers={"Authorization": f"Bearer {media_env.token_a}"},
    )

    assert response.status_code == 200


# ---------------------------------------------------------------- 验收 6


@pytest.mark.parametrize("filename", ["icon.svg", "page.html", "evil.js", "note.txt", "noext"])
def test_non_whitelisted_types_get_404(media_env, filename):
    media_env._write_file(f"images/{filename}", b"<svg onload=alert(1)></svg>")

    response = media_env.owner_bearer_client("203.0.113.240").get(
        media_env.media_url(f"images/{filename}"),
        headers={"Authorization": f"Bearer {media_env.token_a}"},
    )

    assert response.status_code == 404


# ---------------------------------------------------------------- 验收 7


def test_media_token_cannot_call_normal_api(media_env):
    client = media_env.owner_cookie_client()
    media_token_value = client.cookies.get("media_token")
    assert media_token_value

    # 只带 Cookie 调普通接口：没有 Authorization 头 → 401
    only_cookie = client.get("/api/auth/me")
    assert only_cookie.status_code == 401

    # 把 media_token 当登录 token 用（Authorization: Bearer）→ 401
    as_login_token = media_env.owner_bearer_client("203.0.113.241").get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {media_token_value}"},
    )
    assert as_login_token.status_code == 401


# ---------------------------------------------------------------- 验收 8


def test_login_sets_media_cookie_with_expected_attributes(media_env):
    client = TestClient(media_env.app, client=("203.0.113.250", 5000))

    response = client.post(
        "/api/auth/login", json={"username": "owner-a", "password": "owner-a-pw"}
    )

    assert response.status_code == 200
    # 响应字段保持不变（Set-Cookie 不影响响应体）
    assert set(response.json()) == {"access_token", "token_type", "user"}

    set_cookie = response.headers["set-cookie"]
    assert "media_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/media" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()
    assert f"Max-Age={settings.JWT_EXPIRE_HOURS * 3600}" in set_cookie
    assert "Secure" not in set_cookie


def test_login_cookie_secure_when_configured(media_env):
    client = TestClient(media_env.app, client=("203.0.113.251", 5000))

    with settings_override(COOKIE_SECURE=True):
        response = client.post(
            "/api/auth/login", json={"username": "owner-a", "password": "owner-a-pw"}
        )

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_logout_clears_media_cookie(media_env):
    client = media_env.owner_cookie_client()
    assert client.cookies.get("media_token")

    response = client.post("/api/auth/logout")

    assert response.status_code == 204
    cleared = response.headers["set-cookie"]
    assert "media_token=" in cleared
    assert "Max-Age=0" in cleared or "expires=" in cleared.lower()
    # 清除指令的属性要与下发时一致，浏览器才认这是同一个 Cookie
    assert "Path=/media" in cleared
    assert "HttpOnly" in cleared
    assert "Secure" not in cleared
    # 客户端不再携带该 Cookie，之后的图片请求变回 401
    assert client.cookies.get("media_token") in (None, "")
    assert client.get(media_env.media_url(media_env.image_rel)).status_code == 401


def test_logout_cookie_is_secure_when_configured(media_env):
    client = media_env.owner_cookie_client()
    assert client.cookies.get("media_token")

    with settings_override(COOKIE_SECURE=True):
        response = client.post("/api/auth/logout")

    assert response.status_code == 204
    cleared = response.headers["set-cookie"]
    assert "Secure" in cleared
    assert "Path=/media" in cleared
    assert "HttpOnly" in cleared


def test_media_token_of_deleted_user_is_401(media_env):
    client = media_env.owner_cookie_client()

    async def _delete_users() -> None:
        async with media_env.factory() as db:
            await db.execute(delete(User))
            await db.commit()

    asyncio.run(_delete_users())

    assert client.get(media_env.media_url(media_env.image_rel)).status_code == 401


# ---------------------------------------------------------------- 验收 9


def test_deleted_project_gets_404(media_env):
    client = media_env.owner_bearer_client("203.0.113.260")
    headers = {"Authorization": f"Bearer {media_env.token_a}"}
    url = media_env.media_url(media_env.image_rel)

    assert client.get(url, headers=headers).status_code == 200

    # 只删项目记录、文件还在：仍然 404，说明放行条件是“项目属于我”而不只是文件存在
    media_env.delete_project_row()
    assert client.get(url, headers=headers).status_code == 404

    # 文件也删掉（真实删除流程里 shutil.rmtree 做的就是这个）
    import shutil

    shutil.rmtree(media_env.uploads / "projects" / media_env.project_id)
    assert client.get(url, headers=headers).status_code == 404
