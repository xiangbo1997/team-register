# -*- coding: utf-8 -*-
"""
最终执行：aimizy 免费试用链接 + 新卡 → 一次提交
"""

import random
import time

from playwright.sync_api import sync_playwright

from src.config import load_config
from src.browser import get_browser_ws
from src.efuncard import EfunCard
from src.payment_link import PaymentLinkGenerator
from src.utils import setup_logger, human_delay

logger = setup_logger()

TOKEN = 'eyJhbGciOiJSUzI1NiIsImtpZCI6IjE5MzQ0ZTY1LWJiYzktNDRkMS1hOWQwLWY5NTdiMDc5YmQwZSIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS92MSJdLCJjbGllbnRfaWQiOiJhcHBfWDh6WTZ2VzJwUTl0UjNkRTduSzFqTDVnSCIsImV4cCI6MTc3NjU2NjY2NywiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9hdXRoIjp7ImFtciI6WyJvdHAiLCJ1cm46b3BlbmFpOmFtcjpvdHBfZW1haWwiXSwiY2hhdGdwdF9hY2NvdW50X2lkIjoiN2E4NmNlYWMtN2MxNi00YWY5LWEyYjUtMzhhNjMwMTU3OTRlIiwiY2hhdGdwdF9hY2NvdW50X3VzZXJfaWQiOiJ1c2VyLWtsVEZPV05GQjVNdjNVdmVTaWE1SlNQMF9fN2E4NmNlYWMtN2MxNi00YWY5LWEyYjUtMzhhNjMwMTU3OTRlIiwiY2hhdGdwdF9jb21wdXRlX3Jlc2lkZW5jeSI6Im5vX2NvbnN0cmFpbnQiLCJjaGF0Z3B0X3BsYW5fdHlwZSI6ImZyZWUiLCJjaGF0Z3B0X3VzZXJfaWQiOiJ1c2VyLWtsVEZPV05GQjVNdjNVdmVTaWE1SlNQMCIsInVzZXJfaWQiOiJ1c2VyLWtsVEZPV05GQjVNdjNVdmVTaWE1SlNQMCJ9LCJodHRwczovL2FwaS5vcGVuYWkuY29tL3Byb2ZpbGUiOnsiZW1haWwiOiJybXF2aHEzOTM0MnBAb3V0bG9vay5jb20iLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZX0sImlhdCI6MTc3NTcwMjY2NiwiaXNzIjoiaHR0cHM6Ly9hdXRoLm9wZW5haS5jb20iLCJqdGkiOiI2MWUyMzliNC1lMDk2LTRhMjItOGFlMC0yMjcxNDkwOThkZDIiLCJuYmYiOjE3NzU3MDI2NjYsInB3ZF9hdXRoX3RpbWUiOjE3NzU3MDI2NjQ1OTgsInNjcCI6WyJvcGVuaWQiLCJlbWFpbCIsInByb2ZpbGUiLCJvZmZsaW5lX2FjY2VzcyIsIm1vZGVsLnJlcXVlc3QiLCJtb2RlbC5yZWFkIiwib3JnYW5pemF0aW9uLnJlYWQiLCJvcmdhbml6YXRpb24ud3JpdGUiXSwic2Vzc2lvbl9pZCI6ImF1dGhzZXNzX3VmNWRXZzBKMFVkT3ozVE4yekpyQ29FMiIsInNsIjp0cnVlLCJzdWIiOiJhdXRoMHxydmpkOWJqdVN0UWN6OGlaUGJZTFVUZ0IifQ.Y4TIkEE-XeU22xPSyArAqCz932zEtYLBtFm6rOUQU-cT1dB-hbgPaWmfv6K7ju-b_3xeQMtDHzu08FN21zt_U8AwfpuVvmBhLmtWsMkus69m-bPYJ3HYMQWGsueQduX-ymvwi34SFH0F7aIJGthsdQoQuMmNKxmie1TrdktQqCR6ULLjAHV--LtEwQAhchwFgAP6OZZtV8xM6PTvtIyROAxbUpa1i_AJ5aI4djVrcJX4BrYoLrVn5BmJ0-xtG0kg_YYQvXLr1mkriEybJCaD4NNcG4g1-6UAf-2x-KBIexC6MOYdp-kAEqbV-vG3vc32eJH7z6YYqK380AwiKoseBxVzMuS2oD_rvDzuemnEyPpxHMz3Agoky9jiPmkihZbkuq58cH9fJuk8IVXkS6VbAlXHni6abGm4VMZhMMLQdFxL4epgLd0HIsu_aNBP48jVjoligLe_s6zHhIxSIKfaT8QLatjDFjBPa3yNdXC3AnwY3xy2nM9DFFzIhY0A8eM_0ryG26CEDfJD9-p2kGP-2lDGwyEuv_0medloHyAHjG1fKT5k1CM-hSX5qBZ9KYjtUk6nncjWFM6qILzba6-aXZEqRAz2fVR4JAnAB8E_jVpc6tdOP9fOY0YYulwTzxeV_sY4kKcaoeagpcIEE0dYOku9_JvewtC4zj3gfg6X_fg'
CDK = "ES-U5VWE-QXHCB-73K4F-MPE6S-9SSTF"
_EMAIL = "rmqvhq39342p@outlook.com"
_BILLING = {
    "country": "ES",
    "line1": "1 Calle San Pablo",
    "city": "Alicante",
    "postal_code": "03012",
}


def slow_type(locator, text: str) -> None:
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
    config = load_config()
    card_api = EfunCard(token=config.efuncard_token)

    # 1. 获取新卡（刚才已激活，query 即可）
    logger.info("获取新卡...")
    card = card_api.get_card(CDK)
    if not card:
        logger.error("获取卡失败: %s", card_api.last_lookup_meta)
        return
    logger.info("卡: %s****%s  有效期: %s  姓名: %s",
                card.card_number[:4], card.card_number[-4:], card.expiry_display, card.name_on_card)

    # 2. 生成 aimizy 免费试用链接（long 模式 → pay.openai.com）
    logger.info("生成 aimizy 免费试用链接...")
    ok, checkout_link = PaymentLinkGenerator._generate_via_aimizy(
        TOKEN, return_mode='long', seat_quantity=5, price_interval='month',
        aimizy_country='ES', aimizy_currency='EUR',
    )
    if not ok:
        logger.error("aimizy 失败: %s", checkout_link)
        return
    logger.info("链接: %s", checkout_link[:80])

    # 3. 连接浏览器
    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        # 关闭旧 checkout 页面
        for pg in list(context.pages):
            url = pg.url or ""
            if "pay.openai.com" in url or "checkout" in url:
                pg.close()

        # 4. 打开新链接
        page = context.new_page()
        page.set_default_timeout(60000)
        logger.info("打开支付页面...")
        page.goto(checkout_link, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        human_delay(3, 5)

        # 等待表单（pay.openai.com 需要 30-40 秒才能加载）
        logger.info("等待 Stripe 表单加载（最长 60 秒）...")
        card_input = page.locator('input[name="cardNumber"]').first
        try:
            card_input.wait_for(state="visible", timeout=60000)
        except Exception:
            # 刷新重试一次
            logger.warning("表单未加载，刷新页面重试...")
            page.reload(wait_until="domcontentloaded")
            try:
                card_input.wait_for(state="visible", timeout=60000)
            except Exception:
                logger.error("表单仍未加载！URL: %s", page.url)
                human_delay(30, 30)
                return

        logger.info("=== 表单已加载，开始一次性填写 ===")

        # 邮箱
        try:
            el = page.locator('input[name="email"], input#email').first
            if el.is_visible(timeout=2000):
                el.click()
                el.fill(_EMAIL)
                logger.info("[1/7] 邮箱 ✓")
        except Exception:
            pass
        human_delay(1, 1.5)

        # 卡号
        slow_type(card_input, card.card_number)
        logger.info("[2/7] 卡号 ✓")
        human_delay(0.8, 1.2)

        # 有效期
        el = page.locator('input[name="cardExpiry"]').first
        if el.is_visible(timeout=3000):
            slow_type(el, card.expiry_display)
            logger.info("[3/7] 有效期 ✓")
        human_delay(0.8, 1.2)

        # CVC
        el = page.locator('input[name="cardCvc"]').first
        if el.is_visible(timeout=3000):
            slow_type(el, card.cvv)
            logger.info("[4/7] CVC ✓")
        human_delay(0.8, 1.2)

        # 持卡人
        el = page.locator('input[name="billingName"]').first
        if el.is_visible(timeout=3000):
            el.click()
            el.fill(card.name_on_card or "OpenAI User")
            logger.info("[5/7] 持卡人 ✓")
        human_delay(0.8, 1.2)

        # 地址
        logger.info("填写地址...")
        for sel in ('button:has-text("手动输入地址")', 'a:has-text("手动输入地址")',
                     'button:has-text("Enter address manually")'):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    human_delay(0.5, 1)
                    break
            except Exception:
                continue

        try:
            page.locator('select[name="billingCountry"]').first.select_option(value="ES")
            human_delay(0.5, 0.8)
        except Exception:
            pass

        for field, value in (
            ('input[name="billingAddressLine1"]', _BILLING["line1"]),
            ('input[name="billingPostalCode"]', _BILLING["postal_code"]),
            ('input[name="billingLocality"]', _BILLING["city"]),
        ):
            try:
                loc = page.locator(field).first
                if loc.is_visible(timeout=2000):
                    loc.click()
                    loc.fill(value)
                    human_delay(0.3, 0.5)
            except Exception:
                continue

        # 省份
        try:
            sel = page.locator('select[name="billingAdministrativeArea"]').first
            if sel.is_visible(timeout=1500):
                for v in ("Alicante", "A", "Alacant"):
                    try:
                        sel.select_option(label=v)
                        break
                    except Exception:
                        try:
                            sel.select_option(value=v)
                            break
                        except Exception:
                            continue
        except Exception:
            pass

        logger.info("[6/7] 地址 ✓")
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
        logger.info("[7/7] >>> 点击订阅 <<<")
        page.locator('button[type="submit"]').first.click()
        logger.info("已提交！")

        # 3DS
        logger.info("等待 3DS（最长 5 分钟）...")
        otp = card_api.wait_for_3ds(CDK, timeout_sec=300)
        if not otp:
            logger.warning("3DS 超时。")
            try:
                err = page.locator('[class*="Error"], [class*="error"], [role="alert"]').first
                if err.is_visible(timeout=5000):
                    logger.error(">>> 错误: %s <<<", err.text_content())
            except Exception:
                pass
            logger.info("URL: %s", page.url)
            human_delay(30, 30)
            return

        logger.info(">>> 3DS: %s <<<", otp)
        try:
            acs = page.frame_locator('iframe[name^="acsFrame"]').first
            ci = acs.locator('input[type="password"], input[name*="code"], input[name*="challenge"]').first
            ci.wait_for(state="visible", timeout=15000)
            slow_type(ci, otp)
            human_delay(0.5, 1)
            acs.locator('button[type="submit"], input[type="submit"]').first.click()
            logger.info(">>> 3DS 提交成功！ <<<")
        except Exception as exc:
            logger.warning("3DS 回填失败: %s", exc)

        human_delay(15, 20)
        logger.info("完毕。URL: %s", page.url)
        if "subscribed=true" in page.url or "success" in page.url:
            logger.info("🎉 支付成功！")


if __name__ == "__main__":
    main()
