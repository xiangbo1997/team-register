# -*- coding: utf-8 -*-
"""Stripe 二跳：把 OpenAI custom checkout session 换成 hosted 长链。

背景（2026-06-04 实测坐实）：
  OpenAI 的 /backend-api/payments/checkout 改版后，对 API 直调一律返回
  custom checkout session —— 响应里 `url` 恒为 null，只给 checkout_session_id +
  publishable_key + client_secret，期望前端用 Stripe.js 在浏览器内渲染 embedded 支付。
  因此旧逻辑（只读 OpenAI 响应的 `url`）拿不到 pay.openai.com 长链，降级到站内短链。

解决：补一步 Stripe 二跳（别人项目同款路线）——
  GET https://api.stripe.com/v1/payment_pages/{checkout_session_id}
  用 OpenAI 返回的 publishable_key 做 Bearer 鉴权（pk_live 是公钥，设计上就给前端用），
  Stripe 返回 `stripe_hosted_url`（checkout.stripe.com/c/pay/cs_live_xxx#fid...）。
  OpenAI 给 Stripe 配了 custom domain `pay.openai.com`（响应里 management_url 证实），
  把 checkout.stripe.com 域名替换为 pay.openai.com 即得到 OpenAI 站内长链。

实测验证脚本：scripts/diag_stripe_second_hop.py
"""
from __future__ import annotations

import logging
from typing import Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)

# Stripe 内部 payment_pages 端点（Stripe.js 初始化 checkout 时打的就是它）
_STRIPE_PAYMENT_PAGES_URL = "https://api.stripe.com/v1/payment_pages/{cs_id}"

# OpenAI 给 Stripe 配的自定义域名（Stripe 响应 management_url 字段证实）
_OPENAI_PAY_DOMAIN = "pay.openai.com"
_STRIPE_CHECKOUT_DOMAIN = "checkout.stripe.com"

_HOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def fetch_hosted_url_via_stripe(
    checkout_session_id: str,
    publishable_key: str,
    *,
    proxy: Optional[str] = None,
    prefer_openai_domain: bool = True,
    timeout: int = 20,
) -> str:
    """用 OpenAI custom session 的凭证向 Stripe 换 hosted 长链。

    Args:
        checkout_session_id: OpenAI 响应里的 checkout_session_id（cs_live_xxx）
        publishable_key: OpenAI 响应里的 publishable_key（pk_live_xxx，Stripe 公钥）
        proxy: 可选代理（与 OpenAI 请求用同一出口即可）
        prefer_openai_domain: True 时把 checkout.stripe.com 替换为 pay.openai.com（OpenAI 站内长链）
        timeout: 请求超时秒数

    Returns:
        hosted 长链（成功）；空字符串（失败，调用方应回退到站内短链）
    """
    if not checkout_session_id or not publishable_key:
        logger.warning("Stripe 二跳缺 checkout_session_id 或 publishable_key，跳过")
        return ""

    url = _STRIPE_PAYMENT_PAGES_URL.format(cs_id=checkout_session_id)
    headers = {
        "Authorization": f"Bearer {publishable_key}",
        "User-Agent": _HOP_UA,
        "Accept": "application/json",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = requests.get(
            url,
            headers=headers,
            proxies=proxies,
            impersonate="chrome120",
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Stripe 二跳请求异常: %s", exc)
        return ""

    if resp.status_code != 200:
        logger.warning("Stripe 二跳非 200（%s）: %s", resp.status_code, resp.text[:200])
        return ""

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("Stripe 二跳响应非 JSON: %s", exc)
        return ""

    hosted = str(data.get("stripe_hosted_url", "") or "")
    if not hosted:
        logger.warning("Stripe 二跳响应缺 stripe_hosted_url，keys=%s", list(data.keys())[:20])
        return ""

    if prefer_openai_domain and _STRIPE_CHECKOUT_DOMAIN in hosted:
        hosted = hosted.replace(_STRIPE_CHECKOUT_DOMAIN, _OPENAI_PAY_DOMAIN)

    logger.info("Stripe 二跳成功，拿到 hosted 长链。")
    return hosted
