# -*- coding: utf-8 -*-
"""
配置管理 API

GET/PUT /api/config — 应用配置读写
GET/PUT/DELETE /api/providers/* — Provider 配置 CRUD
POST /api/providers/{type}/{name}/test — 连通性测试
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api.deps import get_audit_service, get_config_service
from src.api.i18n import _
from src.api.security import require_authenticated_user, require_csrf, require_role
from src.db.models import User
from src.services.audit_service import AuditService
from src.services.config_service import ConfigService

router = APIRouter(prefix="/api", tags=["config"])


# ── 请求/响应模型 ────────────────────────────────


class ConfigUpdateRequest(BaseModel):
    updates: dict[str, Any]


class ProviderConfigRequest(BaseModel):
    config: dict[str, Any]
    is_active: bool = True


class MailAccountRequest(BaseModel):
    label: str
    provider_name: str
    email: str
    client_id: str = ""
    refresh_token: str = ""
    extra: dict[str, Any] = {}
    is_active: bool = True
    # role 取值: 'regular'（默认，注册流程用）或 'pro_warmup'（卡预热垫脚石）
    role: str = "regular"


# ── Mail provider 配置完整性校验集中规则 ──
#
# 跨端 contract（见 docs/architecture/mail-provider-contract.md）：
# managed-session 必填字段约束按 provider 决定；admin UI 创建 ProviderConfig
# 时如果不校验，错配置会一直跑到 worker 启动任务才崩成裸 5xx，影响运维体验。
# 这里是 4 层 fail-fast 的 L1 — 在配置入库前拦下错配。
_MAIL_MANAGED_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    # cfworker / skymail 这类"服务端调上游"的 provider，必须有 config_name
    # 让服务端从加密 DB 注入 cfworker_api_url / admin_token 等
    "cfworker": frozenset({"config_name"}),
    "skymail": frozenset({"config_name"}),
    # outlook_email_plus 自托管账号池 — 必须有 config_name 指向 outlook-pool-default
    # 让服务端从加密 DB 注入 outlook_email_plus_api_url + api_key
    "outlook_email_plus": frozenset({"config_name"}),
    # freemail / tempmail_lol 等 auto-allocate 类不需要 config_name；不在白名单的
    # provider 默认不强制校验，避免锁死扩展
}


def _validate_mail_provider_config(provider_name: str, config: dict[str, Any]) -> None:
    """L1 fail-fast — admin UI 创建/更新 mail-* ProviderConfig 时强制必填字段。

    **插拔式优先**：先委托给 ``ProviderRegistry.validate("mail", kind, config)``，
    每个 mail provider 类自己在 ``@register_provider(schema=...)`` 里声明必填字段。
    新增 mail provider 自动获得校验能力，**无需修改本函数**。

    **白名单兼容**：如果 kind 未在 registry 注册（向后兼容旧 provider），回落到
    ``_MAIL_MANAGED_REQUIRED_FIELDS`` 老规则。
    """
    payload = dict(config or {})
    session_mode = str(payload.get("session_mode") or "managed").strip().lower()
    if session_mode != "managed":
        return
    # config 内的 provider_name 即 kind（mail provider 的历史命名）
    normalized_kind = str(payload.get("provider_name") or provider_name or "").strip().lower()

    # 路径 1：registry 驱动
    try:
        from src.providers import get_registry

        meta = get_registry().get_meta("mail", normalized_kind)
        if meta is not None:
            missing = get_registry().validate("mail", normalized_kind, payload)
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "PROVIDER_NOT_CONFIGURED",
                        "message": (
                            f"mail provider '{normalized_kind}' (managed) "
                            f"必须配置: {', '.join(missing)}"
                        ),
                        "missing_fields": missing,
                    },
                )
            return
    except HTTPException:
        raise
    except Exception as exc:  # registry 异常不阻塞，回落老规则
        import logging
        logging.getLogger(__name__).warning(
            "registry 校验 mail/%s 异常，回落白名单: %s", normalized_kind, exc,
        )

    # 路径 2：旧白名单兼容
    required = _MAIL_MANAGED_REQUIRED_FIELDS.get(normalized_kind)
    if not required:
        return
    missing = [field for field in sorted(required) if not str(payload.get(field) or "").strip()]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PROVIDER_NOT_CONFIGURED",
                "message": (
                    f"mail provider '{normalized_kind}' (managed) "
                    f"必须配置: {', '.join(missing)}"
                ),
                "missing_fields": missing,
            },
        )


def _validate_sms_provider_config(provider_name: str, config: dict[str, Any]) -> None:
    """SMS provider 必填字段：api_key（country 可选，默认 0=any）。"""
    api_key = str((config or {}).get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PROVIDER_NOT_CONFIGURED",
                "message": f"sms provider '{provider_name}' 必须配置: api_key",
                "missing_fields": ["api_key"],
            },
        )


def _validate_llm_provider_config(provider_name: str, config: dict[str, Any]) -> None:
    """LLM provider 必填：base_url + api_key + model。"""
    payload = config or {}
    missing = [
        field
        for field in ("base_url", "api_key", "model")
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PROVIDER_NOT_CONFIGURED",
                "message": (
                    f"llm provider '{provider_name}' 必须配置: {', '.join(missing)}"
                ),
                "missing_fields": missing,
            },
        )


_CAPTCHA_VALID_KINDS = frozenset({"noop", "manual", "nocaptcha"})


def _validate_captcha_provider_config(provider_name: str, config: dict[str, Any]) -> None:
    """Captcha provider 校验：
    - kind 必填且属于 {noop, manual, nocaptcha}
    - kind=nocaptcha 时 user_token 非空
    """
    payload = config or {}
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in _CAPTCHA_VALID_KINDS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PROVIDER_NOT_CONFIGURED",
                "message": (
                    f"captcha provider '{provider_name}' kind 必须是 "
                    f"{sorted(_CAPTCHA_VALID_KINDS)} 之一"
                ),
                "missing_fields": ["kind"],
            },
        )
    if kind == "nocaptcha":
        token = str(payload.get("user_token") or "").strip()
        if not token:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PROVIDER_NOT_CONFIGURED",
                    "message": (
                        f"captcha provider '{provider_name}' kind=nocaptcha "
                        f"必须配置: user_token"
                    ),
                    "missing_fields": ["user_token"],
                },
            )


def _validate_provider_config(provider_type: str, provider_name: str, config: dict[str, Any]) -> None:
    """按 provider_type 分派到对应 validator。未识别类型直接放行（向后兼容）。"""
    type_norm = str(provider_type or "").strip().lower()
    if type_norm == "mail":
        _validate_mail_provider_config(provider_name, config)
    elif type_norm == "sms":
        _validate_sms_provider_config(provider_name, config)
    elif type_norm == "llm":
        _validate_llm_provider_config(provider_name, config)
    elif type_norm == "captcha":
        _validate_captcha_provider_config(provider_name, config)
    # browser / card 暂不强制必填，保持当前行为


def _validate_pro_warmup_credentials(body: MailAccountRequest, *, is_update: bool = False) -> None:
    """role=pro_warmup 仅强制必填 email（DB 主键级标识）。

    历史上这里曾强制必填 ``email + extra.adspower_profile_id + extra.password``，
    假设所有 pro_warmup 账号都走"ChatGPT 密码登录"路径。但实测发现一些 OAuth-only
    邮箱（如 outlook + Microsoft Graph）需要走 ``client_id + refresh_token`` 路径
    拉验证码 — 此时 ``adspower_profile_id`` / ``password`` 反而留空。

    现状：**只校验 email**，其它字段全部可选。具体哪些字段必须有取决于运行时
    选择哪条路径（OAuth / 密码登录），由 caller（execute_card_warmup 等）在
    使用时按场景再校验，避免 UI 强制必填导致用户填不进去。
    """
    if str(body.role or "").strip().lower() != "pro_warmup":
        return
    if not str(body.email or "").strip():
        raise HTTPException(
            status_code=422,
            detail="pro_warmup 账号必须填 email（账号唯一标识）",
        )


def _provider_to_dict(svc: ConfigService, config) -> dict[str, Any]:
    return {
        "id": config.id,
        "provider_type": config.provider_type,
        "provider_name": config.provider_name,
        "config": svc.redact_provider_config(config.config),
        "is_active": config.is_active,
        "updated_at": config.updated_at.isoformat() if config.updated_at else "",
    }


def _mail_account_to_dict(svc: ConfigService, account) -> dict[str, Any]:
    return svc.redact_mail_account(account)


def _audit_or_raise(audit_service: AuditService, *, action_type: str, payload: dict[str, Any]) -> None:
    audit = audit_service.review({"action_type": action_type, "payload": payload})
    status_value = str(audit.get("status") or "")
    if status_value == "ALLOW":
        return
    detail = str(audit.get("reason") or "该操作未通过隐藏审核链")
    if status_value == "NEEDS_INFO":
        raise HTTPException(status_code=400, detail=detail)
    raise HTTPException(status_code=403, detail=detail)


# ── 应用配置 ──────────────────────────────────────


@router.get("/config")
def get_config(
    user: User = Depends(require_authenticated_user),
    svc: ConfigService = Depends(get_config_service),
):
    """获取当前配置（脱敏）。"""
    return svc.get_config_snapshot()


@router.put("/config")
def update_config(
    body: ConfigUpdateRequest,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    """动态更新配置。"""
    _audit_or_raise(audit_service, action_type="update_config", payload={"updates": dict(body.updates or {})})
    svc.update_config(body.updates, allowed_fields=svc.SAFE_UPDATE_FIELDS)
    return svc.get_config_snapshot()


@router.post("/config/reload")
def reload_config(
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    _audit_or_raise(audit_service, action_type="reload_config", payload={})
    svc.reload_base()
    return svc.get_config_snapshot()


# ── Provider 配置 ─────────────────────────────────


@router.get("/providers")
def list_providers(
    provider_type: str | None = None,
    active_only: bool = False,
    user: User = Depends(require_authenticated_user),
    svc: ConfigService = Depends(get_config_service),
):
    """列出 Provider 配置。"""
    configs = svc.get_provider_configs(provider_type, active_only=active_only)
    return [_provider_to_dict(svc, c) for c in configs]


@router.get("/providers/{provider_type}/{provider_name}")
def get_provider(
    request: Request,
    provider_type: str,
    provider_name: str,
    user: User = Depends(require_authenticated_user),
    svc: ConfigService = Depends(get_config_service),
):
    """获取单个 Provider 配置。"""
    config = svc.get_provider_config(provider_type, provider_name)
    if not config:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))
    return _provider_to_dict(svc, config)


@router.put("/providers/{provider_type}/{provider_name}")
def upsert_provider(
    provider_type: str,
    provider_name: str,
    body: ProviderConfigRequest,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    """创建或更新 Provider 配置。

    L1 fail-fast：mail / sms / llm / captcha provider 必填字段在入库前校验。
    """
    _validate_provider_config(provider_type, provider_name, dict(body.config or {}))
    _audit_or_raise(
        audit_service,
        action_type="upsert_provider",
        payload={
            "provider_type": provider_type,
            "provider_name": provider_name,
            "config": dict(body.config or {}),
            "is_active": body.is_active,
        },
    )
    saved = svc.save_provider_config(
        provider_type=provider_type,
        provider_name=provider_name,
        config=body.config,
        is_active=body.is_active,
        actor=user.id,
    )
    return _provider_to_dict(svc, saved)


@router.delete("/providers/{provider_type}/{provider_name}")
def delete_provider(
    request: Request,
    provider_type: str,
    provider_name: str,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    """删除 Provider 配置。"""
    _audit_or_raise(
        audit_service,
        action_type="delete_provider",
        payload={"provider_type": provider_type, "provider_name": provider_name},
    )
    deleted = svc.delete_provider_config(provider_type, provider_name, actor=user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))
    return {"ok": True}


@router.post("/providers/{provider_type}/{provider_name}/test")
def test_provider(
    request: Request,
    provider_type: str,
    provider_name: str,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    """测试 Provider 连通性（预留）。"""
    _audit_or_raise(
        audit_service,
        action_type="test_provider",
        payload={"provider_type": provider_type, "provider_name": provider_name},
    )
    config = svc.get_provider_config(provider_type, provider_name)
    if not config:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))

    payload = dict(config.config or {})
    ok = True
    message = "provider 配置结构校验通过"
    if provider_type == "browser":
        if str(payload.get("driver") or "adspower").strip().lower() != "adspower":
            ok = False
            message = "browser provider 目前仅支持 adspower"
        elif not str(payload.get("ads_api") or payload.get("api_url") or "").strip():
            ok = False
            message = "browser provider 缺少 ads_api/api_url"
    elif provider_type == "card":
        driver = str(payload.get("driver") or "").strip().lower()
        if driver not in {"efuncard", "nodecard", "x988card"}:
            ok = False
            message = "card provider.driver 必须是 efuncard / nodecard / x988card"
        elif driver == "efuncard" and not str(payload.get("efuncard_token") or "").strip():
            ok = False
            message = "efuncard provider 缺少 efuncard_token"
        elif driver == "x988card" and not str(payload.get("x988card_api_base") or "").strip():
            ok = False
            message = "x988card provider 缺少 x988card_api_base（默认 https://cards.779.chat）"
    elif provider_type == "mail":
        provider_value = str(payload.get("provider_name") or "").strip().lower()
        session_mode = str(payload.get("session_mode") or "managed").strip().lower()
        if not provider_value:
            ok = False
            message = "mail provider 缺少 provider_name"
        elif session_mode not in {"managed", "credentialed"}:
            ok = False
            message = "mail provider.session_mode 必须是 managed 或 credentialed"
    elif provider_type == "sms":
        # SMS provider 走真实连通性自检：实例化对应 driver → 调 test_connection
        # 比 browser/card/mail 三类「只看 schema」严格一档，因为 SMS 的 api_key
        # 是否有效在保存当下就能验证（getBalance 端点不消耗配额）
        from src.providers.registry import (
            ProviderConfigInvalidError,
            ProviderNotRegisteredError,
            get_registry,
        )

        registry = get_registry()
        # ProviderRegistry._extract_kind_and_config 同款逻辑：优先 driver，再
        # provider_name 字段，最后兜底 pc.provider_name 实例名
        raw_cfg = dict(payload)
        kind = (
            str(raw_cfg.pop("driver", "") or "").strip()
            or str(raw_cfg.pop("provider_name", "") or "").strip()
            or str(provider_name or "").strip()
        )
        try:
            provider_instance = registry.build("sms", kind, raw_cfg)
        except ProviderNotRegisteredError:
            ok = False
            available = registry.list_kinds("sms")
            message = (
                f"sms provider driver={kind!r} 未注册；"
                f"当前可用: {available or '(空，请先重启服务让 discover 扫到新 driver)'}"
            )
        except ProviderConfigInvalidError as exc:
            ok = False
            message = f"配置无效: {exc}"
        except (ValueError, TypeError) as exc:
            # SmsActivateProvider __init__ 的 operator/max_price 互斥校验等
            ok = False
            message = f"配置无效: {exc}"
        else:
            try:
                result = provider_instance.test_connection()
                ok = bool(result.get("ok"))
                message = (
                    str(result.get("message") or "")
                    or ("连通正常" if ok else "连通失败")
                )
            except NotImplementedError:
                # 子类没实现自检 → 只能给个静态消息
                ok = True
                message = "provider 配置结构校验通过（该 driver 未实现 test_connection）"
            except Exception as exc:  # noqa: BLE001 — provider 自检无论何种错误都不应阻塞端点
                ok = False
                message = f"测试失败: {exc}"
    else:
        ok = False
        message = "当前仅支持 browser/card/mail/sms 四类 provider"
    return {"ok": ok, "message": message}


@router.get("/providers/{provider_type}/{provider_name}/revisions")
def list_provider_revisions(
    request: Request,
    provider_type: str,
    provider_name: str,
    user: User = Depends(require_role("admin")),
    svc: ConfigService = Depends(get_config_service),
):
    config = svc.get_provider_config(provider_type, provider_name)
    revisions = svc.list_provider_revisions(provider_type, provider_name)
    if not config and not revisions:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))
    return [
        {
            "id": item.id,
            "provider_type": item.provider_type,
            "provider_name": item.provider_name,
            "snapshot": redact_provider_snapshot(svc, item.snapshot),
            "action_log_id": item.action_log_id,
            "created_by": item.created_by,
            "created_at": item.created_at.isoformat() if item.created_at else "",
        }
        for item in revisions
    ]


@router.post("/providers/{provider_type}/{provider_name}/revisions/{revision_id}/rollback")
def rollback_provider_revision(
    request: Request,
    provider_type: str,
    provider_name: str,
    revision_id: int,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    _audit_or_raise(
        audit_service,
        action_type="rollback_provider",
        payload={
            "provider_type": provider_type,
            "provider_name": provider_name,
            "revision_id": revision_id,
        },
    )
    try:
        restored = svc.rollback_provider_config(revision_id, actor=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if restored is None:
        return {"ok": True, "deleted": True}
    if restored.provider_type != provider_type or restored.provider_name != provider_name:
        raise HTTPException(status_code=400, detail="revision 与目标 provider 不匹配")
    return _provider_to_dict(svc, restored)


def redact_provider_snapshot(svc: ConfigService, snapshot: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(snapshot or {})
    if isinstance(redacted.get("config"), dict):
        redacted["config"] = svc.redact_provider_config(dict(redacted["config"]))
    return redacted


@router.get("/mail-accounts")
def list_mail_accounts(
    provider_name: Optional[str] = Query(None),
    active_only: bool = Query(False),
    user: User = Depends(require_authenticated_user),
    svc: ConfigService = Depends(get_config_service),
):
    accounts = svc.get_mail_accounts(provider_name=provider_name, active_only=active_only)
    return [_mail_account_to_dict(svc, account) for account in accounts]


@router.get("/mail-accounts/{account_id}")
def get_mail_account(
    account_id: str,
    request: Request,
    user: User = Depends(require_authenticated_user),
    svc: ConfigService = Depends(get_config_service),
):
    account = svc.get_mail_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))
    return _mail_account_to_dict(svc, account)


@router.post("/mail-accounts")
def create_mail_account(
    body: MailAccountRequest,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    _validate_pro_warmup_credentials(body)
    _audit_or_raise(
        audit_service,
        action_type="create_mail_account",
        payload={
            "provider_name": body.provider_name,
            "email": body.email,
            "label": body.label,
            "is_active": body.is_active,
            "role": body.role,
        },
    )
    from src.services.config_service import InvalidMailAccountRoleError
    try:
        account = svc.save_mail_account(
            label=body.label,
            provider_name=body.provider_name,
            email=body.email,
            client_id=body.client_id,
            refresh_token=body.refresh_token,
            extra=body.extra,
            is_active=body.is_active,
            role=body.role,
        )
    except InvalidMailAccountRoleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _mail_account_to_dict(svc, account)


@router.put("/mail-accounts/{account_id}")
def update_mail_account(
    account_id: str,
    body: MailAccountRequest,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    _validate_pro_warmup_credentials(body, is_update=True)
    _audit_or_raise(
        audit_service,
        action_type="update_mail_account",
        payload={
            "account_id": account_id,
            "provider_name": body.provider_name,
            "email": body.email,
            "label": body.label,
            "is_active": body.is_active,
            "role": body.role,
        },
    )
    from src.services.config_service import InvalidMailAccountRoleError
    try:
        account = svc.save_mail_account(
            account_id=account_id,
            label=body.label,
            provider_name=body.provider_name,
            email=body.email,
            client_id=body.client_id,
            refresh_token=body.refresh_token,
            extra=body.extra,
            is_active=body.is_active,
            role=body.role,
        )
    except InvalidMailAccountRoleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _mail_account_to_dict(svc, account)


@router.delete("/mail-accounts/{account_id}")
def delete_mail_account(
    account_id: str,
    request: Request,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    _audit_or_raise(
        audit_service,
        action_type="delete_mail_account",
        payload={"account_id": account_id},
    )
    from src.services.config_service import MailAccountInDefaultUseError
    try:
        deleted = svc.delete_mail_account(account_id)
    except MailAccountInDefaultUseError as exc:
        # 409 Conflict：删除被默认引用的账号需要先改 default_mail_account_id
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))
    return {"ok": True}


@router.post("/mail-accounts/{account_id}/test")
def test_mail_account(
    account_id: str,
    request: Request,
    user: User = Depends(require_role("admin")),
    _: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    audit_service: AuditService = Depends(get_audit_service),
):
    _audit_or_raise(
        audit_service,
        action_type="test_mail_account",
        payload={"account_id": account_id},
    )
    account = svc.get_mail_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=_(request, "api.provider_not_found"))
    ok = bool(account.email and account.client_id and account.refresh_token)
    if ok:
        svc.mark_mail_account_verified(account_id)
    return {
        "ok": ok,
        "message": "邮箱账号结构校验通过" if ok else "邮箱账号缺少 email/client_id/refresh_token",
    }


# ── 字段 effective source 端点（消除 Ambiguity #1 + #6）─────────────────


@router.get("/config/effective")
def get_config_effective(
    user: User = Depends(require_role("admin")),
    svc: ConfigService = Depends(get_config_service),
):
    """返回每个 AppConfig 字段的 effective value + source。

    source 取值：
      - "appconfig"  : 仅来自 .env / AppConfig 默认（L1a）
      - "appsetting" : 被 AppSetting 表覆盖（L1b 动态覆盖）
      - "provider_profile:<type>:<name>" : 被某个 ProviderConfig profile 覆盖（L2）

    用于 /config UI 在每个字段下方标注"被 XXX 覆盖"，消除 Ambiguity #1。
    """
    from dataclasses import asdict

    config = svc.get_config()
    snapshot = asdict(config)
    overrides = dict(getattr(svc, "_overrides", {}) or {})

    # 收集所有 active provider profile 的 config dict
    profiles = svc.get_provider_configs(active_only=True)
    # 按字段名建反向索引：哪些 profile 含此 key
    profile_overrides: dict[str, list[str]] = {}
    for pc in profiles:
        for key in (pc.config or {}).keys():
            profile_overrides.setdefault(key, []).append(f"{pc.provider_type}:{pc.provider_name}")

    sensitive_keys = {
        "efuncard_token", "sms_api_key", "email_provider_api_key",
        "mail_refresh_token", "mail_client_id", "llm_api_key",
        "ads_api_key", "task_password", "proxy", "known_mail_accounts_json",
        "triage_api_key",
    }

    result: dict[str, dict] = {}
    for key, value in snapshot.items():
        # 脱敏
        display_value = value
        if key in sensitive_keys and value:
            v = str(value)
            display_value = f"{v[:4]}****" if len(v) > 4 else "****"
        # 推断 source（按优先级倒序：profile > appsetting > appconfig）
        if key in profile_overrides:
            source = "provider_profile:" + ",".join(profile_overrides[key])
        elif key in overrides:
            source = "appsetting"
        else:
            source = "appconfig"
        result[key] = {"value": display_value, "source": source}
    return {"fields": result}


@router.get("/config/revisions")
def list_config_revisions(
    key: Optional[str] = None,
    limit: int = 20,
    user: User = Depends(require_role("admin")),
    svc: ConfigService = Depends(get_config_service),
):
    """列出 AppSetting 改动历史（消除 Ambiguity #6）。"""
    revisions = svc.list_app_setting_revisions(key=key, limit=limit)
    return {
        "items": [
            {
                "id": rev.id,
                "key": rev.key,
                "previous_value": rev.previous_value,
                "new_value": rev.new_value,
                "created_at": rev.created_at.isoformat() if rev.created_at else "",
                "created_by": rev.created_by,
            }
            for rev in revisions
        ]
    }


# ── 插拔式 Provider Registry 暴露端点 ──────────────
#
# 前端 /providers 页面从这里拿可选 kinds + 字段 schema 动态渲染表单，
# 新增 provider（丢一个 .py 到 src/providers/<type>s/ 子目录）后刷新即可见。

@router.get("/providers/kinds")
def list_provider_kinds(
    provider_type: Optional[str] = Query(default=None),
    user: User = Depends(require_authenticated_user),
):
    """列出已注册的 provider kinds。

    - 无 ``provider_type`` 参数 → 返回 {type: [kinds]} 全量
    - 指定 ``provider_type`` → 返回该类型的 kind + display_name + description 详情
    """
    from src.providers import get_registry

    registry = get_registry()
    if not provider_type:
        all_metas = registry.list_all_metas()
        grouped: dict[str, list[dict[str, str]]] = {}
        for meta in all_metas:
            grouped.setdefault(meta.provider_type, []).append({
                "kind": meta.kind,
                "display_name": meta.display_name,
                "description": meta.description,
            })
        for v in grouped.values():
            v.sort(key=lambda x: x["kind"])
        return grouped

    type_norm = str(provider_type).strip().lower()
    return [
        {
            "kind": meta.kind,
            "display_name": meta.display_name,
            "description": meta.description,
        }
        for meta in sorted(registry.list_all_metas(), key=lambda m: m.kind)
        if meta.provider_type == type_norm
    ]


@router.get("/providers/schemas/{provider_type}/{kind}")
def get_provider_schema(
    provider_type: str,
    kind: str,
    user: User = Depends(require_authenticated_user),
):
    """返回指定 provider kind 的字段 schema，给前端动态渲染表单用。"""
    from src.providers import get_registry

    meta = get_registry().get_meta(
        str(provider_type).strip().lower(),
        str(kind).strip().lower(),
    )
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"未注册的 provider: {provider_type}/{kind}",
        )
    return meta.to_dict()
