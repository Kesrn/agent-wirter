"""API Key 加密/解密工具

使用 Fernet 对称加密，密钥从 JWT_SECRET 通过 SHA-256 派生。
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet

from config.settings import settings

logger = logging.getLogger(__name__)

_FALLBACK_KEY = "ai-creative-platform-default-key-not-for-production"


def _get_fernet_key() -> bytes:
    """从 JWT_SECRET 派生 Fernet 密钥"""
    secret = settings.JWT_SECRET
    if not secret:
        logger.warning("JWT_SECRET 为空，使用 fallback 密钥加密 API Key — 仅适用于开发环境")
        secret = _FALLBACK_KEY
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def encrypt_api_key(plain_key: str) -> str:
    """加密 API Key"""
    f = Fernet(_get_fernet_key())
    return f.encrypt(plain_key.encode()).decode()


def decrypt_api_key(encrypted_key: str) -> str:
    """解密 API Key"""
    f = Fernet(_get_fernet_key())
    return f.decrypt(encrypted_key.encode()).decode()