# -*- coding: utf-8 -*-
"""控制台登录、角色与密码校验服务。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import User

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 200_000
_DEFAULT_ADMIN_USERNAME = "admin"
_DEFAULT_ADMIN_PASSWORD = "admin123456"
_SUPPORTED_ROLES = {"viewer", "operator", "admin"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt_bytes = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, _PBKDF2_ITERATIONS)
    return f"{salt_bytes.hex()}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, digest_hex = stored_hash.split("$", 1)
    except ValueError:
        return False
    computed = _hash_password(password, salt=bytes.fromhex(salt_hex)).split("$", 1)[1]
    return hmac.compare_digest(computed, digest_hex)


class AuthService:
    """用户鉴权与初始化服务。"""

    def ensure_bootstrap_users(self) -> None:
        """启动时确保至少有管理员账号可登录。"""
        self._ensure_user(
            username=os.getenv("ADMIN_USERNAME", _DEFAULT_ADMIN_USERNAME),
            password=os.getenv("ADMIN_PASSWORD", _DEFAULT_ADMIN_PASSWORD),
            role="admin",
        )

        # 额外角色账号是可选项，便于测试或未来多人使用。
        self._ensure_optional_user(
            username=os.getenv("OPERATOR_USERNAME", "").strip(),
            password=os.getenv("OPERATOR_PASSWORD", "").strip(),
            role="operator",
        )
        self._ensure_optional_user(
            username=os.getenv("VIEWER_USERNAME", "").strip(),
            password=os.getenv("VIEWER_PASSWORD", "").strip(),
            role="viewer",
        )

    def authenticate(self, username: str, password: str) -> Optional[User]:
        if not username or not password:
            return None
        with get_session() as session:
            stmt = select(User).where(User.username == username.strip())
            user = session.exec(stmt).first()
            if not user or not user.is_active:
                return None
            if not _verify_password(password, user.password_hash):
                return None
            return user

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        if not user_id:
            return None
        with get_session() as session:
            user = session.get(User, user_id)
            if not user or not user.is_active:
                return None
            return user

    def create_or_update_user(self, username: str, password: str, role: str = "viewer") -> User:
        normalized_role = (role or "viewer").strip().lower()
        if normalized_role not in _SUPPORTED_ROLES:
            raise ValueError(f"不支持的角色: {role}")
        if not username or not password:
            raise ValueError("用户名和密码不能为空")

        now = _utc_now()
        with get_session() as session:
            stmt = select(User).where(User.username == username.strip())
            existing = session.exec(stmt).first()
            password_hash = _hash_password(password)
            if existing:
                existing.password_hash = password_hash
                existing.role = normalized_role
                existing.is_active = True
                existing.updated_at = now
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return existing

            user = User(
                username=username.strip(),
                password_hash=password_hash,
                role=normalized_role,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def _ensure_user(self, username: str, password: str, role: str) -> None:
        user = self.create_or_update_user(username=username, password=password, role=role)
        if username == _DEFAULT_ADMIN_USERNAME and password == _DEFAULT_ADMIN_PASSWORD:
            logger.warning("使用默认管理员账号 %s/%s 启动，仅适合本地开发。", username, password)
        logger.info("已确保角色账号存在: %s(%s)", user.username, user.role)

    def _ensure_optional_user(self, username: str, password: str, role: str) -> None:
        if username and password:
            self._ensure_user(username=username, password=password, role=role)
