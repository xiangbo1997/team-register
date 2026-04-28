# -*- coding: utf-8 -*-
"""
邮件服务模块 (HTTP API 集成 email-provider)

通过 HTTP API 调用已部署的 email-provider 服务，获取邮箱验证码。
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from src.providers.mail import HttpMailProvider, MailSession


@dataclass(frozen=True)
class _PerEmailOverride:
    """单封邮件的临时 provider 覆盖。

    当 DB 里存在 ``MailAccount(email=...)`` 且其 ``provider_name`` 与全局
    ``MailManager._provider_name`` 不一致时（比如全局 cfworker 但某 outlook
    账号配成 applemail），构造此 override 让本次 ``create_session`` 临时切到
    对应 provider + 凭据，避免邮件请求被错误 provider 处理。

    字段不可变：仅承载本次 _poll_code_via_api 用到的值，不污染 MailManager 状态。
    """

    provider_name: str
    session_mode: str
    config_name: str
    client_id: str
    refresh_token: str
    account_id: str
    extra: dict[str, Any]

logger = logging.getLogger(__name__)

_APPLEMAIL_PROVIDER = "applemail"
_APPLEMAIL_PLACEHOLDER_PASSWORD = "unused"

# 跨端 contract（见 docs/architecture/mail-provider-contract.md）：
# managed-session 必填字段约束按 provider 决定。这些 provider 在 managed
# 模式下必须有 config_name，否则服务端 cfworker / skymail 实现拿不到加密 DB
# 注入的 cfworker_api_url / admin_token，会抛 PROVIDER_NOT_CONFIGURED。
# 与 src/api/routes/config.py:_MAIL_MANAGED_REQUIRED_FIELDS 保持同步。
_PROVIDERS_REQUIRING_CONFIG_NAME = frozenset({"cfworker", "skymail"})


def _mask_email(email: str) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return value[:3] + "***" if value else ""
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        local = local[:1] + "***"
    else:
        local = local[:2] + "***"
    return f"{local}@{domain}"


class MailManager:
    """基于 email-provider HTTP API 的邮件服务包装器"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        provider_name: str = "applemail",
        # 以下为历史兼容参数：当远端 email-provider 未预置 applemail 配置时，
        # 退回为当前邮箱动态注入单账号上下文，避免验证码阶段无配置可查。
        known_accounts: Optional[dict[str, list[dict[str, Any]]]] = None,
        refresh_token: str = "",
        client_id: str = "",
        proxy: str = "",
        preferred_session_mode: str = "",
        config_name: str = "",
    ) -> None:
        self._provider = HttpMailProvider(base_url=base_url, api_key=api_key)
        self._provider_name = str(provider_name or _APPLEMAIL_PROVIDER).strip().lower() or _APPLEMAIL_PROVIDER
        self._proxy = proxy
        self._known_accounts = self._normalize_known_accounts(known_accounts)
        self._preferred_session_mode = str(preferred_session_mode or "").strip().lower()
        # email-provider 的 provider_config name（指向远程 admin DB 里的 mailbox_provider_configs.name）
        # 任务执行时把它一起带给 /managed-sessions，让服务端从 DB 把 cfworker_api_url、admin_token 等 extra 注入。
        self._config_name = str(config_name or "").strip()
        # 保留旧参数引用以兼容日志，但不再用于业务逻辑
        self._refresh_token = refresh_token
        self._client_id = client_id

    def get_latest_mail(self, email: str, mailbox: str = "INBOX") -> None:
        """向后兼容占位，已废弃"""
        logger.warning("get_latest_mail() 已废弃，通过 email-provider HTTP API 轮询。")
        return None

    def get_verification_code(self, email: str, wait_timeout: int = 60) -> Optional[str]:
        """获取邮箱验证码"""
        return self._poll_code_via_api(email, wait_timeout)

    def get_verification_code_via_browser(self, email: str, page=None, wait_timeout: int = 120) -> Optional[str]:
        """
        获取邮箱验证码（已脱离浏览器依赖）。

        保留 page 参数以兼容调用方签名，但不再使用。
        """
        logger.info("开始通过 email-provider HTTP API 获取验证码: %s", _mask_email(email))
        return self._poll_code_via_api(email, wait_timeout)

    def _normalize_known_accounts(
        self,
        known_accounts: Optional[dict[str, list[dict[str, Any]]]],
    ) -> dict[str, list[dict[str, Any]]]:
        normalized: dict[str, list[dict[str, Any]]] = {}
        for provider_name, accounts in dict(known_accounts or {}).items():
            provider_key = str(provider_name or "").strip().lower()
            if not provider_key:
                continue
            items: list[dict[str, Any]] = []
            for item in list(accounts or []):
                if not isinstance(item, dict):
                    continue
                account = dict(item)
                email = str(account.get("email") or "").strip().lower()
                if email:
                    account["email"] = email
                items.append(account)
            if items:
                normalized[provider_key] = items
        return normalized

    def _provider_known_accounts(self) -> list[dict[str, Any]]:
        return list(self._known_accounts.get(self._provider_name, []))

    def _resolve_known_account(self, email: str) -> Optional[dict[str, Any]]:
        target_email = str(email or "").strip().lower()
        for item in self._provider_known_accounts():
            if str(item.get("email") or "").strip().lower() == target_email:
                return dict(item)
        return None

    def _build_provider_extra(self, email: str) -> Optional[dict[str, str]]:
        """当前主路径已改为 session_mode 分流，provider extra 仅保留给托管型 provider。"""
        return None

    def _build_existing_account(self, email: str) -> Optional[dict[str, Any]]:
        known = self._resolve_known_account(email)
        if known:
            credentials = dict(known.get("credentials") or {})
            if not credentials:
                credentials = {
                    "client_id": known.get("client_id", ""),
                    "refresh_token": known.get("refresh_token", ""),
                    "password": known.get("password", _APPLEMAIL_PLACEHOLDER_PASSWORD),
                }
            return {
                "email": email,
                "account_id": str(known.get("account_id") or ""),
                "extra": dict(known.get("extra") or {}),
                "preserve_existing_mail": bool(known.get("preserve_existing_mail", True)),
                "credentials": {
                    "client_id": str(credentials.get("client_id") or ""),
                    "refresh_token": str(credentials.get("refresh_token") or ""),
                    "password": str(credentials.get("password") or _APPLEMAIL_PLACEHOLDER_PASSWORD),
                },
            }

        if self._provider_name != _APPLEMAIL_PROVIDER:
            return None
        if not (self._client_id and self._refresh_token):
            return None
        logger.info("applemail 使用兼容 MAIL_CLIENT_ID / MAIL_REFRESH_TOKEN 接管邮箱: %s", _mask_email(email))
        return {
            "email": email,
            "account_id": "",
            "extra": {},
            "preserve_existing_mail": True,
            "credentials": {
                "client_id": self._client_id,
                "refresh_token": self._refresh_token,
                "password": _APPLEMAIL_PLACEHOLDER_PASSWORD,
            },
        }

    def _resolve_session_mode(self, email: str) -> str:
        if self._preferred_session_mode in {"managed", "credentialed"}:
            return self._preferred_session_mode

        if self._provider_name != _APPLEMAIL_PROVIDER:
            return "managed"

        known_accounts = self._provider_known_accounts()
        if known_accounts:
            if self._resolve_known_account(email):
                return "credentialed"
            if self._client_id and self._refresh_token:
                logger.warning(
                    "applemail 未在 KNOWN_MAIL_ACCOUNTS_JSON 命中 %s，回退到 MAIL_CLIENT_ID / MAIL_REFRESH_TOKEN 兼容凭据。",
                    _mask_email(email),
                )
                return "credentialed"
            raise RuntimeError(
                f"applemail 已配置 KNOWN_MAIL_ACCOUNTS_JSON，但未找到邮箱 {_mask_email(email)} 的账号凭据"
            )

        if self._client_id and self._refresh_token:
            return "credentialed"

        raise RuntimeError(
            "applemail 缺少已知账号配置：请配置 KNOWN_MAIL_ACCOUNTS_JSON，"
            "或至少提供 MAIL_CLIENT_ID / MAIL_REFRESH_TOKEN 兼容凭据。"
        )

    def ensure_runtime_ready(self, email: str) -> str:
        """
        任务执行前对 email-provider 运行态做 latest-only 预检。

        返回解析后的 session_mode，便于上层复用而不重复推断。

        L3 fail-fast：当 session_mode == "managed" 且 provider 在
        ``_PROVIDERS_REQUIRING_CONFIG_NAME`` 白名单（如 cfworker / skymail）
        但本地 ``self._config_name`` 为空时，立即抛 MissingProviderConfigError，
        不发任何 HTTP 请求。这是 L1（UI 校验）/ L2（任务 preflight）的运行时
        兜底 — 直接走 main.py 不通过 API 的入口（旧 .env 用法）也能拦下。

        详见 docs/architecture/mail-provider-contract.md
        """
        session_mode = self._resolve_session_mode(email)

        # L3 本地 fail-fast：白名单 provider managed 模式必须有 config_name
        if (
            session_mode == "managed"
            and self._provider_name in _PROVIDERS_REQUIRING_CONFIG_NAME
            and not (self._config_name or "").strip()
        ):
            from src.providers.mail import MissingProviderConfigError
            raise MissingProviderConfigError(
                f"mail provider '{self._provider_name}' (managed) 必须配置 config_name；"
                f"当前 mail_config_name 为空。请在 /providers 选择对应 mail-default 的 "
                f"provider_config name（如 'mydomain-cfworker'）后重试。",
                error_code="PROVIDER_NOT_CONFIGURED",
                missing_fields=["config_name"],
            )

        self._provider.ensure_runtime_ready(
            self._provider_name,
            session_mode=session_mode,
        )
        return session_mode

    def _resolve_per_email_override(self, email: str) -> Optional[_PerEmailOverride]:
        """如果 DB 里有 ``MailAccount(email=email)`` 且其 ``provider_name`` 与全局
        ``self._provider_name`` 不一致，构造一个临时 override 让本次 create_session
        切到 DB 配置的 provider + 凭据。

        典型场景：``EMAIL_PROVIDER_NAME=cfworker`` 全局，但运维在 /mail-accounts
        加了 outlook 账号配成 ``provider_name=applemail`` + OAuth client_id /
        refresh_token。此时 cfworker 不能处理 outlook 域 → 应当走 applemail 路径。

        若 DB 查询失败 / 账号未找到 / provider_name 与全局一致 → 返回 None，
        调用方走原有 self._provider_name 路径（向后兼容）。
        """
        target_email = (email or "").strip().lower()
        if not target_email:
            return None
        try:
            from src.db.engine import get_session
            from src.db.models import MailAccount
            from sqlmodel import select
        except Exception as exc:
            logger.debug("DB module 不可用，跳过 per-email provider override: %s", exc)
            return None
        try:
            with get_session() as s:
                acc = s.exec(
                    select(MailAccount).where(MailAccount.email == target_email)
                ).first()
                if acc is None or not acc.is_active:
                    return None
                acc_provider = str(acc.provider_name or "").strip().lower()
                if not acc_provider or acc_provider == self._provider_name:
                    # 与全局 provider 一致 → 不需要 override，走原路径
                    return None
                # session_mode 推断：applemail / credentialed-capable provider 默认走
                # credentialed（凭据登录）；其它 managed-only provider 走 managed。
                # 与 _resolve_session_mode 同款规则 — 这里复制一份避免对 self._provider_name
                # 的依赖，因为 override 路径下 self._provider_name 不再代表本次请求的 provider。
                if acc_provider == _APPLEMAIL_PROVIDER:
                    session_mode = "credentialed"
                else:
                    session_mode = "managed"
                logger.info(
                    "MailManager: %s 命中 DB MailAccount provider 覆盖：%s → %s (session_mode=%s)",
                    _mask_email(target_email),
                    self._provider_name,
                    acc_provider,
                    session_mode,
                )
                return _PerEmailOverride(
                    provider_name=acc_provider,
                    session_mode=session_mode,
                    # cfworker / skymail 等需要 config_name；applemail 走凭据不需要 config_name
                    config_name="",
                    client_id=str(acc.client_id or ""),
                    refresh_token=str(acc.refresh_token or ""),
                    account_id=str((acc.extra or {}).get("account_id") or ""),
                    extra=dict(acc.extra or {}),
                )
        except Exception as exc:
            logger.debug("查询 MailAccount provider 覆盖失败（按全局 provider 处理）: %s", exc)
            return None

    def _poll_code_via_api(self, email: str, wait_timeout: int) -> Optional[str]:
        """通过 HTTP API 创建会话并轮询验证码"""
        session: Optional[MailSession] = None
        try:
            # 优先按 DB MailAccount 配置临时切 provider（per-email override），
            # 让全局 EMAIL_PROVIDER_NAME 能跟个别邮箱使用的 provider 不一致而不冲突。
            override = self._resolve_per_email_override(email)
            if override is not None:
                effective_provider = override.provider_name
                effective_config_name = override.config_name
                session_mode = override.session_mode
                if session_mode == "credentialed":
                    if override.client_id and override.refresh_token:
                        existing_account = {
                            "email": email,
                            "account_id": override.account_id,
                            "extra": dict(override.extra or {}),
                            "preserve_existing_mail": bool(
                                (override.extra or {}).get("preserve_existing_mail", True)
                            ),
                            "credentials": {
                                "client_id": override.client_id,
                                "refresh_token": override.refresh_token,
                                "password": _APPLEMAIL_PLACEHOLDER_PASSWORD,
                            },
                        }
                    else:
                        # credentialed 模式但 DB 里没填凭据 — 让服务端按错路径报 422
                        # PROVIDER_NOT_CONFIGURED，而不是这里默默 fallback 到 managed
                        existing_account = None
                else:
                    existing_account = None
                # override 路径下不调用 self.ensure_runtime_ready（它依赖 self._provider_name）
                # 直接对 effective_provider 做轻量 health 检查
                self._provider.ensure_runtime_ready(
                    effective_provider, session_mode=session_mode,
                )
            else:
                effective_provider = self._provider_name
                effective_config_name = self._config_name
                session_mode = self.ensure_runtime_ready(email)
                existing_account = self._build_existing_account(email) if session_mode == "credentialed" else None

            logger.info(
                "创建邮箱会话: email=%s provider=%s session_mode=%s timeout=%ss",
                _mask_email(email),
                effective_provider,
                session_mode,
                wait_timeout,
            )
            session = self._provider.create_session(
                provider=effective_provider,
                purpose="otp",
                session_mode=session_mode,
                email=email,
                proxy=self._proxy,
                extra=self._build_provider_extra(email),
                existing_account=existing_account,
                config_name=effective_config_name,
            )
            logger.info(
                "邮箱会话已创建: session_id=%s email=%s session_mode=%s",
                session.session_id,
                _mask_email(session.email),
                session.session_mode,
            )

            code = self._provider.poll_code(session, timeout_seconds=wait_timeout)
            if code:
                logger.info("成功捕获邮件验证码: %s", code)
                self._complete_session(session, "success")
                return code

            logger.warning(
                "轮询验证码超时: email=%s timeout=%ss session_id=%s session_mode=%s",
                _mask_email(email),
                wait_timeout,
                getattr(session, "session_id", ""),
                getattr(session, "session_mode", ""),
            )
            self._complete_session(session, "failed", "timeout")
        except ConnectionError as exc:
            logger.error(
                "邮件服务连接失败: email=%s session_id=%s error=%s",
                _mask_email(email),
                getattr(session, "session_id", "") if session else "",
                exc,
            )
            if session:
                self._complete_session(session, "failed", str(exc))
            raise
        except RuntimeError as exc:
            logger.error(
                "邮件服务返回致命错误: email=%s session_id=%s session_mode=%s error=%s",
                _mask_email(email),
                getattr(session, "session_id", "") if session else "",
                getattr(session, "session_mode", "") if session else "",
                exc,
            )
            if session:
                self._complete_session(session, "failed", str(exc))
            raise
        except Exception as exc:
            logger.error("获取邮件验证码异常: email=%s error=%s", _mask_email(email), exc)
            if session:
                self._complete_session(session, "failed", str(exc))
        return None

    def _complete_session(self, session: MailSession, result: str, reason: str = "") -> None:
        """安全地完成会话，不抛异常"""
        try:
            self._provider.complete(session, result=result, reason=reason)
        except Exception as exc:
            logger.warning("完成邮箱会话失败 (非致命): %s", exc)

    def clear_mailbox(self, email: str, folder: str = "inbox") -> bool:
        """清空邮箱（HTTP API 模式下由服务端管理，此处为兼容占位）"""
        logger.info("clear_mailbox() 在 HTTP API 模式下无需主动调用")
        return True
