# -*- coding: utf-8 -*-
"""Plus checkout payload schema v2.1（2026-05-26 修订）。

权威 ground truth：用户从 chatgpt.com 浏览器复制的成功半价 cURL，
带 PayPal + UnionPay 完整支付方式（实测 $10/月页）。

精确字段结构（与成功 cURL 一字不差对齐）：
{
  "entry_point": "all_plans_pricing_modal",       # OpenAI 用来识别 checkout 来源，影响支付方式可见性
  "plan_name": "chatgptplusplan",
  "billing_details": {"country": "US", "currency": "USD"},
  "promo_campaign": {
    "promo_campaign_id": "plus-1-month-free|plus-1-month-50-pct-off|...",
    "is_coupon_from_query_param": false           # 嵌在 promo_campaign 内，不是顶层
  },
  "checkout_ui_mode": "hosted|custom"
}

历史教训：
- v1 用 plan_type=plus（错字段名）→ $20 付费
- v2 (上一版) 加 success_url + cancel_url=#pricing + is_coupon 移顶层 → 试用拿到但 PayPal 消失
- v2.1 (本版) 严格对齐用户成功 cURL：不发 success/cancel_url、is_coupon 嵌套 → PayPal 回来

关键决策：
- promo_code 对 Plus 是 Team 语义，OpenAI 不识别，不进 payload（沿用 v2）
- 不发 success_url / cancel_url（OpenAI 后端有默认值，多发可能触发风控）
- entry_point=all_plans_pricing_modal 让 OpenAI 把请求视为「正常用户从定价页发起」
"""
from __future__ import annotations

from typing import Optional

from . import register
from ._common import (
    CANCEL_URL_PLUS_PRICING,
    DEFAULT_BILLING_COUNTRY,
    DEFAULT_BILLING_CURRENCY,
    PLUS_PLAN_NAME,
    PLUS_PROMO_ID,
    merge_extra_payload,
    normalize_promo_id,
)


_VALID_UI_MODES = ("hosted", "custom")
# entry_point: OpenAI 用来识别 checkout 来源的字段；
# 实测「all_plans_pricing_modal」会让支付方式包含 PayPal（来自用户提供的成功 cURL ground truth）
# 缺失时 OpenAI 似乎限制为只显示 Card（反作弊风控）
_DEFAULT_ENTRY_POINT = "all_plans_pricing_modal"


@register("plus", "v2")
def build_payload(
    *,
    workspace_name: str = "",  # noqa: ARG001  Plus 不用 workspace
    seat_quantity: int = 1,    # noqa: ARG001  Plus 个人计划无 seat
    price_interval: str = "",  # noqa: ARG001
    promo_code: Optional[str] = None,  # noqa: ARG001  Plus 忽略 promo_code（见模块 docstring）
    promo_campaign_id: Optional[str] = None,
    billing_country: str = "",
    billing_currency: str = "",
    extra_payload: Optional[dict] = None,
    success_url: str = "",  # noqa: ARG001  v2 用硬编码 SUCCESS_URL_PLUS
    cancel_url: str = "",   # noqa: ARG001  v2 用硬编码 CANCEL_URL_PLUS_PRICING
    checkout_ui_mode: str = "hosted",  # P6 暴露：hosted（默认）/ custom（半价 promo 从 pricing modal 弹时用）
) -> dict:
    effective_promo = normalize_promo_id(promo_campaign_id, PLUS_PROMO_ID)
    mode = (checkout_ui_mode or "hosted").strip().lower()
    if mode not in _VALID_UI_MODES:
        mode = "hosted"  # 非法值 fallback，避免给 OpenAI 发未知值
    # 字段顺序严格按 PayPal Auto Filler 工作脚本（v36.9.5）的 generatePlusHostedLink() 排
    # 引用：Downloads/d4cc975fa50058192e7d4468b8d517a6fd835695/paypal-auto-filler-*.user.js:1820-1827
    # 该脚本已知能在浏览器里拿到带 PayPal 的 $0 试用链
    payload: dict = {
        "entry_point": _DEFAULT_ENTRY_POINT,  # 让 OpenAI 识别为正常 pricing modal 发起
        "plan_name": PLUS_PLAN_NAME,
        "billing_details": {
            "country": (billing_country or DEFAULT_BILLING_COUNTRY).upper(),
            "currency": (billing_currency or DEFAULT_BILLING_CURRENCY).upper(),
        },
        "cancel_url": CANCEL_URL_PLUS_PRICING,  # 带 cancel_url（v2.2: 工作脚本带，v2.1 错删了）
        "promo_campaign": {
            "promo_campaign_id": effective_promo,
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": mode,
        # 不发 success_url：工作脚本不带，OpenAI 后端用默认值
    }
    return merge_extra_payload(payload, extra_payload)
