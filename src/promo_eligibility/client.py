# -*- coding: utf-8 -*-
"""ChatGPT 促销码 eligibility 客户端

调两个 API：
  GET /backend-api/promotions/eligibility/{code}?type=promo  —— 判定状态
  GET /backend-api/promotions/metadata/{code}?type=promo     —— 拿折扣元数据

实现注意：
  - curl_cffi impersonate 必须与 src/payment_link.py 一致（chrome120），
    避免同 access_token 在短时间内出现两种 chrome 指纹被风控
  - 不内置 retry / sleep —— 由上层 service 控制节奏
  - 不内置 session 复用 —— 每次调用新建 session，避免 cookie 在多代理之间串
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from curl_cffi import requests

from src.automation.sentinel import SentinelProvider, try_get_sentinel_token
from src.db.models import EligibilityStatus

logger = logging.getLogger(__name__)


_BASE_URL = "https://chatgpt.com/backend-api/promotions"
_IMPERSONATE = "chrome120"  # 必须与 src/payment_link.py:145 保持一致
_TIMEOUT_SEC = 15

# 与 payment_link 用同一 UA，保证同 access_token 对外 TLS 指纹自洽
_PROMO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass
class EligibilityResult:
    """单个 promo 码验证结果。"""

    code: str
    status: str                              # EligibilityStatus.* 之一
    reason_code: str = ""                    # ChatGPT 返回的原始 ineligible_reason.code
    reason_message: str = ""                 # ChatGPT 返回的原始 ineligible_reason.message
    metadata_raw: Optional[dict] = None      # /metadata API 的完整响应；status=not_found 时为 None
    http_status: int = 0                     # eligibility API 的 HTTP 状态码（debug 用）
    error: str = ""                          # status=error 时填写的人可读错误描述


def _classify(eligibility_data: dict) -> tuple[str, str, str]:
    """从 /eligibility 响应里解析 (status, reason_code, reason_message)。"""
    if eligibility_data.get("is_eligible"):
        return EligibilityStatus.ELIGIBLE, "", ""

    reason = eligibility_data.get("ineligible_reason") or {}
    reason_code = str(reason.get("code") or "")
    reason_message = str(reason.get("message") or "")

    if reason_code == "user_not_eligible":
        return EligibilityStatus.EXISTS, reason_code, reason_message
    if reason_code == "invalid_code":
        return EligibilityStatus.NOT_FOUND, reason_code, reason_message
    if reason_code == "code_already_redeemed":
        return EligibilityStatus.REDEEMED, reason_code, reason_message
    return EligibilityStatus.UNKNOWN, reason_code, reason_message


def check_eligibility(
    *,
    access_token: str,
    code: str,
    proxy_url: Optional[str] = None,
    sentinel_provider: Optional[SentinelProvider] = None,
    borrow_headers: Optional[dict] = None,
    borrow_cookies: Optional[dict] = None,
) -> EligibilityResult:
    """验证单个 promo 码。

    Args:
        access_token: ChatGPT accessToken（不带 "Bearer " 前缀）
        code: 促销码（如 "talentgeniusus"）
        proxy_url: 出口代理 URL；None 表示直连。需要与 promo 码所属国家匹配，
                   否则即使码有效也只会返回 EXISTS 而非 ELIGIBLE
        sentinel_provider: 可选 Sentinel PoW token 生成器（实验路径，主路径用 borrow_headers）。
        borrow_headers: 从 AdsPower 浏览器借来的反爬 header（x-oai-is / oai-device-id / sec-ch-ua-*）
                        详见 src/automation/browser_borrow.py
        borrow_cookies: 从 AdsPower 浏览器借来的 cookies（cf_clearance / session-token 等）

    Returns:
        EligibilityResult；网络/解析异常时 status=ERROR + error 字段填错误描述
    """
    safe_code = (code or "").strip()
    if not safe_code:
        return EligibilityResult(code="", status=EligibilityStatus.ERROR, error="code 为空")
    if not access_token:
        return EligibilityResult(code=safe_code, status=EligibilityStatus.ERROR, error="access_token 为空")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    # P0-借用：从 AdsPower 浏览器借来的反爬 header（x-oai-is / oai-device-id / sec-ch-ua-*）
    if borrow_headers:
        for k, v in borrow_headers.items():
            if v:
                headers[k] = v
    # 兼容旧的 sentinel_provider 路径（实验留作 fallback）
    sentinel_token = try_get_sentinel_token(
        sentinel_provider,
        flow="authorize_continue",
        user_agent=_PROMO_UA,
        proxy=proxy_url,
    )
    if sentinel_token and "x-oai-is" not in {k.lower() for k in headers}:
        headers["openai-sentinel-token"] = sentinel_token
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    cookies_to_send = dict(borrow_cookies) if borrow_cookies else None

    eligibility_url = f"{_BASE_URL}/eligibility/{safe_code}?type=promo"
    try:
        resp = requests.get(
            eligibility_url,
            headers=headers,
            cookies=cookies_to_send,
            proxies=proxies,
            impersonate=_IMPERSONATE,
            timeout=_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.warning("eligibility 请求异常 code=%s err=%s", safe_code, exc)
        return EligibilityResult(code=safe_code, status=EligibilityStatus.ERROR, error=str(exc))

    if resp.status_code == 401:
        return EligibilityResult(
            code=safe_code,
            status=EligibilityStatus.ERROR,
            http_status=401,
            error="access_token 无效或过期 (401)",
        )
    if resp.status_code == 403:
        return EligibilityResult(
            code=safe_code,
            status=EligibilityStatus.ERROR,
            http_status=403,
            error="请求被拒绝 (403, 可能 Cloudflare 拦截或代理 IP 被识别)",
        )

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("eligibility 响应非 JSON code=%s status=%s body[:200]=%r",
                       safe_code, resp.status_code, resp.text[:200])
        return EligibilityResult(
            code=safe_code,
            status=EligibilityStatus.ERROR,
            http_status=resp.status_code,
            error=f"响应非 JSON: {exc}",
        )

    status, reason_code, reason_message = _classify(data)
    result = EligibilityResult(
        code=safe_code,
        status=status,
        reason_code=reason_code,
        reason_message=reason_message,
        http_status=resp.status_code,
    )

    # 仅 eligible/exists 才拉 metadata（not_found 拉了也是 invalid_code）
    if status in (EligibilityStatus.ELIGIBLE, EligibilityStatus.EXISTS):
        result.metadata_raw = _fetch_metadata(safe_code, headers, proxies, cookies_to_send)

    return result


def _fetch_metadata(
    code: str,
    headers: dict,
    proxies: Optional[dict],
    cookies: Optional[dict] = None,
) -> Optional[dict]:
    """拉 /metadata API 的折扣详情；失败时返回 None（不阻断主流程）。"""
    url = f"{_BASE_URL}/metadata/{code}?type=promo"
    try:
        resp = requests.get(
            url,
            headers=headers,
            cookies=cookies,
            proxies=proxies,
            impersonate=_IMPERSONATE,
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.info("metadata 非 200 code=%s status=%s", code, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        logger.info("metadata 请求异常 code=%s err=%s", code, exc)
        return None
