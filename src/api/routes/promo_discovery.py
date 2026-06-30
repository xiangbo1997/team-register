# -*- coding: utf-8 -*-
"""促销码主动发现 API

POST   /api/promo-discovery/start            — 启动一次发现任务（返 task_id）
POST   /api/promo-discovery/{task_id}/cancel — 请求取消
GET    /api/promo-discovery/{task_id}/status — 查询单任务状态
GET    /api/promo-discovery/recent           — 列最近 N 个任务（含已完成）
GET    /api/promo-discovery/supported-countries — 列支持的国家（有字典文件的）

权限：admin only；写操作均加 CSRF
SSE 进度：复用 GET /api/tasks/{task_id}/events
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import get_event_broadcaster
from src.api.security import require_csrf, require_role
from src.data.promo_seeds import COUNTRY_SUFFIXES, list_supported_countries
from src.db.models import User
from src.services.code_discovery_service import (
    BusyError,
    DiscoveryNotFound,
    cancel_discovery,
    get_discovery_status,
    list_recent_discoveries,
    start_discovery,
)
from src.services.event_service import EventBroadcaster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/promo-discovery", tags=["promo-discovery"])


class StartDiscoveryRequest(BaseModel):
    country: str = Field(
        default="",
        max_length=8,
        description="ISO 国家码（大写）；mode=cross_matrix 时忽略",
    )
    mode: str = Field(default="seeds", description="seeds / cross_matrix")
    extra_words: list[str] = Field(
        default_factory=list,
        description="自定义关键词，会做 normalize 后参与候选码生成",
        max_length=200,
    )
    cross_countries: Optional[list[str]] = Field(
        default=None,
        description="仅 cross_matrix 模式生效；为空时用所有有字典的国家",
        max_length=50,
    )
    delay_sec: Optional[float] = Field(
        default=None,
        ge=0.1,
        le=10.0,
        description="调用间隔覆盖；不传走默认 1.0s",
    )
    # 代理来源（P3 引入）；不传走默认 static_proxy（按国家自动选，保持向后兼容）
    proxy_source: str = Field(
        default="static_proxy",
        description="dynamic_provider | static_proxy | direct",
    )
    proxy_provider_id: Optional[int] = Field(
        default=None,
        description="proxy_source=dynamic_provider 时必填，对应 ProxyProvider.id",
    )
    static_proxy_id: Optional[int] = Field(
        default=None,
        description="proxy_source=static_proxy 时可选，对应 Proxy.id；None 时按国家自动选",
    )


@router.get("/supported-countries")
def get_supported_countries(
    user: User = Depends(require_role("admin")),
):
    """列出所有受支持的国家码 + 是否有公司字典（仅有字典的国家才能跑 seeds 模式有意义）。"""
    with_dict = set(list_supported_countries())
    return {
        "countries": [
            {
                "code": cc,
                "suffixes": list(COUNTRY_SUFFIXES[cc]),
                "has_company_dict": cc in with_dict,
            }
            for cc in sorted(COUNTRY_SUFFIXES.keys())
        ],
    }


@router.post("/start")
def start(
    body: StartDiscoveryRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """启动一次发现任务。

    返回 {"task_id": ..., "total": ..., "country": ..., "mode": ...}
    冲突（已有任务在跑）→ 409；参数非法 → 400
    """
    kwargs = {
        "country": body.country,
        "mode": body.mode,
        "extra_words": body.extra_words,
        "cross_countries": body.cross_countries,
        "broadcaster": broadcaster,
        "proxy_source": body.proxy_source,
        "proxy_provider_id": body.proxy_provider_id,
        "static_proxy_id": body.static_proxy_id,
    }
    if body.delay_sec is not None:
        kwargs["delay_sec"] = body.delay_sec
    try:
        return start_discovery(**kwargs)
    except BusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{task_id}/cancel")
def cancel(
    task_id: str,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """请求取消任务；返回 {"cancelled": bool}（false 表示任务已终止）。"""
    try:
        ok = cancel_discovery(task_id)
        return {"cancelled": ok, "task_id": task_id}
    except DiscoveryNotFound:
        raise HTTPException(status_code=404, detail=f"task_id 不存在: {task_id}")


@router.get("/{task_id}/status")
def status(
    task_id: str,
    user: User = Depends(require_role("admin")),
):
    """查询单任务状态快照。"""
    try:
        return get_discovery_status(task_id)
    except DiscoveryNotFound:
        raise HTTPException(status_code=404, detail=f"task_id 不存在: {task_id}")


@router.get("/recent")
def recent(
    limit: int = 10,
    user: User = Depends(require_role("admin")),
):
    """列最近 N 个任务（含已完成）。limit 默认 10，上限 50。"""
    return {"items": list_recent_discoveries(limit=min(max(1, limit), 50))}
