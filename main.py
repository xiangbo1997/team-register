# -*- coding: utf-8 -*-
"""
主入口 — 组装模块并启动任务

从 src 包导入各业务模块，加载配置并校验后执行。
"""

import random
import string
import csv
import datetime
import os
import re
import time
from typing import Callable, Optional
from playwright.sync_api import sync_playwright, BrowserContext, Page, Frame
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.automation import (
    ArtifactRecorder,
    AutomationRuntime,
    AutomationState,
    ExperienceStore,
    LLMDecisionProvider,
    OpenAICompatibleLLMClient,
    RegistrationStateMachine,
    extract_session_tokens_with_http,
)
from src.config import load_config, AppConfig
from src.efuncard import EfunCard
from src.nodecard import NodeCard
from src.sms import SMSManager
from src.mail import MailManager
from src.browser import get_browser_ws, run_preflight_checks
from src.utils import setup_logger, human_delay
from src.payment_link import PaymentLinkGenerator

logger = setup_logger()

_EMAIL_SELECTOR = 'input#email-input, input[name="email"], input[type="email"]'
_PASSWORD_SELECTOR = 'input#password, input[name="password"], input[type="password"]'
_PHONE_SELECTOR = 'input[name="phoneNumber"]'
_VERIFICATION_INDICATORS = ['text="Consulta tu bandeja"', 'text="Check your email"', 'input[name="code"]']
_AUTH_HOST_MARKERS = ("auth.openai.com", "auth0.openai.com")
_COOKIE_ACCEPT_SELECTORS = (
    '#onetrust-accept-btn-handler',
    'button[data-testid*="cookie"][data-testid*="accept"]',
    'button:has-text("Aceptar todas")',
    'button:has-text("Accept all")',
    'button:has-text("允许所有")',
)
_SIGNUP_SELECTORS = (
    'a[href*="screen_hint=signup"]',
    'a[href*="/signup"]',
    'button[data-testid*="signup"]',
    'a[data-testid*="signup"]',
    'button:has-text("Registrarse gratuitamente")',
    'button:has-text("Sign up")',
    'a:has-text("Sign up")',
)
_PRIMARY_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button[data-action-button-primary="true"]',
    'button:has-text("Continue")',
    'button:has-text("Continuar")',
    'button:has-text("Siguiente")',
    'button:has-text("Finalizar")',
)
_DEFAULT_BILLING_PROFILE = {
    "country": "US",
    "line1": "350 5th Ave",
    "line2": "",
    "city": "New York",
    "state": "NY",
    "postal_code": "10118",
}
_HOSTED_CHECKOUT_PREFIX = "https://pay.openai.com/c/pay/"
_CHECKOUT_DECLINE_PATTERNS = (
    r"您的银行卡被拒绝了",
    r"银行卡被拒绝",
    r"Your card was declined",
    r"card was declined",
)

def export_success(email, password, access_token, refresh_token):
    file_exists = os.path.isfile("accounts.csv")
    try:
        with open("accounts.csv", "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Time", "Email", "Password", "AccessToken", "RefreshToken"])
            writer.writerow([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), email, password, access_token, refresh_token])
        logger.info(f"账号数据已成功导出: {email}")
    except Exception as e:
        logger.error(f"导出账号数据到 CSV 失败: {e}")



def human_typing(page, selector: str, text: str) -> None:
    """模拟人类打字速度"""
    # 如果 page 是 FrameLocator，不一定有 wait_for_selector 的直接写法，
    # 这里统一转换为 locator 操作
    target_locator = page.locator(selector).first if hasattr(page, "locator") else page.first
    target_locator.wait_for(state="visible", timeout=20000)
    target_locator.click() # 先点击聚焦
    human_delay(0.2, 0.8)
    # 使用 press_sequentially 模拟逐字敲击的延迟
    target_locator.press_sequentially(text, delay=random.randint(50, 150))
    logger.debug(f"填入内容到: {selector}")


def _checkout_link_kind(checkout_link: str) -> str:
    """区分 hosted / app checkout 链接，便于 long 模式做严格校验。"""
    value = str(checkout_link or "").strip()
    if value.startswith(_HOSTED_CHECKOUT_PREFIX):
        return "hosted"
    if "/checkout/openai_llc/" in value:
        return "app"
    return "unknown"


def _build_billing_profile(billing_profile: Optional[dict[str, str]] = None) -> dict[str, str]:
    """合并默认账单资料与调用方覆盖项。"""
    profile = dict(_DEFAULT_BILLING_PROFILE)
    if billing_profile:
        profile.update({k: str(v) for k, v in billing_profile.items() if v is not None})
    profile["country"] = str(profile.get("country", "US") or "US").strip().upper() or "US"
    for key in ("line1", "line2", "city", "state", "postal_code"):
        profile[key] = str(profile.get(key, "") or "").strip()
    return profile


def _clear_and_fill_checkout_input(page: Page, selector: str, value: str, *, timeout_ms: int = 1000) -> bool:
    """用于普通 checkout 输入框：先清空再填，降低浏览器 autofill 干扰。"""
    locator = page.locator(selector).first
    if not value:
        return False
    try:
        if locator.is_visible(timeout=timeout_ms):
            locator.click(timeout=3000)
            try:
                locator.fill("")
            except Exception:
                logger.debug("清空 checkout 输入框失败（忽略）: %s", selector)
            locator.fill(value)
            return True
    except Exception as exc:
        logger.debug("填写 checkout 输入框失败（忽略）: %s error=%s", selector, exc)
    return False


def _select_checkout_option(page: Page, selector: str, value: str, *, timeout_ms: int = 1000) -> bool:
    """用于 checkout 下拉框：统一包一层显式选择与异常兜底。"""
    locator = page.locator(selector).first
    if not value:
        return False
    try:
        if locator.is_visible(timeout=timeout_ms):
            locator.select_option(value=value)
            return True
    except Exception as exc:
        logger.debug("选择 checkout 下拉框失败（忽略）: %s=%s error=%s", selector, value, exc)
    return False


def _snapshot_checkout_billing_details(page: Page) -> dict[str, object]:
    """抓取支付页当前账单快照，供提交前/后校验与诊断使用。"""
    try:
        snapshot = page.evaluate(
            """() => {
                const getValue = (selector) => {
                    const el = document.querySelector(selector);
                    return el && typeof el.value !== "undefined" ? String(el.value || "") : "";
                };
                const isChecked = (selector) => {
                    const el = document.querySelector(selector);
                    return !!(el && "checked" in el && el.checked);
                };
                const body = document.body ? String(document.body.innerText || "") : "";
                let decline = "";
                const patterns = [
                    /您的银行卡被拒绝了/,
                    /银行卡被拒绝/,
                    /Your card was declined/i,
                    /card was declined/i,
                ];
                for (const pattern of patterns) {
                    const match = body.match(pattern);
                    if (match) {
                        decline = match[0];
                        break;
                    }
                }
                return {
                    email: getValue('input[name="email"]'),
                    billingName: getValue('input[name="billingName"]'),
                    billingCountry: getValue('select[name="billingCountry"]'),
                    billingAddressLine1: getValue('input[name="billingAddressLine1"]'),
                    billingAddressLine2: getValue('input[name="billingAddressLine2"]'),
                    billingLocality: getValue('input[name="billingLocality"]'),
                    billingPostalCode: getValue('input[name="billingPostalCode"]'),
                    billingAdministrativeArea: getValue('select[name="billingAdministrativeArea"]'),
                    termsAccepted: isChecked('input[name="termsOfServiceConsentCheckbox"]'),
                    decline_message: decline,
                };
            }"""
        )
        return snapshot if isinstance(snapshot, dict) else {}
    except Exception as exc:
        logger.debug("抓取 checkout 账单快照失败（忽略）: %s", exc)
        return {}


def _billing_snapshot_matches(
    snapshot: dict[str, object],
    expected_profile: dict[str, str],
    *,
    expected_email: str = "",
    expected_name: str = "",
) -> bool:
    """判断当前 checkout 表单回读值是否与期望一致。"""
    if not isinstance(snapshot, dict) or not snapshot:
        return False

    checks = {
        "billingCountry": str(expected_profile.get("country", "")).strip().upper(),
        "billingAddressLine1": str(expected_profile.get("line1", "")).strip(),
        "billingAddressLine2": str(expected_profile.get("line2", "")).strip(),
        "billingLocality": str(expected_profile.get("city", "")).strip(),
        "billingPostalCode": str(expected_profile.get("postal_code", "")).strip(),
        "billingAdministrativeArea": str(expected_profile.get("state", "")).strip(),
    }
    for field, expected in checks.items():
        actual = str(snapshot.get(field, "") or "").strip()
        if expected != actual:
            return False

    if expected_email:
        actual_email = str(snapshot.get("email", "") or "").strip()
        if actual_email and actual_email != expected_email.strip():
            return False

    if expected_name:
        actual_name = str(snapshot.get("billingName", "") or "").strip()
        if actual_name and actual_name != expected_name.strip():
            return False

    return bool(snapshot.get("termsAccepted"))


def _snapshot_for_event(snapshot: dict[str, object]) -> dict[str, object]:
    """压缩支付页快照，避免事件 payload 过大。"""
    keys = (
        "email",
        "billingName",
        "billingCountry",
        "billingAddressLine1",
        "billingAddressLine2",
        "billingLocality",
        "billingPostalCode",
        "billingAdministrativeArea",
        "termsAccepted",
        "decline_message",
    )
    return {key: snapshot.get(key) for key in keys if key in snapshot}


def _detect_checkout_decline_message(page: Page, *, snapshot: Optional[dict[str, object]] = None) -> str:
    """识别支付页是否已出现拒卡文案，优先复用已抓取的快照。"""
    candidate = str((snapshot or {}).get("decline_message", "") or "").strip()
    if candidate:
        return candidate

    try:
        body_text = str(page.locator("body").first.inner_text(timeout=3000) or "")
    except Exception:
        return ""

    for pattern in _CHECKOUT_DECLINE_PATTERNS:
        match = re.search(pattern, body_text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def _click_first_visible(page, selectors: tuple[str, ...], *, description: str, timeout_ms: int = 1500) -> bool:
    """按优先级点击第一个可见候选，优先走结构化 selector，文案仅作兜底。"""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            visible = locator.is_visible(timeout=timeout_ms)
            if visible is True:
                locator.click(timeout=5000)
                logger.info("%s: %s", description, selector)
                return True
        except Exception:
            continue
    return False


def _is_auth_url(url: str) -> bool:
    """判断当前 URL 是否已经进入 OpenAI/Auth0 鉴权页。"""
    current = str(url or "")
    return any(marker in current for marker in _AUTH_HOST_MARKERS)


def _is_home_page(url: str) -> bool:
    """判断是否已经回到 ChatGPT 主界面。"""
    current = str(url or "")
    return "chatgpt.com" in current and "auth" not in current


def _build_card_client(config: AppConfig) -> Optional[EfunCard | NodeCard]:
    """根据 CARD_PROVIDER 配置选择卡源客户端。"""
    if config.card_provider == "nodecard":
        logger.info("使用 NodeCard 作为虚拟卡供应商。")
        return NodeCard(
            base_url=config.nodecard_api_url,
            merchant_dict_id=config.nodecard_merchant_id or None,
            platform_id=config.nodecard_platform_id or None,
        )
    if config.efuncard_token:
        logger.info("使用 EfunCard 作为虚拟卡供应商。")
        return EfunCard(token=config.efuncard_token)
    return None


def _build_runtime_clients(config: AppConfig) -> tuple[Optional[EfunCard | NodeCard], SMSManager, MailManager]:
    """根据配置实例化运行时依赖。"""
    card_api = _build_card_client(config)
    sms_api = SMSManager(
        api_key=config.sms_api_key,
        country=config.sms_country,
        proxy=config.proxy,
    )
    mail_api = MailManager(
        base_url=config.mail_domain,
        refresh_token=config.mail_refresh_token,
        client_id=config.mail_client_id,
        proxy=config.proxy,
    )
    return card_api, sms_api, mail_api


def _get_or_create_page(context: BrowserContext) -> Page:
    """优先复用 AdsPower 已有页面，避免跟踪错标签页。"""
    if context.pages:
        page = context.pages[0]
        logger.info(f"复用 AdsPower 已有页面 (共 {len(context.pages)} 个标签页)")
    else:
        page = context.new_page()
        logger.info("AdsPower 无已有页面，创建新标签页")

    page.set_default_timeout(60000)
    return page


def _prepare_clean_start_page(context: BrowserContext) -> Page:
    """尽量把上下文整理成一个可重复的干净起点。"""
    logger.info("执行 clean-start：清理目标域残留标签页与 Cookie。")

    for candidate in list(context.pages):
        try:
            candidate_url = str(candidate.url or "")
        except Exception:
            candidate_url = ""

        is_target_tab = any(marker in candidate_url for marker in ("chatgpt.com", "auth.openai.com", "auth0.openai.com"))
        is_error_tab = candidate_url.startswith("chrome-error://") or "auth/error" in candidate_url
        if is_target_tab or is_error_tab:
            try:
                candidate.close()
            except Exception:
                logger.debug("关闭残留标签页失败（忽略）: %s", candidate_url)

    try:
        context.clear_cookies()
    except Exception as exc:
        logger.warning("清理 Cookie 失败，继续使用新标签页: %s", exc)

    page = context.new_page()
    page.set_default_timeout(60000)
    return page


def _open_signup_entry(page: Page, email: str) -> None:
    """打开入口页并完成邮箱提交。"""
    logger.info("尝试进入 OpenAI 注册流程...")
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    human_delay(5, 8)

    try:
        if _click_first_visible(page, _COOKIE_ACCEPT_SELECTORS, description="点击 Cookie 同意按钮"):
            human_delay(1, 2)

        if _click_first_visible(page, _SIGNUP_SELECTORS, description="点击首页注册入口"):
            human_delay(3, 5)
    except Exception as exc:
        logger.warning(f"处理弹窗或跳转时发生非致命错误: {exc}")

    logger.info("寻找邮箱输入框并填写...")
    try:
        page.wait_for_selector(_EMAIL_SELECTOR, state="visible", timeout=20000)
        human_typing(page, _EMAIL_SELECTOR, email)
        human_delay()
        page.keyboard.press("Enter")

        try:
            _click_first_visible(page, _PRIMARY_SUBMIT_SELECTORS, description="点击邮箱页继续按钮", timeout_ms=3000)
        except Exception:
            pass
    except PlaywrightTimeoutError:
        logger.error("未找到邮箱输入框。正在保存错误截图到 error_debug.png...")
        page.screenshot(path="error_debug.png")
        raise


def _find_auth_page(context: BrowserContext, current_page: Page) -> Page | None:
    """在当前标签页或其他标签页中寻找 auth 页面。"""
    if _is_auth_url(current_page.url):
        return current_page

    for candidate in context.pages:
        try:
            candidate_url = candidate.url
        except Exception:
            continue
        if _is_auth_url(candidate_url):
            if candidate is not current_page:
                logger.info(f"在其他标签页发现 auth 页面: {candidate_url}，切换过去。")
                candidate.bring_to_front()
            return candidate
    return None


def _wait_for_auth_page(context: BrowserContext, page: Page) -> Page:
    """等待页面从首页跳转到 OpenAI/Auth0 鉴权页。"""
    logger.info("等待页面从首页跳转到注册/登录页面...")
    for wait_i in range(60):
        current = page.url
        try:
            current = page.evaluate("window.location.href")
        except Exception:
            pass

        auth_page = _find_auth_page(context, page)
        if auth_page:
            if auth_page is page:
                logger.info(f"当前页面已跳转到: {current}")
            return auth_page

        if wait_i % 10 == 0 and wait_i > 0:
            logger.info(f"仍在等待跳转... (已等 {wait_i} 秒, 当前: {current}, 共 {len(context.pages)} 个标签页)")
        human_delay(0.8, 1.2)

    logger.warning(f"等待跳转超时 (60秒)，当前 URL: {page.url}，继续尝试...")
    return page


def _read_onboarding_metrics(page: Page) -> dict[str, int | bool]:
    """读取注册后 onboarding 问卷的结构化信号，避免依赖单一语言文案。"""
    try:
        raw = page.evaluate(
            """
            () => {
              const root = document.querySelector('main') || document.body;
              const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const isDisabled = (node) => Boolean(
                node.disabled || node.getAttribute('aria-disabled') === 'true'
              );
              const clickables = Array.from(root.querySelectorAll('button, [role="button"], [role="radio"]'))
                .filter(isVisible);
              const optionNodes = clickables.filter((node) => {
                const rect = node.getBoundingClientRect();
                const label = (node.innerText || node.getAttribute('aria-label') || '').trim();
                return !isDisabled(node) && rect.top < window.innerHeight * 0.72 && rect.height >= 24 && label.length > 0;
              });
              const footerNodes = clickables.filter((node) => {
                const rect = node.getBoundingClientRect();
                return !isDisabled(node) && rect.top >= window.innerHeight * 0.62 && rect.height >= 24;
              });
              const text = (root.innerText || '').toLowerCase();
              return {
                prompt_present: text.includes('chatgpt') && optionNodes.length >= 3 && footerNodes.length >= 1,
                option_count: optionNodes.length,
                footer_button_count: footerNodes.length,
              };
            }
            """
        )
    except Exception:
        raw = {}

    if not isinstance(raw, dict):
        return {"prompt_present": False, "option_count": 0, "footer_button_count": 0}
    return {
        "prompt_present": bool(raw.get("prompt_present")),
        "option_count": int(raw.get("option_count", 0) or 0),
        "footer_button_count": int(raw.get("footer_button_count", 0) or 0),
    }


def _click_onboarding_option(page: Page) -> str:
    """优先点击问卷选项区里的第一项，避免被底部继续/跳过按钮干扰。"""
    try:
        label = page.evaluate(
            """
            () => {
              const root = document.querySelector('main') || document.body;
              const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const isDisabled = (node) => Boolean(
                node.disabled || node.getAttribute('aria-disabled') === 'true'
              );
              const option = Array.from(root.querySelectorAll('button, [role="button"], [role="radio"]'))
                .filter(isVisible)
                .find((node) => {
                  const rect = node.getBoundingClientRect();
                  const text = (node.innerText || node.getAttribute('aria-label') || '').trim();
                  return !isDisabled(node) && rect.top < window.innerHeight * 0.72 && rect.height >= 24 && text.length > 0;
                });
              if (!option) {
                return '';
              }
              option.click();
              return (option.innerText || option.getAttribute('aria-label') || '').trim();
            }
            """
        )
        return str(label or "")
    except Exception:
        return ""


def _click_onboarding_footer_action(page: Page) -> str:
    """在继续按钮无法直接匹配时，退化为点击底部可用动作按钮。"""
    try:
        label = page.evaluate(
            """
            () => {
              const root = document.querySelector('main') || document.body;
              const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const isDisabled = (node) => Boolean(
                node.disabled || node.getAttribute('aria-disabled') === 'true'
              );
              const action = Array.from(root.querySelectorAll('button, [role="button"]'))
                .filter(isVisible)
                .find((node) => {
                  const rect = node.getBoundingClientRect();
                  return !isDisabled(node) && rect.top >= window.innerHeight * 0.62 && rect.height >= 24;
                });
              if (!action) {
                return '';
              }
              action.click();
              return (action.innerText || action.getAttribute('aria-label') || '').trim();
            }
            """
        )
        return str(label or "")
    except Exception:
        return ""


def _wait_for_profile_step_transition(page: Page, *, prompt_name: str) -> None:
    """在点击提交后短等页面切换，避免状态机立刻误判为还停留在原步骤。"""
    for _ in range(15):
        current_url = str(getattr(page, "url", "") or "")
        if prompt_name == "about-you" and "about-you" not in current_url:
            logger.info("about-you 页面已离开: %s", current_url)
            return
        if prompt_name == "onboarding" and not _read_onboarding_metrics(page).get("prompt_present", False):
            logger.info("onboarding 问卷已完成/跳过。")
            return
        human_delay(1, 1.4)

    logger.info("%s 页面暂未离开，交给状态机继续轮询。", prompt_name)


def _dismiss_home_welcome_modal(page: Page) -> bool:
    """关闭进入首页后的欢迎/提示弹窗，避免遮挡后续稳定化逻辑。"""
    dialog = page.locator('[role="dialog"]').first
    try:
        if dialog.is_visible(timeout=1200) is not True:
            return False
    except Exception:
        return False

    action = dialog.locator('button, [role="button"]').last
    try:
        if action.is_visible(timeout=800) is True:
            action.click(timeout=5000)
            logger.info("已关闭首页欢迎弹窗。")
            human_delay(0.5, 1)
            return True
    except Exception as exc:
        logger.debug("关闭首页欢迎弹窗失败（忽略）: %s", exc)
    return False


def _ordered_payment_variants(experience_store: ExperienceStore | None, checkout_url: str) -> list[str]:
    """优先复用最近成功的 Stripe 表单形态。"""
    variants = ["split_frames", "single_frame"]
    if not experience_store:
        return variants

    latest = experience_store.latest_event(
        category="payment",
        name="stripe_variant_success",
        location=checkout_url,
    )
    preferred = str((latest or {}).get("variant", "")).strip()
    if preferred in variants:
        return [preferred] + [item for item in variants if item != preferred]
    return variants


def _find_payment_input_target(
    page: Page,
    selectors: tuple[str, ...],
    *,
    timeout_ms: int = 1200,
) -> tuple[Frame | None, str]:
    """遍历真实 iframe，返回首个可见 Stripe 输入框及其命中的 selector。"""
    for frame in getattr(page, "frames", []):
        for selector in selectors:
            try:
                locator = frame.locator(selector).first
                if locator.is_visible(timeout=timeout_ms):
                    return frame, selector
            except Exception:
                continue
    return None, ""


def _find_payment_input_frame(page: Page, selectors: tuple[str, ...], *, timeout_ms: int = 1200) -> Frame | None:
    """兼容旧调用：仅返回命中输入框所在的 iframe。"""
    frame, _selector = _find_payment_input_target(page, selectors, timeout_ms=timeout_ms)
    return frame


def _wait_for_stripe_form(page: Page, *, timeout_sec: int = 30) -> None:
    """等待 Stripe Elements 实际挂载，兼容 split-frames 与 single-frame 两种形态。"""
    split_frame_selectors = (
        'input[name="cardnumber"]',
        'input[autocomplete="cc-number"]',
        'input[name="number"]',
        'input[name="exp-date"]',
        'input[autocomplete="cc-exp"]',
        'input[name="expiry"]',
        'input[name="cvc"]',
        'input[autocomplete="cc-csc"]',
        'input[name="verification_value"]',
    )
    single_frame_iframe_selectors = (
        'iframe[title="Secure payment input frame"]',
        'iframe[title*="payment" i]',
        'iframe[name*="__privateStripeFrame"]',
    )
    single_frame_input_selectors = (
        'input[name="cardNumber"]',
        'input[name="cardnumber"]',
        'input[name="number"]',
    )

    deadline = time.time() + max(timeout_sec, 1)
    last_hint = "未检测到 Stripe 输入框"

    while time.time() < deadline:
        if _find_payment_input_frame(page, split_frame_selectors, timeout_ms=250):
            return

        for iframe_sel in single_frame_iframe_selectors:
            try:
                stripe_frame = page.frame_locator(iframe_sel).first
            except Exception as exc:
                last_hint = str(exc)
                continue

            for input_sel in single_frame_input_selectors:
                try:
                    if stripe_frame.locator(input_sel).first.is_visible(timeout=250):
                        return
                except Exception as exc:
                    last_hint = str(exc)
                    continue

        # Stripe iframe 经常先出现再延迟注入 input，短轮询比一次性长超时更稳定。
        human_delay(0.5, 0.8)

    raise PlaywrightTimeoutError(f"等待 Stripe 支付表单加载超时: {last_hint}")


def _fill_checkout_contact_and_billing_details(
    page: Page,
    *,
    email: str = "",
    billing_profile: Optional[dict[str, str]] = None,
) -> tuple[bool, dict[str, object]]:
    """补齐 Stripe Checkout 的联系信息、账单地址与必要勾选项，并回读校验是否被 autofill 覆盖。"""
    profile = _build_billing_profile(billing_profile)
    latest_snapshot: dict[str, object] = {}

    for attempt in range(1, 3):
        manual_address_button = page.locator('button:has-text("手动输入地址")').first
        try:
            if manual_address_button.is_visible(timeout=800):
                manual_address_button.click(timeout=3000)
                human_delay(0.2, 0.4)
        except Exception:
            logger.debug("切换手动地址输入失败（忽略）。")

        if email:
            _clear_and_fill_checkout_input(page, 'input[name="email"]', email)

        # 国家切换通常会导致省州/邮编等联动重绘，先单独设置并等待稳定。
        if _select_checkout_option(page, 'select[name="billingCountry"]', profile["country"]):
            human_delay(0.3, 0.6)

        field_values = (
            ('input[name="billingAddressLine1"]', profile["line1"]),
            ('input[name="billingAddressLine2"]', profile.get("line2", "")),
            ('input[name="billingLocality"]', profile["city"]),
            ('input[name="billingPostalCode"]', profile["postal_code"]),
        )
        for selector, value in field_values:
            if value:
                _clear_and_fill_checkout_input(page, selector, value)

        if profile["state"]:
            _select_checkout_option(page, 'select[name="billingAdministrativeArea"]', profile["state"])

        terms_checkbox = page.locator('input[name="termsOfServiceConsentCheckbox"]').first
        try:
            if terms_checkbox.is_visible(timeout=1000) and not terms_checkbox.is_checked():
                terms_checkbox.check(force=True)
        except Exception:
            logger.debug("勾选业务条款失败（忽略）。")

        human_delay(0.3, 0.6)
        latest_snapshot = _snapshot_checkout_billing_details(page)
        if _billing_snapshot_matches(latest_snapshot, profile, expected_email=email):
            return True, latest_snapshot

        logger.warning(
            "账单资料第 %d/2 次回读不一致，疑似被 autofill/联动覆盖: %s",
            attempt,
            _snapshot_for_event(latest_snapshot),
        )

    return False, latest_snapshot


def _handle_post_signup_onboarding(page: Page) -> bool:
    """处理注册完成后的 onboarding 问卷页。"""
    metrics = _read_onboarding_metrics(page)
    if not metrics.get("prompt_present"):
        return False

    logger.info("检测到注册后 onboarding 问卷，尝试自动完成/跳过。")
    chosen_label = _click_onboarding_option(page)
    if chosen_label:
        logger.info("已选择 onboarding 选项: %s", chosen_label)
        human_delay(0.5, 1)

    clicked_primary = _click_first_visible(
        page,
        _PRIMARY_SUBMIT_SELECTORS,
        description="点击 onboarding 底部主按钮",
        timeout_ms=1200,
    )
    if not clicked_primary:
        footer_label = _click_onboarding_footer_action(page)
        if footer_label:
            logger.info("已点击 onboarding 底部动作: %s", footer_label)

    _wait_for_profile_step_transition(page, prompt_name="onboarding")
    return True


def _fill_about_you_form(page: Page) -> None:
    """处理年龄/生日确认页。"""
    if _handle_post_signup_onboarding(page):
        return

    logger.info("检测到 '确认年龄' 页面，填写姓名和年龄/生日...")
    if "about-you" not in str(page.url or ""):
        logger.info("about-you 表单开始前页面已跳转，跳过填写。")
        return

    first_name = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 7))).capitalize()
    last_name = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 8))).capitalize()
    full_name = f"{first_name} {last_name}"

    name_input = page.locator('input[name="name"][type="text"], input[name="name"], input[autocomplete="name"]').first
    try:
        if "about-you" in str(page.url or "") and name_input.is_visible(timeout=3000) and name_input.is_enabled(timeout=1000):
            name_input.fill("")
            name_input.fill(full_name)
            logger.info(f"已填写姓名: {full_name}")
    except Exception as exc:
        if "about-you" not in str(page.url or ""):
            logger.info("about-you 页面正在跳转，姓名输入阶段视为已完成。")
            return
        logger.warning(f"姓名填写失败: {exc}")

    age_input = page.locator('input[name="age"]').first
    try:
        has_age_input = age_input.is_visible(timeout=2000) and age_input.is_enabled(timeout=1000)
    except Exception:
        has_age_input = False

    if has_age_input:
        age = str(random.randint(25, 35))
        try:
            age_input.fill("")
            age_input.fill(age)
            logger.info(f"已填写年龄: {age}")
        except Exception as exc:
            if "about-you" not in str(page.url or ""):
                logger.info("about-you 页面在年龄填写时已跳转，继续后续流程。")
                return
            logger.warning(f"年龄填写失败: {exc}")
    else:
        logger.info("未找到年龄输入框，尝试处理日期选择器...")
        birth_day = str(random.randint(1, 28))
        birth_month = str(random.randint(1, 12))
        birth_year = str(random.randint(1990, 2000))
        formatted_birthdate = f"{int(birth_day):02d}/{int(birth_month):02d}/{birth_year}"
        try:
            birthdate_input = page.locator(
                'input[name="birthdate"], input[name="birthday"], input[autocomplete="bday"], '
                'input[inputmode="numeric"]'
            ).first
            segment = page.locator('[role="spinbutton"]').first

            if "about-you" in str(page.url or "") and birthdate_input.is_visible(timeout=1000):
                birthdate_input.fill(formatted_birthdate)
                logger.info(f"已填写生日: {formatted_birthdate}")
            elif "about-you" in str(page.url or "") and segment.is_visible(timeout=2000):
                segment.focus()
                page.keyboard.type(formatted_birthdate, delay=100)
                logger.info(f"已填写生日: {formatted_birthdate}")
        except Exception as exc:
            if "about-you" not in str(page.url or ""):
                logger.info("about-you 页面在生日填写时已跳转，继续后续流程。")
                return
            logger.warning(f"日期填写失败: {exc}")

    human_delay(1, 2)
    submit_btn = page.locator('button[type="submit"]').first
    try:
        if "about-you" in str(page.url or "") and submit_btn.is_visible(timeout=3000) and submit_btn.is_enabled(timeout=1000):
            submit_btn.click(timeout=5000)
            logger.info("已点击提交按钮")
            _wait_for_profile_step_transition(page, prompt_name="about-you")
            return
    except Exception as exc:
        if "about-you" not in str(page.url or ""):
            logger.info("about-you 提交时页面已跳转，按成功处理。")
            return
        logger.warning(f"about-you 提交按钮点击失败: {exc}")

    try:
        if "about-you" in str(page.url or ""):
            page.keyboard.press("Enter")
            logger.info("about-you 未找到稳定提交按钮，改用 Enter 提交。")
            _wait_for_profile_step_transition(page, prompt_name="about-you")
    except Exception as exc:
        if "about-you" not in str(page.url or ""):
            logger.info("about-you Enter 提交时页面已跳转，按成功处理。")
            return
        logger.warning(f"about-you Enter 提交失败: {exc}")


def _handle_email_verification_step(page: Page, mail_api: MailManager, email: str) -> bool:
    """处理邮箱验证码提交，返回是否应继续后续状态轮询。"""
    logger.info("发现邮箱验证页面，开始拉取验证码...")
    try:
        mail_code = mail_api.get_verification_code_via_browser(email=email, page=page, wait_timeout=120)
        if mail_code:
            logger.info(f"拉取成功: {mail_code}，正在填入...")
            code_input = page.locator(
                'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
            ).first
            if code_input.is_visible(timeout=3000):
                code_input.click()
                try:
                    code_input.fill("")
                    code_input.fill(mail_code)
                except Exception:
                    page.keyboard.press("Meta+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(mail_code)
            else:
                page.keyboard.type(mail_code)
            human_delay(2, 3)

            try:
                _click_first_visible(page, _PRIMARY_SUBMIT_SELECTORS, description="点击验证码页继续按钮", timeout_ms=3000)
            except Exception:
                pass

            logger.info("等待验证码提交后页面跳转...")
            for _ in range(30):
                if "email-verification" not in page.url:
                    logger.info(f"页面已跳转: {page.url}")
                    return True
                human_delay(1, 1.5)
            logger.warning("验证码提交后页面未跳转，继续轮询...")
            return True

        logger.error("未拉取到邮箱验证码，请在浏览器中【手动输入】验证码并点击确认...")
        for _ in range(60):
            if "email-verification" not in page.url:
                logger.info(f"检测到手动操作成功，页面已跳转: {page.url}")
                return True
            human_delay(2, 3)
        logger.warning("等待手动操作超时，流程可能停滞。")
        return False
    except Exception as exc:
        logger.error(f"邮箱验证步骤失败: {exc}")
        return False


def _complete_registration_flow(page: Page, mail_api: MailManager, email: str, password: str) -> None:
    """执行密码/邮箱验证/年龄页等注册状态流转。"""
    logger.info("开始处理注册阶段状态流转...")

    has_typed_password = False
    has_filled_about_you = False

    for _ in range(10):
        human_delay(2, 4)
        current_url = page.url
        logger.info(f"检查当前状态: {current_url}")

        if _is_home_page(current_url):
            logger.info("🎉 检测到 ChatGPT 主界面，跳出注册流水线！")
            break

        if page.locator(_PHONE_SELECTOR).count() > 0 or "onboarding" in current_url or "phone" in current_url:
            logger.info("到达手机号验证页面/或提示页面，退出当前轮询开始处理手机号。")
            break

        if "about-you" in current_url:
            accept_btn = page.locator('button:has-text("Aceptar"), button:has-text("Accept")')
            if not has_filled_about_you:
                if accept_btn.is_visible(timeout=1000):
                    accept_btn.click()
                    logger.info("检测到遗留确认弹窗，已点击 'Aceptar'")
                    has_filled_about_you = True
                    human_delay(5, 8)
                    continue

                _fill_about_you_form(page)
                has_filled_about_you = True
                human_delay(5, 8)
                continue

            if accept_btn.is_visible(timeout=2000):
                accept_btn.click()
                logger.info("已点击确认弹窗的 'Aceptar' 按钮")
                human_delay(5, 8)
            else:
                logger.info("about-you 页面仍未跳转，等待中...")
                human_delay(3, 5)
            continue

        is_password_page = "/password" in current_url or page.locator(_PASSWORD_SELECTOR).is_visible(timeout=2000)
        if is_password_page and not has_typed_password:
            logger.info("发现密码输入框，正在填写...")
            human_typing(page, _PASSWORD_SELECTOR, password)
            human_delay(1, 2)
            page.keyboard.press("Enter")
            has_typed_password = True
            human_delay(3, 5)
            continue
        if is_password_page and has_typed_password:
            logger.info("密码已填写过，等待页面跳转中...")
            human_delay(3, 5)
            continue

        is_email_page = "email-verification" in current_url
        if not is_email_page:
            is_email_page = any(page.locator(indicator).count() > 0 for indicator in _VERIFICATION_INDICATORS)
        if is_email_page:
            if not _handle_email_verification_step(page, mail_api, email):
                break
            continue

        logger.debug("未发现明确后续指引，继续等待页面加载...")
        human_delay(3, 5)


def _submit_password(page: Page, password: str) -> None:
    """填写密码并提交。"""
    logger.info("发现密码输入框，正在填写...")
    human_typing(page, _PASSWORD_SELECTOR, password)
    human_delay(1, 2)
    page.keyboard.press("Enter")


def _extract_session_tokens_with_retry(
    page: Page,
    context: BrowserContext,
    attempts: int = 3,
    proxy_url: str = "",
    experience_store: ExperienceStore | None = None,
) -> tuple[str, str]:
    """等待页面稳定后重试独立 session 提取，降低跳转竞态带来的空 token 概率。"""
    refresh_token = ""
    user_agent = "Mozilla/5.0"
    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        pass

    for attempt in range(1, max(attempts, 1) + 1):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        _dismiss_home_welcome_modal(page)

        cookies = context.cookies()
        has_session_cookie = any("next-auth.session-token" in str(item.get("name", "")) for item in cookies)
        if not has_session_cookie:
            logger.warning("第 %d/%d 次 session 提取前未发现 next-auth session cookie，继续等待。", attempt, attempts)
        try:
            access_token, latest_refresh = extract_session_tokens_with_http(
                cookies=cookies,
                user_agent=str(user_agent or "Mozilla/5.0"),
                proxy_url=proxy_url,
            )
        except Exception as exc:
            logger.warning("第 %d/%d 次 session 提取异常，按可重试 miss 处理: %s", attempt, attempts, exc)
            access_token, latest_refresh = "", refresh_token
        if latest_refresh:
            refresh_token = latest_refresh
        if experience_store:
            experience_store.record_event(
                category="session",
                name="extract_attempt",
                location=str(getattr(page, "url", "") or "https://chatgpt.com/"),
                payload={
                    "attempt": attempt,
                    "has_session_cookie": has_session_cookie,
                    "access_token_present": bool(access_token),
                    "refresh_token_present": bool(refresh_token),
                },
            )
        if access_token:
            return access_token, refresh_token
        if attempt >= attempts:
            break

        logger.warning("第 %d/%d 次 session 提取未拿到 AccessToken，等待页面稳定后重试。", attempt, attempts)
        human_delay(2, 4)
        try:
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        except Exception:
            logger.debug("session 提取重试时刷新首页失败，继续下一次尝试。")

    fallback_access, fallback_refresh = _extract_session_tokens(page, context)
    if experience_store:
        experience_store.record_event(
            category="session",
            name="extract_fallback",
            location=str(getattr(page, "url", "") or "https://chatgpt.com/"),
            payload={
                "access_token_present": bool(fallback_access),
                "refresh_token_present": bool(fallback_refresh or refresh_token),
            },
        )
    return fallback_access, fallback_refresh or refresh_token


def _recover_from_error_page(runtime: AutomationRuntime, _action) -> bool:
    """遇到 error 页后，清理上下文并新开标签页。"""
    logger.warning("检测到异常页，执行恢复流程。")
    try:
        if runtime.page:
            try:
                runtime.page.close()
            except Exception:
                pass

        runtime.context.clear_cookies()
    except Exception as exc:
        logger.warning("恢复流程清理 Cookie 失败: %s", exc)

    runtime.page = _prepare_clean_start_page(runtime.context)
    return True


def _manual_handoff(runtime: AutomationRuntime, payload: dict) -> bool:
    """
    人工接管兜底。

    当前实现会把浏览器置前并等待用户把状态推进到可继续的页面。
    """
    reason = payload.get("reason", "UNKNOWN")
    state = payload.get("state", "UNKNOWN")
    logger.warning("进入人工接管：state=%s, reason=%s", state, reason)
    logger.warning("请在浏览器中手动推进当前步骤，脚本将在 180 秒内轮询恢复。")

    try:
        runtime.page.bring_to_front()
    except Exception:
        pass

    for _ in range(60):
        current_url = str(getattr(runtime.page, "url", "") or "")
        if "challenge" not in current_url and "captcha" not in current_url and "phone" not in current_url:
            logger.info("检测到页面已离开阻塞状态，恢复自动化。")
            return True
        human_delay(3, 3.5)

    logger.warning("人工接管等待超时。")
    return False


def _build_llm_provider(config: AppConfig) -> LLMDecisionProvider | None:
    """按配置构造受限 LLM 决策器。"""
    if not config.llm_enabled:
        return None

    missing = config.validate(required_modules=["llm"])
    if missing:
        logger.warning("LLM 已启用但配置不完整，降级为纯规则模式: %s", ", ".join(missing))
        return None

    client = OpenAICompatibleLLMClient(
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        model=config.llm_model,
        timeout_ms=config.llm_timeout_ms,
    )
    return LLMDecisionProvider(
        client=client,
        confidence_threshold=config.llm_confidence_threshold,
    )


def _build_runtime_handlers(
    *,
    email: str,
    password: str,
) -> dict[str, Callable[[AutomationRuntime, object], bool]]:
    """构造状态机执行器需要的内部 handler。"""

    def enter_signup(runtime: AutomationRuntime, _action) -> bool:
        _open_signup_entry(runtime.page, email)
        runtime.page = _wait_for_auth_page(runtime.context, runtime.page)
        return True

    def submit_password(runtime: AutomationRuntime, _action) -> bool:
        _submit_password(runtime.page, password)
        return True

    def verify_email(runtime: AutomationRuntime, _action) -> bool:
        return _handle_email_verification_step(runtime.page, runtime.mail_api, email)

    def fill_about_you(runtime: AutomationRuntime, _action) -> bool:
        _fill_about_you_form(runtime.page)
        return True

    def wait_short(runtime: AutomationRuntime, _action) -> bool:
        human_delay(2, 3)
        return True

    return {
        "enter_signup": enter_signup,
        "submit_password": submit_password,
        "verify_email": verify_email,
        "fill_about_you": fill_about_you,
        "wait_short": wait_short,
        "recover_error": _recover_from_error_page,
        "manual_handoff": _manual_handoff,
    }


def _wait_for_home_page(page: Page) -> None:
    """等待鉴权完成，进入主界面。"""
    human_delay(5, 8)
    logger.info("等待进入 ChatGPT 主界面...")
    try:
        page.wait_for_url("**/chatgpt.com**", timeout=30000)
    except PlaywrightTimeoutError:
        logger.warning("页面未能进入到预期首页，可能会影响后续提取 Token。")


def _extract_session_tokens(page: Page, context: BrowserContext) -> tuple[str, str]:
    """从页面会话和 cookie 中提取 access/refresh token。"""
    logger.info("提取 AccessToken 和 RefreshToken...")

    access_token = ""
    try:
        session_data = page.evaluate("fetch('https://chatgpt.com/api/auth/session').then(r => r.json())")
        access_token = session_data.get("accessToken", "")
        if access_token:
            logger.info("成功提取到 AccessToken")
        else:
            logger.warning("请求 auth/session 成功，但未找到 accessToken。")
    except Exception as exc:
        logger.error(f"获取 AccessToken 失败: {exc}")

    refresh_token = ""
    for cookie in context.cookies():
        if "next-auth.session-token" in cookie["name"]:
            refresh_token = cookie["value"]
            break

    return access_token, refresh_token


def _complete_payment_flow(
    page: Page,
    card_api: EfunCard,
    cdk: str,
    access_token: str,
    plan_type: str = "team",
    proxy_url: str = "",
    link_return_mode: str = "app",
    aimizy_country: str = "SG",
    aimizy_currency: str = "SGD",
    email: str = "",
    billing_profile: Optional[dict[str, str]] = None,
    experience_store: ExperienceStore | None = None,
) -> None:
    """在获得 access token 后尝试完成订阅支付。"""
    if not access_token:
        logger.error("由于无 AccessToken，跳过支付流程。")
        return

    logger.info("准备生成支付订阅链接...")
    normalized_link_mode = str(link_return_mode or "app").strip().lower()
    if normalized_link_mode == "app":
        success, checkout_link = PaymentLinkGenerator.generate_short_link(
            access_token,
            plan_type,
            proxy=proxy_url or None,
            aimizy_country=aimizy_country,
            aimizy_currency=aimizy_currency,
        )
    else:
        success, checkout_link = PaymentLinkGenerator.generate_checkout_link(
            access_token,
            plan_type=plan_type,
            proxy=proxy_url or None,
            return_mode=normalized_link_mode,
            aimizy_country=aimizy_country,
            aimizy_currency=aimizy_currency,
        )
    if not success:
        logger.error(f"支付链接生成失败: {checkout_link}")
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="short_link_failed",
                location="https://pay.openai.com/",
                payload={"error": str(checkout_link), "return_mode": normalized_link_mode},
            )
        return

    link_kind = _checkout_link_kind(checkout_link)
    if normalized_link_mode == "long" and link_kind != "hosted":
        logger.error("要求 long 模式，但支付链路返回了非 hosted 链接: %s", checkout_link)
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_link_kind_mismatch",
                location=checkout_link,
                payload={"return_mode": normalized_link_mode, "link_kind": link_kind},
            )
        return

    try:
        logger.info(f"生成支付链接成功，正在新标签页打开: {checkout_link}")
        checkout_page = _open_checkout_page_in_new_tab(page, checkout_link)
        human_delay(5, 10)
        card = card_api.get_card(cdk)
        if not card:
            logger.error("任务中止: 未能获取可用虚拟卡。跳过支付流程。")
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="card_lookup_failed",
                    location=checkout_link,
                    payload={
                        "cdk_present": bool(cdk),
                        "provider_meta": dict(getattr(card_api, "last_lookup_meta", {}) or {}),
                    },
                )
            return

        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_loaded",
                location=checkout_link,
                payload={
                    "url": checkout_link,
                    "opened_in_new_tab": checkout_page is not page,
                    "return_mode": normalized_link_mode,
                    "link_kind": link_kind,
                },
            )

        logger.info("正在等待 Stripe 支付表单加载...")
        # 站内 checkout 的 Stripe Elements 可能需要较长加载时间
        _wait_for_stripe_form(checkout_page, timeout_sec=30)

        logger.info("正在定位支付表单并填写信用卡信息...")
        used_variant = ""
        for variant in _ordered_payment_variants(experience_store, checkout_link):
            if variant == "split_frames":
                card_number_frame, card_number_selector = _find_payment_input_target(
                    checkout_page,
                    ('input[name="cardnumber"]', 'input[autocomplete="cc-number"]',
                     'input[name="number"]', 'input[placeholder*="card number" i]'),
                    timeout_ms=3000,
                )
                expiry_frame, expiry_selector = _find_payment_input_target(
                    checkout_page,
                    ('input[name="exp-date"]', 'input[autocomplete="cc-exp"]',
                     'input[name="expiry"]', 'input[placeholder*="MM" i]'),
                    timeout_ms=3000,
                )
                cvc_frame, cvc_selector = _find_payment_input_target(
                    checkout_page,
                    ('input[name="cvc"]', 'input[autocomplete="cc-csc"]',
                     'input[name="verification_value"]', 'input[placeholder*="CVC" i]'),
                    timeout_ms=3000,
                )
                if card_number_frame and expiry_frame and cvc_frame:
                    human_typing(card_number_frame, card_number_selector, card.card_number)
                    human_typing(expiry_frame, expiry_selector, card.expiry_display)
                    human_typing(cvc_frame, cvc_selector, card.cvv)
                    used_variant = variant
                    break
            else:
                # 尝试多种 Stripe iframe 标题选择器
                for iframe_sel in (
                    'iframe[title="Secure payment input frame"]',
                    'iframe[title*="payment" i]',
                    'iframe[name*="__privateStripeFrame"]',
                ):
                    try:
                        stripe_frame = checkout_page.frame_locator(iframe_sel).first
                        for card_input_name in ('input[name="cardNumber"]', 'input[name="cardnumber"]', 'input[name="number"]'):
                            try:
                                if stripe_frame.locator(card_input_name).first.is_visible(timeout=2000):
                                    human_typing(stripe_frame, card_input_name, card.card_number)
                                    # 尝试多种有效期字段名
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
                                    used_variant = variant
                                    break
                            except Exception:
                                continue
                        if used_variant:
                            break
                    except Exception:
                        continue
                if used_variant:
                    break

        if experience_store:
            experience_store.record_event(
                category="payment",
                name="stripe_variant_attempt",
                location=checkout_link,
                payload={"variant": used_variant or "not_found"},
            )
        if not used_variant:
            logger.warning("未找到可用 Stripe 表单形态，支付流程中断。")
            return

        expected_profile = _build_billing_profile(billing_profile)
        expected_name = card.name_on_card or "OpenAI User"
        name_input = checkout_page.locator('input[name="billingName"]')
        try:
            if name_input.is_visible():
                _clear_and_fill_checkout_input(checkout_page, 'input[name="billingName"]', expected_name)
        except Exception:
            logger.debug("填写账单姓名失败（忽略），后续由回读校验兜底。")

        billing_ok, billing_snapshot = _fill_checkout_contact_and_billing_details(
            checkout_page,
            email=email,
            billing_profile=billing_profile,
        )
        final_snapshot = billing_snapshot or _snapshot_checkout_billing_details(checkout_page)
        billing_ok = billing_ok and _billing_snapshot_matches(
            final_snapshot,
            expected_profile,
            expected_email=email,
            expected_name=expected_name,
        )
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_form_snapshot_before_submit",
                location=checkout_link,
                payload={"variant": used_variant, "snapshot": _snapshot_for_event(final_snapshot)},
            )
        if not billing_ok:
            logger.warning("提交前账单资料回读不一致，停止提交以避免 autofill 污染: %s", _snapshot_for_event(final_snapshot))
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="billing_profile_mismatch",
                    location=checkout_link,
                    payload={"variant": used_variant, "snapshot": _snapshot_for_event(final_snapshot)},
                )
            return

        logger.info("点击订阅按钮...")
        submit_btn = checkout_page.locator('button[type="submit"]')
        if submit_btn.is_visible():
            submit_btn.click()
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="stripe_variant_success",
                    location=checkout_link,
                    payload={"variant": used_variant},
                )
        else:
            logger.warning("支付页未发现可提交按钮，停止支付流程。")
            return

        human_delay(1.2, 2.0)
        post_submit_snapshot = _snapshot_checkout_billing_details(checkout_page)
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_form_snapshot_after_submit",
                location=checkout_link,
                payload={"variant": used_variant, "snapshot": _snapshot_for_event(post_submit_snapshot)},
            )
        decline_message = _detect_checkout_decline_message(checkout_page, snapshot=post_submit_snapshot)
        if decline_message:
            logger.warning("支付页提交后直接返回拒卡文案: %s", decline_message)
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="card_declined",
                    location=checkout_link,
                    payload={
                        "variant": used_variant,
                        "message": decline_message,
                        "snapshot": _snapshot_for_event(post_submit_snapshot),
                    },
                )
            return

        logger.info("等待 3DS 验证/支付请求...")
        otp = card_api.wait_for_3ds(cdk)
        if not otp:
            timeout_snapshot = _snapshot_checkout_billing_details(checkout_page)
            decline_message = _detect_checkout_decline_message(checkout_page, snapshot=timeout_snapshot)
            if decline_message:
                logger.warning("等待 3DS 期间检测到拒卡文案: %s", decline_message)
                if experience_store:
                    experience_store.record_event(
                        category="payment",
                        name="card_declined",
                        location=checkout_link,
                        payload={
                            "variant": used_variant,
                            "message": decline_message,
                            "snapshot": _snapshot_for_event(timeout_snapshot),
                        },
                    )
                return
            logger.warning("3DS 获取超时或未发出 3DS，支付可能已经直接成功或被拒。")
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="three_ds_timeout",
                    location=checkout_link,
                    payload={"variant": used_variant, "snapshot": _snapshot_for_event(timeout_snapshot)},
                )
            return

        logger.info(f"捕获到 3DS 验证码: {otp}，尝试回填...")
        acs_frame = checkout_page.frame_locator('iframe[name^="acsFrame"]').first
        try:
            human_typing(
                acs_frame,
                'input[type="password"], input[name*="code"], input[name*="challenge"]',
                otp,
            )
            btn = acs_frame.locator('button[type="submit"], input[type="submit"]').first
            btn.click()
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="three_ds_submit_success",
                    location=checkout_link,
                    payload={"variant": used_variant},
                )
        except Exception as exc:
            logger.warning(f"3DS 回填失败，手动介入或页面结构不符: {exc}")
            if experience_store:
                experience_store.record_event(
                    category="payment",
                    name="three_ds_submit_failed",
                    location=checkout_link,
                    payload={"variant": used_variant, "error": str(exc)},
                )
    except PlaywrightTimeoutError:
        logger.warning("未找到信用卡输入框，支付流程中断。")
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_timeout",
                location=checkout_link if 'checkout_link' in locals() else "https://pay.openai.com/",
                payload={},
            )


def _generate_payment_link_only(
    access_token: str,
    *,
    plan_type: str = "team",
    proxy_url: str = "",
    return_mode: str = "long",
    aimizy_country: str = "SG",
    aimizy_currency: str = "SGD",
    experience_store: ExperienceStore | None = None,
) -> str:
    """仅生成 checkout 链接，不打开页面、不绑卡。"""
    if not access_token:
        logger.error("由于无 AccessToken，无法生成支付链接。")
        return ""

    success, checkout_link = PaymentLinkGenerator.generate_checkout_link(
        access_token,
        plan_type=plan_type,
        proxy=proxy_url or None,
        return_mode=return_mode,
        aimizy_country=aimizy_country,
        aimizy_currency=aimizy_currency,
    )
    if not success:
        logger.error("支付链接生成失败: %s", checkout_link)
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_link_failed",
                location="https://chatgpt.com/backend-api/payments/checkout",
                payload={
                    "plan_type": plan_type,
                    "return_mode": return_mode,
                    "error": str(checkout_link),
                },
            )
        return ""

    link_kind = _checkout_link_kind(checkout_link)
    if str(return_mode or "").strip().lower() == "long" and link_kind != "hosted":
        logger.error("要求 long 模式，但返回了非 hosted 链接: %s", checkout_link)
        if experience_store:
            experience_store.record_event(
                category="payment",
                name="checkout_link_kind_mismatch",
                location=checkout_link,
                payload={
                    "plan_type": plan_type,
                    "return_mode": return_mode,
                    "link_kind": link_kind,
                },
            )
        return ""

    logger.info("支付链接生成成功: plan=%s mode=%s kind=%s link=%s", plan_type, return_mode, link_kind, checkout_link)
    if experience_store:
        experience_store.record_event(
            category="payment",
            name="checkout_link_generated",
            location=checkout_link,
            payload={
                "plan_type": plan_type,
                "return_mode": return_mode,
                "link_kind": link_kind,
            },
        )
    return checkout_link


def _open_checkout_page_in_new_tab(page: Page, checkout_url: str) -> Page:
    """在同一浏览器上下文中打开支付新标签页，避免覆盖主流程页。"""
    context = getattr(page, "context", None)
    if not context or not hasattr(context, "new_page"):
        raise RuntimeError("当前页面缺少可复用 context，无法按要求在新 tab 打开支付页。")

    checkout_page = context.new_page()
    checkout_page.set_default_timeout(60000)
    checkout_page.goto(checkout_url, wait_until="domcontentloaded")
    try:
        checkout_page.bring_to_front()
    except Exception:
        pass
    return checkout_page


def run_task(
    config: AppConfig, 
    card_api: Optional[EfunCard], 
    sms_api: SMSManager, 
    mail_api: MailManager,
    ads_id: str, 
    cdk: str, 
    email: str, 
    password: str
) -> None:
    """执行自动化注册及绑卡主流程"""
    reconnect_attempts = 0
    llm_provider = _build_llm_provider(config)

    while reconnect_attempts <= config.max_profile_reconnects:
        try:
            try:
                run_preflight_checks(
                    ads_api=config.ads_api,
                    target_url="https://chatgpt.com/",
                    proxy_url=config.proxy,
                )
            except Exception as exc:
                logger.warning("启动前检查失败，降级为直接调用浏览器启动接口: %s", exc)
            ws_url = get_browser_ws(
                ads_api=config.ads_api,
                user_id=ads_id,
                api_key=config.ads_api_key
            )
        except Exception as exc:
            logger.error("终止任务：启动前检查/连接 AdsPower 失败 (%s)", exc)
            return

        with sync_playwright() as p:
            try:
                logger.info("连接到 Playwright 浏览器实例...")
                browser = p.chromium.connect_over_cdp(ws_url)
                context: BrowserContext = browser.contexts[0]
                page = _prepare_clean_start_page(context)

                recorder = ArtifactRecorder(config.run_artifacts_dir)
                run_id = recorder.start_run(email)
                experience_store = ExperienceStore(os.path.join(config.run_artifacts_dir, "experience-memory.jsonl"))
                runtime = AutomationRuntime(
                    page=page,
                    context=context,
                    config=config,
                    email=email,
                    password=password,
                    mail_api=mail_api,
                    logger=logger,
                    handlers=_build_runtime_handlers(email=email, password=password),
                    artifact_recorder=recorder,
                    run_id=run_id,
                    llm_provider=llm_provider,
                    experience_store=experience_store,
                )

                machine = RegistrationStateMachine()
                result = machine.run(runtime)
                if not result.success:
                    logger.error(
                        "注册状态机失败: state=%s reason=%s",
                        result.final_state.value,
                        result.failure_reason,
                    )
                    if reconnect_attempts < config.max_profile_reconnects and result.final_state in {AutomationState.ERROR, AutomationState.UNKNOWN}:
                        reconnect_attempts += 1
                        logger.warning("尝试重连 AdsPower / 重新开始流程 (第 %d 次)...", reconnect_attempts)
                        continue
                    return

                logger.info("状态机检测到主流程已进入 HOME，开始提取 session。")
                access_token, refresh_token = _extract_session_tokens_with_retry(
                    page,
                    context,
                    proxy_url=config.proxy,
                    experience_store=experience_store,
                )
                if not access_token:
                    logger.error("未能提取到 AccessToken，本次不记为成功。")
                    if reconnect_attempts < config.max_profile_reconnects:
                        reconnect_attempts += 1
                        logger.warning("会话提取失败，尝试重新运行主流程 (第 %d 次)...", reconnect_attempts)
                        continue
                    return

                export_success(email, password, access_token, refresh_token)

                if config.enable_payment_flow:
                    if config.payment_link_only:
                        _generate_payment_link_only(
                            access_token,
                            plan_type=config.payment_plan,
                            proxy_url=config.proxy,
                            return_mode=config.payment_link_return_mode,
                            aimizy_country=config.aimizy_country,
                            aimizy_currency=config.aimizy_currency,
                            experience_store=experience_store,
                        )
                    else:
                        if not card_api:
                            logger.error("未提供可用 EfunCard 客户端，无法继续完整支付流程。")
                            return
                        _complete_payment_flow(
                            page,
                            card_api,
                            cdk,
                            access_token,
                            plan_type=config.payment_plan,
                            proxy_url=config.proxy,
                            link_return_mode=config.payment_link_return_mode,
                            aimizy_country=config.aimizy_country,
                            aimizy_currency=config.aimizy_currency,
                            email=email,
                            billing_profile=config.build_billing_profile(),
                            experience_store=experience_store,
                        )
                else:
                    logger.info("ENABLE_PAYMENT_FLOW=false，跳过支付阶段。")

                logger.info("自动化任务执行完毕。保持浏览器开启状态供检查。")
                human_delay(10, 15)
                return

            except Exception as exc:
                logger.error(f"执行过程中发生未捕获异常: {exc}")
                if reconnect_attempts < config.max_profile_reconnects:
                    reconnect_attempts += 1
                    logger.warning("出现未捕获异常，尝试重新启动主流程 (第 %d 次)...", reconnect_attempts)
                    continue
                return


def main() -> None:
    """应用主入口"""
    # 1. 加载配置
    config = load_config()

    # 2. 校验所需模块的配置完整性
    required_modules = ["sms", "mail", "ads"]
    if config.enable_payment_flow and not config.payment_link_only and config.card_provider != "nodecard":
        required_modules.insert(0, "efuncard")

    missing = config.validate(required_modules=required_modules)
    if missing:
        logger.error("配置不完整，缺少以下环境变量: %s", ", ".join(missing))
        logger.error("请检查 .env 文件或 .env.example 的说明。")
        return

    # 3. 初始化业务模块
    card_api, sms_api, mail_api = _build_runtime_clients(config)

    logger.info("所有模块初始化完成。")

    # 4. 执行具体业务逻辑
    # 从配置中读取测试参数
    if not all([config.task_ads_id, config.task_email, config.task_password]):
        logger.warning("跳过主流程：未在 .env 中配置完整的 TASK_ 变量 (TASK_ADS_ID, TASK_EMAIL, TASK_PASSWORD)。")
    else:
        logger.info("检测到测试配置，准备运行主流程 (即将启动浏览器)...")
        run_task(
            config=config,
            card_api=card_api,
            sms_api=sms_api,
            mail_api=mail_api,
            ads_id=config.task_ads_id,
            cdk=config.task_cdk,
            email=config.task_email,
            password=config.task_password
        )


if __name__ == "__main__":
    main()
