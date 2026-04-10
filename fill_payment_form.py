# -*- coding: utf-8 -*-
"""
在已打开的 Stripe hosted checkout 页面上填写信用卡信息并提交。

用法：
    python fill_payment_form.py
"""

import os
import random

from playwright.sync_api import sync_playwright

from src.config import load_config
from src.browser import get_browser_ws
from src.efuncard import EfunCard
from src.utils import setup_logger, human_delay

logger = setup_logger()

# 账号邮箱
_EMAIL = "rmqvhq39342p@outlook.com"

# 西班牙账单地址（匹配截图中已选的"西班牙"）
_BILLING_PROFILE = {
    "country": "ES",
    "line1": "Calle Gran Via 28",
    "city": "Madrid",
    "postal_code": "28013",
}


def human_typing(target, selector: str, text: str) -> None:
    """模拟人类打字"""
    loc = target.locator(selector).first if hasattr(target, "locator") else target.first
    loc.wait_for(state="visible", timeout=15000)
    loc.click()
    human_delay(0.2, 0.5)
    loc.press_sequentially(text, delay=random.randint(50, 150))
    logger.info("填入: %s", selector)


def main():
    config = load_config()
    card_api = EfunCard(token=config.efuncard_token)

    # 获取虚拟卡
    cdk = config.task_cdk
    card = card_api.get_card(cdk)
    if not card:
        logger.error("未能获取可用虚拟卡，终止。")
        return

    logger.info("虚拟卡: %s****%s  有效期: %s", card.card_number[:4], card.card_number[-4:], card.expiry_display)

    # 连接 AdsPower 浏览器
    logger.info("连接 AdsPower...")
    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        # 找到已打开的 pay.openai.com 页面
        checkout_page = None
        for pg in context.pages:
            if "pay.openai.com" in (pg.url or ""):
                checkout_page = pg
                break

        if not checkout_page:
            logger.error("未找到已打开的 pay.openai.com 页面。当前页面:")
            for pg in context.pages:
                logger.error("  - %s", pg.url)
            return

        logger.info("找到支付页面: %s", checkout_page.url[:80])
        checkout_page.bring_to_front()
        human_delay(1, 2)

        # 1. 填写邮箱
        logger.info("填写联系信息...")
        try:
            email_input = checkout_page.locator('input[name="email"], input#email').first
            if email_input.is_visible(timeout=3000):
                email_input.click()
                email_input.fill(_EMAIL)
                logger.info("邮箱已填写: %s", _EMAIL)
        except Exception as exc:
            logger.warning("邮箱填写失败: %s", exc)

        human_delay(0.5, 1)

        # 2. 填写银行卡信息（hosted checkout 页面，Stripe Elements 在 iframe 中）
        logger.info("填写银行卡信息...")
        filled_card = False

        # 尝试方式 A: 直接在页面上找 input（非 iframe）
        for card_sel in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]',
                         'input[placeholder*="1234"]', 'input[autocomplete="cc-number"]'):
            try:
                if checkout_page.locator(card_sel).first.is_visible(timeout=2000):
                    human_typing(checkout_page, card_sel, card.card_number)
                    # 有效期
                    for exp_sel in ('input[name="cardExpiry"]', 'input[name="exp-date"]',
                                    'input[placeholder*="月份"]', 'input[placeholder*="MM"]'):
                        try:
                            if checkout_page.locator(exp_sel).first.is_visible(timeout=1500):
                                human_typing(checkout_page, exp_sel, card.expiry_display)
                                break
                        except Exception:
                            continue
                    # CVC
                    for cvc_sel in ('input[name="cardCvc"]', 'input[name="cvc"]',
                                    'input[placeholder*="CVC"]'):
                        try:
                            if checkout_page.locator(cvc_sel).first.is_visible(timeout=1500):
                                human_typing(checkout_page, cvc_sel, card.cvv)
                                break
                        except Exception:
                            continue
                    filled_card = True
                    logger.info("卡信息已填写（页面直接 input）。")
                    break
            except Exception:
                continue

        # 尝试方式 B: Stripe iframe
        if not filled_card:
            for iframe_sel in (
                'iframe[title="Secure payment input frame"]',
                'iframe[title*="payment" i]',
                'iframe[title*="支付" i]',
                'iframe[name*="__privateStripeFrame"]',
            ):
                try:
                    stripe_frame = checkout_page.frame_locator(iframe_sel).first
                    for card_input in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]'):
                        try:
                            if stripe_frame.locator(card_input).first.is_visible(timeout=2000):
                                human_typing(stripe_frame, card_input, card.card_number)
                                for exp_name in ('input[name="cardExpiry"]', 'input[name="exp-date"]'):
                                    try:
                                        if stripe_frame.locator(exp_name).first.is_visible(timeout=1500):
                                            human_typing(stripe_frame, exp_name, card.expiry_display)
                                            break
                                    except Exception:
                                        continue
                                for cvc_name in ('input[name="cardCvc"]', 'input[name="cvc"]'):
                                    try:
                                        if stripe_frame.locator(cvc_name).first.is_visible(timeout=1500):
                                            human_typing(stripe_frame, cvc_name, card.cvv)
                                            break
                                    except Exception:
                                        continue
                                filled_card = True
                                logger.info("卡信息已填写（Stripe iframe）。")
                                break
                        except Exception:
                            continue
                    if filled_card:
                        break
                except Exception:
                    continue

        if not filled_card:
            logger.error("未能定位到卡号输入框。尝试打印页面所有 iframe 和 input...")
            try:
                frames = checkout_page.frames
                logger.info("页面 frames 数量: %d", len(frames))
                for i, f in enumerate(frames):
                    logger.info("  frame[%d]: name=%s url=%s", i, f.name, f.url[:80] if f.url else "")
                    inputs = f.query_selector_all("input")
                    for inp in inputs[:10]:
                        name = inp.get_attribute("name") or ""
                        placeholder = inp.get_attribute("placeholder") or ""
                        inp_type = inp.get_attribute("type") or ""
                        logger.info("    input: name=%s type=%s placeholder=%s", name, inp_type, placeholder)
            except Exception as exc:
                logger.warning("诊断失败: %s", exc)
            return

        human_delay(0.5, 1)

        # 3. 持卡人姓名
        logger.info("填写持卡人姓名...")
        try:
            name_input = checkout_page.locator('input[name="billingName"]').first
            if name_input.is_visible(timeout=2000):
                name_input.click()
                name_input.fill(card.name_on_card or "OpenAI User")
                logger.info("持卡人: %s", card.name_on_card or "OpenAI User")
        except Exception as exc:
            logger.warning("持卡人姓名填写失败: %s", exc)

        human_delay(0.5, 1)

        # 4. 账单地址
        logger.info("填写账单地址...")
        # 点击"手动输入地址"
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
            country_sel = checkout_page.locator('select[name="billingCountry"]').first
            if country_sel.is_visible(timeout=1500):
                country_sel.select_option(value=_BILLING_PROFILE["country"])
                logger.info("国家: %s", _BILLING_PROFILE["country"])
        except Exception as exc:
            logger.warning("国家选择失败: %s", exc)

        human_delay(0.3, 0.5)

        # 地址字段
        for field_name, value in (
            ('input[name="billingAddressLine1"]', _BILLING_PROFILE["line1"]),
            ('input[name="billingLocality"]', _BILLING_PROFILE["city"]),
            ('input[name="billingPostalCode"]', _BILLING_PROFILE["postal_code"]),
        ):
            if not value:
                continue
            try:
                loc = checkout_page.locator(field_name).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    loc.fill(value)
                    logger.info("  %s = %s", field_name, value)
            except Exception:
                continue

        human_delay(0.5, 1)

        # 5. 勾选服务条款（如果有）
        try:
            terms = checkout_page.locator('input[name="termsOfServiceConsentCheckbox"]').first
            if terms.is_visible(timeout=1000) and not terms.is_checked():
                terms.check(force=True)
                logger.info("已勾选服务条款。")
        except Exception:
            pass

        human_delay(1, 2)

        # 6. 点击订阅按钮
        logger.info("点击订阅按钮...")
        try:
            for submit_sel in ('button[type="submit"]', 'button:has-text("订阅")', 'button:has-text("Subscribe")'):
                btn = checkout_page.locator(submit_sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    logger.info("订阅按钮已点击。")
                    break
        except Exception as exc:
            logger.warning("点击订阅按钮失败: %s", exc)
            return

        # 7. 等待 3DS 并回填
        logger.info("等待 3DS 验证...")
        otp = card_api.wait_for_3ds(cdk)
        if not otp:
            logger.warning("3DS 获取超时，支付可能已直接成功或被拒。")
            human_delay(15, 20)
            logger.info("最终页面: %s", checkout_page.url)
            return

        logger.info("3DS 验证码: %s，回填中...", otp)
        try:
            acs_frame = checkout_page.frame_locator('iframe[name^="acsFrame"]').first
            human_typing(
                acs_frame,
                'input[type="password"], input[name*="code"], input[name*="challenge"]',
                otp,
            )
            acs_frame.locator('button[type="submit"], input[type="submit"]').first.click()
            logger.info("3DS 已提交！")
        except Exception as exc:
            logger.warning("3DS 回填失败: %s", exc)

        human_delay(15, 20)
        logger.info("支付流程完毕。最终页面: %s", checkout_page.url)


if __name__ == "__main__":
    main()
