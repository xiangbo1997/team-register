# -*- coding: utf-8 -*-
"""Pro / Pro Lite checkout payload schema v1（实验性，未实测）。

按 Plus 同款 hosted 结构推演。OpenAI 后端 plan_name 字段实际值待真实
access_token 调通后确认（参考 src/orchestration/handlers.py:1648 DOM 命名
button#chatgptpro / button#chatgptprolite）。

注意：当前仍用 plan_type=pro 字段（不是 plan_name），等真实接口实测后调整。
"""
from __future__ import annotations

from typing import Optional

from . import register
from ._common import (
    HOME_URL,
    SUCCESS_URL_TEAM,
    build_billing_details,
    merge_extra_payload,
)


def _build_pro_payload(
    plan_type_value: str,
    *,
    workspace_name: str = "",  # noqa: ARG001  Pro 不用
    seat_quantity: int = 1,    # noqa: ARG001
    price_interval: str = "",  # noqa: ARG001
    promo_code: Optional[str] = None,
    promo_campaign_id: Optional[str] = None,  # noqa: ARG001
    billing_country: str = "",
    billing_currency: str = "",
    extra_payload: Optional[dict] = None,
    success_url: str = "",
    cancel_url: str = "",
    checkout_ui_mode: str = "",  # noqa: ARG001  Pro 实验性，本轮不接受，固定 hosted
) -> dict:
    payload: dict = {
        "plan_type": plan_type_value,
        "checkout_ui_mode": "hosted",
        "cancel_url": cancel_url or HOME_URL,
        "success_url": success_url or SUCCESS_URL_TEAM,
    }
    if promo_code:
        payload["promo_code"] = promo_code
    if billing_country or billing_currency:
        payload["billing_details"] = build_billing_details(billing_country, billing_currency)
    return merge_extra_payload(payload, extra_payload)


@register("pro", "v1")
def build_pro_payload(**kwargs) -> dict:
    return _build_pro_payload("pro", **kwargs)


@register("pro_lite", "v1")
def build_pro_lite_payload(**kwargs) -> dict:
    return _build_pro_payload("pro_lite", **kwargs)
