# -*- coding: utf-8 -*-
"""Outlook / Hotmail 邮箱供应商。

对接远端 email-provider 的 ``outlook_email_plus`` provider + ``outlook-pool-default``
config，后者由 outlook-email-plus 容器（自托管账号池）轮换 Outlook 账号拉验证码。

与远端 email-provider 的契约（已通过 SSH 实测确认）：
- 端点：``POST /api/mailbox-service/managed-sessions``
- provider 字段：``outlook_email_plus``
- config_name 字段：``outlook-pool-default``
- session_mode 字段：``managed``
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.providers.base import FieldSpec, register_provider
from src.providers.mail import HttpMailProvider, MailSession

logger = logging.getLogger(__name__)


@register_provider(
    provider_type="mail",
    kind="outlook_email_plus",
    display_name="Outlook / Hotmail（自托管池）",
    description="对接 email-provider 的 outlook_email_plus provider，managed-only",
    schema=(
        FieldSpec(
            name="base_url",
            type="str",
            required=True,
            description="email-provider 服务端 base URL",
        ),
        FieldSpec(
            name="api_key",
            type="secret",
            required=False,
            description="email-provider API key（可选）",
        ),
        FieldSpec(
            name="config_name",
            type="str",
            required=False,
            default="outlook-pool-default",
            description="服务端 ProviderConfig 名",
        ),
    ),
)
class OutlookMailProvider(HttpMailProvider):
    """Outlook/Hotmail/Live/MSN 域名邮箱专属 provider。"""

    PROVIDER_NAME = "outlook_email_plus"
    DEFAULT_CONFIG_NAME = "outlook-pool-default"
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

    @classmethod
    def can_handle(cls, email: str) -> bool:
        if not email or "@" not in email:
            return False
        domain = email.lower().rsplit("@", 1)[-1].strip()
        return domain in cls.SUPPORTED_DOMAINS

    def create_session(
        self,
        provider: str = "",
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
        )

    def ensure_runtime_ready(
        self,
        provider: str = "",
        *,
        session_mode: str = "",
    ) -> dict[str, Any]:
        return super().ensure_runtime_ready(
            self.PROVIDER_NAME,
            session_mode="managed",
        )
