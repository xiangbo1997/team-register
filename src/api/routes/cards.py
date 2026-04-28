# -*- coding: utf-8 -*-
"""
虚拟卡缓存管理 API

GET    /api/cards                     — 列表（默认隐藏 invalidated；?include_invalidated=true 全量）
GET    /api/cards/{card_key}          — 详情
POST   /api/cards/{card_key}/invalidate — 手动作废

所有响应严格脱敏：
  - card_number 只返回 last4 + bin（前 6 位）
  - cvv / sms_api 完全不返回
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api.security import require_csrf, require_role
from src.db.models import CardActivation, User
from src.services.card_activation_service import (
    WARMUP_CARD_CACHE_MAX_AGE_DAYS,
    _ensure_aware,
    _is_card_still_valid,
    get_activation,
    invalidate as svc_invalidate,
    list_activations,
)

router = APIRouter(prefix="/api/cards", tags=["cards"])


class InvalidateRequest(BaseModel):
    reason: str = "manual"


def _redact(rec: CardActivation) -> dict[str, Any]:
    """脱敏序列化：卡号只露 last4 + 前 6 位 BIN，CVV 和 sms_api 完全不露。"""
    raw = rec.card_number or ""
    last4 = raw[-4:] if len(raw) >= 4 else ""
    bin_prefix = raw[:6] if len(raw) >= 6 else ""

    # 卡片自身是否过期 + max_age 是否触发
    valid = _is_card_still_valid(rec)
    now = datetime.now(timezone.utc)
    activated_aware = _ensure_aware(rec.activated_at) if rec.activated_at else None
    age_days = (now - activated_aware).days if activated_aware else 0
    days_until_max_age = max(0, WARMUP_CARD_CACHE_MAX_AGE_DAYS - age_days) if WARMUP_CARD_CACHE_MAX_AGE_DAYS > 0 else None

    if rec.is_invalidated:
        status = "invalidated"
    elif not valid:
        status = "expired"
    else:
        status = "active"

    return {
        "card_key": rec.card_key,
        "card_provider": rec.card_provider,
        "last4": last4,
        "bin_prefix": bin_prefix,
        "expiry_month": rec.expiry_month,
        "expiry_year": rec.expiry_year,
        "name_on_card": rec.name_on_card,
        "billing_address": rec.billing_address,
        "bin_country": rec.bin_country,
        "phone": (rec.phone or "")[-4:] if rec.phone else "",  # 只露后 4 位
        "activated_at": activated_aware.isoformat() if activated_aware else "",
        "last_used_at": _ensure_aware(rec.last_used_at).isoformat() if rec.last_used_at else "",
        "use_count": int(rec.use_count or 0),
        "is_invalidated": bool(rec.is_invalidated),
        "invalidate_reason": rec.invalidate_reason or "",
        "status": status,
        "days_until_max_age": days_until_max_age,
    }


@router.get("")
def list_cards(
    include_invalidated: bool = Query(False),
    card_provider: Optional[str] = Query(None, description="按卡商过滤: x988card / efuncard / nodecard"),
    user: User = Depends(require_role("admin")),
):
    """列表查询（admin 角色）。可按 card_provider 过滤。"""
    records = list_activations(
        include_invalidated=include_invalidated,
        card_provider=card_provider,
    )
    return [_redact(r) for r in records]


@router.get("/{card_key}")
def get_card_detail(
    card_key: str,
    user: User = Depends(require_role("admin")),
):
    """单卡详情。"""
    rec = get_activation(card_key)
    if rec is None:
        raise HTTPException(status_code=404, detail="卡密不存在或未激活")
    return _redact(rec)


@router.post("/{card_key}/invalidate")
def invalidate_card(
    card_key: str,
    body: Optional[InvalidateRequest] = None,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """手动作废卡密缓存。作废后该 cdk 不会再被尝试 verify（X988 一次性消耗）。"""
    reason = (body.reason if body else "manual") or "manual"
    ok = svc_invalidate(card_key, reason=reason)
    if not ok:
        raise HTTPException(status_code=404, detail="卡密不存在")
    rec = get_activation(card_key)
    return _redact(rec) if rec else {"ok": True}
