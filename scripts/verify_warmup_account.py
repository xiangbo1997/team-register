# -*- coding: utf-8 -*-
"""手动验证 mail_accounts 中 role=pro_warmup 账号的可用性（不参与 pytest 自动收集）。

用法：
    python scripts/verify_warmup_account.py                     # 验证池里第一个未禁用的账号
    python scripts/verify_warmup_account.py --account-id <id>   # 指定账号 ID
    python scripts/verify_warmup_account.py --email <email>     # 指定账号邮箱

零卡消耗：仅做 ① AdsPower 起浏览器 ② 调 pro_account_login 登 ChatGPT
③ 提取 access_token ④ 调 /backend-api/me 探账号 plan，绝不调用 PaymentLinkGenerator、
不打开 Stripe 表单。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

# 允许 ``python scripts/verify_warmup_account.py`` 直接运行（项目根加入 sys.path）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from sqlmodel import select

from src.automation.runtime import extract_session_tokens_with_http
from src.browser import get_browser_ws
from src.config import load_config
from src.db.engine import get_session
from src.db.models import MailAccount
from src.orchestration.handlers import (
    is_chatgpt_logged_in,
    pro_account_login,
    prepare_clean_warmup_page,
    navigate_to_pro_checkout,
    select_pro_tier,
    wait_for_stripe_form,
    WarmupUpgradeNotFound,
)
from src.utils import setup_logger

logger = setup_logger(name="VerifyWarmup")

ME_URL = "https://chatgpt.com/backend-api/me"
ACCOUNT_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"


def _pick_account(*, account_id: Optional[str], email: Optional[str]) -> Optional[MailAccount]:
    """从 mail_accounts 表挑一个 pro_warmup 账号。

    优先级：account_id > email > 第一个 is_active=True。
    """
    with get_session() as session:
        stmt = select(MailAccount).where(MailAccount.role == "pro_warmup")
        if account_id:
            stmt = stmt.where(MailAccount.id == account_id)
        elif email:
            stmt = stmt.where(MailAccount.email == email)
        else:
            stmt = stmt.where(MailAccount.is_active == True)  # noqa: E712
        candidates = list(session.exec(stmt).all())
        if not candidates:
            return None
        # SQLModel 会在 session close 后让 instance 失效，先 expunge
        for cand in candidates:
            session.expunge(cand)
        return candidates[0]


def _query_account_plan(access_token: str, proxy_url: str = "") -> dict[str, Any]:
    """调 OpenAI /backend-api/me 和 accounts/check 接口探账号 plan。

    Returns:
        {"me": {...}, "accounts": {...}, "plan_summary": "free|plus|team|enterprise|unknown"}
    """
    import requests

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    result: dict[str, Any] = {"me": None, "accounts": None, "plan_summary": "unknown"}

    try:
        resp = requests.get(ME_URL, headers=headers, proxies=proxies, timeout=15)
        if resp.status_code == 200:
            result["me"] = resp.json()
        else:
            result["me"] = {"http_status": resp.status_code, "body": resp.text[:300]}
    except Exception as exc:
        result["me"] = {"error": str(exc)}

    try:
        resp = requests.get(ACCOUNT_URL, headers=headers, proxies=proxies, timeout=15)
        if resp.status_code == 200:
            result["accounts"] = resp.json()
            # 解析 plan
            accounts_data = result["accounts"].get("accounts", {})
            plan_codes: list[str] = []
            for acc in accounts_data.values():
                features = acc.get("entitlement", {}) or {}
                plan_code = features.get("subscription_plan") or ""
                if plan_code:
                    plan_codes.append(str(plan_code))
            joined = ",".join(plan_codes)
            low = joined.lower()
            if "team" in low:
                result["plan_summary"] = "team"
            elif "plus" in low:
                result["plan_summary"] = "plus"
            elif "enterprise" in low:
                result["plan_summary"] = "enterprise"
            elif plan_codes:
                result["plan_summary"] = f"other({joined})"
            else:
                result["plan_summary"] = "free"
        else:
            result["accounts"] = {"http_status": resp.status_code, "body": resp.text[:300]}
    except Exception as exc:
        result["accounts"] = {"error": str(exc)}

    return result


def _redact_email(email: str) -> str:
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 pro_warmup 账号可用性")
    parser.add_argument("--account-id", default=None, help="指定 mail_accounts.id")
    parser.add_argument("--email", default=None, help="指定账号邮箱")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="登录完成后保留浏览器（手动观察用），默认登录完毕直接关闭",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="跳过 prepare_clean_warmup_page + pro_account_login，直接 connect_over_cdp 复用"
             "AdsPower profile 已有的登录态。用于 Phase 2 调试，0 卡 / 0 风控配额消耗。"
             "前提：profile 已经登录过 ChatGPT 且 cookies 没过期。",
    )
    parser.add_argument(
        "--explore-upgrade",
        choices=["100", "200"],
        default=None,
        help="仅 --skip-login 模式生效：调 navigate_to_pro_checkout + select_pro_tier(amount_usd) "
             "验证 Phase 2 工具能否到达 Configure plan 页 + 切档位 + Stripe iframe 出现。"
             "**不点 Subscribe**，0 卡消耗。",
    )
    parser.add_argument(
        "--skip-navigate",
        action="store_true",
        help="跟 --explore-upgrade 配合：跳过 navigate_to_pro_checkout（假设浏览器已在 "
             "Configure your plan 页），直接 select_pro_tier。用户手动点入口后用此模式调试。",
    )
    parser.add_argument(
        "--explore-fill",
        action="store_true",
        help="跟 --explore-upgrade 配合：探完 Stripe iframe 后用 X988 缓存的真卡数据"
             "尝试 fill_checkout_card（**不会**点 Subscribe，仍 0 卡消耗）。"
             "失败时 dump Stripe iframe 里所有 input 元素，定位 selector 漂移。",
    )
    parser.add_argument(
        "--card-key",
        default="",
        help="跟 --explore-fill 配合：从 card_activations 缓存读取真卡数据。"
             "默认会自动挑一张 valid 的 X988 缓存卡。",
    )
    args = parser.parse_args()

    load_dotenv()

    # 复用 main.py 的运行时构造逻辑：load_config + _build_runtime_clients 拿 mail_api，
    # 不重新写一遍 MailManager 初始化。这样验证脚本和主流程走同一份配置注入。
    try:
        config = load_config()
    except Exception as exc:
        logger.warning("load_config 失败（部分必填字段未设？仅取 ads/proxy 字段）: %s", exc)
        config = None

    from main import _build_runtime_clients  # 延迟 import 避免顶层副作用
    mail_api = None
    if config is not None:
        try:
            _, _, mail_api = _build_runtime_clients(config)
            logger.info("MailManager 已构造（base=%s provider=%s）", config.email_provider_base_url, config.email_provider_name)
        except Exception as exc:
            logger.warning("_build_runtime_clients 失败: %s", exc)

    ads_api = os.getenv("ADS_API", "http://local.adspower.net:50325")
    ads_api_key = os.getenv("ADS_API_KEY", "")
    proxy_url = os.getenv("PROXY", "")

    account = _pick_account(account_id=args.account_id, email=args.email)
    if account is None:
        logger.error("未找到匹配的 pro_warmup 账号")
        return 2

    extra = dict(account.extra or {})
    profile_id = str(extra.get("adspower_profile_id") or "").strip()
    password = str(extra.get("password") or "").strip()
    email = str(account.email or "").strip()

    logger.info("=" * 70)
    logger.info("验证目标 pro_warmup 账号")
    logger.info("  id              : %s", account.id)
    logger.info("  email           : %s", _redact_email(email))
    logger.info("  profile_id      : %s", profile_id or "<empty>")
    logger.info("  is_active       : %s", account.is_active)
    logger.info("  consecutive_fail: %s", account.consecutive_failures)
    logger.info("  last_failure    : %s", account.last_failure_reason or "<none>")
    logger.info("  cooldown_until  : %s", account.cooldown_until or "<none>")
    logger.info("=" * 70)

    if not (email and password and profile_id):
        logger.error(
            "账号字段不完整：email=%s password=%s profile_id=%s",
            bool(email), bool(password), bool(profile_id),
        )
        return 3

    # ── 第 1 步：起 AdsPower ──
    logger.info("[1/4] 启动 AdsPower 浏览器（profile=%s）...", profile_id)
    try:
        ws_url = get_browser_ws(ads_api=ads_api, user_id=profile_id, api_key=ads_api_key)
    except Exception as exc:
        logger.error("AdsPower 启动失败: %s", exc)
        return 4
    logger.info("AdsPower 启动成功，CDP=%s...", ws_url[:60])

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(ws_url)
        except Exception as exc:
            logger.error("CDP 连接失败: %s", exc)
            return 5

        contexts = browser.contexts
        if not contexts:
            logger.error("AdsPower 未提供浏览器上下文")
            return 5
        context = contexts[0]

        if args.skip_login:
            # --skip-login 模式：复用 AdsPower profile 已有的登录态
            # 不清 cookies、不调 pro_account_login。直接拿现有 page 或新开一个空白 page。
            existing_pages = [p for p in context.pages if "chatgpt.com" in (p.url or "")]
            if existing_pages:
                page = existing_pages[0]
                page.bring_to_front()
                logger.info("[skip-login] 复用现有 ChatGPT tab: %s", page.url)
            else:
                page = context.new_page()
                page.set_default_timeout(60000)
                page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)
                logger.info("[skip-login] 已 goto chatgpt.com，当前 URL=%s", page.url)
            # 验证登录态：复用 src/orchestration/handlers.py:is_chatgpt_logged_in，
            # 与 execute_card_warmup 的快路径预检共用同一份判定逻辑（避免漂移）。
            cur_url = str(page.url or "")
            if not is_chatgpt_logged_in(page):
                logger.error(
                    "[skip-login] 登录态已失效，URL=%s。"
                    "请去掉 --skip-login 重新完整登录。",
                    cur_url,
                )
                if not args.keep_open:
                    try:
                        page.close()
                    except Exception:
                        pass
                return 11
            logger.info(
                "[skip-login] 登录态可用，URL=%s。Phase 2/3 调试可继续。",
                cur_url,
            )

            # --explore-upgrade：调 Phase 2 工具验证能否到 Configure plan 页 + 切档位
            if args.explore_upgrade:
                amount_usd = int(args.explore_upgrade)
                logger.info("[explore-upgrade] 开始验证 Phase 2 工具：amount_usd=$%d", amount_usd)

                # Step A: navigate（除非 --skip-navigate 跳过）
                if args.skip_navigate:
                    logger.info("[explore-upgrade] --skip-navigate 跳过 navigate_to_pro_checkout")
                    logger.info("[explore-upgrade] 当前 URL=%s", page.url)
                    if "/checkout/" not in str(page.url or ""):
                        logger.warning("[explore-upgrade] 当前 URL 不含 /checkout/，select_pro_tier 可能失败")
                else:
                    logger.info("[explore-upgrade] 等待 SPA 渲染 5s ...")
                    page.wait_for_timeout(5000)
                    try:
                        navigate_to_pro_checkout(page, timeout_sec=30)
                        logger.info("[explore-upgrade] ✅ navigate_to_pro_checkout 成功 url=%s", page.url)
                    except WarmupUpgradeNotFound as exc:
                        logger.error("[explore-upgrade] ❌ navigate_to_pro_checkout 失败: %s", exc)
                        try:
                            dump_dir = Path("artifacts/warmup-debug")
                            dump_dir.mkdir(parents=True, exist_ok=True)
                            ts = __import__("datetime").datetime.now().strftime("%Y%m%dT%H%M%S")
                            page.screenshot(path=str(dump_dir / f"navigate_fail_{ts}.png"), full_page=True, timeout=8000)
                            (dump_dir / f"navigate_fail_{ts}.html").write_text(page.content() or "", encoding="utf-8")
                            # 重点 dump：所有带 select-plan / upgrade / pro / plus 关键词的 testid + 当前可见的所有 button
                            elements = page.evaluate("""() => {
                                const items = Array.from(document.querySelectorAll(
                                    'button, [role="radio"], [role="button"], [role="tab"], [data-testid]'
                                ));
                                return items.slice(0, 80).map(el => ({
                                    tag: el.tagName,
                                    role: el.getAttribute('role') || '',
                                    id: el.id || '',
                                    testid: el.getAttribute('data-testid') || '',
                                    aria_label: el.getAttribute('aria-label') || '',
                                    aria_checked: el.getAttribute('aria-checked') || '',
                                    aria_selected: el.getAttribute('aria-selected') || '',
                                    text: (el.innerText || '').trim().slice(0, 100),
                                    visible: el.offsetParent !== null,
                                })).filter(b => b.text || b.testid || b.id || b.aria_label);
                            }""")
                            logger.error("[explore-upgrade] 当前页前 80 个可点击元素 (含 visible/aria_checked/aria_selected):")
                            for b in elements or []:
                                logger.error(
                                    "  %s role='%s' id='%s' testid='%s' aria='%s' checked='%s' selected='%s' visible=%s text='%s'",
                                    b.get("tag"), b.get("role"), b.get("id"),
                                    b.get("testid"), b.get("aria_label"),
                                    b.get("aria_checked"), b.get("aria_selected"),
                                    b.get("visible"), b.get("text"),
                                )
                            logger.error("[explore-upgrade] dump 文件：%s/navigate_fail_%s.{png,html}", dump_dir, ts)
                        except Exception as dump_exc:
                            logger.warning("[explore-upgrade] dump 失败: %s", dump_exc)
                        return 12

                # Step B: select_pro_tier
                # 如果 navigate 已经把 URL 推到 cs_live Stripe checkout 页（说明账号有 hosted
                # checkout 直跳），button#chatgptpro 这种 plan 页档位按钮就不存在了 — 直接跳过 select。
                cur_url = str(page.url or "")
                if "cs_live" in cur_url or "checkout/openai_llc/cs_" in cur_url:
                    logger.info(
                        "[explore-upgrade] 当前已在 Stripe hosted checkout 页 (%s)，跳过 select_pro_tier",
                        cur_url[:80],
                    )
                else:
                    try:
                        select_pro_tier(page, amount_usd=amount_usd)
                        logger.info("[explore-upgrade] ✅ select_pro_tier($%d) 成功", amount_usd)
                    except WarmupUpgradeNotFound as exc:
                        logger.error("[explore-upgrade] ❌ select_pro_tier 失败: %s", exc)
                        # dump plan 弹窗里所有可点击元素，便于排障 ChatGPT UI 漂移
                        try:
                            dump_dir = Path("artifacts/warmup-debug")
                            dump_dir.mkdir(parents=True, exist_ok=True)
                            ts = __import__("datetime").datetime.now().strftime("%Y%m%dT%H%M%S")
                            page.screenshot(
                                path=str(dump_dir / f"select_tier_fail_{ts}.png"),
                                full_page=True, timeout=8000,
                            )
                            (dump_dir / f"select_tier_fail_{ts}.html").write_text(
                                page.content() or "", encoding="utf-8",
                            )
                            elements = page.evaluate("""() => {
                                const items = Array.from(document.querySelectorAll(
                                    'button, [role="radio"], [role="button"], [data-testid]'
                                ));
                                return items.slice(0, 60).map(el => ({
                                    tag: el.tagName,
                                    role: el.getAttribute('role') || '',
                                    id: el.id || '',
                                    testid: el.getAttribute('data-testid') || '',
                                    aria_label: el.getAttribute('aria-label') || '',
                                    aria_checked: el.getAttribute('aria-checked') || '',
                                    text: (el.innerText || '').trim().slice(0, 80),
                                    visible: el.offsetParent !== null,
                                })).filter(b => b.text || b.testid || b.id || b.aria_label);
                            }""")
                            logger.error("[explore-upgrade] plan 弹窗里前 60 个可点击元素：")
                            for el in elements or []:
                                logger.error(
                                    "  %s role='%s' id='%s' testid='%s' aria='%s' checked='%s' visible=%s text='%s'",
                                    el.get("tag"), el.get("role"), el.get("id"),
                                    el.get("testid"), el.get("aria_label"),
                                    el.get("aria_checked"), el.get("visible"),
                                    el.get("text"),
                                )
                            logger.error("[explore-upgrade] dump 文件：%s/select_tier_fail_%s.{png,html}",
                                         dump_dir, ts)
                        except Exception as dump_exc:
                            logger.warning("[explore-upgrade] dump 失败: %s", dump_exc)
                        return 13
                    except ValueError as exc:
                        logger.error("[explore-upgrade] ❌ select_pro_tier 参数错误: %s", exc)
                        return 13

                # Step C: 等 Stripe iframe
                try:
                    if wait_for_stripe_form(page, timeout_sec=20):
                        logger.info("[explore-upgrade] ✅ Stripe iframe 已出现，Phase 2 完整验证通过")
                    else:
                        logger.warning("[explore-upgrade] ⚠️ Stripe iframe 未在 20s 内出现")
                except Exception as exc:
                    logger.warning("[explore-upgrade] wait_for_stripe_form 异常: %s", exc)

                # Step D: 可选 — 用 X988 缓存的真卡数据尝试填卡（不点 Subscribe）
                if args.explore_fill:
                    logger.info("[explore-fill] 开始测试 fill_checkout_card 用真卡数据（不点 Subscribe，0 卡消耗）")
                    from src.orchestration.handlers import fill_checkout_card
                    from src.services.card_activation_service import (
                        get_activation, list_activations,
                    )

                    # 选卡：优先用 --card-key 指定的；否则挑第一张 valid X988 缓存卡
                    if args.card_key:
                        card_rec = get_activation(args.card_key)
                        if card_rec is None:
                            logger.error("[explore-fill] --card-key %s 在 card_activations 缓存中找不到", args.card_key)
                            return 14
                    else:
                        valid_cards = [
                            c for c in list_activations(card_provider="x988card")
                            if not c.is_invalidated and c.card_number
                        ]
                        if not valid_cards:
                            logger.error("[explore-fill] 缓存里没有 valid 的 X988 卡，请用 --card-key 指定")
                            return 14
                        card_rec = valid_cards[0]
                        logger.info("[explore-fill] 自动选用缓存卡 cdk=%s last4=%s",
                                    card_rec.card_key[:8], card_rec.card_number[-4:])

                    expiry = f"{card_rec.expiry_month}/{str(card_rec.expiry_year)[-2:]}"
                    logger.info("[explore-fill] 调 fill_checkout_card(card=%s***%s, exp=%s)",
                                card_rec.card_number[:4], card_rec.card_number[-4:], expiry)
                    # Stripe React payment element 在 iframe element 出现后还需要数秒加载内部
                    # input。给 8s buffer 让 Stripe iframe 真正可交互。
                    logger.info("[explore-fill] 等 8s 让 Stripe React 渲染内部 input ...")
                    page.wait_for_timeout(8000)
                    variant = fill_checkout_card(
                        page,
                        card_rec.card_number,
                        expiry,
                        str(card_rec.cvv or ""),
                    )
                    if variant:
                        logger.info("[explore-fill] ✅ fill_checkout_card 成功（variant=%s），page 停在填好卡未提交状态", variant)
                    else:
                        logger.error("[explore-fill] ❌ fill_checkout_card 返回空 — variant 都没匹配")
                        # 用 Playwright frame_locator 精准查 Stripe unified iframe 内的 input
                        try:
                            stripe_fl = page.frame_locator(
                                'iframe[title="Secure payment input frame"]'
                            ).first
                            # 列出该 frame 内所有 input 的属性（Playwright 跨域 frame_locator 应该能 work）
                            input_count = stripe_fl.locator("input").count()
                            logger.error("[explore-fill] Stripe iframe 内 input 数量：%d", input_count)
                            for idx in range(min(input_count, 20)):
                                inp = stripe_fl.locator("input").nth(idx)
                                try:
                                    name = inp.get_attribute("name", timeout=1000) or ""
                                    placeholder = inp.get_attribute("placeholder", timeout=1000) or ""
                                    aria = inp.get_attribute("aria-label", timeout=1000) or ""
                                    autocomplete = inp.get_attribute("autocomplete", timeout=1000) or ""
                                    visible = inp.is_visible(timeout=1000)
                                    logger.error(
                                        "  input[%d] name='%s' placeholder='%s' aria='%s' autocomplete='%s' visible=%s",
                                        idx, name, placeholder, aria, autocomplete, visible,
                                    )
                                except Exception as inp_exc:
                                    logger.warning("  input[%d] 属性读取失败: %s", idx, inp_exc)
                        except Exception as fl_exc:
                            logger.warning("[explore-fill] frame_locator 探查失败: %s", fl_exc)
                        # dump Stripe iframe 内 input 元素
                        try:
                            dump_dir = Path("artifacts/warmup-debug")
                            dump_dir.mkdir(parents=True, exist_ok=True)
                            ts = __import__("datetime").datetime.now().strftime("%Y%m%dT%H%M%S")
                            page.screenshot(
                                path=str(dump_dir / f"fill_fail_{ts}.png"),
                                full_page=True, timeout=8000,
                            )
                            (dump_dir / f"fill_fail_{ts}.html").write_text(
                                page.content() or "", encoding="utf-8",
                            )
                            # dump 所有 iframes 元数据
                            iframes = page.evaluate("""() => {
                                return Array.from(document.querySelectorAll('iframe')).map(f => ({
                                    name: f.name || '',
                                    title: f.title || '',
                                    src: (f.src || '').slice(0, 80),
                                    visible: f.offsetParent !== null,
                                }));
                            }""")
                            logger.error("[explore-fill] 当前页所有 iframe:")
                            for f in iframes or []:
                                logger.error("  name='%s' title='%s' visible=%s src=%s",
                                             f.get("name"), f.get("title"), f.get("visible"), f.get("src"))

                            # 进入第一个看起来像 Stripe 的 iframe，dump 其内 input
                            stripe_iframe_handle = None
                            for frame in page.frames:
                                fname = frame.name or ""
                                furl = frame.url or ""
                                if "stripe" in fname.lower() or "stripe" in furl.lower() or "__privateStripeFrame" in fname:
                                    stripe_iframe_handle = frame
                                    logger.error("[explore-fill] 找到 Stripe-like frame name='%s' url='%s'",
                                                 fname, furl[:80])
                                    try:
                                        inputs = frame.evaluate("""() => {
                                            return Array.from(document.querySelectorAll('input')).map(i => ({
                                                name: i.name || '',
                                                id: i.id || '',
                                                type: i.type || '',
                                                placeholder: i.placeholder || '',
                                                aria_label: i.getAttribute('aria-label') || '',
                                                autocomplete: i.autocomplete || '',
                                                visible: i.offsetParent !== null,
                                            }));
                                        }""")
                                        logger.error("[explore-fill] 该 frame 内 input 元素:")
                                        for inp in inputs or []:
                                            logger.error("  name='%s' id='%s' type='%s' placeholder='%s' aria='%s' autocomplete='%s' visible=%s",
                                                         inp.get("name"), inp.get("id"), inp.get("type"),
                                                         inp.get("placeholder"), inp.get("aria_label"),
                                                         inp.get("autocomplete"), inp.get("visible"))
                                    except Exception as fexc:
                                        logger.warning("[explore-fill] 无法 evaluate frame inputs: %s", fexc)
                            if stripe_iframe_handle is None:
                                logger.error("[explore-fill] 未找到任何 Stripe-like frame")
                            logger.error("[explore-fill] dump 文件：%s/fill_fail_%s.{png,html}",
                                         dump_dir, ts)
                        except Exception as dump_exc:
                            logger.warning("[explore-fill] dump 失败: %s", dump_exc)
                        return 14

                logger.info("[explore-upgrade] 已停在 Configure your plan 页（不会 click Subscribe，0 卡消耗）")

            # skip-login 模式下不走后续 cookie/token 提取，直接保留浏览器供调试
            if not args.keep_open:
                logger.info("[skip-login] 不带 --keep-open 也保持 page 不 close，方便后续 attach 调试")
            return 0

        # 与生产 warmup 路径保持一致：调用前彻底清 cookies / localStorage / SW，
        # 避免 profile 历史失败痕迹阻塞密码框出现。
        page = prepare_clean_warmup_page(context)

        # ── 第 2 步：调 pro_account_login ──
        logger.info("[2/4] 调用 pro_account_login(%s) ...", _redact_email(email))
        try:
            login_ok = pro_account_login(page, email, password, mail_api=mail_api, timeout_sec=120)
        except Exception as exc:
            logger.error("pro_account_login 抛异常: %s", exc)
            login_ok = False
        if not login_ok:
            cur_url = getattr(page, "url", "<unknown>")
            logger.error("登录失败，URL=%s", cur_url)
            # 失败时落盘 screenshot + body 摘要，便于离线诊断（不 keep_open 也能看）
            try:
                dump_dir = Path("artifacts/warmup-debug")
                dump_dir.mkdir(parents=True, exist_ok=True)
                ts = __import__("datetime").datetime.now().strftime("%Y%m%dT%H%M%S")
                shot_path = dump_dir / f"login_fail_{ts}.png"
                html_path = dump_dir / f"login_fail_{ts}.html"
                try:
                    page.screenshot(path=str(shot_path), full_page=True, timeout=8000)
                    logger.info("已落盘失败截图: %s", shot_path)
                except Exception as exc:
                    logger.warning("截图失败: %s", exc)
                try:
                    body_text = page.content()
                    html_path.write_text(body_text or "", encoding="utf-8")
                    logger.info("已落盘失败页面 HTML: %s (%d bytes)", html_path, len(body_text or ""))
                except Exception as exc:
                    logger.warning("HTML dump 失败: %s", exc)
                # 提取页面可见文本前 800 字，直接打到日志
                try:
                    visible_text = page.evaluate(
                        "() => (document.body && document.body.innerText) || ''"
                    )
                    snippet = (visible_text or "").strip().replace("\n", " | ")[:800]
                    logger.error("页面可见文本（前 800 字）: %s", snippet)
                except Exception as exc:
                    logger.warning("提取可见文本失败: %s", exc)
            except Exception as exc:
                logger.warning("dump 整体异常: %s", exc)
            if not args.keep_open:
                try:
                    page.close()
                except Exception:
                    pass
            return 6
        logger.info("登录成功，最终 URL=%s", getattr(page, "url", ""))

        # ── 第 3 步：提取 access_token ──
        logger.info("[3/4] 提取 cookies 和 access_token ...")
        try:
            cookies = context.cookies()
            user_agent = page.evaluate("() => navigator.userAgent") or ""
        except Exception as exc:
            logger.error("提取 cookies 失败: %s", exc)
            return 7

        access_token, refresh_token = extract_session_tokens_with_http(
            cookies=cookies,
            user_agent=str(user_agent),
            proxy_url=proxy_url,
        )
        if not access_token:
            logger.error("未提取到 access_token（cookie 可能被风控屏蔽）")
            return 8
        logger.info(
            "access_token 提取成功，长度=%d 前缀=%s...",
            len(access_token), access_token[:24],
        )
        if refresh_token:
            logger.info("refresh_token 也已捕获（长度=%d）", len(refresh_token))

        # ── 第 4 步：探账号 plan ──
        logger.info("[4/4] 调 /backend-api/me + accounts/check 探账号身份 ...")
        plan_info = _query_account_plan(access_token, proxy_url=proxy_url)
        plan = plan_info.get("plan_summary", "unknown")
        logger.info("=" * 70)
        logger.info("✅ 账号验证完毕")
        logger.info("  plan_summary    : %s", plan)
        me = plan_info.get("me") or {}
        if isinstance(me, dict):
            logger.info("  /me name        : %s", me.get("name") or "<n/a>")
            logger.info("  /me email match : %s", str(me.get("email") or "").lower() == email.lower())
        logger.info("=" * 70)
        # 完整 JSON 打到 debug
        logger.debug("完整 plan_info: %s", json.dumps(plan_info, ensure_ascii=False, default=str)[:1500])

        if not args.keep_open:
            try:
                page.close()
            except Exception:
                pass

        # 退出码：0=完整可用 + 是 plus/team；10=登录 OK 但是 free/unknown
        if plan in {"plus", "team", "enterprise"}:
            return 0
        return 10


if __name__ == "__main__":
    sys.exit(main())
