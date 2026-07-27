"""API Key 加密/解密工具。

使用 Fernet 对称加密，密钥从 JWT_SECRET 通过 SHA-256 派生。

这里解决的是“数据库不要保存用户 API Key 明文”。它不是完整的密钥管理系统：
如果生产环境的 JWT_SECRET 泄露，攻击者仍然可以解密数据库中的 API Key。
正式生产应改为云 KMS/Secret Manager，本地作品集项目用 Fernet 足够演示安全意识。
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet

from config.settings import settings

logger = logging.getLogger(__name__)

_FALLBACK_KEY = "ai-creative-platform-default-key-not-for-production"


def _get_fernet_key() -> bytes:
    """从 JWT_SECRET 派生 Fernet 密钥。

    Fernet 需要 32 字节 urlsafe base64 key。这里用 SHA-256 把任意长度的
    JWT_SECRET 固定为 32 字节，再 base64 编码。
    """
    secret = settings.JWT_SECRET
    if not secret:
        logger.warning("JWT_SECRET 为空，使用 fallback 密钥加密 API Key — 仅适用于开发环境")
        secret = _FALLBACK_KEY
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def encrypt_api_key(plain_key: str) -> str:
    """加密 API Key，返回可存入数据库的字符串密文。"""
    f = Fernet(_get_fernet_key())
    return f.encrypt(plain_key.encode()).decode()


def decrypt_api_key(encrypted_key: str) -> str:
    """解密数据库中的 API Key 密文。

    如果 JWT_SECRET 变更或密文损坏，Fernet 会抛异常；调用方一般会捕获后
    提示用户重新填写 API Key。
    """
    f = Fernet(_get_fernet_key())
    return f.decrypt(encrypted_key.encode()).decode()
