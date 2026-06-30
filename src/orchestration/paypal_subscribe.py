# -*- coding: utf-8 -*-
"""在已登录 chatgpt.com 的 AdsPower 浏览器里自动走完 PayPal Plus 订阅。

为什么是这条路（2026-06-24 经 8 agent + 3 轮真机实测 + 竞品 gpt-pp 源码逆向定案）：
  「从服务端 API 抠一条可分享的 PayPal 链」在 OpenAI 商户上**物理不可行**——
  OpenAI 在 Stripe 后台关闭了 pk_live tokenize（confirm card 直接报
  `unsupported for publishable key tokenization`），且 $0 首期订阅走 setup 路径
  没有可 confirm 出 next_action 的 PaymentIntent，pm-redirects / paypal.com 链
  都抠不出（详见内存 reference_paypal_serverside_link_impossible）。

  pay.openai.com 长链匿名打开点 PayPal 卡「正在处理」= embedded session 的
  return_url 绑登录态前端上下文，匿名缺失（reference_paypal_longlink_unsolvable）。

  唯一能让 PayPal 真正跳出去的路 = 在 chatgpt.com **登录态浏览器**里走完支付：
  return_url 上下文完整 → 点 PayPal 能跳出去授权 → 回跳完成订阅。
  产物不是「可分享链」，是「替本号在本机走完 Plus 订阅」。

链路（油猴 generatePlusHostedLink 实证姿势）：
  1. 起该号 AdsPower 浏览器，_ensure_logged_in 确保登录态 + 串号防护（读 session.user.email 比对）
  2. 浏览器内 page.evaluate(fetch) 调 /backend-api/payments/checkout（带 cookie，
     US/USD + plus-1-month-free）→ 拿 cs_id。**必须带 cookie**：裸 token 调 promo 会被拒
     （实测 requires_manual_approval=true / $20），登录态 cookie 才认 $0 免月。
  3. 同标签页 window.location.href 整页跳短链 chatgpt.com/checkout/openai_llc/{cs_id}
     （**不是 page.goto 新开页**——新开页丢登录态会卡）
  4. 等 embedded Stripe.js 渲染 → 点选 PayPal 支付方式 → 提交
  5. 跳 paypal.com 授权（可选自动登录）→ 回跳 chatgpt.com 完成订阅
  6. 读 session 验证 plan 是否变 plus

复用积木（与 verify_plus / token_refresh 同源，避免登录代码漂移）：
  - get_browser_ws（起浏览器，src/browser.py）
  - _ensure_logged_in / _fetch_session / _detect_account_deactivated（verify_plus.py）
  - _click_first_visible / _fill_first_visible（handlers.py）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from src.browser import get_browser_ws
from src.config import AppConfig
from src.orchestration.handlers import (
    _click_first_visible,
    is_chatgpt_logged_in,
)
from src.orchestration.verify_plus import (
    _detect_account_deactivated,
    _detect_subscription,
    _ensure_logged_in,
    _fetch_session,
)
from src.utils import human_delay

logger = logging.getLogger(__name__)

CHATGPT_URL = "https://chatgpt.com/"
CHECKOUT_API = "https://chatgpt.com/backend-api/payments/checkout"
# 站内短链：在登录态前端路由消费，挂载 embedded Stripe.js（return_url 上下文完整）
_APP_CHECKOUT_PREFIX = "https://chatgpt.com/checkout/openai_llc/"

# PayPal 单选钮 selector 候选（embedded checkout 是 Stripe.js react 组件，DOM 随版本变，
# 用多重 fallback + 截图存证，真机 DOM 为准）。
_PAYPAL_RADIO_SELECTORS = (
    'input[type="radio"][value="paypal"]',
    'input[id*="paypal" i]',
    '[data-testid*="paypal" i]',
    'label:has-text("PayPal")',
    'button:has-text("PayPal")',
    'text=PayPal',
)
# 提交订阅按钮 selector 候选（中/英/日文兜底）。
_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button:has-text("订阅")',
    'button:has-text("サブスクリプションを登録")',
    'button:has-text("Subscribe")',
    'button:has-text("Start")',
    '[data-testid*="confirm" i]',
)


def execute_paypal_subscription(
    config: AppConfig,
    profile_id: str,
    email: str,
    *,
    password: str = "",
    mail_api: Any = None,
    billing_country: str = "US",
    billing_currency: str = "USD",
    auto_authorize_paypal: bool = False,
    half_auto: bool = True,
    promo_campaign_id: str = "plus-1-month-free",
    playwright: Optional[Playwright] = None,
) -> dict[str, Any]:
    """起该号 AdsPower 浏览器，在登录态里走 PayPal Plus 订阅。

    **半自动模式（half_auto=True，默认）**：自动备好 checkout 页（创建 session +
    跳页 + 选 PayPal + 填账单），**停在提交前不自动点提交、不关浏览器页面**，由真人
    在浏览器里点最后一下「订阅」完成 PayPal 授权。原因：提交按钮触发的 confirm 受
    invisible-captcha/sentinel 客户端人机校验保护，CDP 自动化点击拿不到 token →
    「出错了」不发请求；真人点击的连续行为信号能让 invisible captcha 通过（详见
    reference_paypal_serverside_link_impossible）。
    half_auto=False 时仍尝试自动点提交（大概率被 captcha 拦，仅留作实验/接 solver 后用）。

    Args:
        config: AppConfig（取 ads_api / ads_api_key / run_artifacts_dir）
        profile_id: 该号 AdsPower profile（Run.profile_id）
        email / password: 登录凭据（未登录自动 magic link 登录用；防串号校验用 email）
        mail_api: MailManager 实例，magic link 拉验证码用；缺失则未登录时无法自动登录
        billing_country / billing_currency: 账单国家/货币（PayPal 在售依赖 US/EUR，
            SG/SGD 只有 card；默认 US/USD）
        auto_authorize_paypal: True 时尝试在 paypal.com 自动登录授权（需 PayPal 凭据，
            当前未接，默认 False = 走到 PayPal 页停下等人工授权或后续扩展）
        promo_campaign_id: 优惠类型（决定能否走 PayPal）：
            - "plus-1-month-free"（默认）: 100% 券 → $0 → **PayPal 物理跳不出**（无 intent），
              此组合只适合银行卡，PayPal 必失败。
            - "plus-1-month-50-pct-off": 50% 券 → $10 真金额 → 有 PaymentIntent →
              **PayPal 能跳出授权（真付 $10，非白嫖）**。
        playwright: 已存在的 sync Playwright 实例（API 同步线程应自己开传进来）

    Returns:
        {
          "success": bool,            # 订阅是否走完且 Plus 生效
          "plan": str,                # 走完后实时 plan（plus/free/...）
          "is_plus": bool,
          "stage": str,               # 走到哪一步（checkout/redirect/paypal_selected/submitted/authorized/verified/failed_*）
          "checkout_session_id": str,
          "reached_paypal": bool,     # 是否成功跳到 paypal.com
          "logged_in": bool,
          "relogged_in": bool,
          "message": str,             # 给运维看的人话结论
          "detail": str,
          "screenshots": list[str],   # 证据截图路径
        }
    """
    try:
        ws_url = get_browser_ws(
            ads_api=config.ads_api,
            user_id=profile_id,
            api_key=config.ads_api_key,
        )
    except Exception as exc:
        logger.error("PayPal 订阅中止：无法连接 AdsPower (%s)", exc)
        return _result(message="无法启动浏览器", detail=f"adspower_failed: {exc}"[:300])

    artifacts_dir = str(getattr(config, "run_artifacts_dir", "") or "artifacts")
    inner_args = (
        ws_url, email, password, mail_api,
        billing_country, billing_currency, auto_authorize_paypal, half_auto,
        promo_campaign_id, artifacts_dir,
    )
    if playwright is not None:
        return _run_inner(playwright, *inner_args)
    with sync_playwright() as p:
        return _run_inner(p, *inner_args)


def _run_inner(
    p: Playwright,
    ws_url: str,
    email: str,
    password: str,
    mail_api: Any,
    billing_country: str,
    billing_currency: str,
    auto_authorize_paypal: bool,
    half_auto: bool,
    promo_campaign_id: str,
    artifacts_dir: str,
) -> dict[str, Any]:
    """订阅核心逻辑，要求外部已提供活跃的 Playwright 实例。"""
    page: Optional[Page] = None
    screenshots: list[str] = []
    try:
        logger.info("连接浏览器实例执行 PayPal 订阅...")
        browser = p.chromium.connect_over_cdp(ws_url)
        context: BrowserContext = browser.contexts[0]
        page = context.new_page()
        page.set_default_timeout(30000)

        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30000)
        human_delay(1, 2)

        # 停用号探测：命中则短路（不白费一次 magic link 验证码）
        if _detect_account_deactivated(page):
            out = _result(
                logged_in=False,
                stage="failed_deactivated",
                message="⛔ 该号已被 OpenAI 停用（account_deactivated），无法订阅，建议归档",
                detail="account_deactivated",
            )
            out["relogged_in"] = False
            return out

        # 未登录则自动登录（与刷新 token / 核验同一套积木，含串号防护）
        page, relogged, login_err = _ensure_logged_in(context, page, email, password, mail_api)
        if login_err is not None:
            login_err["relogged_in"] = relogged
            login_err.setdefault("stage", "failed_login")
            login_err.setdefault("success", False)
            return login_err

        if not is_chatgpt_logged_in(page):
            out = _result(
                logged_in=False, stage="failed_login",
                message="登录态校验未通过，无法订阅", detail="not_logged_in_after_ensure",
            )
            out["relogged_in"] = relogged
            return out

        # ── 步骤 1：浏览器内带 cookie 创建 checkout session（promo 决定 $0/$10）──
        logger.info("PayPal 订阅：创建 checkout session（promo=%s）", promo_campaign_id)
        cs_id = _create_checkout_session_in_browser(
            page, billing_country, billing_currency, promo_campaign_id,
        )
        if not cs_id:
            out = _result(
                logged_in=True, relogged_in=relogged, stage="failed_checkout",
                message="创建 checkout session 失败（可能 promo 未生效或风控，看日志）",
                detail="checkout_session_not_created",
            )
            return out
        logger.info("PayPal 订阅：拿到 checkout_session_id=%s", cs_id[:24])

        # ── 步骤 2：同标签页 window.location.href 整页跳短链（油猴姿势）──
        short_link = _APP_CHECKOUT_PREFIX + cs_id
        logger.info("PayPal 订阅：整页跳转短链 %s", short_link[:70])
        try:
            page.evaluate("(url) => { window.location.href = url; }", short_link)
        except Exception as exc:
            logger.warning("location.href 跳转异常: %s", exc)
        # 等 SPA 路由 + embedded Stripe.js 渲染（checkout 页较慢）
        try:
            page.wait_for_load_state("domcontentloaded", timeout=40000)
        except Exception:
            pass
        # 等 checkout 页关键元素出现（SPA 路由 + embedded Stripe.js 渲染慢，
        # 整页跳转后纯固定 sleep 截到白屏；轮询关键文本/iframe 就绪再继续）
        ready = _wait_checkout_ready(page, timeout_sec=45)
        screenshots.append(_shot(page, artifacts_dir, "pp_01_checkout_page", cs_id))
        if not ready:
            logger.warning("checkout 页未在超时内渲染就绪，仍尝试选 PayPal（看截图）")

        # ── 步骤 3：点选 PayPal 支付方式（选后展开账单表单）──
        paypal_selected = _select_paypal_method(page)
        screenshots.append(_shot(page, artifacts_dir, "pp_02_paypal_selected", cs_id))
        if not paypal_selected:
            out = _result(
                logged_in=True, relogged_in=relogged, stage="failed_select_paypal",
                checkout_session_id=cs_id, screenshots=screenshots,
                message="未能在 checkout 页选中 PayPal（PayPal 可能不在售，需 US/EUR 账单；或页面结构变化，看截图）",
                detail="paypal_method_not_selected",
            )
            return out

        # ── 步骤 3.5：账单国家设为目标国家 + 填地址 ──
        # US/USD session 配日本账单会被 Stripe 拒（货币国家不一致 →「出错了请重试」）。
        # 选 PayPal 后账单表单已展开，此时改国家（触发重渲染等稳定），再填对应国家地址。
        country_changed = _set_billing_country(page, billing_country)
        if country_changed:
            # 改国家会重渲染、可能重置 PayPal 选中态，重选一次确保 PayPal 仍选中
            _select_paypal_method(page)
        _fill_billing_address(page, billing_country)
        screenshots.append(_shot(page, artifacts_dir, "pp_02b_billing_filled", cs_id))

        # ── 半自动模式：备好就停，留浏览器给真人点提交（绕 invisible-captcha）──
        # 提交按钮的 confirm 受 hcaptcha-invisible/sentinel 客户端人机校验保护，CDP
        # 自动点击拿不到 token →「出错了」不发请求；真人点击的连续行为信号能过校验。
        # 故备好账单后停下，不自动点提交、**不关页面**，返回明确指引。
        if half_auto:
            page = None  # 置空避免 finally 关掉这个 checkout 页（留给真人操作）
            logger.info("半自动：checkout 已备好（PayPal 选中 + 账单已填），等真人点提交")
            return _result(
                success=False, logged_in=True, relogged_in=relogged, stage="ready_for_manual_submit",
                checkout_session_id=cs_id, reached_paypal=False, screenshots=screenshots,
                message="✅ 已备好 PayPal checkout（已选 PayPal + 已填账单 + $0 免月）。"
                        "请在弹出的浏览器里点「サブスクリプションを登録する / 订阅」按钮，"
                        "即可跳转 PayPal 完成授权（机器点会被人机校验拦，需真人点这一下）。",
                detail="ready_for_manual_submit",
            )

        # ── 步骤 4：提交订阅，触发 PayPal 跳转（带「出错了请重试」自动重试）──
        reached_paypal = False
        submit_error = ""
        for attempt in range(3):
            _click_first_visible(page, _SUBMIT_SELECTORS, description=f"订阅提交#{attempt+1}")
            reached_paypal = _wait_paypal_redirect(page, timeout_sec=25)
            if reached_paypal:
                break
            # 检测「出错了请重试」类错误（Stripe/OpenAI 提交失败提示，可重试）
            submit_error = _detect_submit_error(page)
            if submit_error:
                logger.warning("提交#%d 出错（%s），重试...", attempt + 1, submit_error[:40])
                time.sleep(3)
                continue
            # 无错误也没跳，可能在处理中，再等一轮
            logger.info("提交#%d 未跳转也无明确错误，再等...", attempt + 1)
            reached_paypal = _wait_paypal_redirect(page, timeout_sec=15)
            if reached_paypal:
                break
        screenshots.append(_shot(page, artifacts_dir, "pp_03_after_submit", cs_id))

        if not reached_paypal:
            err_hint = f"（提交报错: {submit_error[:60]}）" if submit_error else ""
            out = _result(
                logged_in=True, relogged_in=relogged, stage="submitted",
                checkout_session_id=cs_id, reached_paypal=False, screenshots=screenshots,
                message=f"已提交但未跳到 PayPal{err_hint}。多为风控拦截（脏 profile/数据中心 IP "
                        "触发 Stripe Radar）；建议换干净 profile + 住宅 IP 重试，看截图。",
                detail=f"paypal_redirect_not_reached; submit_error={submit_error[:80]}",
            )
            return out

        logger.info("PayPal 订阅：已跳到 paypal.com 授权页")

        # ── 步骤 5：PayPal 授权（自动授权未接，默认走到这步停下）──
        if not auto_authorize_paypal:
            out = _result(
                logged_in=True, relogged_in=relogged, stage="reached_paypal",
                checkout_session_id=cs_id, reached_paypal=True, screenshots=screenshots,
                message="✅ 已成功跳到 PayPal 授权页（return_url 上下文完整，未卡转圈）。"
                        "请在该浏览器内手动完成 PayPal 登录授权，或后续接入自动授权。",
                detail="reached_paypal_manual_authorize",
            )
            return out

        # auto_authorize 预留：当前未实现 PayPal 凭据自动登录，如实标注
        out = _result(
            logged_in=True, relogged_in=relogged, stage="reached_paypal",
            checkout_session_id=cs_id, reached_paypal=True, screenshots=screenshots,
            message="已到 PayPal 授权页；自动授权尚未实现，需人工或后续扩展",
            detail="auto_authorize_not_implemented",
        )
        return out

    except Exception as exc:
        logger.error("PayPal 订阅执行异常: %s", exc)
        out = _result(
            stage="failed_exception", screenshots=screenshots,
            message="订阅执行异常", detail=f"exception: {exc}"[:300],
        )
        return out
    finally:
        if page is not None:
            try:
                page.close()
            except Exception as close_exc:
                logger.debug("关闭 PayPal 订阅 page 失败（可忽略）: %s", close_exc)


def _create_checkout_session_in_browser(
    page: Page,
    billing_country: str,
    billing_currency: str,
    promo_campaign_id: str = "plus-1-month-free",
) -> str:
    """浏览器内带 cookie 调 OpenAI checkout API 创建 plus session，返回 cs_id。

    **必须带 cookie**（credentials:'include'）：裸 access_token 调 promo 会被 OpenAI 拒
    （实测 requires_manual_approval=true + $20 全价），登录态 cookie 才认 promo 优惠。
    payload 复刻油猴 generatePlusHostedLink。

    promo_campaign_id 决定优惠类型（详见内存 reference_paypal_serverside_link_impossible）：
      - "plus-1-month-free"      : 100% 折扣券 → 首期 $0，但 **$0+PayPal 物理跳不出**
                                   （无 intent → confirm 无 next_action）。$0 只能配银行卡。
      - "plus-1-month-50-pct-off": 50% 折扣券 → 首期 $10 真金额 → 有 PaymentIntent →
                                   PayPal 能 confirm 出 pm-redirects → **能跳出授权（真付 $10）**。
    """
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": (billing_country or "US").upper(),
            "currency": (billing_currency or "USD").upper(),
        },
        "cancel_url": "https://chatgpt.com/#pricing",
        "promo_campaign": {
            "promo_campaign_id": promo_campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
    }
    try:
        # checkout API 同时要 Bearer 鉴权 + cookie（实测光带 cookie 返 401
        # "Access token is missing"）。复刻油猴 generatePlusHostedLink：先在浏览器内
        # fetch /api/auth/session 拿 accessToken，再带 Authorization Bearer + credentials
        # 调 checkout（cookie 是 promo 生效关键，Bearer 是 API 鉴权关键，缺一不可）。
        raw = page.evaluate(
            """async (args) => {
                try {
                    // 1. 同源读 session 拿 accessToken（带 cookie）
                    const sresp = await fetch(args.sessionUrl, {
                        credentials: 'include',
                        headers: { 'accept': 'application/json' },
                    });
                    const sjson = await sresp.json().catch(() => ({}));
                    const accessToken = (sjson && sjson.accessToken) || '';
                    if (!accessToken) {
                        return JSON.stringify({ __error: 'no_access_token_in_session' });
                    }
                    // 2. 带 Bearer + cookie 调 checkout
                    const r = await fetch(args.url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Authorization': 'Bearer ' + accessToken,
                            'Content-Type': 'application/json',
                            'Accept': 'application/json',
                            'OAI-Language': 'zh-CN',
                        },
                        body: JSON.stringify(args.payload),
                    });
                    const text = await r.text();
                    return JSON.stringify({ __status: r.status, body: text });
                } catch (e) {
                    return JSON.stringify({ __error: String(e) });
                }
            }""",
            {"url": CHECKOUT_API, "sessionUrl": "https://chatgpt.com/api/auth/session", "payload": payload},
        )
    except Exception as exc:
        logger.warning("浏览器内创建 checkout session 异常: %s", exc)
        return ""
    if not raw:
        return ""
    try:
        wrapper = json.loads(raw)
    except Exception:
        return ""
    if "__error" in wrapper:
        logger.warning("checkout fetch 出错: %s", wrapper.get("__error"))
        return ""
    status = wrapper.get("__status")
    if status != 200:
        logger.warning("checkout API 非 200（%s）: %s", status, str(wrapper.get("body"))[:200])
        return ""
    try:
        data = json.loads(str(wrapper.get("body") or "{}"))
    except Exception:
        logger.warning("checkout 响应非 JSON")
        return ""
    cs_id = str(data.get("checkout_session_id") or data.get("id") or "").strip()
    if not cs_id.startswith("cs_"):
        logger.warning("checkout 响应未拿到 cs_id，keys=%s", list(data.keys())[:20])
        return ""
    return cs_id


def _find_payment_frame(page: Page):
    """找 Stripe 账单字段所在的 elements-inner-payment iframe（账单字段在它内部）。"""
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        url = frame.url or ""
        if "elements-inner-payment" in url or "elements-inner" in url:
            # 确认这个 frame 里有 billing 字段（避免抓到 express-checkout 那个）
            try:
                if frame.locator('[autocomplete="billing name"], input[name="name"]').count() > 0:
                    return frame
            except Exception:
                continue
    return None


def _set_billing_country(page: Page, billing_country: str) -> bool:
    """把账单国家 select 设为目标国家（页面默认随 IP，需与 session 货币一致）。

    改国家会触发整个 payment iframe 重渲染，所以改完等稳定再继续。若页面已是目标
    国家则跳过（不触发重渲染）。
    """
    cc = (billing_country or "US").upper()
    frame = _find_payment_frame(page)
    if frame is None:
        logger.warning("未找到账单 iframe，跳过设置国家")
        return False
    try:
        cur = str(
            frame.locator('select[name="country"], select[autocomplete="billing country"]')
            .first.input_value(timeout=3000) or ""
        ).strip().upper()
    except Exception:
        cur = ""
    if cur == cc:
        logger.info("账单国家已是 %s，无需更改", cc)
        return True
    try:
        frame.locator('select[name="country"], select[autocomplete="billing country"]') \
            .first.select_option(cc, timeout=4000)
        logger.info("账单国家 %s → %s，等重渲染稳定...", cur or "<未知>", cc)
        time.sleep(3)  # 等 Stripe 重渲染支付方式 + 账单表单
        return True
    except Exception as exc:
        logger.warning("设置账单国家失败（%s），继续按页面默认填: %s", cc, exc)
        return False


def _fill_billing_address(page: Page, billing_country: str) -> bool:
    """填 Stripe 账单地址表单（在 elements-inner-payment iframe 内，PayPal 选中后展开）。

    字段（实测 dump）：name / country(select) / postalCode / administrativeArea(select) /
    locality / addressLine1 / addressLine2，均带 autocomplete="billing ..."。
    用美国 AVS 友好合成地址（pick_random_address），国家 select 改 US 与 billing 一致。
    """
    from src.fintech.billing_addresses import pick_random_address

    frame = _find_payment_frame(page)
    if frame is None:
        # iframe 没找到也试主文档（兜底）
        logger.warning("未找到账单字段 iframe，尝试主文档填账单")
        frame = page.main_frame

    def _fill(selectors: tuple[str, ...], value: str) -> bool:
        # Stripe PaymentElement 账单字段是 react 受控组件，校验只认 isTrusted=true
        # 真实键盘事件（与项目 submit_password/OTP 同源根因，见 handlers.py:478）。
        # .fill() 派发 isTrusted=false 合成事件 → Stripe 判账单无效 → 提交「支払いが
        # 承認されませんでした」。改 click 聚焦 + 清空 + press_sequentially 真实键盘。
        import random

        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=4000)
                # 清空（Meta+A 全选 + 删除，兼容已有值）
                try:
                    loc.press("Meta+a")
                    loc.press("Backspace")
                except Exception:
                    pass
                loc.press_sequentially(value, delay=random.randint(40, 110), timeout=8000)
                # 回读校验：空则降级 fill 兜底
                try:
                    if not str(loc.input_value(timeout=2000) or "").strip():
                        loc.fill(value, timeout=3000)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    def _select(selectors: tuple[str, ...], value: str) -> bool:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.select_option(value, timeout=4000)
                return True
            except Exception:
                continue
        return False

    # 读页面当前国家 select 值（页面默认随 IP/profile，如日本 JP）。
    # **绝不主动改国家**——改国家会触发整个 payment iframe 重渲染（PayPal 选中态 +
    # 账单表单都会临时消失/重置），导致填了也白填。按页面当前国家选地址，保持地理一致。
    page_cc = ""
    try:
        page_cc = str(
            frame.locator('select[name="country"], select[autocomplete="billing country"]')
            .first.input_value(timeout=3000) or ""
        ).strip().upper()
    except Exception:
        pass
    cc = page_cc or (billing_country or "US").upper()
    if cc not in ("US", "GB", "CA", "SG", "HK", "JP"):
        cc = "US"  # 地址池不支持的国家退回 US
    addr = pick_random_address(country=cc)
    full_name = "John Miller"  # 通用持卡人名（AVS 主要校验地址，名字弱相关）

    _fill(('input[name="name"]', 'input[autocomplete="billing name"]'), full_name)
    _fill(('input[name="addressLine1"]', 'input[autocomplete="billing address-line1"]'), addr.line1)
    _fill(('input[name="locality"]', 'input[autocomplete="billing address-level2"]'), addr.city)
    # 州/省：US 是 select（2 字母州码），JP 等也可能是 select；select 不中再试 input
    if addr.state:
        _select(
            ('select[name="administrativeArea"]', 'select[autocomplete="billing address-level1"]'),
            addr.state,
        ) or _fill(
            ('input[name="administrativeArea"]', 'input[autocomplete="billing address-level1"]'),
            addr.state,
        )
    if addr.zip_code:
        _fill(('input[name="postalCode"]', 'input[autocomplete="billing postal-code"]'), addr.zip_code)
    logger.info("已填账单地址（页面国家=%s, %s %s %s %s）", cc, addr.line1, addr.city, addr.state, addr.zip_code)
    human_delay(0.5, 1)
    return True


def _wait_checkout_ready(page: Page, *, timeout_sec: int = 45) -> bool:
    """轮询等 checkout 页渲染就绪（主文档/iframe 出现支付方式或订阅关键文本）。

    整页 window.location.href 跳转后，SPA 路由 + embedded Stripe.js 加载较慢，
    固定 sleep 会截到白屏。就绪标志：主文档或任一 iframe 文本含 PayPal/订阅/
    ChatGPT Plus，或出现 Stripe elements iframe。
    """
    ready_markers = ("paypal", "chatgpt plus", "订阅", "subscribe", "サブスクリプション", "银行卡", "支付方式", "payment method")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            # 主文档 body 文本
            body_text = ""
            try:
                body_text = (page.locator("body").inner_text(timeout=2000) or "").lower()
            except Exception:
                pass
            if any(m in body_text for m in ready_markers):
                logger.info("checkout 页就绪（主文档命中关键文本）")
                time.sleep(1.5)  # 再给 Stripe.js 一点渲染时间
                return True
            # 任一 iframe 文本（Stripe elements 常在 iframe 内）
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    ftext = (frame.locator("body").inner_text(timeout=1500) or "").lower()
                except Exception:
                    continue
                if any(m in ftext for m in ready_markers):
                    logger.info("checkout 页就绪（iframe 命中关键文本）")
                    time.sleep(1.5)
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _detect_submit_error(page: Page) -> str:
    """检测提交后的「出错了，请重试」类错误提示，返回错误文本（无则空串）。"""
    err_markers = ("出错了", "出错", "請重試", "请重试", "something went wrong", "try again", "エラー", "もう一度")
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        body = ""
    for m in err_markers:
        if m.lower() in body:
            return m
    return ""


def _select_paypal_method(page: Page) -> bool:
    """在 embedded checkout 页点选 PayPal 支付方式。

    PayPal 单选钮在 Stripe.js 渲染的 react 组件里，可能在 iframe 内。先在主文档找，
    再遍历 iframe 找（embedded checkout 常把支付方式渲染在 elements iframe 里）。
    """
    # 主文档直接找
    if _click_first_visible(page, _PAYPAL_RADIO_SELECTORS, description="PayPal 支付方式"):
        human_delay(1, 2)
        return True
    # 遍历 iframe（Stripe elements 常在 iframe 内渲染支付方式）
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        for sel in _PAYPAL_RADIO_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=4000)
                logger.info("已在 iframe 内点选 PayPal（selector=%s）", sel)
                human_delay(1, 2)
                return True
            except Exception:
                continue
    logger.warning("未能选中 PayPal 支付方式（主文档 + iframe 均未命中）")
    return False


def _wait_paypal_redirect(page: Page, *, timeout_sec: int = 30) -> bool:
    """轮询页面 URL，等整页跳到 paypal.com（提交后 Stripe.js 现场换 PayPal authorize URL）。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        if "paypal.com" in cur:
            return True
        time.sleep(1)
    # 末次检查所有 frame（有时 PayPal 在子 frame）
    try:
        for frame in page.frames:
            if "paypal.com" in (frame.url or "").lower():
                return True
    except Exception:
        pass
    return False


def _shot(page: Page, artifacts_dir: str, name: str, cs_id: str) -> str:
    """存证截图，返回路径（失败返回空串，不阻塞主流程）。"""
    import os

    try:
        out_dir = os.path.join(artifacts_dir, "paypal_subscribe")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{name}_{cs_id[:16]}.png")
        page.screenshot(path=path, full_page=True)
        logger.info("存证截图: %s", path)
        return path
    except Exception as exc:
        logger.debug("截图失败（可忽略）: %s", exc)
        return ""


def _result(
    *,
    success: bool = False,
    plan: str = "",
    is_plus: bool = False,
    stage: str = "",
    checkout_session_id: str = "",
    reached_paypal: bool = False,
    logged_in: bool = False,
    relogged_in: bool = False,
    message: str = "",
    detail: str = "",
    screenshots: Optional[list[str]] = None,
) -> dict[str, Any]:
    """统一结果结构（与 verify_plus._result 风格对齐）。"""
    return {
        "success": success,
        "plan": plan,
        "is_plus": is_plus,
        "stage": stage,
        "checkout_session_id": checkout_session_id,
        "reached_paypal": reached_paypal,
        "logged_in": logged_in,
        "relogged_in": relogged_in,
        "message": message,
        "detail": detail,
        "screenshots": [s for s in (screenshots or []) if s],
    }
