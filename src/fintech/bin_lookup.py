# -*- coding: utf-8 -*-
"""
BIN（Bank Identification Number）→ 发卡国查询

Stripe 风控依赖卡 BIN 所在国与代理 IP/SMS/账单地址的一致性。本模块提供：

1. 内置静态表（离线可用，覆盖常见 BIN 前缀）
2. 可选 HTTP 回源 binlist.net（仅作补充，不强依赖）

所有函数都做"静默失败"处理：失败场景一律返回空串 ""，
由 `src/fintech/coherence.py` 的 `validate_identity_coherence()` 据此判 block。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


# 内置静态 BIN 前缀 → ISO 3166-1 alpha-2 发卡国映射表
# 设计要点：
# - Key 为 4~6 位纯数字前缀
# - 查询时按"长前缀优先"策略逐级回退（先尝试 6 位、再 5 位、再 4 位）
# - 仅覆盖高频前缀，其他走 HTTP 回源兜底
_BIN_STATIC_TABLE: dict[str, str] = {
    # --- Efuncard 常见香港 BIN ---
    "4085": "HK",
    "453921": "HK",
    "530220": "HK",
    "540926": "HK",

    # --- NodeCard 常见美国 BIN ---
    "5577": "US",

    # --- 美国 Visa（含常见测试卡） ---
    "4111": "US",
    "4000": "US",
    "4242": "US",
    "444433": "US",
    "424242": "US",

    # --- 美国 Mastercard ---
    "5555": "US",
    "5200": "US",
    "5105": "US",

    # --- 美国 American Express ---
    "371449": "US",
    "498503": "US",

    # --- 中国大陆（Visa 合作 + 银联） ---
    "400115": "CN",
    "521302": "CN",
    "622588": "CN",
}


# binlist.net 回源地址
_BINLIST_URL = "https://lookup.binlist.net/{bin}"
_HTTP_TIMEOUT_SEC = 3


def _clean_card_number(card_number: Optional[str]) -> str:
    """
    清洗卡号：去除所有非数字字符（空格、短横线等），返回纯数字字符串。

    空串或 None 返回 ""。
    """
    if not card_number:
        return ""
    # 仅保留数字字符
    return "".join(ch for ch in str(card_number) if ch.isdigit())


def _match_static_table(digits: str) -> str:
    """
    按长度从 6 位到 4 位依次尝试命中静态表。

    返回命中的 ISO alpha-2 代码；未命中返回 ""。
    """
    if len(digits) < 4:
        return ""
    # 先 6 位 → 5 位 → 4 位（长前缀优先，避免 4085 被 4 位笼统规则覆盖）
    for prefix_len in (6, 5, 4):
        if len(digits) < prefix_len:
            continue
        prefix = digits[:prefix_len]
        if prefix in _BIN_STATIC_TABLE:
            return _BIN_STATIC_TABLE[prefix]
    return ""


def _default_http_get(url: str) -> dict:
    """
    默认 HTTP 回源实现。

    仅在 200 响应且 JSON 可解析时返回 payload；
    其余（超时、429、非 200、JSON 错误）统一抛出 RuntimeError 让上层捕获。
    """
    resp = requests.get(url, timeout=_HTTP_TIMEOUT_SEC)
    status = getattr(resp, "status_code", None)
    if status != 200:
        raise RuntimeError(f"binlist HTTP {status}")
    return resp.json()


def lookup_bin_country(
    card_number: str,
    *,
    http_get: Callable[[str], dict] | None = None,
) -> str:
    """
    根据卡号前 6 位（BIN）查发卡国（ISO alpha-2）。

    查找顺序：
      1. 内置静态表（长前缀优先，覆盖主流 BIN）
      2. 可选 HTTP 回源：GET https://lookup.binlist.net/<bin>
      3. 全部失败返回空字符串（coherence 校验会据此判 block）

    Args:
        card_number: 卡号（可以包含空格/短横线，会被清洗）
        http_get: 可注入的 HTTP 获取函数（测试时使用）；
                  生产默认 None → 走 requests.get 带 3s 超时。

    Returns:
        大写 ISO alpha-2 国家代码（例如 "US"、"HK"、"CN"），
        或空串 ""（未命中且 HTTP 回源失败）。
    """
    digits = _clean_card_number(card_number)
    if not digits:
        return ""

    # 1. 静态表命中
    static_hit = _match_static_table(digits)
    if static_hit:
        return static_hit

    # 2. HTTP 回源（需要至少 6 位 BIN）
    bin_prefix = digits[:6]
    if len(bin_prefix) < 6:
        return ""

    fetch = http_get if http_get is not None else _default_http_get
    try:
        payload = fetch(_BINLIST_URL.format(bin=bin_prefix))
    except Exception as exc:  # 包含 requests.RequestException、RuntimeError、ValueError 等
        logger.warning("BIN 回源查询失败 (bin=%s): %s", bin_prefix, exc)
        return ""

    if not isinstance(payload, dict):
        return ""

    country = payload.get("country")
    if not isinstance(country, dict):
        return ""

    alpha2 = country.get("alpha2")
    if not isinstance(alpha2, str):
        return ""

    alpha2 = alpha2.strip().upper()
    if len(alpha2) != 2 or not alpha2.isalpha():
        return ""
    return alpha2


__all__ = ["lookup_bin_country"]
