# -*- coding: utf-8 -*-
"""
用全新 hosted checkout session 重试绑卡。
关键：新 session 没有失败历史。
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
    "state": "Alicante",
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

    cdk = config.task_cdk
    card = card_api.get_card(cdk)
    if not card:
        logger.error("未能获取虚拟卡。")
        return
    logger.info("卡: %s****%s  有效期: %s  姓名: %s",
                card.card_number[:4], card.card_number[-4:], card.expiry_display, card.name_on_card)

    # 生成全新 hosted checkout session
    logger.info("生成全新 checkout session (long 模式)...")
    success, checkout_link = PaymentLinkGenerator.generate_checkout_link(
        ACCESS_TOKEN,
        plan_type=config.payment_plan,
        proxy=config.proxy or None,
        return_mode="long",
        aimizy_country=config.aimizy_country,
        aimizy_currency=config.aimizy_currency,
    )
    if not success:
        logger.error("链接生成失败: %s", checkout_link)
        return

    logger.info("新 checkout 链接: %s", checkout_link[:80])

    # 连接浏览器
    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        # 关闭旧的 checkout 页面
        for pg in list(context.pages):
            url = pg.url or ""
            if "pay.openai.com" in url or "checkout" in url:
                logger.info("关闭旧页面: %s", url[:60])
                pg.close()

        # 打开新链接
        page = context.new_page()
        page.set_default_timeout(60000)
        logger.info("打开新支付页面...")
        page.goto(checkout_link, wait_until="domcontentloaded", timeout=60000)

        # 等待 Stripe 表单充分加载
        logger.info("等待页面完全加载...")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        human_delay(3, 5)

        # 检查表单是否可用
        card_input = page.locator('input[name="cardNumber"]').first
        try:
            card_input.wait_for(state="visible", timeout=15000)
        except Exception:
            logger.error("卡号输入框未出现。页面文本: %s", page.locator("body").text_content()[:300])
            human_delay(30, 30)
            return

        logger.info("Stripe 表单已加载！开始填写...")

        # 填写邮箱
        try:
            email_loc = page.locator('input[name="email"], input#email').first
            if email_loc.is_visible(timeout=2000):
                email_loc.click()
                email_loc.fill(_EMAIL)
                logger.info("邮箱: %s", _EMAIL)
        except Exception:
            pass

        human_delay(1, 2)

        # 填写卡号 — 用 slow_type 模拟真人
        slow_type(card_input, card.card_number)
        logger.info("卡号已填写。")

        human_delay(0.8, 1.5)

        # 有效期
        exp_loc = page.locator('input[name="cardExpiry"]').first
        if exp_loc.is_visible(timeout=3000):
            slow_type(exp_loc, card.expiry_display)
            logger.info("有效期已填写。")

        human_delay(0.8, 1.5)

        # CVC
        cvc_loc = page.locator('input[name="cardCvc"]').first
        if cvc_loc.is_visible(timeout=3000):
            slow_type(cvc_loc, card.cvv)
            logger.info("CVC 已填写。")

        human_delay(0.8, 1.5)

        # 持卡人
        name_loc = page.locator('input[name="billingName"]').first
        if name_loc.is_visible(timeout=3000):
            name_loc.click()
            name_loc.fill(card.name_on_card or "OpenAI User")
            logger.info("持卡人: %s", card.name_on_card)

        human_delay(0.8, 1.5)

        # 账单地址
        logger.info("填写账单地址...")
        for manual_sel in ('button:has-text("手动输入地址")', 'a:has-text("手动输入地址")',
                           'button:has-text("Enter address manually")'):
            try:
                btn = page.locator(manual_sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    human_delay(0.5, 1)
                    break
            except Exception:
                continue

        # 国家
        try:
            page.locator('select[name="billingCountry"]').first.select_option(value="ES")
        except Exception:
            pass
        human_delay(0.5, 0.8)

        # 地址
        for field, value in (
            ('input[name="billingAddressLine1"]', _BILLING["line1"]),
            ('input[name="billingPostalCode"]', _BILLING["postal_code"]),
            ('input[name="billingLocality"]', _BILLING["city"]),
        ):
            try:
                loc = page.locator(field).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    loc.fill(value)
            except Exception:
                continue
            human_delay(0.3, 0.5)

        # 省份
        try:
            page.locator('select[name="billingAdministrativeArea"]').first.select_option(label="Alicante")
        except Exception:
            try:
                page.locator('select[name="billingAdministrativeArea"]').first.select_option(value="A")
            except Exception:
                pass

        human_delay(0.5, 1)

        # 服务条款
        try:
            terms = page.locator('input[name="termsOfServiceConsentCheckbox"]').first
            if terms.is_visible(timeout=1000) and not terms.is_checked():
                terms.check(force=True)
        except Exception:
            pass

        human_delay(2, 3)

        # 提交
        logger.info("点击订阅按钮...")
        page.locator('button[type="submit"]').first.click()
        logger.info("已提交！等待响应...")

        # 等 3DS
        logger.info("等待 3DS 验证（最长 5 分钟）...")
        otp = card_api.wait_for_3ds(cdk, timeout_sec=300)
        if not otp:
            logger.warning("3DS 超时。")
            try:
                err = page.locator('[class*="Error"], [class*="error"], [role="alert"]').first
                if err.is_visible(timeout=5000):
                    logger.error("页面错误: %s", err.text_content())
            except Exception:
                pass
            logger.info("最终 URL: %s", page.url)
            human_delay(20, 20)
            return

        logger.info("3DS 验证码: %s", otp)
        try:
            acs = page.frame_locator('iframe[name^="acsFrame"]').first
            code_input = acs.locator('input[type="password"], input[name*="code"], input[name*="challenge"]').first
            code_input.wait_for(state="visible", timeout=15000)
            slow_type(code_input, otp)
            human_delay(0.5, 1)
            acs.locator('button[type="submit"], input[type="submit"]').first.click()
            logger.info("3DS 已提交！")
        except Exception as exc:
            logger.warning("3DS 回填失败: %s", exc)

        human_delay(15, 20)
        logger.info("完毕。URL: %s", page.url)


if __name__ == "__main__":
    main()
