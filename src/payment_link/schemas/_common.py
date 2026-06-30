# -*- coding: utf-8 -*-
"""Schema 共享常量 + helper（不带 @register，纯工具）。"""
from __future__ import annotations

from typing import Optional

# 上游 URL 常量
CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
AIMIZY_URL = "https://team.aimizy.com/api/public/generate-payment-link"
HOME_URL = "https://chatgpt.com/"
APP_CHECKOUT_PREFIX = "https://chatgpt.com/checkout/openai_llc/"

# Plus 试用专用 URL（跟 payurl.ark2.cn server.py + 用户 JS ground truth 一致）
CANCEL_URL_PLUS_PRICING = "https://chatgpt.com/#pricing"
SUCCESS_URL_PLUS = "https://chatgpt.com/"
SUCCESS_URL_TEAM = "https://chatgpt.com/?subscribed=true"

# plan_name 常量
TEAM_PLAN_NAME = "chatgptteamplan"
PLUS_PLAN_NAME = "chatgptplusplan"

# promo_campaign_id 默认值
TEAM_PROMO_ID = "team-1-month-free"
PLUS_PROMO_ID = "plus-1-month-free"

# billing 默认值（兜底，避免 OpenAI 后端 schema 校验失败）
DEFAULT_BILLING_COUNTRY = "SG"
DEFAULT_BILLING_CURRENCY = "SGD"

# Team 默认值
DEFAULT_WORKSPACE_NAME = "MyTeam"
DEFAULT_SEAT_QUANTITY = 5
DEFAULT_PRICE_INTERVAL = "month"


def build_billing_details(country: str, currency: str) -> dict:
    """构造 billing_details 子对象；country/currency 自动 upper。空字符串保留为空。"""
    out: dict = {}
    if country:
        out["country"] = country.upper()
    if currency:
        out["currency"] = currency.upper()
    return out


def normalize_promo_id(promo_campaign_id: Optional[str], default: str) -> str:
    """显式 promo_campaign_id 覆盖默认；空字符串 / None 回退到 default。"""
    candidate = (promo_campaign_id or default).strip()
    return candidate or default


def merge_extra_payload(payload: dict, extra: Optional[dict]) -> dict:
    """把模板的 extra_payload_json 浅合并到 payload。

    设计取舍：浅合并 vs 深合并 —— 选浅。理由：
    - OpenAI 接受的 payload 最多 2 层（billing_details 是子 dict）
    - 浅合并语义清晰：extra 给的 key 整体覆盖 payload 的同名 key
    - 深合并会让运营难以预测最终结构（特别是 list 合并行为）
    """
    if not extra:
        return payload
    if not isinstance(extra, dict):
        return payload
    payload.update(extra)
    return payload
