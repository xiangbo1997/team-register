# -*- coding: utf-8 -*-
"""响应解析 + URL 后处理（locale 注入等）。"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .schemas._common import APP_CHECKOUT_PREFIX


def select_checkout_link(response_data: dict, *, return_mode: str) -> str:
    """从 OpenAI / aimizy 响应里挑长链或短链。

    return_mode='app' → 优先短链（app_link 站内 checkout）
    return_mode='long' → 优先长链（pay.openai.com hosted url）
    """
    long_url = str(response_data.get("url", "") or "")
    app_link = extract_app_checkout_link(response_data)
    mode = str(return_mode or "long").strip().lower()

    if mode == "app":
        return app_link or long_url
    return long_url or app_link


def extract_app_checkout_link(response_data: dict) -> str:
    """优先 checkout_session_id 拼 app prefix；否则从 url 里抓 cs_live_*。"""
    checkout_session_id = str(response_data.get("checkout_session_id", "") or "")
    if checkout_session_id:
        return f"{APP_CHECKOUT_PREFIX}{checkout_session_id}"

    long_url = str(response_data.get("url", "") or "")
    if not long_url:
        return ""
    if long_url.startswith(APP_CHECKOUT_PREFIX):
        return long_url

    match = re.search(r"cs_live_[a-zA-Z0-9]+", long_url)
    if match:
        return f"{APP_CHECKOUT_PREFIX}{match.group(0)}"
    return ""


def append_promo_code_param(url: str, promo_code: str) -> str:
    """把 ?promoCode=xxx 拼到 URL 末尾，自动处理已有 query string。

    仅给 Team 的 cancel_url 注入用；ascii 优惠码安全 fallback。
    """
    sep = "&" if "?" in url else "?"
    safe_code = re.sub(r"[^A-Za-z0-9_\-]", "", str(promo_code))
    if not safe_code:
        return url
    return f"{url}{sep}promoCode={safe_code}"


def apply_locale(url: str, locale: Optional[str]) -> str:
    """给 hosted url 拼 ?locale=xxx，None / 空字符串 noop。

    场景：模板里设置 url_locale='en' / 'ja' / 'zh-CN' 让 Stripe hosted page
    用指定语言渲染，对 OpenAI 后端 trial 校验无影响。
    """
    if not locale or not url:
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query["locale"] = locale.strip()
    return urlunparse(parts._replace(query=urlencode(query)))
