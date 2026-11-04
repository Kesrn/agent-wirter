"""任务 0.3 + 0.4 回归测试：bcrypt 线程化、IP 维度限流、防用户名枚举。

覆盖任务单第五节验收条目：
1. 同一 IP 用不同用户名连续登录，超过 AUTH_IP_RATE_LIMIT_PER_MINUTE 后 429 且带 Retry-After；
2. 不同 IP 互不影响；
3. TRUSTED_PROXY_COUNT=0 时伪造 X-Forwarded-For 不能绕过；=1 时按头里最后一个地址限流；
4. 原有按用户名限流仍生效；
5. llm_settings.py 复用的 auth_limiter 默认行为不变（check 不加头）；
6. 并发 20 个 verify_password_async 时事件循环最大停顿 <= 200ms（并给出同步基线）；
7. “账号不存在”“密码错误”“空库且禁止首次建号”三种 401 响应体完全相同。

限流器是进程级全局状态，每个用例前后都会 reset，避免互相影响。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.auth import (
    _hash_password,
    _verify_password,
    hash_password_async,
    verify_password_async,
)
from api.auth import router as auth_router
from api.rate_limiter import (
    _SlidingWindowLimiter,
    auth_ip_limiter,
    auth_limiter,
    get_client_ip,
)
from config.settings import settings
from db.session import get_db
from models.user import User

RETRY_AFTER_SECONDS = "60"

# 库里先放一个账号，让登录走“正常校验”分支而不是首次建号分支
_SEED_USER = (("seeduser", "seed-password"),)


@contextmanager
def settings_override(**changes):
    """临时改写 frozen settings 字段（与 test_auth_hardening.py 同一做法）。"""
    original = {name: getattr(settings, name) for name in changes}
    try:
        for name, value in changes.items():
            object.__setattr__(settings, name, value)
        yield
    finally:
        for name, value in original.items():
            object.__setattr__(settings, name, value)


@pytest.fixture(autouse=True)
def _reset_limiters():
    """限流器是进程级全局状态，每个用例前清空。"""
    auth_limiter.reset()
    auth_ip_limiter.reset()
    yield
    auth_limiter.reset()
    auth_ip_limiter.reset()


def _prepare_db(
    db_path: Path,
    users: tuple[tuple[str, str], ...] = (),
    disabled_users: tuple[tuple[str, str], ...] = (),
):
    """建一个临时 SQLite 库（可选预置用户），返回服务用的 session 工厂。

    用文件库而不是内存库：TestClient 在独立线程/事件循环里跑，文件库每次新建连接
    不会遇到跨事件循环复用同一个 aiosqlite 连接的问题。
    """
    url = f"sqlite+aiosqlite:///{db_path}"

    async def _setup() -> None:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create, checkfirst=True)
        if users or disabled_users:
            async with factory() as db:
                for username, password in users:
                    db.add(
                        User(
                            username=username,
                            hashed_password=_hash_password(password),
                            is_active=True,
                        )
                    )
                for username, password in disabled_users:
                    db.add(
                        User(
                            username=username,
                            hashed_password=_hash_password(password),
                            is_active=False,
                        )
                    )
                await db.commit()
        await engine.dispose()

    asyncio.run(_setup())

    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _make_client(factory, client_ip: str) -> TestClient:
    """只挂载认证路由的最小 app，避免拉起整个 main（DB/checkpointer/SSE 都不需要）。"""
    app = FastAPI()
    app.include_router(auth_router)

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, client=(client_ip, 50000))


def _fake_request(
    *,
    client_ip: str = "10.0.0.1",
    forwarded_for: str | None = None,
) -> Request:
    """构造 Request，用于直接测 get_client_ip（不经过 HTTP 栈）。"""
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": headers,
            "client": (client_ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


# ---------------------------------------------------------------- 验收 1


def test_ip_rate_limit_returns_429_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_ip_limiter, "_max_requests", 3)
    _, factory = _prepare_db(tmp_path / "ip.sqlite3", users=_SEED_USER)
    client = _make_client(factory, "203.0.113.10")

    responses = [
        client.post("/api/auth/login", json={"username": f"someone{i}", "password": "whatever"})
        for i in range(4)
    ]

    assert [r.status_code for r in responses[:3]] == [401, 401, 401]
    assert responses[3].status_code == 429
    assert responses[3].headers.get("retry-after") == RETRY_AFTER_SECONDS


# ---------------------------------------------------------------- 验收 2


def test_ip_limit_does_not_leak_between_ips(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_ip_limiter, "_max_requests", 2)
    _, factory = _prepare_db(tmp_path / "ip-two.sqlite3", users=_SEED_USER)
    client_a = _make_client(factory, "203.0.113.20")
    client_b = _make_client(factory, "203.0.113.21")

    assert client_a.post("/api/auth/login", json={"username": "a1", "password": "x"}).status_code == 401
    assert client_a.post("/api/auth/login", json={"username": "a2", "password": "x"}).status_code == 401
    assert client_a.post("/api/auth/login", json={"username": "a3", "password": "x"}).status_code == 429

    # 另一个 IP 还没被限流
    assert client_b.post("/api/auth/login", json={"username": "b1", "password": "x"}).status_code == 401


# ---------------------------------------------------------------- 验收 3


def test_spoofed_forwarded_for_cannot_bypass_ip_limit(tmp_path, monkeypatch):
    """TRUSTED_PROXY_COUNT=0：完全不看 X-Forwarded-For，换头也绕不过去。"""
    monkeypatch.setattr(auth_ip_limiter, "_max_requests", 2)
    _, factory = _prepare_db(tmp_path / "xff0.sqlite3", users=_SEED_USER)

    with settings_override(TRUSTED_PROXY_COUNT=0):
        client = _make_client(factory, "203.0.113.30")
        responses = [
            client.post(
                "/api/auth/login",
                json={"username": f"spoof{i}", "password": "x"},
                headers={"X-Forwarded-For": f"198.51.100.{i}"},
            )
            for i in range(3)
        ]

    assert [r.status_code for r in responses[:2]] == [401, 401]
    assert responses[2].status_code == 429


def test_get_client_ip_uses_last_forwarded_address_when_behind_one_proxy():
    with settings_override(TRUSTED_PROXY_COUNT=1):
        request = _fake_request(
            client_ip="10.0.0.1",
            forwarded_for="6.6.6.6, 7.7.7.7",
        )
        # 最右边由我们自己的代理追加，左边可能是客户端伪造的
        assert get_client_ip(request) == "7.7.7.7"


def test_get_client_ip_falls_back_to_peer_on_missing_or_garbage_header():
    with settings_override(TRUSTED_PROXY_COUNT=1):
        assert get_client_ip(_fake_request(client_ip="10.0.0.2", forwarded_for=None)) == "10.0.0.2"
        assert get_client_ip(_fake_request(client_ip="10.0.0.2", forwarded_for="")) == "10.0.0.2"
        assert get_client_ip(_fake_request(client_ip="10.0.0.2", forwarded_for="not-an-ip")) == "10.0.0.2"

    with settings_override(TRUSTED_PROXY_COUNT=2):
        # 只有一条地址，不够两层代理 → 回退
        assert get_client_ip(_fake_request(client_ip="10.0.0.3", forwarded_for="1.2.3.4")) == "10.0.0.3"


def test_forwarded_for_groups_by_last_address_when_trusted(tmp_path, monkeypatch):
    """TRUSTED_PROXY_COUNT=1 时按头里最后一个地址分组，伪造左侧地址不能换桶。"""
    monkeypatch.setattr(auth_ip_limiter, "_max_requests", 2)
    _, factory = _prepare_db(tmp_path / "xff1.sqlite3", users=_SEED_USER)

    with settings_override(TRUSTED_PROXY_COUNT=1):
        client = _make_client(factory, "10.0.0.9")
        responses = [
            client.post(
                "/api/auth/login",
                json={"username": f"real{i}", "password": "x"},
                headers={"X-Forwarded-For": f"198.51.100.{i}, 9.9.9.9"},
            )
            for i in range(3)
        ]

    assert [r.status_code for r in responses[:2]] == [401, 401]
    assert responses[2].status_code == 429
    assert responses[2].headers.get("retry-after") == RETRY_AFTER_SECONDS


# ---------------------------------------------------------------- 验收 4


def test_username_rate_limit_still_enforced(tmp_path, monkeypatch):
    """IP 维度放开，只按用户名限流时也要在阈值处 429（原有行为不能丢）。"""
    monkeypatch.setattr(auth_limiter, "_max_requests", 2)
    monkeypatch.setattr(auth_ip_limiter, "_max_requests", 50)
    _, factory = _prepare_db(tmp_path / "identity.sqlite3", users=_SEED_USER)
    client = _make_client(factory, "203.0.113.50")

    responses = [
        client.post("/api/auth/login", json={"username": "sameuser", "password": "x"})
        for _ in range(3)
    ]
    assert [r.status_code for r in responses[:2]] == [401, 401]
    assert responses[2].status_code == 429

    # 换个用户名仍然可以继续尝试（IP 维度没到上限）
    assert client.post("/api/auth/login", json={"username": "otheruser", "password": "x"}).status_code == 401


# ---------------------------------------------------------------- 验收 5


def test_limiter_default_response_has_no_retry_after_header():
    """默认调用（llm_settings.py 的 9 处）不能因为这次改动多出响应头。"""
    limiter = _SlidingWindowLimiter(max_requests=1)
    limiter.check("k")
    with pytest.raises(HTTPException) as excinfo:
        limiter.check("k")
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers is None

    limiter_with_header = _SlidingWindowLimiter(max_requests=1)
    limiter_with_header.check("k", with_retry_after=True)
    with pytest.raises(HTTPException) as excinfo2:
        limiter_with_header.check("k", with_retry_after=True)
    assert excinfo2.value.headers == {"Retry-After": RETRY_AFTER_SECONDS}


def test_auth_limiters_are_separate_instances():
    """IP 限流必须是独立实例，不能篡改 auth_limiter 的上限（llm_settings 在共用它）。"""
    assert auth_limiter is not auth_ip_limiter
    assert auth_limiter.max_requests == settings.AUTH_RATE_LIMIT_PER_MINUTE
    assert auth_ip_limiter.max_requests == settings.AUTH_IP_RATE_LIMIT_PER_MINUTE
    assert auth_limiter.window_seconds == 60
    assert auth_ip_limiter.window_seconds == 60


# ---------------------------------------------------------------- 验收 6

_TICK_INTERVAL_MS = 10
_MAX_ALLOWED_GAP_MS = 200
_CONCURRENCY = 20


async def _measure_max_gap(work) -> tuple[float, int]:
    """跑 work() 的同时每 10ms 打点，返回（最大间隔秒数, 打点次数）。"""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(_TICK_INTERVAL_MS / 1000)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        await work()
    finally:
        stop.set()
        await task

    return (max(gaps) if gaps else 0.0), len(gaps)


def test_bcrypt_runs_in_thread_and_does_not_block_event_loop():
    password = "correct-horse-battery-staple"
    hashed = _hash_password(password)

    async def work_async():
        await asyncio.gather(
            *(verify_password_async(password, hashed) for _ in range(_CONCURRENCY))
        )

    async def work_sync():
        # 改动前的写法：在 async 函数里直接调同步 bcrypt，仅在两次调用之间让出控制权
        for _ in range(_CONCURRENCY):
            _verify_password(password, hashed)
            await asyncio.sleep(0)

    async def run_both():
        async_gap, async_ticks = await _measure_max_gap(work_async)
        sync_gap, sync_ticks = await _measure_max_gap(work_sync)
        return async_gap, async_ticks, sync_gap, sync_ticks

    async_gap, async_ticks, sync_gap, sync_ticks = asyncio.run(run_both())

    print(
        f"\n[0.3 线程化] 异步版本最大停顿 {async_gap * 1000:.1f}ms（{async_ticks} 次打点）；"
        f"同步基线最大停顿 {sync_gap * 1000:.1f}ms（{sync_ticks} 次打点）"
    )

    assert async_ticks >= 5, "打点次数过少，测量不可信"
    assert async_gap <= _MAX_ALLOWED_GAP_MS / 1000, (
        f"verify_password_async 阻塞事件循环 {async_gap * 1000:.1f}ms，超过 {_MAX_ALLOWED_GAP_MS}ms"
    )
    # 同步基线必须明显更差，否则这个测试没有区分度（不做硬断言，只防止测量写错）
    assert sync_gap >= async_gap


def test_hash_password_async_roundtrip():
    password = "another-secret-" + uuid.uuid4().hex

    async def _roundtrip():
        hashed = await hash_password_async(password)
        assert await verify_password_async(password, hashed) is True
        assert await verify_password_async(password + "x", hashed) is False

    asyncio.run(_roundtrip())


# ---------------------------------------------------------------- 验收 7


def test_failed_login_responses_are_identical(tmp_path):
    """账号不存在 / 密码错误 / 空库且禁止首次建号：三种 401 的响应体必须完全一致。"""
    _, factory = _prepare_db(tmp_path / "enum.sqlite3", users=(("alice", "correct-pw"),))
    client = _make_client(factory, "203.0.113.60")

    wrong_password = client.post("/api/auth/login", json={"username": "alice", "password": "wrong-pw"})
    unknown_user = client.post("/api/auth/login", json={"username": "nobody", "password": "whatever"})
    unknown_user_by_email = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever"}
    )

    assert wrong_password.status_code == unknown_user.status_code == unknown_user_by_email.status_code == 401
    assert wrong_password.json() == unknown_user.json() == unknown_user_by_email.json()

    # 空库 + 禁止首次建号（0.2 引入的分支）也要给出完全相同的响应
    _, empty_factory = _prepare_db(tmp_path / "enum-empty.sqlite3")
    empty_client = _make_client(empty_factory, "203.0.113.61")
    with settings_override(ALLOW_FIRST_USER_BOOTSTRAP=False):
        empty_db_response = empty_client.post(
            "/api/auth/login", json={"username": "nobody", "password": "whatever"}
        )

    assert empty_db_response.status_code == 401
    assert empty_db_response.json() == wrong_password.json()


# ---------------------------------------------------------------- 验收返修：禁用账号
# C1：禁用账号不能成为“用户名存在”的探针——密码错误时必须与其它 401 完全一致。

_DISABLED_DETAIL = "用户已被禁用"


def test_disabled_account_with_wrong_password_is_indistinguishable(tmp_path):
    _, factory = _prepare_db(
        tmp_path / "disabled.sqlite3",
        users=(("active-user", "active-pw"),),
        disabled_users=(("disabled-user", "disabled-pw"),),
    )
    client = _make_client(factory, "203.0.113.70")

    wrong_password = client.post(
        "/api/auth/login", json={"username": "active-user", "password": "wrong-pw"}
    )
    disabled_wrong = client.post(
        "/api/auth/login", json={"username": "disabled-user", "password": "wrong-pw"}
    )
    unknown_user = client.post(
        "/api/auth/login", json={"username": "nobody-here", "password": "wrong-pw"}
    )

    assert wrong_password.status_code == disabled_wrong.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == disabled_wrong.json() == unknown_user.json()
    assert _DISABLED_DETAIL not in disabled_wrong.text


def test_disabled_account_with_correct_password_reports_disabled(tmp_path):
    _, factory = _prepare_db(
        tmp_path / "disabled-ok.sqlite3",
        disabled_users=(("disabled-user", "disabled-pw"),),
    )
    client = _make_client(factory, "203.0.113.71")

    response = client.post(
        "/api/auth/login", json={"username": "disabled-user", "password": "disabled-pw"}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": _DISABLED_DETAIL}


# ---------------------------------------------------------------- 验收返修：72 字节密码
# C2：bcrypt 5.x 对 >72 字节抛 ValueError，必须变成可预期的 400 / 401，而不是 500。

PASSWORD_TOO_LONG = "密码过长：编码后最多 72 字节（约 24 个汉字或 72 个英文字符）"


def test_register_accepts_exactly_72_bytes(tmp_path):
    _, factory = _prepare_db(tmp_path / "pw72.sqlite3")
    client = _make_client(factory, "203.0.113.80")

    response = client.post(
        "/api/auth/register", json={"username": "edge72", "password": "a" * 72}
    )

    assert response.status_code == 200
    assert response.json()["username"] == "edge72"


def test_register_rejects_73_bytes(tmp_path):
    _, factory = _prepare_db(tmp_path / "pw73.sqlite3")
    client = _make_client(factory, "203.0.113.81")

    response = client.post(
        "/api/auth/register", json={"username": "edge73", "password": "a" * 73}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": PASSWORD_TOO_LONG}


def test_register_rejects_25_chinese_chars(tmp_path):
    """25 个汉字 = 75 字节，字符数看着不多但已经超限。"""
    _, factory = _prepare_db(tmp_path / "pwcn.sqlite3")
    client = _make_client(factory, "203.0.113.82")
    password = "密" * 24 + "码"

    response = client.post(
        "/api/auth/register", json={"username": "chinesepw", "password": password}
    )

    assert len(password.encode("utf-8")) == 75
    assert response.status_code == 400
    assert response.json() == {"detail": PASSWORD_TOO_LONG}


def test_login_with_over_long_password_returns_401_not_500(tmp_path):
    _, factory = _prepare_db(tmp_path / "pwlogin.sqlite3", users=(("alice", "correct-pw"),))
    client = _make_client(factory, "203.0.113.83")

    long_password = "a" * 100
    existing_user = client.post(
        "/api/auth/login", json={"username": "alice", "password": long_password}
    )
    unknown_user = client.post(
        "/api/auth/login", json={"username": "nobody", "password": long_password}
    )
    normal_wrong = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong-pw"}
    )

    assert existing_user.status_code == 401
    assert existing_user.json() == unknown_user.json() == normal_wrong.json()


def test_login_bootstrap_rejects_over_long_password_without_creating_user(tmp_path):
    """空库首次建号分支：超长密码返回 400，且不能建出用户。"""
    db_path = tmp_path / "bootstrap.sqlite3"
    _, factory = _prepare_db(db_path)
    client = _make_client(factory, "203.0.113.84")

    with settings_override(ALLOW_FIRST_USER_BOOTSTRAP=True):
        response = client.post(
            "/api/auth/login", json={"username": "firstuser", "password": "a" * 73}
        )

    assert response.status_code == 400
    assert response.json() == {"detail": PASSWORD_TOO_LONG}

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from users").fetchone()[0] == 0


def test_create_admin_script_rejects_over_long_password(tmp_path):
    """命令行建号脚本同样要挡住超长密码，且不能把密码打进输出。"""
    password = "a" * 73
    db_path = tmp_path / "admin-long.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "create_admin.py"),
            "--username",
            "toolong",
            "--password",
            password,
            "--database-url",
            f"sqlite+aiosqlite:///{db_path}",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert PASSWORD_TOO_LONG in result.stderr
    assert password not in result.stdout + result.stderr
