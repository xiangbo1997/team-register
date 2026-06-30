# -*- coding: utf-8 -*-
"""Checkout 链生成主客户端（HTTP 调用 + retry + schema 路由）。

公开类 PaymentLinkGenerator 保留所有原来的 classmethod 接口，外部调用方
（main.py / orchestrator.py / account_pool_service.py）无需修改。

内部分工：
- schemas/<plan>_v<n>.py  纯函数构造 payload
- aimizy.py               aimizy 中转（仅 Team 工作）
- url_postprocess.py      响应解析 + locale 注入
- client.py（本文件）     编排：路由 schema 版本 → 调上游 → 解析响应
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from curl_cffi import requests

from src.automation.sentinel import SentinelProvider, try_get_sentinel_token

from .aimizy import generate_via_aimizy
from .schemas import get_schema
from .stripe_hop import fetch_hosted_url_via_stripe
from .schemas._common import (
    APP_CHECKOUT_PREFIX,
    CHECKOUT_URL,
    DEFAULT_BILLING_COUNTRY,
    DEFAULT_BILLING_CURRENCY,
    DEFAULT_PRICE_INTERVAL,
    DEFAULT_SEAT_QUANTITY,
    DEFAULT_WORKSPACE_NAME,
    HOME_URL,
    PLUS_PLAN_NAME,
    PLUS_PROMO_ID,
    SUCCESS_URL_TEAM,
    TEAM_PLAN_NAME,
    TEAM_PROMO_ID,
)
from .url_postprocess import (
    append_promo_code_param,
    apply_locale,
    extract_app_checkout_link,
    select_checkout_link,
)

logger = logging.getLogger(__name__)

_PAYMENT_LINK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# 全局默认 schema 版本（被 .env PLUS_SCHEMA_VERSION / TEAM_SCHEMA_VERSION 覆盖）
_DEFAULT_SCHEMA_VERSIONS: dict[str, str] = {
    "plus": "v2",
    "team": "v1",
    "pro": "v1",
    "pro_lite": "v1",
}


def _resolve_schema_version(plan: str, override: Optional[str] = None) -> str:
    """决定用哪个 schema 版本。

    优先级：override（模板字段）> .env > _DEFAULT_SCHEMA_VERSIONS
    """
    if override:
        return override.strip().lower()
    # 延迟 import 避免循环
    try:
        from src.config import load_config  # type: ignore
        cfg = load_config()
        attr = f"{plan}_schema_version"
        env_value = getattr(cfg, attr, None)
        if env_value:
            return str(env_value).strip().lower()
    except Exception:  # config 加载失败不该阻塞链接生成
        pass
    return _DEFAULT_SCHEMA_VERSIONS.get(plan, "v1")


# 重试无意义的"连接层"错误关键词：代理/网关连不上时，立刻重试同一个抖动的
# 网关只会再等满一个超时窗口（curl_cffi 的 curl(28) = Connection timed out）。
# 命中这些关键词直接 fail-fast，把控制权交回用户（换代理 / 稍后重试），
# 而不是默默卡满 _MAX_RETRIES × timeout（实测可达 60s+）。
_CONNECTION_ERROR_MARKERS = (
    "curl: (28)",          # Connection timed out
    "connection timed out",
    "curl: (7)",           # Couldn't connect to server
    "failed to connect",
    "curl: (56)",          # Recv failure（代理隧道中断）
    "connection reset",
)


def _is_connection_error(message: str) -> bool:
    """判断异常文本是否属于"重试无意义的连接层错误"。

    业务错误（如 401/403，本就走 return 不进重试）和偶发 5xx 不在此列——
    那些值得重试。只对代理/网关连不上的网络故障 fail-fast。
    """
    low = (message or "").lower()
    return any(marker in low for marker in _CONNECTION_ERROR_MARKERS)


class PaymentLinkGenerator:
    """生成支付链接客户端。

    所有 classmethod 接口与原 src.payment_link.PaymentLinkGenerator 100% 兼容。
    内部改造：payload 构造从 _build_payload 单分支函数拆到 schemas/<plan>_v<n>.py。
    """

    # 兼容：原来作为类属性的常量（外部某些代码可能用 PaymentLinkGenerator._XXX 访问）
    _CHECKOUT_URL = CHECKOUT_URL
    _HOME_URL = HOME_URL
    _SUCCESS_URL = SUCCESS_URL_TEAM
    _CANCEL_URL_PLUS_PRICING = "https://chatgpt.com/#pricing"
    _SUCCESS_URL_PLUS = "https://chatgpt.com/"
    _TEAM_PLAN_NAME = TEAM_PLAN_NAME
    _TEAM_PROMO_ID = TEAM_PROMO_ID
    _PLUS_PLAN_NAME = PLUS_PLAN_NAME
    _PLUS_PROMO_ID = PLUS_PROMO_ID
    _DEFAULT_WORKSPACE_NAME = DEFAULT_WORKSPACE_NAME
    _DEFAULT_SEAT_QUANTITY = DEFAULT_SEAT_QUANTITY
    _DEFAULT_PRICE_INTERVAL = DEFAULT_PRICE_INTERVAL
    _AIMIZY_COUNTRY = DEFAULT_BILLING_COUNTRY
    _AIMIZY_CURRENCY = DEFAULT_BILLING_CURRENCY
    _APP_CHECKOUT_PREFIX = APP_CHECKOUT_PREFIX
    _PLUS_ALIASES = {"plus"}
    _TEAM_ALIASES = {"team", "team_trial", "team-free-trial", "team_free_trial"}
    _PRO_ALIASES = {"pro", "chatgptpro"}
    _PRO_LITE_ALIASES = {"pro_lite", "prolite", "pro-lite", "chatgptprolite"}
    _MAX_RETRIES = 3
    _RETRY_DELAY_SEC = 2

    @classmethod
    def generate_checkout_link(
        cls,
        access_token: str,
        plan_type: str = "team",
        proxy: Optional[str] = None,
        return_mode: str = "long",
        workspace_name: str = DEFAULT_WORKSPACE_NAME,
        seat_quantity: int = DEFAULT_SEAT_QUANTITY,
        price_interval: str = DEFAULT_PRICE_INTERVAL,
        success_url: str = SUCCESS_URL_TEAM,
        cancel_url: str = HOME_URL,
        aimizy_country: str = DEFAULT_BILLING_COUNTRY,
        aimizy_currency: str = DEFAULT_BILLING_CURRENCY,
        promo_code: Optional[str] = None,
        promo_campaign_id: Optional[str] = None,
        sentinel_provider: Optional[SentinelProvider] = None,
        borrow_headers: Optional[dict[str, str]] = None,
        borrow_cookies: Optional[dict[str, str]] = None,
        # P4 新增：模板字段（向后兼容默认 None）
        schema_version: Optional[str] = None,
        url_locale: Optional[str] = None,
        extra_payload: Optional[dict] = None,
        # P6 新增：checkout_ui_mode 暴露（默认 hosted，可改 custom）
        checkout_ui_mode: str = "hosted",
    ) -> Tuple[bool, str]:
        """生成 checkout 链接。

        新增参数（P4 模板化）：
        - schema_version: 单点覆盖全局默认 schema 版本（None → 用 .env / 内置默认）
        - url_locale: 生成成功后给 URL 加 ?locale=xxx（Stripe hosted page 渲染语言）
        - extra_payload: 浅合并到 OpenAI payload；运营 UI 的低代码扩展逃生口
        """
        normalized_plan = cls._normalize_plan_type(plan_type)
        if not normalized_plan:
            return False, f"不支持的支付计划: {plan_type}"

        # Team 计划：没传 promo_code 时走 aimizy 中间层拿 trial（已知工作路径）
        # Plus 计划：永远直调 OpenAI（aimizy 对 Plus 返回付费链 $20）
        if normalized_plan == "team" and not promo_code:
            ok, link = generate_via_aimizy(
                access_token,
                plan=normalized_plan,
                return_mode=return_mode,
                seat_quantity=seat_quantity,
                price_interval=price_interval,
                aimizy_country=aimizy_country,
                aimizy_currency=aimizy_currency,
                promo_campaign_id=promo_campaign_id,
            )
            if ok:
                return True, apply_locale(link, url_locale)
            logger.warning(
                "aimizy 通道失败/不支持 plan=%s，回退到直调 OpenAI API: %s",
                normalized_plan, link,
            )

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _PAYMENT_LINK_UA,
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            # v2.2: PayPal Auto Filler 脚本带这个 header，跟工作 ground truth 对齐
            "OAI-Language": "zh-CN",
        }
        # P0-借用：从 AdsPower 浏览器借来的反爬 header（x-oai-is / oai-device-id / sec-ch-ua-*）
        if borrow_headers:
            for k, v in borrow_headers.items():
                if v:
                    headers[k] = v
        sentinel_token = try_get_sentinel_token(
            sentinel_provider,
            flow="authorize_continue",
            user_agent=_PAYMENT_LINK_UA,
            proxy=proxy,
        )
        if sentinel_token and "x-oai-is" not in {k.lower() for k in headers}:
            headers["openai-sentinel-token"] = sentinel_token

        # 通过 Registry 取对应 schema 的 builder
        version = _resolve_schema_version(normalized_plan, schema_version)
        try:
            build_payload = get_schema(normalized_plan, version)
        except ValueError as exc:
            logger.error("schema 路由失败: %s", exc)
            return False, str(exc)

        payload = build_payload(
            workspace_name=workspace_name,
            seat_quantity=seat_quantity,
            price_interval=price_interval,
            promo_code=promo_code,
            promo_campaign_id=promo_campaign_id,
            billing_country=aimizy_country,
            billing_currency=aimizy_currency,
            extra_payload=extra_payload,
            success_url=success_url,
            cancel_url=cancel_url,
            checkout_ui_mode=checkout_ui_mode,
        )

        proxies = {"http": proxy, "https": proxy} if proxy else None
        cookies_to_send = dict(borrow_cookies) if borrow_cookies else None

        last_error = ""
        for attempt in range(1, cls._MAX_RETRIES + 1):
            try:
                logger.info(
                    "正在请求 %s 的结账会话... schema=%s/%s (attempt %d/%d)",
                    normalized_plan, normalized_plan, version, attempt, cls._MAX_RETRIES,
                )
                # 默认不打（含 access_token / billing_details 等敏感字段）
                # 需要排障时设 LOG_LEVEL=DEBUG 或 logging.getLogger("src.payment_link").setLevel(logging.DEBUG)
                if logger.isEnabledFor(logging.DEBUG):
                    import json as _json
                    logger.debug("payload: %s", _json.dumps(payload, ensure_ascii=False))
                response = requests.post(
                    CHECKOUT_URL,
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
                # 默认不打（OpenAI 响应可能含 checkout_session_id / hosted_url 等）
                if logger.isEnabledFor(logging.DEBUG):
                    import json as _json
                    logger.debug("response keys: %s", list(response_data.keys()))
                    logger.debug("response sample: %s", _json.dumps(response_data, ensure_ascii=False)[:2000])
                link = select_checkout_link(response_data, return_mode=return_mode)

                # Stripe 二跳：OpenAI 改版后 custom session 的 `url` 恒为 null，
                # select_checkout_link 只能退回站内短链。若调用方要长链（return_mode=long）
                # 但 OpenAI 没给 hosted url，则用 session 凭证向 Stripe 换 pay.openai.com 长链。
                # 详见 src/payment_link/stripe_hop.py 与 scripts/diag_stripe_second_hop.py
                wants_long = str(return_mode or "long").strip().lower() == "long"
                openai_url = str(response_data.get("url", "") or "")
                if wants_long and not openai_url:
                    cs_id = str(response_data.get("checkout_session_id", "") or "")
                    pk = str(response_data.get("publishable_key", "") or "")
                    if cs_id and pk:
                        hosted = fetch_hosted_url_via_stripe(cs_id, pk, proxy=proxy)
                        if hosted:
                            logger.info("成功获取 %s 计划 hosted 长链（Stripe 二跳）。", normalized_plan)
                            return True, apply_locale(hosted, url_locale)
                        logger.warning(
                            "%s 计划 Stripe 二跳未拿到长链，回退到 select_checkout_link 结果。",
                            normalized_plan,
                        )

                if link:
                    logger.info("成功获取 %s 计划 checkout 链接。", normalized_plan)
                    return True, apply_locale(link, url_locale)

                last_error = (
                    f"响应中未找到可用 checkout 链接 "
                    f"(状态码: {response.status_code}): {response.text}"
                )
                break
            except Exception as exc:  # pragma: no cover - 统一错误兜底
                last_error = str(exc)
                # 连接层错误（代理/网关连不上）：重试同一个抖动网关无意义，
                # 立刻 fail-fast 把控制权交回用户（换代理 / 稍后重试），
                # 避免默默卡满 _MAX_RETRIES × timeout（实测可达 60s+）。
                if _is_connection_error(last_error):
                    logger.warning(
                        "生成支付链接遇连接层错误，跳过剩余重试 (attempt %d/%d): %s",
                        attempt, cls._MAX_RETRIES, exc,
                    )
                    return False, f"代理/网络连接超时，请检查代理或稍后重试（{exc}）"
                logger.warning(
                    "生成支付链接异常 (attempt %d/%d): %s",
                    attempt, cls._MAX_RETRIES, exc,
                )
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
        aimizy_country: str = DEFAULT_BILLING_COUNTRY,
        aimizy_currency: str = DEFAULT_BILLING_CURRENCY,
        sentinel_provider: Optional[SentinelProvider] = None,
        borrow_headers: Optional[dict[str, str]] = None,
        borrow_cookies: Optional[dict[str, str]] = None,
    ) -> Tuple[bool, str]:
        """生成主流程可直接打开的站内 checkout 链接。"""
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
    def _normalize_plan_type(cls, plan_type: str) -> str:
        value = str(plan_type or "").strip().lower()
        if value in cls._PLUS_ALIASES:
            return "plus"
        if value in cls._TEAM_ALIASES:
            return "team"
        if value in cls._PRO_ALIASES:
            return "pro"
        if value in cls._PRO_LITE_ALIASES:
            return "pro_lite"
        return ""

    # 兼容：旧测试 / 调用方可能直接调这些 helper
    _append_promo_code_param = staticmethod(append_promo_code_param)
    _select_checkout_link = staticmethod(
        lambda response_data, *, return_mode: select_checkout_link(
            response_data, return_mode=return_mode
        )
    )
    _extract_app_checkout_link = staticmethod(extract_app_checkout_link)
