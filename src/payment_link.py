# -*- coding: utf-8 -*-
"""
生成 ChatGPT 支付 / 订阅链接工具。

本模块同时兼容：
- Plus: 参考用户提供的油猴脚本，优先请求 hosted checkout，并可返回长链接；
- Team 免费试用: 使用 `chatgptteamplan + promo_campaign(team-1-month-free)` 生成 Team trial checkout。

说明：
- `generate_checkout_link(..., return_mode="long")`：给“只测试链接生成”使用，优先返回服务端原始长链接；
- `generate_short_link(...)`：给主支付流程使用，优先返回 chatgpt.com 站内 checkout 链接。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional, Tuple

from curl_cffi import requests

from src.automation.sentinel import SentinelProvider, try_get_sentinel_token

logger = logging.getLogger(__name__)

_PAYMENT_LINK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


class PaymentLinkGenerator:
    """生成支付链接客户端"""

    _CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
    _AIMIZY_URL = "https://team.aimizy.com/api/public/generate-payment-link"
    _HOME_URL = "https://chatgpt.com/"
    _SUCCESS_URL = "https://chatgpt.com/?subscribed=true"
    _TEAM_PLAN_NAME = "chatgptteamplan"
    _TEAM_PROMO_ID = "team-1-month-free"
    _DEFAULT_WORKSPACE_NAME = "MyTeam"
    _DEFAULT_SEAT_QUANTITY = 5
    _DEFAULT_PRICE_INTERVAL = "month"
    _AIMIZY_COUNTRY = "SG"
    _AIMIZY_CURRENCY = "SGD"
    _APP_CHECKOUT_PREFIX = "https://chatgpt.com/checkout/openai_llc/"
    _PLUS_ALIASES = {"plus"}
    _TEAM_ALIASES = {"team", "team_trial", "team-free-trial", "team_free_trial"}
    _MAX_RETRIES = 3
    _RETRY_DELAY_SEC = 2

    @classmethod
    def generate_checkout_link(
        cls,
        access_token: str,
        plan_type: str = "team",
        proxy: Optional[str] = None,
        return_mode: str = "long",
        workspace_name: str = _DEFAULT_WORKSPACE_NAME,
        seat_quantity: int = _DEFAULT_SEAT_QUANTITY,
        price_interval: str = _DEFAULT_PRICE_INTERVAL,
        success_url: str = _SUCCESS_URL,
        cancel_url: str = _HOME_URL,
        aimizy_country: str = _AIMIZY_COUNTRY,
        aimizy_currency: str = _AIMIZY_CURRENCY,
        sentinel_provider: Optional[SentinelProvider] = None,
        borrow_headers: Optional[dict[str, str]] = None,
        borrow_cookies: Optional[dict[str, str]] = None,
    ) -> Tuple[bool, str]:
        """
        生成 checkout 链接。

        Args:
            access_token: 用户 Access Token
            plan_type: `plus` / `team`
            proxy: 可选代理
            return_mode: `long` 优先原始长链接；`app` 优先 chatgpt.com 站内 checkout
            workspace_name: Team workspace 名称
            seat_quantity: Team seat 数量
            price_interval: Team 周期，默认 month
            success_url: Hosted checkout 成功回跳地址
            cancel_url: Hosted checkout 取消回跳地址
            borrow_headers: 从 AdsPower 浏览器借来的反爬 header（x-oai-is / oai-device-id /
                            sec-ch-ua-* / user-agent 等）。详见 src/automation/browser_borrow.py
            borrow_cookies: 从 AdsPower 浏览器借来的 cookies（cf_clearance / session-token 等）
        """
        normalized_plan = cls._normalize_plan_type(plan_type)
        if not normalized_plan:
            return False, f"不支持的支付计划: {plan_type}"

        # Team 计划优先走 aimizy 中间层（免费试用优惠券依赖其后端处理）
        if normalized_plan == "team":
            ok, link = cls._generate_via_aimizy(
                access_token,
                return_mode=return_mode,
                seat_quantity=seat_quantity,
                price_interval=price_interval,
                aimizy_country=aimizy_country,
                aimizy_currency=aimizy_currency,
            )
            if ok:
                return True, link
            logger.warning("aimizy 通道失败，回退到直调 OpenAI API: %s", link)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _PAYMENT_LINK_UA,
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        # P0-借用：从 AdsPower 浏览器借来的反爬 header（x-oai-is / oai-device-id / sec-ch-ua-*）
        # 优先级：borrow_headers > 默认 headers，让 UA / sec-ch-ua 用浏览器真实值
        if borrow_headers:
            for k, v in borrow_headers.items():
                if v:
                    headers[k] = v
        # 兼容旧的 sentinel_provider 路径（noop 时返回空，pure_python 时试图注入 PoW）
        # 注：实测 OpenAI 2026 用 x-oai-is（来自 borrow_headers），openai-sentinel-token 仅为实验保留
        sentinel_token = try_get_sentinel_token(
            sentinel_provider,
            flow="authorize_continue",
            user_agent=_PAYMENT_LINK_UA,
            proxy=proxy,
        )
        if sentinel_token and "x-oai-is" not in {k.lower() for k in headers}:
            headers["openai-sentinel-token"] = sentinel_token
        payload = cls._build_payload(
            normalized_plan,
            return_mode=return_mode,
            workspace_name=workspace_name,
            seat_quantity=seat_quantity,
            price_interval=price_interval,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        proxies = {"http": proxy, "https": proxy} if proxy else None
        # P0-借用：把浏览器借来的 cookie 喂给 curl_cffi（含 cf_clearance / session-token 等）
        cookies_to_send = dict(borrow_cookies) if borrow_cookies else None

        last_error = ""
        for attempt in range(1, cls._MAX_RETRIES + 1):
            try:
                logger.info("正在请求 %s 的结账会话... (attempt %d/%d)", normalized_plan, attempt, cls._MAX_RETRIES)
                response = requests.post(
                    cls._CHECKOUT_URL,
                    headers=headers,
                    json=payload,
                    cookies=cookies_to_send,
                    proxies=proxies,
                    impersonate="chrome120",
                    timeout=20,
                )

                if response.status_code == 401:
                    return False, "Access Token 无效或已过期 (401 Unauthorized)"
                if response.status_code == 403:
                    return False, "请求被拒绝 (可能是 Cloudflare 拦截或账号状态异常 403 Forbidden)"

                response_data = response.json()
                link = cls._select_checkout_link(response_data, return_mode=return_mode)
                if link:
                    logger.info("成功获取 %s 计划 checkout 链接。", normalized_plan)
                    return True, link

                last_error = f"响应中未找到可用 checkout 链接 (状态码: {response.status_code}): {response.text}"
                break
            except Exception as exc:  # pragma: no cover - 统一错误兜底
                last_error = str(exc)
                logger.warning("生成支付链接异常 (attempt %d/%d): %s", attempt, cls._MAX_RETRIES, exc)
                if attempt < cls._MAX_RETRIES:
                    time.sleep(cls._RETRY_DELAY_SEC)
                    continue

        logger.error("生成支付链接时发生异常: %s", last_error)
        return False, last_error

    @classmethod
    def generate_short_link(
        cls,
        access_token: str,
        plan_type: str = "team",
        proxy: Optional[str] = None,
        aimizy_country: str = _AIMIZY_COUNTRY,
        aimizy_currency: str = _AIMIZY_CURRENCY,
        sentinel_provider: Optional[SentinelProvider] = None,
        borrow_headers: Optional[dict[str, str]] = None,
        borrow_cookies: Optional[dict[str, str]] = None,
    ) -> Tuple[bool, str]:
        """
        生成主流程可直接打开的站内 checkout 链接。

        - Team trial 优先返回 `checkout_session_id -> chatgpt.com/checkout/openai_llc/...`
        - Plus 若只有 Stripe 长链接，则尽量回退为 chatgpt.com app link；回退失败时再返回原始 url
        """
        return cls.generate_checkout_link(
            access_token,
            plan_type=plan_type,
            proxy=proxy,
            return_mode="app",
            aimizy_country=aimizy_country,
            aimizy_currency=aimizy_currency,
            sentinel_provider=sentinel_provider,
            borrow_headers=borrow_headers,
            borrow_cookies=borrow_cookies,
        )

    @classmethod
    def _generate_via_aimizy(
        cls,
        access_token: str,
        *,
        return_mode: str = "app",
        seat_quantity: int = _DEFAULT_SEAT_QUANTITY,
        price_interval: str = _DEFAULT_PRICE_INTERVAL,
        aimizy_country: str = _AIMIZY_COUNTRY,
        aimizy_currency: str = _AIMIZY_CURRENCY,
    ) -> Tuple[bool, str]:
        """通过 aimizy 中间层生成 Team 免费试用链接。"""
        normalized_mode = str(return_mode or "app").strip().lower()
        payload = {
            "access_token": access_token,
            "plan_name": cls._TEAM_PLAN_NAME,
            "country": str(aimizy_country or cls._AIMIZY_COUNTRY).upper(),
            "currency": str(aimizy_currency or cls._AIMIZY_CURRENCY).upper(),
            "promo_campaign_id": cls._TEAM_PROMO_ID,
            "is_coupon_from_query_param": True,
            "seat_quantity": int(seat_quantity),
            "price_interval": price_interval,
            "check_card_proxy": False,
            "is_short_link": normalized_mode == "app",
        }
        try:
            resp = requests.post(
                cls._AIMIZY_URL,
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
                link = cls._select_checkout_link(data, return_mode=normalized_mode)
                if link:
                    logger.info("通过 aimizy 成功生成 Team checkout 链接。")
                    return True, link
            return False, f"aimizy 响应无可用链接: {resp.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    @classmethod
    def _normalize_plan_type(cls, plan_type: str) -> str:
        value = str(plan_type or "").strip().lower()
        if value in cls._PLUS_ALIASES:
            return "plus"
        if value in cls._TEAM_ALIASES:
            return "team"
        return ""

    @classmethod
    def _build_payload(
        cls,
        normalized_plan: str,
        *,
        return_mode: str,
        workspace_name: str,
        seat_quantity: int,
        price_interval: str,
        success_url: str,
        cancel_url: str,
    ) -> dict:
        if normalized_plan == "plus":
            # 对齐用户给的油猴脚本 hpp() 逻辑
            return {
                "plan_type": "plus",
                "checkout_ui_mode": "hosted",
                "cancel_url": cancel_url,
                "success_url": success_url,
            }

        # 对齐油猴脚本 hts() 与 ChatGPT 官网：Team 统一 custom 模式
        # hosted 模式会导致免费试用优惠券无法正确应用
        return {
            "plan_name": cls._TEAM_PLAN_NAME,
            "team_plan_data": {
                "workspace_name": workspace_name,
                "price_interval": price_interval,
                "seat_quantity": int(seat_quantity),
            },
            "promo_campaign": {
                "promo_campaign_id": cls._TEAM_PROMO_ID,
                "is_coupon_from_query_param": True,
            },
            "checkout_ui_mode": "custom",
        }

    @classmethod
    def _select_checkout_link(cls, response_data: dict, *, return_mode: str) -> str:
        long_url = str(response_data.get("url", "") or "")
        app_link = cls._extract_app_checkout_link(response_data)
        mode = str(return_mode or "long").strip().lower()

        if mode == "app":
            return app_link or long_url
        return long_url or app_link

    @classmethod
    def _extract_app_checkout_link(cls, response_data: dict) -> str:
        checkout_session_id = str(response_data.get("checkout_session_id", "") or "")
        if checkout_session_id:
            return f"{cls._APP_CHECKOUT_PREFIX}{checkout_session_id}"

        long_url = str(response_data.get("url", "") or "")
        if not long_url:
            return ""
        if long_url.startswith(cls._APP_CHECKOUT_PREFIX):
            return long_url

        match = re.search(r"cs_live_[a-zA-Z0-9]+", long_url)
        if match:
            return f"{cls._APP_CHECKOUT_PREFIX}{match.group(0)}"
        return ""


if __name__ == "__main__":
    import os

    test_token = os.getenv("TEST_ACCESS_TOKEN", "")
    plan = os.getenv("PAYMENT_PLAN", "team")
    link_mode = os.getenv("PAYMENT_LINK_MODE", "long")
    proxy = os.getenv("PROXY", "") or None
    aimizy_country = os.getenv("AIMIZY_COUNTRY", "SG")
    aimizy_currency = os.getenv("AIMIZY_CURRENCY", "SGD")

    if not test_token:
        print("请提供环境变量 TEST_ACCESS_TOKEN 来进行测试")
    else:
        success, link = PaymentLinkGenerator.generate_checkout_link(
            test_token,
            plan_type=plan,
            proxy=proxy,
            return_mode=link_mode,
            aimizy_country=aimizy_country,
            aimizy_currency=aimizy_currency,
        )
        if success:
            print("生成成功! checkout 链接:", link)
        else:
            print("生成失败:", link)
