"""API Key 加密/解密工具。

加密目标是“数据库不要保存用户 API Key 明文”。使用 Fernet 对称加密，
密钥来源分两层：

- 主密钥：``settings.ENCRYPTION_KEY``（标准 Fernet 密钥）。**加密只使用主密钥。**
  开发环境未配置时回退到“由 JWT_SECRET 派生”并打 warning，生产环境未配置则
  启动阶段直接失败（见 config/settings.py 的 validate_security_config）。
- 旧密钥：历史上由 JWT_SECRET 经 SHA-256 派生，**仅用于解密**，让升级前写入的
  密文（llm_configs.encrypted_api_key、LangGraph checkpoint 状态里的
  encrypted_api_key）仍可读。

两者通过 MultiFernet 串起来：解密依次尝试主密钥和旧密钥，都失败才抛异常。
这不是完整密钥管理：正式生产应改用云 KMS/Secret Manager，本地项目用 Fernet
足够演示“密文不落明文”的边界。
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, MultiFernet

from config.settings import settings

logger = logging.getLogger(__name__)


def _derive_key_from_secret(secret: str) -> bytes:
    """把任意长度字符串派生为 Fernet 密钥（旧方案，仅解密时使用）。

    Fernet 需要 32 字节 urlsafe base64 key。这里用 SHA-256 把任意长度的
    JWT_SECRET 固定为 32 字节，再 base64 编码。

    注意：该派生方式必须与升级前完全一致，任何改动都会让历史密文不可解密。
    """
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def _primary_key() -> bytes:
    """返回主密钥：优先 ENCRYPTION_KEY，开发环境缺失时由 JWT_SECRET 派生。"""
    configured = (settings.ENCRYPTION_KEY or "").strip()
    if configured:
        # 生产环境的合法性已在启动时校验；这里再校验一次，避免脚本/测试绕过启动流程。
        try:
            Fernet(configured.encode())
        except Exception as exc:  # noqa: BLE001 - 统一转成可读的配置错误
            raise RuntimeError(
                "ENCRYPTION_KEY 不是合法的 Fernet 密钥（应为 Fernet.generate_key() 的输出）"
            ) from exc
        return configured.encode()

    logger.warning(
        "ENCRYPTION_KEY 未配置，开发环境回退为“由 JWT_SECRET 派生”的密钥加密 API Key；"
        "生产环境必须配置 ENCRYPTION_KEY（缺失时启动即失败）"
    )
    return _derive_key_from_secret(settings.JWT_SECRET or "")


def _legacy_keys() -> list[bytes]:
    """返回仅用于解密的旧密钥。

    升级前所有密文都由 settings.JWT_SECRET 派生，因此只要 JWT_SECRET 还在，
    历史密文就能继续读。轮换 ENCRYPTION_KEY 不影响这一步。
    """
    secret = (settings.JWT_SECRET or "").strip()
    if not secret:
        return []
    return [_derive_key_from_secret(secret)]


def _multi_fernet() -> MultiFernet:
    """构造 MultiFernet：第一个是加密用主密钥，其余是解密备用旧密钥。"""
    keys = [_primary_key(), *_legacy_keys()]
    # 去重：开发环境未配 ENCRYPTION_KEY 时主密钥与旧密钥是同一条，不必重复尝试。
    unique_keys = list(dict.fromkeys(keys))
    return MultiFernet([Fernet(key) for key in unique_keys])


def encrypt_api_key(plain_key: str) -> str:
    """加密 API Key，返回可存入数据库的字符串密文（只使用主密钥）。"""
    return _multi_fernet().encrypt(plain_key.encode()).decode()


def decrypt_api_key(encrypted_key: str) -> str:
    """解密数据库中的 API Key 密文（主密钥优先，旧密钥兜底）。

    主密钥和旧密钥都无法解密时，Fernet 会抛 InvalidToken；调用方一般捕获后
    提示用户重新填写 API Key。
    """
    return _multi_fernet().decrypt(encrypted_key.encode()).decode()


def decrypt_api_key_with_primary_key(encrypted_key: str) -> str:
    """只用主密钥解密，不尝试旧密钥。

    供迁移脚本 reencrypt_api_keys.py 校验“重新加密后的密文已不依赖旧密钥”。
    正常运行路径请用 decrypt_api_key，否则会让旧密文无法读取。
    """
    return Fernet(_primary_key()).decrypt(encrypted_key.encode()).decode()
