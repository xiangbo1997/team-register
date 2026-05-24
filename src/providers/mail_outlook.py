# -*- coding: utf-8 -*-
"""
Outlook / Hotmail 邮箱供应商

对接远端 email-provider 的 ``outlook_email_plus`` provider + ``outlook-pool-default``
config，后者由 outlook-email-plus 容器（自托管账号池）轮换 Outlook 账号拉验证码。

设计立场：
- 仅封装 ``outlook_email_plus`` 专属的"该传什么参数"知识：provider 名 / 默认 config
  name / managed-only session mode / 支持的邮箱域名
- 不重写 HTTP 通信逻辑（``HttpMailProvider`` 已实现完整 email-provider 协议）
- ``can_handle()`` 是路由扩展点，让 ``MailManager`` 按邮箱域名自动挑 provider

与远端 email-provider 的契约（已通过 SSH 实测确认）：
- 端点：``POST /api/mailbox-service/managed-sessions``
- provider 字段：``outlook_email_plus``
- config_name 字段：``outlook-pool-default``（id=3, enabled=true, last_validation_ok=true）
- session_mode 字段：``managed``（outlook_email_plus 是 managed-only，不接受 credentialed）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.providers.mail import HttpMailProvider, MailSession

logger = logging.getLogger(__name__)


class OutlookMailProvider(HttpMailProvider):
    """Outlook/Hotmail/Live/MSN 域名邮箱专属 provider。

    继承 HttpMailProvider 复用 HTTP 通信 / 重试 / 错误分类逻辑，
    仅在 ``create_session`` / ``ensure_runtime_ready`` 强制注入 outlook 专属参数。
    """

    PROVIDER_NAME = "outlook_email_plus"
    DEFAULT_CONFIG_NAME = "outlook-pool-default"
    # 支持的邮箱域名 — 用于 can_handle() 路由判断
    # 注：实际是否能拿到验证码以 email-provider 后端的 outlook-pool-default config
    # + outlook-email-plus 账号池为准；此处仅做本地路由前置判断。
    SUPPORTED_DOMAINS = frozenset({
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "outlook.jp",
        "hotmail.co.uk",
    })

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        config_name: str = "",
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key)
        self._config_name = (config_name or self.DEFAULT_CONFIG_NAME).strip()

    # ── 路由判断 ──────────────────────────────────────

    @classmethod
    def can_handle(cls, email: str) -> bool:
        """判断该 provider 能否处理给定邮箱（按域名匹配）。"""
        if not email or "@" not in email:
            return False
        domain = email.lower().rsplit("@", 1)[-1].strip()
        return domain in cls.SUPPORTED_DOMAINS

    # ── 覆写 HttpMailProvider 注入专属参数 ────────────

    def create_session(
        self,
        provider: str = "",  # 调用方可忽略；子类强制注入 PROVIDER_NAME
        *,
        purpose: str = "generic",
        session_mode: str = "",
        proxy: str = "",
        extra: Optional[dict[str, Any]] = None,
        email: str = "",
        account_id: str = "",
        account_extra: Optional[dict[str, Any]] = None,
        existing_account: Optional[dict[str, Any]] = None,
        lease_seconds: int = 900,
        config_name: str = "",
    ) -> MailSession:
        """创建 outlook session — 强制 managed + 注入 config_name + provider name。

        outlook_email_plus 是 managed-only provider，不接受 credentialed 模式或
        existing_account 参数，调用方传了也会被忽略并记 warning。
        """
        # 警告并丢弃不支持的参数（防止误用）
        if existing_account or account_id or account_extra:
            logger.warning(
                "OutlookMailProvider: outlook_email_plus 是 managed-only provider，"
                "忽略调用方传入的 existing_account / account_id / account_extra"
            )
        if session_mode and session_mode.strip().lower() != "managed":
            logger.warning(
                "OutlookMailProvider: 忽略调用方传入的 session_mode=%s，强制 managed",
                session_mode,
            )

        effective_config_name = (config_name or self._config_name).strip()

        return super().create_session(
            provider=self.PROVIDER_NAME,
            purpose=purpose,
            session_mode="managed",
            proxy=proxy,
            extra=extra,
            email=email,
            lease_seconds=lease_seconds,
            config_name=effective_config_name,
            # 不传 existing_account / account_id / account_extra
        )

    def ensure_runtime_ready(
        self,
        provider: str = "",  # 同 create_session，子类强制注入
        *,
        session_mode: str = "",
    ) -> dict[str, Any]:
        """预检 email-provider 运行态 — 强制用 outlook_email_plus + managed。"""
        return super().ensure_runtime_ready(
            self.PROVIDER_NAME,
            session_mode="managed",
        )
