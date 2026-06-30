# -*- coding: utf-8 -*-
"""aimizy 中转客户端（Team 免费试用专用）。

已知工作路径：Team + 无 promo_code → 走 aimizy 拿 trial 链接
不工作路径：Plus 走 aimizy → 返回付费链 $20（详见 v3 修复记录）；
            所以 client.py 里 Plus 永远直调 OpenAI，不走 aimizy
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from curl_cffi import requests

from .schemas._common import (
    AIMIZY_URL,
    DEFAULT_BILLING_COUNTRY,
    DEFAULT_BILLING_CURRENCY,
    DEFAULT_PRICE_INTERVAL,
    DEFAULT_SEAT_QUANTITY,
    PLUS_PLAN_NAME,
    PLUS_PROMO_ID,
    TEAM_PLAN_NAME,
    TEAM_PROMO_ID,
)
from .url_postprocess import select_checkout_link

logger = logging.getLogger(__name__)


def generate_via_aimizy(
    access_token: str,
    *,
    plan: str = "team",
    return_mode: str = "app",
    seat_quantity: int = DEFAULT_SEAT_QUANTITY,
    price_interval: str = DEFAULT_PRICE_INTERVAL,
    aimizy_country: str = DEFAULT_BILLING_COUNTRY,
    aimizy_currency: str = DEFAULT_BILLING_CURRENCY,
    promo_campaign_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """通过 aimizy 中间层生成免费试用链接（仅 Team 工作）。"""
    normalized_mode = str(return_mode or "app").strip().lower()
    normalized_plan = str(plan or "team").strip().lower()
    # 按 plan 选择 plan_name + 默认 promo（plus 路径已知不工作，仅保留对称代码）
    if normalized_plan == "plus":
        aimizy_plan_name = PLUS_PLAN_NAME
        default_promo = PLUS_PROMO_ID
    else:
        aimizy_plan_name = TEAM_PLAN_NAME
        default_promo = TEAM_PROMO_ID
    effective_promo = (promo_campaign_id or default_promo).strip() or default_promo
    payload = {
        "access_token": access_token,
        "plan_name": aimizy_plan_name,
        "country": str(aimizy_country or DEFAULT_BILLING_COUNTRY).upper(),
        "currency": str(aimizy_currency or DEFAULT_BILLING_CURRENCY).upper(),
        "promo_campaign_id": effective_promo,
        "is_coupon_from_query_param": True,
        "seat_quantity": int(seat_quantity),
        "price_interval": price_interval,
        "check_card_proxy": False,
        "is_short_link": normalized_mode == "app",
    }
    try:
        resp = requests.post(
            AIMIZY_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://team.aimizy.com",
                "Referer": "https://team.aimizy.com/pay",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
                ),
            },
            json=payload,
            impersonate="chrome120",
            timeout=30,
        )
        data = resp.json()
        if data.get("success"):
            link = select_checkout_link(data, return_mode=normalized_mode)
            if link:
                logger.info("通过 aimizy 成功生成 %s checkout 链接。", normalized_plan)
                return True, link
        return False, f"aimizy 响应无可用链接: {resp.text[:200]}"
    except Exception as exc:
        return False, str(exc)
