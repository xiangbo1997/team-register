# -*- coding: utf-8 -*-
"""自建域名邮箱供应商（CF Worker）。

对接远端 email-provider 的 ``cfworker`` provider + ``mydomain-cfworker`` config，
后者调用 Cloudflare Worker 接口创建/分配 zhangxb.xyz / cloudsentryai.com 域邮箱。

设计立场：
- 仅封装 ``cfworker`` 专属的"该传什么参数"知识：provider 名 / 默认 config name /
  managed-only session mode / 支持的邮箱域名
- 不重写 HTTP 通信逻辑（``HttpMailProvider`` 已实现完整 email-provider 协议）
- ``can_handle()`` 是路由扩展点，让 ``MailManager`` 按邮箱域名自动挑 provider

与远端 email-provider 的契约（已通过 SSH 实测确认）：
- 端点：``POST /api/mailbox-service/managed-sessions``
- provider 字段：``cfworker``
- config_name 字段：``mydomain-cfworker``
- session_mode 字段：``managed``（cfworker 是 managed-only）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.providers.base import FieldSpec, register_provider
from src.providers.mail import HttpMailProvider, MailSession

logger = logging.getLogger(__name__)


@register_provider(
    provider_type="mail",
    kind="cfworker",
    display_name="CF Worker 自建域名邮箱",
    description="对接 email-provider 的 cfworker provider，managed-only auto_allocate",
    schema=(
        FieldSpec(
            name="base_url",
            type="str",
            required=True,
            description="email-provider 服务端 base URL（如 http://127.0.0.1:8000）",
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
            default="mydomain-cfworker",
            description="服务端 ProviderConfig 名（决定用哪份 cfworker 凭据）",
        ),
    ),
)
class CFWorkerMailProvider(HttpMailProvider):
    """自建 CF Worker 域名邮箱 provider。

    继承 HttpMailProvider 复用 HTTP 通信 / 重试 / 错误分类逻辑，
    仅在 ``create_session`` / ``ensure_runtime_ready`` 强制注入 cfworker 专属参数。
    """

    PROVIDER_NAME = "cfworker"
    DEFAULT_CONFIG_NAME = "mydomain-cfworker"
    SUPPORTED_DOMAINS = frozenset({
        "zhangxb.xyz",
        "cloudsentryai.com",
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
                "CFWorkerMailProvider: cfworker 是 managed-only auto_allocate provider，"
                "忽略调用方传入的 existing_account / account_id / account_extra"
            )
        if session_mode and session_mode.strip().lower() != "managed":
            logger.warning(
                "CFWorkerMailProvider: 忽略调用方传入的 session_mode=%s，强制 managed",
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
