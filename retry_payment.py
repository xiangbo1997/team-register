# -*- coding: utf-8 -*-
"""
重新填写支付表单并提交 — 清空后重填，模拟真人操作节奏。
"""

import random
import time

from playwright.sync_api import sync_playwright

from src.config import load_config
from src.browser import get_browser_ws
from src.efuncard import EfunCard
from src.utils import setup_logger, human_delay

logger = setup_logger()

_EMAIL = "rmqvhq39342p@outlook.com"

# 正确的西班牙地址
_BILLING = {
    "country": "ES",
    "line1": "1 Calle San Pablo",
    "city": "Alicante",  # Alacant 的西班牙语名
    "postal_code": "03012",
}


def slow_type(locator, text: str) -> None:
    """更像真人的输入：先清空，再逐字输入，带随机停顿"""
    locator.click()
    human_delay(0.3, 0.6)
    # 全选并删除
    locator.press("Meta+a")
    human_delay(0.1, 0.2)
    locator.press("Backspace")
    human_delay(0.2, 0.4)
    # 逐字输入
    for ch in text:
        locator.press(ch)
        time.sleep(random.uniform(0.05, 0.18))
    human_delay(0.3, 0.5)


def clear_and_fill(locator, value: str) -> None:
    """清空后 fill（用于非卡号字段）"""
    locator.click()
    human_delay(0.1, 0.3)
    locator.fill("")
    human_delay(0.1, 0.2)
    locator.fill(value)
    human_delay(0.2, 0.4)


def main():
    config = load_config()
    card_api = EfunCard(token=config.efuncard_token)

    cdk = config.task_cdk
    card = card_api.get_card(cdk)
    if not card:
        logger.error("未能获取虚拟卡。")
        return

    logger.info("卡: %s****%s  有效期: %s  姓名: %s",
                card.card_number[:4], card.card_number[-4:],
                card.expiry_display, card.name_on_card)

    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]

        # 找到 pay.openai.com 页面
        checkout_page = None
        for pg in context.pages:
            if "pay.openai.com" in (pg.url or ""):
                checkout_page = pg
                break

        if not checkout_page:
            logger.error("未找到 pay.openai.com 页面。")
            for pg in context.pages:
                logger.info("  - %s", pg.url)
            return

        checkout_page.bring_to_front()
        human_delay(1, 2)

        # ===== 清空并重新填写卡号 =====
        logger.info("清空卡号并重新填写...")
        card_input = checkout_page.locator('input[name="cardNumber"]').first
        card_input.wait_for(state="visible", timeout=10000)
        slow_type(card_input, card.card_number)
        logger.info("卡号已重新填写。")

        # 有效期
        human_delay(0.5, 1)
        exp_input = checkout_page.locator('input[name="cardExpiry"]').first
        if exp_input.is_visible(timeout=3000):
            slow_type(exp_input, card.expiry_display)
            logger.info("有效期已重新填写。")

        # CVC
        human_delay(0.5, 1)
        cvc_input = checkout_page.locator('input[name="cardCvc"]').first
        if cvc_input.is_visible(timeout=3000):
            slow_type(cvc_input, card.cvv)
            logger.info("CVC 已重新填写。")

        # 持卡人
        human_delay(0.5, 1)
        name_input = checkout_page.locator('input[name="billingName"]').first
        if name_input.is_visible(timeout=3000):
            clear_and_fill(name_input, card.name_on_card or "OpenAI User")
            logger.info("持卡人已填写。")

        # ===== 确认地址正确 =====
        human_delay(0.5, 1)
        logger.info("确认账单地址...")

        # 地址第一行
        addr_input = checkout_page.locator('input[name="billingAddressLine1"]').first
        if addr_input.is_visible(timeout=2000):
            clear_and_fill(addr_input, _BILLING["line1"])
            logger.info("地址: %s", _BILLING["line1"])

        # 邮编
        human_delay(0.3, 0.5)
        postal_input = checkout_page.locator('input[name="billingPostalCode"]').first
        if postal_input.is_visible(timeout=2000):
            clear_and_fill(postal_input, _BILLING["postal_code"])

        # 城市
        human_delay(0.3, 0.5)
        city_input = checkout_page.locator('input[name="billingLocality"]').first
        if city_input.is_visible(timeout=2000):
            clear_and_fill(city_input, _BILLING["city"])

        human_delay(1, 2)

        # ===== 提交 =====
        logger.info("点击订阅按钮...")
        submit_btn = checkout_page.locator('button[type="submit"]').first
        if submit_btn.is_visible(timeout=5000):
            submit_btn.click()
            logger.info("已提交。")
        else:
            logger.error("未找到提交按钮。")
            return

        # ===== 等待 3DS =====
        logger.info("等待 3DS 验证（最长 5 分钟）...")
        otp = card_api.wait_for_3ds(cdk, timeout_sec=300)
        if not otp:
            logger.warning("3DS 超时。检查页面状态...")
            # 检查是否有错误提示
            try:
                error_text = checkout_page.locator('[class*="Error"], [class*="error"], [role="alert"]').first
                if error_text.is_visible(timeout=3000):
                    logger.error("页面错误: %s", error_text.text_content())
            except Exception:
                pass
            logger.info("最终 URL: %s", checkout_page.url)
            human_delay(15, 15)
            return

        logger.info("3DS 验证码: %s", otp)
        try:
            acs_frame = checkout_page.frame_locator('iframe[name^="acsFrame"]').first
            code_input = acs_frame.locator('input[type="password"], input[name*="code"], input[name*="challenge"]').first
            code_input.wait_for(state="visible", timeout=10000)
            slow_type(code_input, otp)
            human_delay(0.5, 1)
            acs_frame.locator('button[type="submit"], input[type="submit"]').first.click()
            logger.info("3DS 已提交！")
        except Exception as exc:
            logger.warning("3DS 回填失败: %s", exc)

        human_delay(15, 20)
        logger.info("完毕。最终页面: %s", checkout_page.url)


if __name__ == "__main__":
    main()
