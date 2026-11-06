"""命令行创建管理员账号（生产环境关闭自助注册后的唯一建号入口）。

用法：
    cd backend
    venv/bin/python scripts/create_admin.py --username admin          # 交互输入密码
    venv/bin/python scripts/create_admin.py --username admin --password '...'

行为：
- 用与 POST /auth/register 完全相同的 bcrypt 哈希写入 users 表；
- 用户名已存在则报错退出，不覆盖原密码；
- 全程不打印密码，也不把密码写进日志。

连接哪个库由环境变量 DATABASE_URL 决定（与后端一致）：
- 服务端部署：PostgreSQL，例如
  DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/ai_creative
- 桌面端：~/Library/Application Support/AI 创作平台/backend-data/ai_creative.sqlite3，
  DSN 形如 sqlite+aiosqlite:////Users/<你>/Library/Application Support/AI 创作平台/backend-data/ai_creative.sqlite3

退出码：0 创建成功；1 用户名已存在；2 参数/配置错误。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

# 允许直接 `python scripts/create_admin.py`（此时 sys.path[0] 是 scripts/）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.auth import _hash_password  # noqa: E402  与 register 使用同一套 bcrypt 哈希
from api.auth import PASSWORD_TOO_LONG_DETAIL, password_within_byte_limit  # noqa: E402
from config.settings import settings  # noqa: E402
from models.user import User  # noqa: E402

# 与 schemas.api.RegisterRequest 的约束保持一致，避免建出一个注册流程建不出来的账号。
USERNAME_MIN, USERNAME_MAX = 3, 50
PASSWORD_MIN, PASSWORD_MAX = 6, 100


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="create_admin.py",
        description="创建平台账号（生产环境关闭注册后用它建号）。密码不会写入日志。",
    )
    parser.add_argument("--username", help="用户名（3-50 字符）；省略则交互输入")
    parser.add_argument(
        "--password",
        help="密码（6-100 字符，且 UTF-8 编码后不超过 72 字节）；省略则交互输入（不回显）",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="覆盖 DATABASE_URL（默认读取环境变量，与后端一致）",
    )
    return parser.parse_args(argv)


def _prompt_username() -> str:
    return input("用户名: ").strip()


def _prompt_password() -> str:
    first = getpass.getpass("密码: ")
    second = getpass.getpass("再次输入密码: ")
    if first != second:
        raise SystemExit("两次输入的密码不一致，已取消")
    return first


def _validate(username: str, password: str) -> str | None:
    """返回错误信息，None 表示校验通过。"""
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        return f"用户名长度需在 {USERNAME_MIN}-{USERNAME_MAX} 之间"
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return f"密码长度需在 {PASSWORD_MIN}-{PASSWORD_MAX} 之间"
    # bcrypt 只处理 72 字节，超长会在 hashpw 里抛 ValueError，这里提前拒绝。
    # 注意不要回显密码本身，只说长度。
    if not password_within_byte_limit(password):
        return PASSWORD_TOO_LONG_DETAIL
    return None


async def _create_user(database_url: str, username: str, password: str) -> int:
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # 只确保 users 表存在：脚本可能在空库上第一次运行，但不该替服务端做完整建表。
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create, checkfirst=True)

        async with session_factory() as db:
            existing = (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if existing is not None:
                print(f"用户 {username} 已存在，未做任何修改。", file=sys.stderr)
                return 1

            db.add(
                User(
                    username=username,
                    hashed_password=_hash_password(password),
                    is_active=True,
                )
            )
            await db.commit()

        print(f"已创建用户：{username}")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    username = (args.username or "").strip() or _prompt_username()
    password = args.password if args.password is not None else _prompt_password()

    error = _validate(username, password)
    if error:
        print(error, file=sys.stderr)
        return 2

    database_url = args.database_url or settings.DATABASE_URL
    if not database_url:
        print("未配置 DATABASE_URL，无法确定要写入哪个数据库。", file=sys.stderr)
        return 2

    print(f"写入数据库：{_mask_dsn(database_url)}")
    return asyncio.run(_create_user(database_url, username, password))


def _mask_dsn(dsn: str) -> str:
    """隐藏 DSN 里的密码，避免终端/日志泄露。"""
    if "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if "@" not in rest:
        return dsn
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


if __name__ == "__main__":
    sys.exit(main())
