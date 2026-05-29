# -*- coding: utf-8 -*-
"""动态代理供应商 API（ProxyProvider 表）。

GET    /api/proxy-providers                列表（脱敏：url 模板/credentials 都掩码）
POST   /api/proxy-providers                新增
PATCH  /api/proxy-providers/{id}           局部更新
DELETE /api/proxy-providers/{id}           删除
POST   /api/proxy-providers/{id}/test      测试连通性（拉一个 IP 反查国家）
GET    /api/proxy-providers/kinds          列出支持的 kind 元信息（前端表单用）

与 /api/proxies（静态 Proxy 池）正交：本端点管"按需拉 IP 的 API 配置"，
静态池管"固定 host:port"。两套并存，UI 在不同页面。

权限：admin only；写操作均加 CSRF。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.security import require_csrf, require_role
from src.db.models import User
from src.services.proxy_provider_service import (
    create_provider,
    delete_provider,
    get_provider,
    get_supported_kinds,
    list_providers,
    probe_provider,
    update_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proxy-providers", tags=["proxy-providers"])


class CreateProviderRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(..., description="1024proxy / generic_http")
    api_url_template: str = Field(..., min_length=1, max_length=512)
    auth_kind: str = Field(default="none", description="ip_whitelist / api_key / basic_auth / none")
    credentials: Optional[dict] = Field(default=None, description="按 auth_kind 决定 schema")
    response_format: str = Field(default="txt_line", description="txt_line / json_array_host_port")
    country_map: Optional[dict] = Field(default=None, description="项目国家码 → 供应商国家码映射")
    rotation_per_n_requests: int = Field(default=50, ge=1, le=10000)
    sticky_seconds: int = Field(default=0, ge=0, le=86400)
    default_country: str = Field(default="Rand", max_length=8)
    notes: str = Field(default="", max_length=200)
    is_active: bool = True


class UpdateProviderRequest(BaseModel):
    api_url_template: Optional[str] = Field(default=None, max_length=512)
    auth_kind: Optional[str] = None
    credentials: Optional[dict] = None
    response_format: Optional[str] = None
    country_map: Optional[dict] = None
    rotation_per_n_requests: Optional[int] = Field(default=None, ge=1, le=10000)
    sticky_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    default_country: Optional[str] = Field(default=None, max_length=8)
    notes: Optional[str] = Field(default=None, max_length=200)
    is_active: Optional[bool] = None


# 注意：路由顺序很重要 —— /kinds 必须在 /{id} 之前，否则 "kinds" 会被当成 id
@router.get("/kinds")
def list_kinds_endpoint(
    user: User = Depends(require_role("admin")),
):
    """列出当前后端支持的 provider kind 元信息（前端表单 kind 下拉用）。

    返回每项含 kind / label / 支持的 auth_kinds / 占位符列表 / 默认 sticky 等，
    前端可据此动态渲染表单（如选中 1024proxy 时锁定 auth_kind 为 ip_whitelist）。
    """
    return {"kinds": get_supported_kinds()}


@router.get("")
def list_providers_endpoint(
    user: User = Depends(require_role("admin")),
):
    """列出所有动态供应商（脱敏）。"""
    return list_providers(include_inactive=True)


@router.post("")
def create_provider_endpoint(
    body: CreateProviderRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """新增供应商。label 全局唯一；kind/auth_kind/response_format 白名单校验。"""
    try:
        return create_provider(
            label=body.label,
            kind=body.kind,
            api_url_template=body.api_url_template,
            auth_kind=body.auth_kind,
            credentials=body.credentials,
            response_format=body.response_format,
            country_map=body.country_map,
            rotation_per_n_requests=body.rotation_per_n_requests,
            sticky_seconds=body.sticky_seconds,
            default_country=body.default_country,
            notes=body.notes,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{provider_id}")
def update_provider_endpoint(
    provider_id: int,
    body: UpdateProviderRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """局部更新。**不允许改 label / kind**（要换体系直接删了重建）。"""
    try:
        result = update_provider(
            provider_id,
            api_url_template=body.api_url_template,
            auth_kind=body.auth_kind,
            credentials=body.credentials,
            response_format=body.response_format,
            country_map=body.country_map,
            rotation_per_n_requests=body.rotation_per_n_requests,
            sticky_seconds=body.sticky_seconds,
            default_country=body.default_country,
            notes=body.notes,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return result


@router.delete("/{provider_id}")
def delete_provider_endpoint(
    provider_id: int,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """硬删除。本轮不做软引用检查（discover 的 proxy_provider_id 是请求参数）。"""
    ok, msg = delete_provider(provider_id)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"deleted": True, "id": provider_id}


@router.post("/{provider_id}/test")
def test_provider_endpoint(
    provider_id: int,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """触发一次真实 API 调用拉个 IP，返回连通性诊断。

    返回字段：success / ip / country / country_match / latency_ms / error
    UI 应据此显示成功/失败模态框（参考 P5-a Stitch 原型"测试连通性结果模态"）。
    """
    return probe_provider(provider_id)
