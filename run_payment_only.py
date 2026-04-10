# -*- coding: utf-8 -*-
"""
仅执行支付流程：连接已登录的 AdsPower 浏览器 → 提取 token → 生成支付链接 → 绑卡

用法：
    python run_payment_only.py
"""

import os
import sys
import random

from playwright.sync_api import sync_playwright, BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.config import load_config
from src.browser import get_browser_ws
from src.efuncard import EfunCard
from src.payment_link import PaymentLinkGenerator
from src.automation import extract_session_tokens_with_http, ExperienceStore
from src.utils import setup_logger, human_delay

logger = setup_logger()


def human_typing(page, selector: str, text: str) -> None:
    """模拟人类打字速度"""
    target_locator = page.locator(selector).first if hasattr(page, "locator") else page.first
    target_locator.wait_for(state="visible", timeout=20000)
    target_locator.click()
    human_delay(0.2, 0.8)
    target_locator.press_sequentially(text, delay=random.randint(50, 150))
    logger.debug("填入内容到: %s", selector)


def extract_access_token(page: Page, context: BrowserContext, proxy_url: str) -> tuple[str, str]:
    """从已登录的浏览器提取 access_token"""
    logger.info("正在导航到 chatgpt.com 提取 session token...")
    try:
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        logger.warning("首页加载超时，继续尝试提取 token。")

    human_delay(3, 5)

    # 关闭可能弹出的欢迎弹窗
    for dismiss_sel in (
        'button[data-testid="dismiss-button"]',
        'button:has-text("Okay")',
        'button:has-text("OK")',
        'button:has-text("Got it")',
        '[aria-label="Close"]',
    ):
        try:
            btn = page.locator(dismiss_sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                human_delay(0.5, 1)
        except Exception:
            continue

    user_agent = "Mozilla/5.0"
    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        pass

    cookies = context.cookies()
    has_session = any("next-auth.session-token" in str(c.get("name", "")) for c in cookies)
    if not has_session:
        logger.warning("未发现 next-auth session cookie，账号可能未登录。")

    for attempt in range(1, 4):
        try:
            access_token, refresh_token = extract_session_tokens_with_http(
                cookies=cookies,
                user_agent=str(user_agent),
                proxy_url=proxy_url,
            )
            if access_token:
                logger.info("第 %d 次尝试成功提取到 AccessToken。", attempt)
                return access_token, refresh_token
        except Exception as exc:
            logger.warning("第 %d 次 session 提取异常: %s", attempt, exc)

        logger.warning("第 %d/3 次未拿到 AccessToken，等待后重试...", attempt)
        human_delay(2, 4)
        try:
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=15000)
            human_delay(2, 3)
            cookies = context.cookies()
        except Exception:
            pass

    return "", ""


def run_payment(page: Page, context: BrowserContext, config, card_api: EfunCard) -> None:
    """提取 token → 生成链接 → 绑卡"""

    # 1. 提取 access_token
    access_token, refresh_token = extract_access_token(page, context, config.proxy)
    if not access_token:
        logger.error("无法提取 AccessToken，终止支付流程。请确认账号已登录。")
        return

    logger.info("AccessToken 提取成功 (前20字符): %s...", access_token[:20])

    # 2. 生成支付链接
    logger.info("正在生成 %s 计划支付链接...", config.payment_plan)
    link_mode = config.payment_link_return_mode
    if link_mode == "app":
        success, checkout_link = PaymentLinkGenerator.generate_short_link(
            access_token,
            plan_type=config.payment_plan,
            proxy=config.proxy or None,
            aimizy_country=config.aimizy_country,
            aimizy_currency=config.aimizy_currency,
        )
    else:
        success, checkout_link = PaymentLinkGenerator.generate_checkout_link(
            access_token,
            plan_type=config.payment_plan,
            proxy=config.proxy or None,
            return_mode=link_mode,
            aimizy_country=config.aimizy_country,
            aimizy_currency=config.aimizy_currency,
        )

    if not success:
        logger.error("支付链接生成失败: %s", checkout_link)
        return

    logger.info("支付链接生成成功: %s", checkout_link)

    # 3. 获取虚拟卡
    cdk = config.task_cdk
    if not cdk:
        logger.error("未配置 TASK_CDK，无法获取虚拟卡。")
        return

    card = card_api.get_card(cdk)
    if not card:
        logger.error("未能获取可用虚拟卡，终止绑卡流程。")
        return

    logger.info("获取到虚拟卡: %s****%s", card.card_number[:4], card.card_number[-4:])

    # 4. 在新标签页打开支付链接
    logger.info("正在新标签页打开支付链接...")
    checkout_page = context.new_page()
    checkout_page.set_default_timeout(60000)
    checkout_page.goto(checkout_link, wait_until="domcontentloaded", timeout=60000)
    human_delay(5, 10)

    # 5. 等待 Stripe 表单加载
    logger.info("等待 Stripe 支付表单加载...")
    stripe_loaded = False
    for _ in range(30):
        for iframe_sel in (
            'iframe[title="Secure payment input frame"]',
            'iframe[title*="payment" i]',
            'iframe[name*="__privateStripeFrame"]',
        ):
            try:
                if checkout_page.locator(iframe_sel).first.is_visible(timeout=1000):
                    stripe_loaded = True
                    break
            except Exception:
                continue
        if stripe_loaded:
            break
        # 也检查 split frames 模式
        try:
            if checkout_page.locator('input[name="cardnumber"]').first.is_visible(timeout=500):
                stripe_loaded = True
                break
        except Exception:
            pass
        human_delay(1, 1.5)

    if not stripe_loaded:
        logger.error("Stripe 支付表单加载超时，请检查页面状态。")
        logger.info("当前页面 URL: %s", checkout_page.url)
        human_delay(30, 30)  # 保持浏览器供手动检查
        return

    logger.info("Stripe 表单已加载，开始填写信用卡信息...")

    # 6. 填写卡片信息 — 尝试两种 Stripe 形态
    filled = False

    # 形态 A: split frames（独立 input）
    for card_sel in ('input[name="cardnumber"]', 'input[autocomplete="cc-number"]', 'input[name="number"]'):
        try:
            if checkout_page.locator(card_sel).first.is_visible(timeout=2000):
                human_typing(checkout_page, card_sel, card.card_number)
                for exp_sel in ('input[name="exp-date"]', 'input[autocomplete="cc-exp"]', 'input[name="expiry"]'):
                    try:
                        if checkout_page.locator(exp_sel).first.is_visible(timeout=1000):
                            human_typing(checkout_page, exp_sel, card.expiry_display)
                            break
                    except Exception:
                        continue
                for cvc_sel in ('input[name="cvc"]', 'input[autocomplete="cc-csc"]'):
                    try:
                        if checkout_page.locator(cvc_sel).first.is_visible(timeout=1000):
                            human_typing(checkout_page, cvc_sel, card.cvv)
                            break
                    except Exception:
                        continue
                filled = True
                logger.info("使用 split_frames 形态填写完成。")
                break
        except Exception:
            continue

    # 形态 B: 单个 Stripe iframe
    if not filled:
        for iframe_sel in (
            'iframe[title="Secure payment input frame"]',
            'iframe[title*="payment" i]',
            'iframe[name*="__privateStripeFrame"]',
        ):
            try:
                stripe_frame = checkout_page.frame_locator(iframe_sel).first
                for card_input in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]'):
                    try:
                        if stripe_frame.locator(card_input).first.is_visible(timeout=2000):
                            human_typing(stripe_frame, card_input, card.card_number)
                            for exp_name in ('input[name="cardExpiry"]', 'input[name="exp-date"]', 'input[name="expiry"]'):
                                try:
                                    if stripe_frame.locator(exp_name).first.is_visible(timeout=1000):
                                        human_typing(stripe_frame, exp_name, card.expiry_display)
                                        break
                                except Exception:
                                    continue
                            for cvc_name in ('input[name="cardCvc"]', 'input[name="cvc"]'):
                                try:
                                    if stripe_frame.locator(cvc_name).first.is_visible(timeout=1000):
                                        human_typing(stripe_frame, cvc_name, card.cvv)
                                        break
                                except Exception:
                                    continue
                            filled = True
                            logger.info("使用 unified_iframe 形态填写完成。")
                            break
                    except Exception:
                        continue
                if filled:
                    break
            except Exception:
                continue

    if not filled:
        logger.error("未找到可用 Stripe 表单，绑卡失败。")
        human_delay(30, 30)
        return

    # 填写持卡人姓名
    try:
        name_input = checkout_page.locator('input[name="billingName"]')
        if name_input.is_visible(timeout=2000):
            human_typing(checkout_page, 'input[name="billingName"]', card.name_on_card or "OpenAI User")
    except Exception:
        pass

    # 7. 点击订阅按钮
    logger.info("点击订阅按钮...")
    try:
        submit_btn = checkout_page.locator('button[type="submit"]')
        if submit_btn.is_visible(timeout=5000):
            submit_btn.click()
            logger.info("订阅按钮已点击。")
    except Exception as exc:
        logger.warning("点击订阅按钮失败: %s", exc)
        return

    # 8. 等待 3DS 验证并回填
    logger.info("等待 3DS 验证...")
    otp = card_api.wait_for_3ds(cdk)
    if not otp:
        logger.warning("3DS 获取超时，支付可能已直接成功或被拒。")
        human_delay(10, 15)
        return

    logger.info("捕获到 3DS 验证码: %s，尝试回填...", otp)
    try:
        acs_frame = checkout_page.frame_locator('iframe[name^="acsFrame"]').first
        human_typing(
            acs_frame,
            'input[type="password"], input[name*="code"], input[name*="challenge"]',
            otp,
        )
        btn = acs_frame.locator('button[type="submit"], input[type="submit"]').first
        btn.click()
        logger.info("3DS 验证码已提交！")
    except Exception as exc:
        logger.warning("3DS 回填失败: %s", exc)

    human_delay(10, 15)
    logger.info("支付流程执行完毕。当前页面: %s", checkout_page.url)


def main():
    config = load_config()

    if not config.efuncard_token:
        logger.error("请配置 EFUNCARD_TOKEN 环境变量。")
        return

    card_api = EfunCard(token=config.efuncard_token)

    # 支持通过环境变量或命令行直接传入 access_token，跳过浏览器提取
    manual_token = os.getenv("ACCESS_TOKEN", "")

    if manual_token:
        logger.info("使用手动提供的 AccessToken，直接进入支付流程。")
        run_payment_with_token(config, card_api, manual_token)
    else:
        if not config.task_ads_id:
            logger.error("请配置 TASK_ADS_ID 或 ACCESS_TOKEN 环境变量。")
            return

        logger.info("正在连接 AdsPower 浏览器 (profile: %s)...", config.task_ads_id)
        try:
            ws_url = get_browser_ws(
                ads_api=config.ads_api,
                user_id=config.task_ads_id,
                api_key=config.ads_api_key,
            )
        except Exception as exc:
            logger.error("连接 AdsPower 失败: %s", exc)
            return

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(60000)
            run_payment(page, context, config, card_api)
            logger.info("保持浏览器打开 30 秒供检查...")
            human_delay(30, 30)


def run_payment_with_token(config, card_api: EfunCard, access_token: str) -> None:
    """使用已有 token，通过 AdsPower 浏览器打开支付链接并绑卡"""

    # 1. 生成支付链接
    logger.info("正在生成 %s 计划支付链接...", config.payment_plan)
    link_mode = config.payment_link_return_mode
    if link_mode == "app":
        success, checkout_link = PaymentLinkGenerator.generate_short_link(
            access_token,
            plan_type=config.payment_plan,
            proxy=config.proxy or None,
            aimizy_country=config.aimizy_country,
            aimizy_currency=config.aimizy_currency,
        )
    else:
        success, checkout_link = PaymentLinkGenerator.generate_checkout_link(
            access_token,
            plan_type=config.payment_plan,
            proxy=config.proxy or None,
            return_mode=link_mode,
            aimizy_country=config.aimizy_country,
            aimizy_currency=config.aimizy_currency,
        )

    if not success:
        logger.error("支付链接生成失败: %s", checkout_link)
        return

    logger.info("支付链接生成成功: %s", checkout_link)

    # 2. 获取虚拟卡
    cdk = config.task_cdk
    if not cdk:
        logger.error("未配置 TASK_CDK，无法获取虚拟卡。")
        return

    card = card_api.get_card(cdk)
    if not card:
        logger.error("未能获取可用虚拟卡，终止绑卡流程。")
        return

    logger.info("获取到虚拟卡: %s****%s", card.card_number[:4], card.card_number[-4:])

    # 3. 连接浏览器打开支付链接
    if not config.task_ads_id:
        logger.info("未配置 TASK_ADS_ID，仅输出链接，无法自动绑卡。")
        logger.info("请手动打开链接: %s", checkout_link)
        return

    logger.info("正在连接 AdsPower 浏览器...")
    ws_url = get_browser_ws(
        ads_api=config.ads_api,
        user_id=config.task_ads_id,
        api_key=config.ads_api_key,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        checkout_page = context.new_page()
        checkout_page.set_default_timeout(60000)

        logger.info("正在打开支付链接...")
        checkout_page.goto(checkout_link, wait_until="domcontentloaded", timeout=60000)
        human_delay(5, 10)

        # 等待 Stripe 表单
        logger.info("等待 Stripe 支付表单加载...")
        stripe_loaded = False
        for _ in range(30):
            for iframe_sel in (
                'iframe[title="Secure payment input frame"]',
                'iframe[title*="payment" i]',
                'iframe[name*="__privateStripeFrame"]',
            ):
                try:
                    if checkout_page.locator(iframe_sel).first.is_visible(timeout=1000):
                        stripe_loaded = True
                        break
                except Exception:
                    continue
            if stripe_loaded:
                break
            try:
                if checkout_page.locator('input[name="cardnumber"]').first.is_visible(timeout=500):
                    stripe_loaded = True
                    break
            except Exception:
                pass
            human_delay(1, 1.5)

        if not stripe_loaded:
            logger.error("Stripe 支付表单加载超时。当前 URL: %s", checkout_page.url)
            human_delay(30, 30)
            return

        logger.info("Stripe 表单已加载，开始填写信用卡信息...")

        # 填写卡片 — 两种形态
        filled = _fill_card_info(checkout_page, card)
        if not filled:
            logger.error("未找到可用 Stripe 表单，绑卡失败。")
            human_delay(30, 30)
            return

        # 持卡人姓名
        try:
            name_input = checkout_page.locator('input[name="billingName"]')
            if name_input.is_visible(timeout=2000):
                human_typing(checkout_page, 'input[name="billingName"]', card.name_on_card or "OpenAI User")
        except Exception:
            pass

        # 点击订阅
        logger.info("点击订阅按钮...")
        try:
            submit_btn = checkout_page.locator('button[type="submit"]')
            if submit_btn.is_visible(timeout=5000):
                submit_btn.click()
                logger.info("订阅按钮已点击。")
        except Exception as exc:
            logger.warning("点击订阅按钮失败: %s", exc)
            return

        # 3DS 验证
        logger.info("等待 3DS 验证...")
        otp = card_api.wait_for_3ds(cdk)
        if not otp:
            logger.warning("3DS 获取超时，支付可能已直接成功或被拒。")
            human_delay(10, 15)
            return

        logger.info("捕获到 3DS 验证码: %s，尝试回填...", otp)
        try:
            acs_frame = checkout_page.frame_locator('iframe[name^="acsFrame"]').first
            human_typing(
                acs_frame,
                'input[type="password"], input[name*="code"], input[name*="challenge"]',
                otp,
            )
            btn = acs_frame.locator('button[type="submit"], input[type="submit"]').first
            btn.click()
            logger.info("3DS 验证码已提交！")
        except Exception as exc:
            logger.warning("3DS 回填失败: %s", exc)

        human_delay(10, 15)
        logger.info("支付流程完毕。当前页面: %s", checkout_page.url)


def _fill_card_info(checkout_page, card) -> bool:
    """尝试两种 Stripe 形态填写卡片信息"""
    # 形态 A: split frames
    for card_sel in ('input[name="cardnumber"]', 'input[autocomplete="cc-number"]', 'input[name="number"]'):
        try:
            if checkout_page.locator(card_sel).first.is_visible(timeout=2000):
                human_typing(checkout_page, card_sel, card.card_number)
                for exp_sel in ('input[name="exp-date"]', 'input[autocomplete="cc-exp"]', 'input[name="expiry"]'):
                    try:
                        if checkout_page.locator(exp_sel).first.is_visible(timeout=1000):
                            human_typing(checkout_page, exp_sel, card.expiry_display)
                            break
                    except Exception:
                        continue
                for cvc_sel in ('input[name="cvc"]', 'input[autocomplete="cc-csc"]'):
                    try:
                        if checkout_page.locator(cvc_sel).first.is_visible(timeout=1000):
                            human_typing(checkout_page, cvc_sel, card.cvv)
                            break
                    except Exception:
                        continue
                logger.info("使用 split_frames 形态填写完成。")
                return True
        except Exception:
            continue

    # 形态 B: unified Stripe iframe
    for iframe_sel in (
        'iframe[title="Secure payment input frame"]',
        'iframe[title*="payment" i]',
        'iframe[name*="__privateStripeFrame"]',
    ):
        try:
            stripe_frame = checkout_page.frame_locator(iframe_sel).first
            for card_input in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]'):
                try:
                    if stripe_frame.locator(card_input).first.is_visible(timeout=2000):
                        human_typing(stripe_frame, card_input, card.card_number)
                        for exp_name in ('input[name="cardExpiry"]', 'input[name="exp-date"]', 'input[name="expiry"]'):
                            try:
                                if stripe_frame.locator(exp_name).first.is_visible(timeout=1000):
                                    human_typing(stripe_frame, exp_name, card.expiry_display)
                                    break
                            except Exception:
                                continue
                        for cvc_name in ('input[name="cardCvc"]', 'input[name="cvc"]'):
                            try:
                                if stripe_frame.locator(cvc_name).first.is_visible(timeout=1000):
                                    human_typing(stripe_frame, cvc_name, card.cvv)
                                    break
                            except Exception:
                                continue
                        logger.info("使用 unified_iframe 形态填写完成。")
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    return False


if __name__ == "__main__":
    main()
