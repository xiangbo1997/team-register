# -*- coding: utf-8 -*-
"""
DB 字段对称加密层

为持久化的敏感字段提供透明加密（Fernet/AES-128-CBC + HMAC）。

使用约定：
- 通过环境变量 ``DB_ENCRYPTION_KEY`` 注入 32 字节 base64 密钥
- 未配置时，所有读写**直接透传**（dev/test 友好），但会通过 logger 提示一次
- 数据库里的密文统一带 ``enc:v1:`` 前缀，便于识别旧明文与未来版本迁移
- 解密失败时**返回原文**而非抛出，确保旧明文记录在密钥轮换前仍可读
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import String, Text, TypeDecorator

logger = logging.getLogger(__name__)

# 密文前缀，用来区分"明文"与"已加密密文"
_PREFIX = "enc:v1:"

# 进程内单例缓存
_lock = threading.Lock()
_fernet: Optional[Fernet] = None
_warned_missing_key = False


def _normalize_key(raw: str) -> Optional[bytes]:
    """把任意形式的密钥规范化成 Fernet 接受的 32 字节 urlsafe base64。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("utf-8"))
        if len(decoded) == 32:
            return raw.encode("utf-8")
    except Exception:
        pass
    # 任意字符串：右补到 32 字节再 base64
    padded = raw.encode("utf-8")[:32].ljust(32, b"\0")
    return base64.urlsafe_b64encode(padded)


def _get_fernet() -> Optional[Fernet]:
    """懒加载并缓存 Fernet 实例。"""
    global _fernet, _warned_missing_key
    if _fernet is not None:
        return _fernet
    with _lock:
        if _fernet is not None:
            return _fernet
        key_material = _normalize_key(os.getenv("DB_ENCRYPTION_KEY", ""))
        if key_material is None:
            if not _warned_missing_key:
                logger.warning(
                    "未配置 DB_ENCRYPTION_KEY，敏感字段将以明文形式持久化（仅适合开发/测试）。"
                )
                _warned_missing_key = True
            return None
        _fernet = Fernet(key_material)
        return _fernet


def reset_for_tests() -> None:
    """测试钩子：清空缓存的 Fernet，便于切换 key。"""
    global _fernet, _warned_missing_key
    with _lock:
        _fernet = None
        _warned_missing_key = False


def encrypt_value(plain: Optional[str]) -> str:
    """加密单个字符串。空值/None 透传，密钥缺失时透传。"""
    if not plain:
        return plain or ""
    if isinstance(plain, str) and plain.startswith(_PREFIX):
        return plain  # 已经是密文，避免双重加密
    fernet = _get_fernet()
    if fernet is None:
        return plain
    token = fernet.encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_value(stored: Optional[str]) -> str:
    """解密。非密文（旧明文）原样返回，确保读路径永不崩。"""
    if not stored:
        return stored or ""
    if not isinstance(stored, str) or not stored.startswith(_PREFIX):
        return stored
    fernet = _get_fernet()
    if fernet is None:
        # 数据库里有密文但密钥丢了：返回密文原串，让上层察觉
        logger.error("DB_ENCRYPTION_KEY 未配置，但数据库存在密文字段；返回密文原串。")
        return stored
    payload = stored[len(_PREFIX):]
    try:
        return fernet.decrypt(payload.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("解密失败（密钥可能已轮换或密文损坏），返回密文原串。")
        return stored


class EncryptedString(TypeDecorator):
    """SQLAlchemy 字段类型：写入自动加密，读取自动解密。"""

    impl = Text
    cache_ok = True

    def __init__(self, length: Optional[int] = None) -> None:
        # length 仅作为 hint 影响后端 DDL，逻辑不依赖
        if length is not None and length <= 255:
            self.impl = String(length * 4)  # 密文长度约为明文 1.3-2x，留余量
        super().__init__()

    def process_bind_param(self, value, dialect):  # noqa: D401, ANN001
        if value is None:
            return None
        return encrypt_value(str(value))

    def process_result_value(self, value, dialect):  # noqa: D401, ANN001
        if value is None:
            return None
        return decrypt_value(str(value))


def is_ciphertext(value: Optional[str]) -> bool:
    """判断字段是否已经是密文（迁移脚本用）。"""
    return bool(value) and isinstance(value, str) and value.startswith(_PREFIX)


def generate_key() -> str:
    """生成一把新的 base64 密钥，便于初始化 .env。"""
    return Fernet.generate_key().decode("ascii")
