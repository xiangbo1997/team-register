# -*- coding: utf-8 -*-
"""
自建域名邮箱供应商（CF Worker）

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
- config_name 字段：``mydomain-cfworker``（id=2, enabled=true, last_validation_ok=true）
- session_mode 字段：``managed``（cfworker 是 managed-only，由服务端自动分配邮箱）
- 实际支持域名：来自 email-provider config 的 ``cfworker_domains`` 字段
  （当前为 zhangxb.xyz + cloudsentryai.com，可在 /admin/provider-configs 修改）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.providers.mail import HttpMailProvider, MailSession

logger = logging.getLogger(__name__)


class CFWorkerMailProvider(HttpMailProvider):
    """自建 CF Worker 域名邮箱 provider。

    继承 HttpMailProvider 复用 HTTP 通信 / 重试 / 错误分类逻辑，
    仅在 ``create_session`` / ``ensure_runtime_ready`` 强制注入 cfworker 专属参数。
    """

    PROVIDER_NAME = "cfworker"
    DEFAULT_CONFIG_NAME = "mydomain-cfworker"
    # 支持的邮箱域名 — 来自 cfworker config 的 cfworker_domains 字段
    # 注：远端 config 改了这里也要同步；can_handle() 仅做本地路由前置判断。
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
        """创建 cfworker session — 强制 managed + 注入 config_name + provider name。

        cfworker 是 managed-only auto_allocate provider，由服务端创建邮箱并返回，
        调用方传 existing_account 也会被忽略。
        """
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
        """预检 email-provider 运行态 — 强制用 cfworker + managed。"""
        return super().ensure_runtime_ready(
            self.PROVIDER_NAME,
            session_mode="managed",
        )
