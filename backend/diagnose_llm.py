#!/usr/bin/env python3
"""LLM 配置诊断工具 - 检查为什么会超时"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from config.settings import settings
from models.llm_config import LLMConfig
from utils.crypto import decrypt_api_key

async def main():
    print("=" * 60)
    print("LLM 配置诊断")
    print("=" * 60)

    # 1. 检查 .env 配置
    print("\n1. .env 配置:")
    print(f"   LLM_PROVIDER: {settings.LLM_PROVIDER}")
    print(f"   LLM_API_KEY: {'(已设置)' if settings.LLM_API_KEY else '(未设置)'}")
    print(f"   LLM_BASE_URL: {settings.LLM_BASE_URL or '(默认)'}")
    print(f"   LLM_MODEL: {settings.LLM_MODEL}")

    # 2. 检查数据库中的用户配置
    print("\n2. 数据库中的用户 LLM 配置:")
    try:
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with async_session() as db:
            result = await db.execute(select(LLMConfig))
            configs = result.scalars().all()

            if not configs:
                print("   ❌ 未找到任何用户配置（将使用 .env 默认值）")
            else:
                for config in configs:
                    print(f"\n   用户 ID: {config.user_id}")
                    print(f"   Provider: {config.provider}")
                    print(f"   Model: {config.model_id}")
                    print(f"   Base URL: {config.base_url or '(默认)'}")

                    # 尝试解密 API Key
                    if config.encrypted_api_key:
                        try:
                            decrypted = decrypt_api_key(config.encrypted_api_key)
                            # 只显示前4位和后4位
                            if len(decrypted) > 8:
                                masked = f"{decrypted[:4]}...{decrypted[-4:]}"
                            else:
                                masked = "****"
                            print(f"   API Key: {masked} (已加密存储)")
                        except Exception as e:
                            print(f"   API Key: ❌ 解密失败 - {e}")
                    else:
                        print(f"   API Key: (未设置)")

                    # 诊断建议
                    print("\n   🔍 诊断:")
                    if config.provider == "mock":
                        print("      ✅ 使用 mock provider，不会真实调用 LLM（不应超时）")
                    else:
                        if not config.encrypted_api_key:
                            print("      ⚠️  没有配置 API Key，会报错")

                        if config.base_url:
                            print(f"      🔗 使用自定义 base_url: {config.base_url}")
                            print(f"         请确认这个 URL 可访问，否则会超时")

                        print(f"      💡 Provider '{config.provider}' 会真实调用 LLM API")
                        print(f"         如果 API Key 无效、配额用尽、网络问题，会导致超时")

        await engine.dispose()

    except Exception as e:
        print(f"   ❌ 数据库连接失败: {e}")
        print(f"   请确认 PostgreSQL 正在运行: {settings.DATABASE_URL}")

    # 3. 测试建议
    print("\n" + "=" * 60)
    print("💡 排查超时的建议:")
    print("=" * 60)
    print("1. 如果使用 mock provider:")
    print("   → Mock 不会超时，卡住可能是其他问题（数据库查询慢？）")
    print("")
    print("2. 如果使用真实 provider (OpenAI/DeepSeek 等):")
    print("   → 检查 API Key 是否有效")
    print("   → 检查账户余额/配额")
    print("   → 检查 base_url 是否可访问（ping/curl 测试）")
    print("   → 检查网络防火墙是否阻止了 API 请求")
    print("")
    print("3. 临时解决方案:")
    print("   → 在前端「设置」界面，将 Provider 改为 'mock' 测试")
    print("   → 或者删除数据库中的用户配置，回退到 .env 的 mock")
    print("")
    print("4. 现在已添加120秒超时，超时后会看到明确错误提示")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
