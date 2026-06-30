# -*- coding: utf-8 -*-
"""动态代理供应商服务（ProxyProvider 表 CRUD + 连通性测试）。

与 src/services/proxy_service.py 的关系：
  - proxy_service        管 Proxy 表（静态死 IP，注册/支付/手动验证用）
  - proxy_provider_service  管 ProxyProvider 表（动态 API 配置，promo 探索用）

字段脱敏规则（与 proxy_service 对齐）：
  - api_url_template、credentials 永远不向前端原样返回
  - list_providers / get_provider 默认走脱敏；只有 with_secrets=True 才返回原文
    （仅 adapter 层和 test_provider 路径需要）

依赖关系：
  - 创建/更新供应商 → 写入 ProxyProvider 表
  - test_provider 调用 src/proxy_clients/adapters/registry.get_adapter(kind).fetch_one()
    拉一个真实 IP，返回 ipapi 反查结果，给运维一键确认配置可用
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.db.engine import get_session
from src.db.models import ProxyProvider

logger = logging.getLogger(__name__)


# 支持的 kind 列表（与 adapters/registry.py 必须对齐；这里硬编码避免循环 import）
_SUPPORTED_KINDS = ("1024proxy", "generic_http")
_SUPPORTED_AUTH_KINDS = ("ip_whitelist", "api_key", "basic_auth", "none")
_SUPPORTED_RESPONSE_FORMATS = ("txt_line", "json_array_host_port")


def _mask_url_template(url: str) -> str:
    """脱敏 URL 模板（前 25 字符 + 省略号）。

    模板里若含敏感参数（如某些供应商把 token 拼在 URL 里）也防泄露。
    """
    if not url:
        return ""
    return url[:25] + "..." if len(url) > 25 else url


def _mask_credentials(creds: Optional[dict]) -> dict:
    """凭据脱敏：保留 key 名，value 改 *** 或显示前 3 字符。"""
    if not creds:
        return {}
    masked: dict[str, str] = {}
    for k, v in creds.items():
        text = str(v or "")
        if not text:
            masked[k] = ""
        elif len(text) <= 4:
            masked[k] = "***"
        else:
            masked[k] = f"{text[:3]}***"
    return masked


def _provider_to_dict(
    p: ProxyProvider, *, with_secrets: bool = False
) -> dict[str, Any]:
    """脱敏序列化。with_secrets=True 仅供 adapter 层和 test_provider 用。"""
    out: dict[str, Any] = {
        "id": p.id,
        "label": p.label,
        "kind": p.kind,
        "auth_kind": p.auth_kind,
        "response_format": p.response_format,
        "country_map": dict(p.country_map or {}),
        "rotation_per_n_requests": int(p.rotation_per_n_requests),
        "sticky_seconds": int(p.sticky_seconds),
        "default_country": p.default_country or "",
        "notes": p.notes or "",
        "is_active": bool(p.is_active),
        "created_at": p.created_at.isoformat() if p.created_at else "",
        "api_url_template_masked": _mask_url_template(p.api_url_template or ""),
        "credentials_masked": _mask_credentials(p.credentials),
    }
    if with_secrets:
        out["api_url_template"] = p.api_url_template or ""
        out["credentials"] = dict(p.credentials or {})
    return out


def list_providers(*, include_inactive: bool = True) -> list[dict[str, Any]]:
    """列出所有动态供应商（脱敏），按 label 升序。"""
    with get_session() as session:
        stmt = select(ProxyProvider)
        if not include_inactive:
            stmt = stmt.where(ProxyProvider.is_active == True)  # noqa: E712
        rows = list(session.exec(stmt.order_by(ProxyProvider.label)).all())  # type: ignore
        return [_provider_to_dict(p) for p in rows]


def get_provider(
    provider_id: int, *, with_secrets: bool = False
) -> Optional[dict[str, Any]]:
    """按 id 取单条。

    with_secrets=True 仅供 adapter 层（fetch_one）和 test_provider 路径用，
    HTTP 响应层永远走默认脱敏。
    """
    with get_session() as session:
        p = session.get(ProxyProvider, provider_id)
        return _provider_to_dict(p, with_secrets=with_secrets) if p else None


def _validate_create_params(
    *,
    label: str,
    kind: str,
    api_url_template: str,
    auth_kind: str,
    response_format: str,
    rotation_per_n_requests: int,
    sticky_seconds: int,
) -> tuple[str, str, str]:
    """共用校验：返回归一化后的 (safe_label, safe_kind, safe_auth_kind)。"""
    safe_label = (label or "").strip()
    if not safe_label:
        raise ValueError("label 不能为空")
    if len(safe_label) > 80:
        raise ValueError("label 过长（>80）")

    safe_kind = (kind or "").strip().lower()
    if safe_kind not in _SUPPORTED_KINDS:
        raise ValueError(
            f"kind 必须是 {list(_SUPPORTED_KINDS)} 之一，得到 {kind!r}"
        )

    if not (api_url_template or "").strip():
        raise ValueError("api_url_template 不能为空")

    safe_auth_kind = (auth_kind or "none").strip().lower()
    if safe_auth_kind not in _SUPPORTED_AUTH_KINDS:
        raise ValueError(
            f"auth_kind 必须是 {list(_SUPPORTED_AUTH_KINDS)} 之一，得到 {auth_kind!r}"
        )

    if response_format not in _SUPPORTED_RESPONSE_FORMATS:
        raise ValueError(
            f"response_format 必须是 {list(_SUPPORTED_RESPONSE_FORMATS)} 之一"
        )

    if rotation_per_n_requests < 1:
        raise ValueError("rotation_per_n_requests 必须 >= 1")
    if sticky_seconds < 0:
        raise ValueError("sticky_seconds 不能为负")

    return safe_label, safe_kind, safe_auth_kind


def create_provider(
    *,
    label: str,
    kind: str,
    api_url_template: str,
    auth_kind: str = "none",
    credentials: Optional[dict] = None,
    response_format: str = "txt_line",
    country_map: Optional[dict] = None,
    rotation_per_n_requests: int = 50,
    sticky_seconds: int = 0,
    default_country: str = "Rand",
    notes: str = "",
    is_active: bool = True,
) -> dict[str, Any]:
    """新增供应商。label 全局唯一；kind/auth_kind/response_format 必须在白名单。"""
    safe_label, safe_kind, safe_auth_kind = _validate_create_params(
        label=label,
        kind=kind,
        api_url_template=api_url_template,
        auth_kind=auth_kind,
        response_format=response_format,
        rotation_per_n_requests=rotation_per_n_requests,
        sticky_seconds=sticky_seconds,
    )

    safe_country = (default_country or "Rand").strip()[:8] or "Rand"
    safe_notes = (notes or "").strip()[:200]

    with get_session() as session:
        existing = session.exec(
            select(ProxyProvider).where(ProxyProvider.label == safe_label)
        ).first()
        if existing is not None:
            raise ValueError(f"供应商标签已存在: {safe_label}")

        p = ProxyProvider(
            label=safe_label,
            kind=safe_kind,
            api_url_template=api_url_template.strip(),
            auth_kind=safe_auth_kind,
            credentials=dict(credentials or {}),
            response_format=response_format,
            country_map=dict(country_map or {}),
            rotation_per_n_requests=int(rotation_per_n_requests),
            sticky_seconds=int(sticky_seconds),
            default_country=safe_country,
            notes=safe_notes,
            is_active=bool(is_active),
        )
        session.add(p)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(f"供应商标签已存在: {safe_label}") from exc
        session.refresh(p)
        logger.info(
            "创建动态供应商 id=%s label=%s kind=%s auth=%s",
            p.id, p.label, p.kind, p.auth_kind,
        )
        return _provider_to_dict(p)


def update_provider(
    provider_id: int,
    *,
    api_url_template: Optional[str] = None,
    auth_kind: Optional[str] = None,
    credentials: Optional[dict] = None,
    response_format: Optional[str] = None,
    country_map: Optional[dict] = None,
    rotation_per_n_requests: Optional[int] = None,
    sticky_seconds: Optional[int] = None,
    default_country: Optional[str] = None,
    notes: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """局部更新。**不允许改 label / kind**（要换换体系直接删了重建）。"""
    with get_session() as session:
        p = session.get(ProxyProvider, provider_id)
        if p is None:
            return None

        if api_url_template is not None:
            new_url = api_url_template.strip()
            if not new_url:
                raise ValueError("api_url_template 不能改成空")
            p.api_url_template = new_url

        if auth_kind is not None:
            safe_auth = auth_kind.strip().lower()
            if safe_auth not in _SUPPORTED_AUTH_KINDS:
                raise ValueError(f"auth_kind 无效: {auth_kind!r}")
            p.auth_kind = safe_auth

        if credentials is not None:
            p.credentials = dict(credentials)

        if response_format is not None:
            if response_format not in _SUPPORTED_RESPONSE_FORMATS:
                raise ValueError(f"response_format 无效: {response_format!r}")
            p.response_format = response_format

        if country_map is not None:
            p.country_map = dict(country_map)

        if rotation_per_n_requests is not None:
            if rotation_per_n_requests < 1:
                raise ValueError("rotation_per_n_requests 必须 >= 1")
            p.rotation_per_n_requests = int(rotation_per_n_requests)

        if sticky_seconds is not None:
            if sticky_seconds < 0:
                raise ValueError("sticky_seconds 不能为负")
            p.sticky_seconds = int(sticky_seconds)

        if default_country is not None:
            p.default_country = (default_country or "Rand").strip()[:8] or "Rand"

        if notes is not None:
            p.notes = (notes or "").strip()[:200]

        if is_active is not None:
            p.is_active = bool(is_active)

        session.add(p)
        session.commit()
        session.refresh(p)
        logger.info("更新动态供应商 id=%s is_active=%s", p.id, p.is_active)
        return _provider_to_dict(p)


def delete_provider(provider_id: int) -> tuple[bool, str]:
    """硬删除。本轮不做软引用检查（discover 任务的 proxy_provider_id 是请求参数，
    不持久化到任何表），直接删即可。
    """
    with get_session() as session:
        p = session.get(ProxyProvider, provider_id)
        if p is None:
            return False, f"供应商不存在: id={provider_id}"
        label = p.label
        session.delete(p)
        session.commit()
        logger.info("删除动态供应商 id=%s label=%s", provider_id, label)
        return True, ""


def probe_provider(provider_id: int) -> dict[str, Any]:
    """触发一次真实 API 调用拉个 IP，返回连通性诊断信息。

    历史名 ``test_provider`` 与 pytest 的 test_ 收集前缀冲突，改名 ``probe_provider``。
    对外路由层别名仍可保留，但函数本身改名。

    路径：load with_secrets → get_adapter(kind) → adapter.fetch_one()

    Returns:
        {
            "success": bool,
            "ip": str,                  # "host:port" 或 ""
            "country": str,             # ipapi 反查的 ISO；"" 表示查询失败
            "country_match": bool,      # actual_country == default_country (大写比较)
            "latency_ms": int,
            "error": str,               # 失败时的人类可读描述
        }
    """
    from src.proxy_clients.adapters.registry import get_adapter

    full_provider = get_provider(provider_id, with_secrets=True)
    if full_provider is None:
        return {
            "success": False,
            "ip": "",
            "country": "",
            "country_match": False,
            "latency_ms": 0,
            "error": f"供应商不存在: id={provider_id}",
        }

    expected_country = (full_provider.get("default_country") or "").upper()
    started = time.perf_counter()
    try:
        adapter = get_adapter(full_provider["kind"])
    except ValueError as exc:
        return {
            "success": False,
            "ip": "",
            "country": "",
            "country_match": False,
            "latency_ms": 0,
            "error": str(exc),
        }

    try:
        proxy_info = adapter.fetch_one(full_provider, country=expected_country or "Rand")
    except Exception as exc:  # noqa: BLE001 - 测试端点要透出所有错误
        latency = int((time.perf_counter() - started) * 1000)
        return {
            "success": False,
            "ip": "",
            "country": "",
            "country_match": False,
            "latency_ms": latency,
            "error": f"{type(exc).__name__}: {exc}",
        }

    latency = int((time.perf_counter() - started) * 1000)
    if proxy_info is None:
        return {
            "success": False,
            "ip": "",
            "country": "",
            "country_match": False,
            "latency_ms": latency,
            "error": "adapter 返回 None（可能 URL 错误、白名单未配置、响应解析为空）",
        }

    actual_country = (proxy_info.country or "").upper()
    country_match = bool(actual_country) and (
        not expected_country
        or expected_country == "RAND"
        or actual_country == expected_country
    )
    return {
        "success": True,
        "ip": f"{proxy_info.host}:{proxy_info.port}",
        "country": actual_country,
        "country_match": country_match,
        "latency_ms": latency,
        "error": "",
    }


def get_supported_kinds() -> list[dict[str, Any]]:
    """供 /api/proxy-providers/kinds 端点用，前端建表单时填 kind 下拉 + 占位符提示。

    本函数返回的元信息**必须与 src/proxy_clients/adapters/registry.py 对齐**。
    """
    return [
        {
            "kind": "1024proxy",
            "label": "1024Proxy（IP 白名单）",
            "auth_kinds": ["ip_whitelist"],
            "placeholders": ["{country}", "{num}", "{format}", "{session}"],
            "sticky_default": 600,
            "response_format_default": "txt_line",
            "notes": "需在 1024 后台白名单加服务器出口 IP；URL 模板必须含 {session} 占位符以绕过 sticky",
        },
        {
            "kind": "generic_http",
            "label": "通用 HTTP API",
            "auth_kinds": ["api_key", "basic_auth", "none"],
            "placeholders": ["{country}", "{num}", "{format}"],
            "sticky_default": 0,
            "response_format_default": "txt_line",
            "notes": "适用 IPRoyal / SmartProxy / 自建池等标准 HTTP 鉴权供应商",
        },
    ]
