# -*- coding: utf-8 -*-
"""
代理池 API

GET    /api/proxies              列表（脱敏，含 is_active / 模板引用计数）
POST   /api/proxies              新增（支持 4 段冒号 + 完整 URL）
PATCH  /api/proxies/{id}         局部更新（仅 is_active / notes / country）
DELETE /api/proxies/{id}         删除（被模板引用时拒绝）

权限：admin only
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.security import require_csrf, require_role
from src.db.models import User
from src.services.proxy_service import (
    count_templates_using_proxy,
    create_proxy,
    delete_proxy,
    get_proxy,
    list_proxies,
    update_proxy,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


class CreateProxyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    url: str = Field(..., min_length=1, max_length=600)
    country: str = Field(default="", max_length=8)
    notes: str = Field(default="", max_length=200)
    is_active: bool = True


class UpdateProxyRequest(BaseModel):
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=200)
    country: Optional[str] = Field(default=None, max_length=8)


@router.get("")
def list_proxies_endpoint(
    user: User = Depends(require_role("admin")),
):
    """列出所有代理（含 url_masked + 引用计数）。"""
    rows = list_proxies(include_inactive=True)
    # 附加每个代理被多少模板引用，UI 删除按钮判断用
    for row in rows:
        row["template_ref_count"] = count_templates_using_proxy(int(row["id"]))
    return rows


@router.post("")
def create_proxy_endpoint(
    body: CreateProxyRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """新增代理。url 走归一化：支持 socks5h:// / http:// / 4 段冒号。"""
    try:
        return create_proxy(
            label=body.label,
            url=body.url,
            country=body.country,
            notes=body.notes,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{proxy_id}")
def update_proxy_endpoint(
    proxy_id: int,
    body: UpdateProxyRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """局部更新。允许改 is_active / notes / country；不允许改 url 和 label。"""
    result = update_proxy(
        proxy_id,
        is_active=body.is_active,
        notes=body.notes,
        country=body.country,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="代理不存在")
    return result


@router.delete("/{proxy_id}")
def delete_proxy_endpoint(
    proxy_id: int,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """删除代理。被 LinkTemplate 引用时拒绝（返回 409 + 引用清单）。"""
    ok, msg = delete_proxy(proxy_id)
    if not ok:
        # 区分"不存在"和"被引用"
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)
    return {"deleted": True, "id": proxy_id}
