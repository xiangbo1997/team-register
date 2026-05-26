# -*- coding: utf-8 -*-
"""Team checkout payload schema v1（对齐 OzBargain 实测格式）。

关键设计：
- plan_name=chatgptteamplan + team_plan_data{workspace_name, price_interval, seat_quantity}
- 带 promo_code 时 checkout_ui_mode='hosted' + 不带 promo_campaign（避免双重优惠冲突）
- 不带 promo_code 时 checkout_ui_mode='custom' + 带 promo_campaign=team-1-month-free
  + is_coupon_from_query_param 嵌在 promo_campaign 内 = True（Team 走这条路径已验证）
"""
from __future__ import annotations

from typing import Optional

from . import register
from ._common import (
    DEFAULT_PRICE_INTERVAL,
    DEFAULT_SEAT_QUANTITY,
    DEFAULT_WORKSPACE_NAME,
    HOME_URL,
    TEAM_PLAN_NAME,
    TEAM_PROMO_ID,
    build_billing_details,
    merge_extra_payload,
    normalize_promo_id,
)


@register("team", "v1")
def build_payload(
    *,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    seat_quantity: int = DEFAULT_SEAT_QUANTITY,
    price_interval: str = DEFAULT_PRICE_INTERVAL,
    promo_code: Optional[str] = None,
    promo_campaign_id: Optional[str] = None,
    billing_country: str = "",
    billing_currency: str = "",
    extra_payload: Optional[dict] = None,
    success_url: str = "",  # noqa: ARG001  Team 不用
    cancel_url: str = "",
    checkout_ui_mode: str = "",  # noqa: ARG001  Team 自动按 promo_code 切 hosted/custom，本字段忽略
) -> dict:
    effective_cancel = cancel_url or HOME_URL
    payload: dict = {
        "plan_name": TEAM_PLAN_NAME,
        "team_plan_data": {
            "workspace_name": workspace_name or DEFAULT_WORKSPACE_NAME,
            "price_interval": price_interval or DEFAULT_PRICE_INTERVAL,
            "seat_quantity": int(seat_quantity),
        },
        "checkout_ui_mode": "hosted" if promo_code else "custom",
        "cancel_url": effective_cancel,
    }
    billing = build_billing_details(billing_country, billing_currency)
    if billing:
        payload["billing_details"] = billing

    if promo_code:
        # 显式 promo_code → 不带内部 promo_campaign（双重优惠冲突会导致 checkout 失败）
        payload["promo_code"] = promo_code
    else:
        # 走默认 Team 1-month-free（custom 模式）
        effective_promo = normalize_promo_id(promo_campaign_id, TEAM_PROMO_ID)
        payload["promo_campaign"] = {
            "promo_campaign_id": effective_promo,
            "is_coupon_from_query_param": True,
        }
    return merge_extra_payload(payload, extra_payload)
