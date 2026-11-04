"""任务单 0.2：认证与密钥收紧的回归测试。

覆盖范围（对应任务单第四节验收条目）：
1. 生产环境缺 JWT_SECRET → 应用启动失败；
2. 生产环境缺 ENCRYPTION_KEY → 应用启动失败；
3. 生产环境空用户表登录返回 401，且不创建用户；
4. 生产环境注册返回 403；
5. create_admin.py 建出的账号可以正常登录；
6. 旧密钥（JWT_SECRET 派生）密文仍可解密，reencrypt --apply 后可只凭主密钥解密；
7. 开发环境默认行为不变（空库首次登录建号、注册可用）；
9. 桌面端无需手工配环境变量即可启动，且重启复用同一密钥。

追加（0.2 验收后修复）：
- .env 里的 APP_ENV=production 必须生效（加载顺序，不能 fail-open）；
- 历史 fallback 密钥加密的密文在新代码下仍可解密，迁移后只凭主密钥可解。

全部使用临时 SQLite 库，不触碰本地真实数据库，也不会打印任何密钥。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from api.auth import login, register
from api.rate_limiter import auth_ip_limiter, auth_limiter
from config.settings import settings
from models.base import Base
from models.llm_config import LLMConfig
from models.user import User
from schemas.api import LoginRequest, RegisterRequest
from utils.crypto import decrypt_api_key, decrypt_api_key_with_primary_key, encrypt_api_key

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET_ENV_KEYS = (
    "APP_ENV",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
    "ALLOW_REGISTRATION",
    "ALLOW_FIRST_USER_BOOTSTRAP",
    "DATABASE_URL",
)


def _request(ip: str = "127.0.0.1") -> Request:
    """构造一个最小 Request，供直接调用路由函数时使用。

    0.3 起 login/register 需要 Request 来取客户端 IP（IP 维度限流），
    直接调用路由函数时不经过 FastAPI，所以这里手工拼一个 scope。
    """
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [],
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def _response() -> Response:
    """直接调用路由函数时用的空 Response（login 会往上写 Set-Cookie）。"""
    return Response()


@pytest.fixture(autouse=True)
def _reset_auth_limiters():
    """限流器是进程级全局状态，每个用例前清空，避免互相影响。"""
    auth_limiter.reset()
    auth_ip_limiter.reset()
    yield
    auth_limiter.reset()
    auth_ip_limiter.reset()


@contextmanager
def settings_override(**changes):
    """临时改写 frozen settings 字段，退出时恢复原值。

    settings 是 frozen dataclass，测试里用 object.__setattr__ 覆盖（与现有
    test_smoke.py 的做法一致），必须在 finally 里恢复，避免污染同文件其他用例。
    """
    original = {name: getattr(settings, name) for name in changes}
    try:
        for name, value in changes.items():
            object.__setattr__(settings, name, value)
        yield
    finally:
        for name, value in original.items():
            object.__setattr__(settings, name, value)


def _clean_env() -> dict[str, str]:
    """构造一份干净的子进程环境。

    显式清掉可能从 shell/.env 继承的密钥类变量：settings 用 override=False 加载 .env，
    只有“环境里已经存在（哪怕为空字符串）”的变量才不会被 .env 覆盖，所以这里统一
    传空串来模拟“真的没配”。DATABASE_URL 指向内存库，避免子进程连到真实数据库。
    """
    env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_KEYS}
    env.update({key: "" for key in SECRET_ENV_KEYS})
    env["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    return env


def _run_python(code: str, **env_overrides) -> subprocess.CompletedProcess:
    """在 backend 目录下跑一段独立 Python 代码。"""
    env = _clean_env()
    env.update({key: str(value) for key, value in env_overrides.items()})
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _run_script(script: str, args: list[str], **env_overrides) -> subprocess.CompletedProcess:
    env = _clean_env()
    env.update({key: str(value) for key, value in env_overrides.items()})
    return subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / script), *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


async def _create_tables(engine, *tables) -> None:
    async with engine.begin() as conn:
        for table in tables:
            await conn.run_sync(table.create, checkfirst=True)


def _memory_engine():
    return create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)


async def _user_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


# ---------------------------------------------------------------- 验收 1 / 2


def test_production_without_jwt_secret_aborts_startup():
    result = _run_python(
        "import main; print('started')",
        APP_ENV="production",
        JWT_SECRET="",
        ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )

    assert result.returncode != 0, result.stdout
    assert "JWT_SECRET" in result.stderr
    assert "started" not in result.stdout


def test_production_without_encryption_key_aborts_startup():
    result = _run_python(
        "import main; print('started')",
        APP_ENV="production",
        JWT_SECRET="a" * 48,
        ENCRYPTION_KEY="",
    )

    assert result.returncode != 0, result.stdout
    assert "ENCRYPTION_KEY" in result.stderr
    assert "started" not in result.stdout


def test_production_with_complete_secrets_starts():
    """反向确认：配置齐全时生产环境能正常启动，避免上面两条只是“任何情况都失败”。"""
    result = _run_python(
        "import main; from config.settings import settings; "
        "print('started', settings.IS_PRODUCTION, settings.ALLOW_REGISTRATION)",
        APP_ENV="production",
        JWT_SECRET="a" * 48,
        ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )

    assert result.returncode == 0, result.stderr
    assert "started True False" in result.stdout


def test_invalid_encryption_key_fails_fast():
    """密钥格式写错要立刻报出来，而不是等到写库时才炸。"""
    result = _run_python(
        "import config.settings",
        APP_ENV="production",
        JWT_SECRET="a" * 48,
        ENCRYPTION_KEY="not-a-valid-fernet-key",
    )

    assert result.returncode != 0
    assert "ENCRYPTION_KEY" in result.stderr


# ---------------------------------------------------------------- 验收 3 / 4 / 7


@pytest.mark.asyncio
async def test_production_login_on_empty_table_returns_401_and_creates_nothing():
    engine = _memory_engine()
    await _create_tables(engine, User.__table__)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            with settings_override(
                IS_PRODUCTION=True,
                APP_ENV="production",
                ALLOW_FIRST_USER_BOOTSTRAP=False,
                ALLOW_REGISTRATION=False,
            ):
                with pytest.raises(HTTPException) as excinfo:
                    await login(
                        LoginRequest(username="intruder", password="whatever"),
                        _request(),
                        _response(),
                        db=db,
                    )
            assert excinfo.value.status_code == 401
            assert await _user_count(db) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_production_register_returns_403():
    engine = _memory_engine()
    await _create_tables(engine, User.__table__)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            with settings_override(IS_PRODUCTION=True, ALLOW_REGISTRATION=False):
                with pytest.raises(HTTPException) as excinfo:
                    await register(
                        RegisterRequest(username="newcomer", password="secret123"),
                        _request(),
                        db=db,
                    )
            assert excinfo.value.status_code == 403
            assert excinfo.value.detail == "注册已关闭"
            assert await _user_count(db) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_development_defaults_keep_bootstrap_and_registration():
    """开发/桌面端默认：空库首次登录自动建号，注册可用（行为保持不变）。"""
    engine = _memory_engine()
    await _create_tables(engine, User.__table__)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            with settings_override(
                IS_PRODUCTION=False,
                APP_ENV="development",
                ALLOW_FIRST_USER_BOOTSTRAP=True,
                ALLOW_REGISTRATION=True,
            ):
                boot = await login(
                    LoginRequest(username="devboot", password="devpass1"),
                    _request(),
                    _response(),
                    db=db,
                )
                assert boot.access_token
                assert boot.user.username == "devboot"

                created = await register(
                    RegisterRequest(username="devregister", password="devpass2"),
                    _request(),
                    db=db,
                )
                assert created.username == "devregister"
                assert await _user_count(db) == 2
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- 验收 5


@pytest.mark.asyncio
async def test_create_admin_script_creates_loginable_user(tmp_path):
    db_path = tmp_path / "admin.sqlite3"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    created = _run_script(
        "create_admin.py",
        ["--username", "opsadmin", "--password", "strong-admin-pw"],
        DATABASE_URL=database_url,
    )
    assert created.returncode == 0, created.stderr
    assert "opsadmin" in created.stdout
    # 密码绝不能出现在任何输出里
    assert "strong-admin-pw" not in created.stdout + created.stderr

    duplicate = _run_script(
        "create_admin.py",
        ["--username", "opsadmin", "--password", "another-pw"],
        DATABASE_URL=database_url,
    )
    assert duplicate.returncode == 1
    assert "已存在" in duplicate.stderr
    assert "another-pw" not in duplicate.stdout + duplicate.stderr

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            with settings_override(IS_PRODUCTION=True, ALLOW_REGISTRATION=False, ALLOW_FIRST_USER_BOOTSTRAP=False):
                wrong = None
                try:
                    await login(
                        LoginRequest(username="opsadmin", password="wrong-pw"),
                        _request(),
                        _response(),
                        db=db,
                    )
                except HTTPException as exc:  # noqa: PERF203 - 明确断言密码校验生效
                    wrong = exc
                assert wrong is not None and wrong.status_code == 401

                ok = await login(
                    LoginRequest(username="opsadmin", password="strong-admin-pw"),
                    _request(),
                    _response(),
                    db=db,
                )
                assert ok.access_token
                assert ok.user.username == "opsadmin"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- 验收 6


@pytest.mark.asyncio
async def test_legacy_ciphertext_survives_new_encryption_key_and_reencrypt_apply(tmp_path):
    old_jwt_secret = "legacy-jwt-secret-" + uuid.uuid4().hex
    new_encryption_key = Fernet.generate_key().decode()
    plaintext = "sk-legacy-ciphertext-" + uuid.uuid4().hex
    db_path = tmp_path / "reencrypt.sqlite3"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _create_tables(engine, LLMConfig.__table__)

        # 1) 模拟升级前的写库方式：没有 ENCRYPTION_KEY，密钥由 JWT_SECRET 派生。
        with settings_override(ENCRYPTION_KEY="", JWT_SECRET=old_jwt_secret):
            legacy_ciphertext = encrypt_api_key(plaintext)

        async with factory() as db:
            config = LLMConfig(
                user_id=str(uuid.uuid4()),
                name="旧配置",
                provider="deepseek",
                encrypted_api_key=legacy_ciphertext,
                is_active=True,
            )
            db.add(config)
            await db.commit()
            config_id = config.id

        # 2) 配好新 ENCRYPTION_KEY 后，旧密文靠 MultiFernet 的旧密钥仍可读。
        with settings_override(ENCRYPTION_KEY=new_encryption_key, JWT_SECRET=old_jwt_secret):
            assert decrypt_api_key(legacy_ciphertext) == plaintext
            # 主密钥单独解不开，说明走的是旧密钥兜底分支
            with pytest.raises(Exception):
                decrypt_api_key_with_primary_key(legacy_ciphertext)

        # 3) dry-run 不修改数据
        dry_run = _run_script(
            "reencrypt_api_keys.py",
            [],
            DATABASE_URL=database_url,
            JWT_SECRET=old_jwt_secret,
            ENCRYPTION_KEY=new_encryption_key,
        )
        assert dry_run.returncode == 0, dry_run.stderr
        assert "dry-run" in dry_run.stdout
        assert plaintext not in dry_run.stdout + dry_run.stderr

        async with factory() as db:
            unchanged = await db.get(LLMConfig, config_id)
            assert unchanged.encrypted_api_key == legacy_ciphertext

        # 4) --apply 后用“只含主密钥”的方式校验
        applied = _run_script(
            "reencrypt_api_keys.py",
            ["--apply"],
            DATABASE_URL=database_url,
            JWT_SECRET=old_jwt_secret,
            ENCRYPTION_KEY=new_encryption_key,
        )
        assert applied.returncode == 0, applied.stderr
        assert plaintext not in applied.stdout + applied.stderr

        async with factory() as db:
            migrated = await db.get(LLMConfig, config_id)
            assert migrated.encrypted_api_key != legacy_ciphertext
            with settings_override(ENCRYPTION_KEY=new_encryption_key, JWT_SECRET=""):
                # 旧密钥不可用时仍能解密 → 已彻底切到主密钥
                assert decrypt_api_key_with_primary_key(migrated.encrypted_api_key) == plaintext
                assert decrypt_api_key(migrated.encrypted_api_key) == plaintext
    finally:
        await engine.dispose()


def test_reencrypt_reports_failed_rows_without_aborting(tmp_path):
    """单行坏密文要记录行号并继续，不能整批中止。"""
    db_path = tmp_path / "broken.sqlite3"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    jwt_secret = "legacy-jwt-secret-" + uuid.uuid4().hex
    encryption_key = Fernet.generate_key().decode()

    async def _seed():
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            await _create_tables(engine, LLMConfig.__table__)
            with settings_override(ENCRYPTION_KEY="", JWT_SECRET=jwt_secret):
                good = encrypt_api_key("sk-good-" + uuid.uuid4().hex)
            async with factory() as db:
                db.add(
                    LLMConfig(
                        user_id=str(uuid.uuid4()),
                        name="正常",
                        provider="openai",
                        encrypted_api_key=good,
                        is_active=True,
                    )
                )
                db.add(
                    LLMConfig(
                        user_id=str(uuid.uuid4()),
                        name="损坏",
                        provider="openai",
                        encrypted_api_key="not-a-fernet-token",
                        is_active=False,
                    )
                )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())

    result = _run_script(
        "reencrypt_api_keys.py",
        ["--apply"],
        DATABASE_URL=database_url,
        JWT_SECRET=jwt_secret,
        ENCRYPTION_KEY=encryption_key,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "行号" in result.stdout
    assert "已写入：1 条" in result.stdout


# ---------------------------------------------------------------- 验收 9


def test_desktop_server_generates_and_reuses_persisted_secret(monkeypatch, tmp_path):
    """桌面端不配任何环境变量也能启动，且重启后复用同一密钥（权限 0600）。"""
    from desktop_server import configure_desktop_environment

    data_dir = tmp_path / "desktop-data"
    saved = {key: os.environ.get(key) for key in ("JWT_SECRET", "ALLOW_FIRST_USER_BOOTSTRAP", "DATABASE_URL", "UPLOAD_DIR")}
    monkeypatch.setenv("AI_CREATIVE_DESKTOP_DATA_DIR", str(data_dir))
    for key in ("JWT_SECRET", "ALLOW_FIRST_USER_BOOTSTRAP", "DATABASE_URL", "UPLOAD_DIR"):
        monkeypatch.delenv(key, raising=False)
    try:
        configure_desktop_environment()
        secret_path = data_dir / "jwt_secret"
        first_secret = os.environ["JWT_SECRET"]

        assert secret_path.exists()
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
        # 生产环境要求 JWT_SECRET >= 32 字符，桌面端生成的密钥必须满足
        assert len(first_secret) >= 32
        assert os.environ["ALLOW_FIRST_USER_BOOTSTRAP"] == "true"
        assert os.environ["JWT_SECRET"] == secret_path.read_text(encoding="utf-8").strip()

        # 模拟重启：环境变量清空后重新配置，必须读到同一个密钥
        for key in ("JWT_SECRET", "ALLOW_FIRST_USER_BOOTSTRAP"):
            os.environ.pop(key, None)
        configure_desktop_environment()
        assert os.environ["JWT_SECRET"] == first_secret
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_desktop_server_tightens_existing_secret_permissions(monkeypatch, tmp_path):
    """老版本写出的 0644 密钥文件要被纠正为 0600，内容不变。"""
    from desktop_server import configure_desktop_environment

    data_dir = tmp_path / "desktop-data"
    data_dir.mkdir(parents=True)
    secret_path = data_dir / "jwt_secret"
    secret_path.write_text("b" * 48, encoding="utf-8")
    os.chmod(secret_path, 0o644)

    saved = {key: os.environ.get(key) for key in ("JWT_SECRET", "ALLOW_FIRST_USER_BOOTSTRAP")}
    monkeypatch.setenv("AI_CREATIVE_DESKTOP_DATA_DIR", str(data_dir))
    try:
        configure_desktop_environment()
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
        assert os.environ["JWT_SECRET"] == "b" * 48
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------- .env 加载顺序


def _run_settings_with_dotenv(tmp_path: Path, dotenv_body: str) -> subprocess.CompletedProcess:
    """在临时目录树里用指定 .env 启动一个只导入 settings 的子进程。

    settings 只从「仓库根/.env」和「backend/.env」加载，所以这里复制出同样的结构
    （tmp/backend/config/settings.py + tmp/.env），既测到真实代码，又完全不碰仓库里
    真实的 .env。环境里直接 unset APP_ENV 而不是置空：python-dotenv 的 override=False
    对“已存在的空值”同样不覆盖，置空会让 .env 失效、测不出问题。
    """
    tree = tmp_path / "dotenv-tree"
    config_dir = tree / "backend" / "config"
    config_dir.mkdir(parents=True)
    shutil.copyfile(BACKEND_DIR / "config" / "settings.py", config_dir / "settings.py")
    (tree / ".env").write_text(dotenv_body, encoding="utf-8")

    env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_KEYS}
    env["PYTHONPATH"] = str(tree / "backend")
    env["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import config.settings as s;"
            "p = s.settings;"
            "print('APP_ENV=', p.APP_ENV,"
            " 'IS_PRODUCTION=', p.IS_PRODUCTION,"
            " 'ALLOW_REGISTRATION=', p.ALLOW_REGISTRATION,"
            " 'ALLOW_FIRST_USER_BOOTSTRAP=', p.ALLOW_FIRST_USER_BOOTSTRAP)",
        ],
        cwd=str(tree / "backend"),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dotenv_app_env_production_takes_effect(tmp_path):
    """.env 里写 APP_ENV=production 必须生效，不能静默退回 development（fail-open）。"""
    result = _run_settings_with_dotenv(tmp_path, "APP_ENV=production\n")

    assert result.returncode != 0, result.stdout
    assert "生产环境安全配置不完整" in result.stderr
    assert "JWT_SECRET" in result.stderr
    assert "ENCRYPTION_KEY" in result.stderr


def test_dotenv_app_env_production_turns_off_self_service_switches(tmp_path):
    """.env 同时给出生产密钥时，注册与首次建号默认关闭。"""
    body = (
        "APP_ENV=production\n"
        f"JWT_SECRET={'a' * 48}\n"
        f"ENCRYPTION_KEY={Fernet.generate_key().decode()}\n"
    )

    result = _run_settings_with_dotenv(tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert "APP_ENV= production" in result.stdout
    assert "IS_PRODUCTION= True" in result.stdout
    assert "ALLOW_REGISTRATION= False" in result.stdout
    assert "ALLOW_FIRST_USER_BOOTSTRAP= False" in result.stdout


def test_dotenv_without_app_env_stays_development(tmp_path):
    """没写 APP_ENV 时仍然是开发默认值，确认上一条不是“总是 False”。"""
    result = _run_settings_with_dotenv(tmp_path, "LLM_MODEL=whatever\n")

    assert result.returncode == 0, result.stderr
    assert "APP_ENV= development" in result.stdout
    assert "ALLOW_REGISTRATION= True" in result.stdout
    assert "ALLOW_FIRST_USER_BOOTSTRAP= True" in result.stdout


# ---------------------------------------------------------------- 历史 fallback 密钥

# 升级前 JWT_SECRET 为空时使用的固定派生字符串（独立写一份，避免测试跟着实现一起改）
_OLD_FALLBACK_SECRET = "ai-creative-platform-default-key-not-for-production"


def _old_style_fernet(secret: str) -> Fernet:
    """复刻升级前 utils/crypto.py 的派生方式（SHA-256 → urlsafe base64）。"""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def test_dev_ciphertext_survives_adding_encryption_key():
    """开发环境（无 ENCRYPTION_KEY、JWT_SECRET 为空）写出的密文，配上新密钥后仍可解。"""
    plaintext = "sk-dev-" + uuid.uuid4().hex
    with settings_override(ENCRYPTION_KEY="", JWT_SECRET=""):
        ciphertext = encrypt_api_key(plaintext)
        # 用的就是历史 fallback 密钥，不是 sha256("") 这种新密钥
        assert _old_style_fernet(_OLD_FALLBACK_SECRET).decrypt(ciphertext.encode()).decode() == plaintext

    with settings_override(ENCRYPTION_KEY=Fernet.generate_key().decode(), JWT_SECRET=""):
        assert decrypt_api_key(ciphertext) == plaintext


def test_old_code_fallback_ciphertext_is_decryptable():
    """用旧版 crypto.py 逻辑生成的密文，在新代码下必须能解开。"""
    plaintext = "sk-old-" + uuid.uuid4().hex
    legacy_ciphertext = _old_style_fernet(_OLD_FALLBACK_SECRET).encrypt(plaintext.encode()).decode()

    with settings_override(ENCRYPTION_KEY="", JWT_SECRET=""):
        assert decrypt_api_key(legacy_ciphertext) == plaintext

    with settings_override(ENCRYPTION_KEY=Fernet.generate_key().decode(), JWT_SECRET=""):
        assert decrypt_api_key(legacy_ciphertext) == plaintext


@pytest.mark.asyncio
async def test_reencrypt_apply_migrates_fallback_ciphertext_to_primary(tmp_path):
    """fallback 密文经 --apply 迁移后，只带主密钥也能解开。"""
    plaintext = "sk-fallback-" + uuid.uuid4().hex
    legacy_ciphertext = _old_style_fernet(_OLD_FALLBACK_SECRET).encrypt(plaintext.encode()).decode()
    db_path = tmp_path / "fallback.sqlite3"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _create_tables(engine, LLMConfig.__table__)
        async with factory() as db:
            row = LLMConfig(
                user_id=str(uuid.uuid4()),
                name="历史 fallback 密文",
                provider="openai",
                encrypted_api_key=legacy_ciphertext,
                is_active=True,
            )
            db.add(row)
            await db.commit()
            row_id = row.id

        new_key = Fernet.generate_key().decode()
        applied = _run_script(
            "reencrypt_api_keys.py",
            ["--apply"],
            DATABASE_URL=database_url,
            JWT_SECRET="",
            ENCRYPTION_KEY=new_key,
        )
        assert applied.returncode == 0, applied.stdout + applied.stderr
        assert "已写入：1 条" in applied.stdout
        # 不打印明文、也不打印密钥
        assert plaintext not in applied.stdout + applied.stderr
        assert _OLD_FALLBACK_SECRET not in applied.stdout + applied.stderr

        async with factory() as db:
            migrated = await db.get(LLMConfig, row_id)
            assert migrated.encrypted_api_key != legacy_ciphertext
            with settings_override(ENCRYPTION_KEY=new_key, JWT_SECRET=""):
                assert decrypt_api_key_with_primary_key(migrated.encrypted_api_key) == plaintext
    finally:
        await engine.dispose()
