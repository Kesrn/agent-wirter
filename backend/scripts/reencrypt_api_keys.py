"""把已存的 API Key 密文从“JWT_SECRET 派生密钥”迁移到主密钥 ENCRYPTION_KEY。

背景：升级前 Fernet 密钥由 JWT_SECRET 派生；升级后加密改用 ENCRYPTION_KEY，
旧密文靠 MultiFernet 的旧密钥兜底仍可读。本脚本把 llm_configs.encrypted_api_key
逐行解密后用主密钥重新加密，做到“以后可以只带 ENCRYPTION_KEY”。

默认是 dry-run：只统计数量，不修改任何数据。加 --apply 才真正写入。

重要（旧密钥保留期）：LangGraph checkpoint 状态里的 encrypted_api_key 不在本脚本
范围内，仍由旧密钥（JWT_SECRET 派生的那一个）解密。因此 **在相关 checkpoint 过期
或对应任务被清理之前，不要更换/删除 JWT_SECRET**，否则这些历史密文将无法解密。

安全约定：全程不打印任何明文 Key 或密文，只输出行号和数量。

用法：
    cd backend
    venv/bin/python scripts/reencrypt_api_keys.py                    # dry-run
    venv/bin/python scripts/reencrypt_api_keys.py --apply            # 真正写入

退出码：0 全部成功（或无数据）；3 存在失败行（已写入的部分保持成功）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许直接 `python scripts/reencrypt_api_keys.py`（此时 sys.path[0] 是 scripts/）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from config.settings import settings  # noqa: E402
from models.llm_config import LLMConfig  # noqa: E402
from utils.crypto import (  # noqa: E402
    decrypt_api_key,
    decrypt_api_key_with_primary_key,
    encrypt_api_key,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reencrypt_api_keys.py",
        description=(
            "用主密钥 ENCRYPTION_KEY 重新加密 llm_configs.encrypted_api_key。"
            "默认 dry-run 只统计数量；加 --apply 才写入。"
            "注意：checkpoint 里的旧密文仍依赖由 JWT_SECRET 派生的旧密钥，"
            "旧密钥要保留到相关 checkpoint 过期之后再轮换。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库（默认只做 dry-run，不修改数据）",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="覆盖 DATABASE_URL（默认读取环境变量，与后端一致）",
    )
    return parser.parse_args(argv)


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


async def _run(database_url: str, apply: bool) -> int:
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            try:
                rows = (
                    await db.execute(
                        select(LLMConfig).order_by(LLMConfig.created_at, LLMConfig.id)
                    )
                ).scalars().all()
            except Exception as exc:  # noqa: BLE001 - 表不存在等部署问题需可读提示
                print(f"读取 llm_configs 失败（表不存在或数据库不可用）：{exc}", file=sys.stderr)
                return 3

            with_key = [row for row in rows if row.encrypted_api_key]
            print(f"共 {len(rows)} 条 LLM 配置，其中 {len(with_key)} 条带加密 Key")
            if not with_key:
                print("没有需要处理的密文。")
                return 0

            decrypted: list[tuple[int, LLMConfig, str]] = []
            decrypt_failed: list[int] = []
            for index, row in enumerate(with_key, start=1):
                try:
                    plain = decrypt_api_key(row.encrypted_api_key)
                except Exception:  # noqa: BLE001 - 单行失败不阻断整批
                    decrypt_failed.append(index)
                    print(f"  [失败] 行号 {index}（id={row.id}）旧密钥也无法解密，已跳过")
                    continue
                decrypted.append((index, row, plain))

            print(f"可解密：{len(decrypted)} 条；解密失败：{len(decrypt_failed)} 条")

            if not apply:
                print(
                    "dry-run 结束，未修改任何数据。确认无误后加 --apply 执行写入。"
                )
                return 3 if decrypt_failed else 0

            written: list[tuple[int, LLMConfig]] = []
            encrypt_failed = 0
            for index, row, plain in decrypted:
                try:
                    row.encrypted_api_key = encrypt_api_key(plain)
                except Exception:  # noqa: BLE001 - 单行失败不阻断整批
                    encrypt_failed += 1
                    print(f"  [失败] 行号 {index}（id={row.id}）重新加密失败，已跳过")
                    continue
                written.append((index, row))

            await db.commit()
            print(f"已写入：{len(written)} 条；重新加密失败：{encrypt_failed} 条")

            # 写入后再用“只含主密钥”的解密验证一遍，确认已不依赖旧密钥。
            verified = 0
            verify_failed: list[int] = []
            for index, row in written:
                await db.refresh(row)
                try:
                    decrypt_api_key_with_primary_key(row.encrypted_api_key)
                    verified += 1
                except Exception:  # noqa: BLE001 - 校验失败要如实报出
                    verify_failed.append(index)
                    print(f"  [失败] 行号 {index}（id={row.id}）仅用主密钥仍无法解密")

            print(f"主密钥校验通过：{verified} 条；校验失败：{len(verify_failed)} 条")
            failures = len(decrypt_failed) + encrypt_failed + len(verify_failed)
            if failures:
                print(f"总计 {failures} 条未完成，请检查对应行。", file=sys.stderr)
                return 3
            print("全部完成：现有密文均可只凭 ENCRYPTION_KEY 解密。")
            return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    database_url = args.database_url or settings.DATABASE_URL
    if not database_url:
        print("未配置 DATABASE_URL，无法确定要处理哪个数据库。", file=sys.stderr)
        return 3

    print(f"目标数据库：{_mask_dsn(database_url)}")
    print(f"模式：{'--apply（会写入）' if args.apply else 'dry-run（只统计）'}")
    return asyncio.run(_run(database_url, args.apply))


if __name__ == "__main__":
    sys.exit(main())
