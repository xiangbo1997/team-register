# -*- coding: utf-8 -*-
"""
用新 checkout session（app 模式）重新绑卡。

1. 生成全新 chatgpt.com/checkout/ 站内链接
2. 关闭旧 pay.openai.com 页面
3. 打开新链接，填写卡片，提交
"""

import os
import random
import time

from playwright.sync_api import sync_playwright

from src.config import load_config
from src.browser import get_browser_ws
from src.efuncard import EfunCard
from src.payment_link import PaymentLinkGenerator
from src.utils import setup_logger, human_delay

logger = setup_logger()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
_EMAIL = "rmqvhq39342p@outlook.com"
_BILLING = {
    "country": "ES",
    "line1": "1 Calle San Pablo",
    "city": "Alicante",
    "postal_code": "03012",
}


def slow_type(locator, text: str) -> None:
    """真人节奏输入"""
    locator.click()
    human_delay(0.3, 0.6)
    locator.press("Meta+a")
    human_delay(0.1, 0.2)
    locator.press("Backspace")
    human_delay(0.3, 0.5)
    for ch in text:
        locator.press(ch)
        time.sleep(random.uniform(0.06, 0.2))
    human_delay(0.3, 0.5)


def main():
    if not ACCESS_TOKEN:
        logger.error("请设置 ACCESS_TOKEN 环境变量。")
        return

    config = load_config()
    card_api = EfunCard(token=config.efuncard_token)

    # 1. 获取虚拟卡
    cdk = config.task_cdk
    card = card_api.get_card(cdk)
    if not card:
        logger.error("未能获取虚拟卡。")
        return
    logger.info("卡: %s****%s  有效期: %s", card.card_number[:4], card.card_number[-4:], card.expiry_display)

    # 2. 生成新 checkout session — app 模式
    logger.info("生成新 checkout session (app 模式)...")
    success, checkout_link = PaymentLinkGenerator.generate_short_link(
        ACCESS_TOKEN,
        plan_type=config.payment_plan,
        proxy=config.proxy or None,
        aimizy_country=config.aimizy_country,
        aimizy_currency=config.aimizy_currency,
    )
    if not success:
        logger.error("链接生成失败: %s", checkout_link)
        return

    logger.info("新 checkout 链接: %s", checkout_link)

    # 3. 连接浏览器
    logger.info("连接 AdsPower...")
    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        # 关闭旧的 pay.openai.com 页面
        for pg in list(context.pages):
            if "pay.openai.com" in (pg.url or ""):
                logger.info("关闭旧支付页: %s", pg.url[:60])
                pg.close()

        # 4. 打开新链接
        checkout_page = context.new_page()
        checkout_page.set_default_timeout(60000)
        logger.info("打开新支付链接...")
        checkout_page.goto(checkout_link, wait_until="domcontentloaded", timeout=60000)
        human_delay(5, 8)

        logger.info("当前 URL: %s", checkout_page.url)

        # 5. 等待 Stripe 表单 — 站内 checkout 可能是 iframe 或直接 input
        logger.info("等待支付表单加载...")
        stripe_target = None  # 最终要填写的目标（page 或 frame_locator）
        is_iframe = False

        for _ in range(40):
            # 检查直接 input
            for sel in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]'):
                try:
                    if checkout_page.locator(sel).first.is_visible(timeout=500):
                        stripe_target = checkout_page
                        logger.info("找到直接 input 表单。")
                        break
                except Exception:
                    continue
            if stripe_target:
                break

            # 检查 iframe
            for iframe_sel in (
                'iframe[title="Secure payment input frame"]',
                'iframe[title*="payment" i]',
                'iframe[name*="__privateStripeFrame"]',
            ):
                try:
                    if checkout_page.locator(iframe_sel).first.is_visible(timeout=500):
                        stripe_target = checkout_page.frame_locator(iframe_sel).first
                        is_iframe = True
                        logger.info("找到 Stripe iframe: %s", iframe_sel)
                        break
                except Exception:
                    continue
            if stripe_target:
                break

            human_delay(1, 1.5)

        if not stripe_target:
            logger.error("支付表单加载超时。")
            # 诊断页面结构
            try:
                frames = checkout_page.frames
                logger.info("frames 数量: %d", len(frames))
                for i, f in enumerate(frames):
                    logger.info("  frame[%d] name=%s url=%s", i, f.name, (f.url or "")[:80])
                    for inp in f.query_selector_all("input")[:8]:
                        logger.info("    input name=%s type=%s placeholder=%s",
                                    inp.get_attribute("name"), inp.get_attribute("type"),
                                    inp.get_attribute("placeholder"))
            except Exception as exc:
                logger.warning("诊断失败: %s", exc)
            human_delay(30, 30)
            return

        # 6. 填写卡片信息
        logger.info("填写卡片信息...")
        human_delay(1, 2)

        # 卡号
        for card_sel in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]'):
            try:
                loc = stripe_target.locator(card_sel).first
                if loc.is_visible(timeout=2000):
                    slow_type(loc, card.card_number)
                    logger.info("卡号已填写。")
                    break
            except Exception:
                continue

        human_delay(0.5, 1)

        # 有效期
        for exp_sel in ('input[name="cardExpiry"]', 'input[name="exp-date"]', 'input[name="expiry"]'):
            try:
                loc = stripe_target.locator(exp_sel).first
                if loc.is_visible(timeout=2000):
                    slow_type(loc, card.expiry_display)
                    logger.info("有效期已填写。")
                    break
            except Exception:
                continue

        human_delay(0.5, 1)

        # CVC
        for cvc_sel in ('input[name="cardCvc"]', 'input[name="cvc"]'):
            try:
                loc = stripe_target.locator(cvc_sel).first
                if loc.is_visible(timeout=2000):
                    slow_type(loc, card.cvv)
                    logger.info("CVC 已填写。")
                    break
            except Exception:
                continue

        human_delay(0.5, 1)

        # 7. 持卡人姓名（在页面上，不在 iframe 里）
        try:
            name_loc = checkout_page.locator('input[name="billingName"]').first
            if name_loc.is_visible(timeout=3000):
                name_loc.click()
                name_loc.fill(card.name_on_card or "OpenAI User")
                logger.info("持卡人: %s", card.name_on_card)
        except Exception:
            pass

        human_delay(0.5, 1)

        # 8. 邮箱
        try:
            email_loc = checkout_page.locator('input[name="email"], input#email').first
            if email_loc.is_visible(timeout=2000):
                email_loc.click()
                email_loc.fill(_EMAIL)
                logger.info("邮箱: %s", _EMAIL)
        except Exception:
            pass

        human_delay(0.5, 1)

        # 9. 账单地址
        logger.info("填写账单地址...")
        # 手动输入地址
        for manual_sel in ('button:has-text("手动输入地址")', 'a:has-text("手动输入地址")',
                           'button:has-text("Enter address manually")', 'a:has-text("Enter address manually")'):
            try:
                btn = checkout_page.locator(manual_sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    human_delay(0.5, 1)
                    break
            except Exception:
                continue

        # 国家
        try:
            country_loc = checkout_page.locator('select[name="billingCountry"]').first
            if country_loc.is_visible(timeout=1500):
                country_loc.select_option(value=_BILLING["country"])
        except Exception:
            pass

        human_delay(0.3, 0.5)

        for field, value in (
            ('input[name="billingAddressLine1"]', _BILLING["line1"]),
            ('input[name="billingLocality"]', _BILLING["city"]),
            ('input[name="billingPostalCode"]', _BILLING["postal_code"]),
        ):
            try:
                loc = checkout_page.locator(field).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    loc.fill(value)
            except Exception:
                continue

        # 省份（Alicante 省）
        try:
            state_loc = checkout_page.locator('select[name="billingAdministrativeArea"]').first
            if state_loc.is_visible(timeout=1500):
                state_loc.select_option(label="Alicante")
        except Exception:
            pass

        human_delay(0.5, 1)

        # 服务条款
        try:
            terms = checkout_page.locator('input[name="termsOfServiceConsentCheckbox"]').first
            if terms.is_visible(timeout=1000) and not terms.is_checked():
                terms.check(force=True)
        except Exception:
            pass

        human_delay(1, 2)

        # 10. 提交
        logger.info("点击订阅按钮...")
        try:
            submit = checkout_page.locator('button[type="submit"]').first
            submit.wait_for(state="visible", timeout=5000)
            submit.click()
            logger.info("已提交！")
        except Exception as exc:
            logger.error("提交失败: %s", exc)
            return

        # 11. 等待 3DS
        logger.info("等待 3DS 验证（最长 5 分钟）...")
        otp = card_api.wait_for_3ds(cdk, timeout_sec=300)
        if not otp:
            logger.warning("3DS 超时。")
            try:
                err = checkout_page.locator('[class*="Error"], [class*="error"], [role="alert"]').first
                if err.is_visible(timeout=5000):
                    logger.error("页面错误: %s", err.text_content())
            except Exception:
                pass
            logger.info("最终 URL: %s", checkout_page.url)
            human_delay(20, 20)
            return

        logger.info("3DS 验证码: %s，回填...", otp)
        try:
            acs_frame = checkout_page.frame_locator('iframe[name^="acsFrame"]').first
            code_input = acs_frame.locator('input[type="password"], input[name*="code"], input[name*="challenge"]').first
            code_input.wait_for(state="visible", timeout=15000)
            slow_type(code_input, otp)
            human_delay(0.5, 1)
            acs_frame.locator('button[type="submit"], input[type="submit"]').first.click()
            logger.info("3DS 已提交！")
        except Exception as exc:
            logger.warning("3DS 回填失败: %s", exc)

        human_delay(15, 20)
        logger.info("完毕。最终 URL: %s", checkout_page.url)


if __name__ == "__main__":
    main()
