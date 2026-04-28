# -*- coding: utf-8 -*-
"""
状态机 Handler 集合

从 main.py 提取的页面交互函数，供 PhaseOrchestrator 组装到 AutomationRuntime。
这些函数保持原有逻辑不变，仅将选择器常量替换为 selectors 模块引用。
"""

from __future__ import annotations

import random
import string
import logging
import time
from typing import Any, Callable, Optional

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.automation import AutomationRuntime
from src.models import CardInfo
from src.utils import human_delay
from src.workflow import humanize as _humanize
from src.orchestration.selectors import (
    AUTH_HOST_MARKERS,
    COOKIE_ACCEPT_SELECTORS,
    EMAIL_SELECTOR,
    PASSWORD_SELECTOR,
    PHONE_SELECTOR,
    PRIMARY_SUBMIT_SELECTORS,
    SIGNUP_SELECTORS,
    VERIFICATION_INDICATORS,
)

logger = logging.getLogger(__name__)


# ── 工具函数 ──────────────────────────────────────


def _humanize_enabled(runtime) -> bool:
    """读取 runtime.config.humanize_enabled，默认 True。"""
    if runtime is None:
        return False
    config = getattr(runtime, "config", None)
    if config is None:
        return True
    return bool(getattr(config, "humanize_enabled", True))


def _click(runtime, selector: str) -> None:
    """User-input 点击封装：根据配置在拟人化点击与原生点击间切换。"""
    if _humanize_enabled(runtime):
        _humanize.click_humanized(runtime.page, selector)
    else:
        page = runtime.page if runtime is not None else None
        if page is None:
            raise ValueError("_click requires runtime.page")
        page.locator(selector).first.click()


def _fill(runtime, selector: str, value: str) -> None:
    """User-input 填充封装：根据配置在拟人化键入与原生 fill 间切换。"""
    if _humanize_enabled(runtime):
        _humanize.type_humanized(runtime.page, selector, value)
    else:
        page = runtime.page if runtime is not None else None
        if page is None:
            raise ValueError("_fill requires runtime.page")
        page.locator(selector).first.fill(value)


def human_typing(page, selector: str, text: str, *, runtime=None) -> None:
    """模拟人类打字速度。

    当传入 ``runtime`` 且 ``runtime.config.humanize_enabled`` 为真时，
    走贝塞尔/非匀速拟人化路径；否则保留原始 ``press_sequentially`` 逻辑，
    以兼容 Stripe iframe 等受限上下文以及直接传 page 的旧调用位点。
    """
    if runtime is not None and _humanize_enabled(runtime):
        _humanize.type_humanized(page, selector, text)
        return
    target_locator = page.locator(selector).first if hasattr(page, "locator") else page.first
    target_locator.wait_for(state="visible", timeout=20000)
    target_locator.click()
    human_delay(0.2, 0.8)
    target_locator.press_sequentially(text, delay=random.randint(50, 150))


def click_first_visible(page, selectors: tuple[str, ...], *, description: str, timeout_ms: int = 1500) -> bool:
    """按优先级点击第一个可见候选。"""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=timeout_ms) is True:
                locator.click(timeout=5000)
                logger.info("%s: %s", description, selector)
                return True
        except Exception:
            continue
    return False


def is_auth_url(url: str) -> bool:
    """判断是否在 OpenAI/Auth0 鉴权页。"""
    return any(marker in str(url or "") for marker in AUTH_HOST_MARKERS)


def is_home_page(url: str) -> bool:
    """判断是否在 ChatGPT 主界面。"""
    current = str(url or "")
    return "chatgpt.com" in current and "auth" not in current


# ── 页面准备 ──────────────────────────────────────


def prepare_clean_start_page(context: BrowserContext) -> Page:
    """整理上下文为干净起点。"""
    logger.info("执行 clean-start：清理目标域残留标签页与 Cookie。")
    for candidate in list(context.pages):
        try:
            candidate_url = str(candidate.url or "")
        except Exception:
            candidate_url = ""
        is_target = any(m in candidate_url for m in ("chatgpt.com", "auth.openai.com", "auth0.openai.com"))
        is_error = candidate_url.startswith("chrome-error://") or "auth/error" in candidate_url
        if is_target or is_error:
            try:
                candidate.close()
            except Exception:
                pass
    try:
        context.clear_cookies()
    except Exception as exc:
        logger.warning("清理 Cookie 失败: %s", exc)
    page = context.new_page()
    page.set_default_timeout(60000)
    return page


# 仅清理 OpenAI 鉴权域 —— 不动 chatgpt.com 主域。
# 主域 cookies（如 Cloudflare _cf_bm、ChatGPT 自身的 oai-did）是 SPA 渲染所需的
# anti-bot 凭证，全清会导致登录选项页直接白屏（实测：截图全白、HTML 154 处 sso/70 处
# login_with，但 React 不渲染）。仅清 auth/auth0 域既能扫掉旧登录态，又能保留 SPA。
_WARMUP_AUTH_DOMAINS = (
    "auth.openai.com",
    "auth0.openai.com",
    ".openai.com",
)

# 仅在 auth 域执行的彻底清理：localStorage / sessionStorage / IndexedDB /
# cacheStorage / 注销 service worker。chatgpt.com 主域不动。
_WARMUP_STORAGE_CLEAN_JS = """
async () => {
    const errors = [];
    try { localStorage.clear(); } catch (e) { errors.push('ls:' + e.message); }
    try { sessionStorage.clear(); } catch (e) { errors.push('ss:' + e.message); }
    try {
        if (window.indexedDB && indexedDB.databases) {
            const dbs = await indexedDB.databases();
            for (const db of dbs || []) {
                try { if (db.name) indexedDB.deleteDatabase(db.name); } catch (e) {}
            }
        }
    } catch (e) { errors.push('idb:' + e.message); }
    try {
        if (window.caches) {
            const keys = await caches.keys();
            for (const k of keys) { try { await caches.delete(k); } catch (e) {} }
        }
    } catch (e) { errors.push('caches:' + e.message); }
    try {
        if (navigator.serviceWorker) {
            const regs = await navigator.serviceWorker.getRegistrations();
            for (const r of regs) { try { await r.unregister(); } catch (e) {} }
        }
    } catch (e) { errors.push('sw:' + e.message); }
    return errors;
}
"""


# 访客视图（未登录）特征 selector 集合 — ChatGPT 主页未登录时会渲染这些登录/注册按钮。
# 任一命中即视作未登录访客视图。集中放在模块级常量便于未来 ChatGPT UI 更新时一处统一维护。
_CHATGPT_LOGIN_BUTTON_SELECTORS = (
    "[data-testid='login-button']",
    "[data-testid='signup-button']",
    "button:has-text('Sign up for free')",
    "button:has-text('Log in to another account')",
)
_CHATGPT_LOGIN_BUTTON_LOCATOR = ", ".join(_CHATGPT_LOGIN_BUTTON_SELECTORS)


def is_chatgpt_logged_in(page: Page, *, timeout_ms: int = 3000) -> bool:
    """检查 ChatGPT 当前 page 是否处于已登录状态。

    判定方式（任一命中失败 → 视作未登录）：
      1. URL 不在 auth.openai.com 域（排除 ``"auth" in url AND "chatgpt.com" not in url``）
      2. page.locator(login/signup 按钮).count() == 0

    Args:
        page: 已加载 ChatGPT 主页的 Page。本函数**不**主动 goto，调用方应在调用前
            完成导航（典型用法：``page.goto("https://chatgpt.com/")`` 后立即调用）。
        timeout_ms: 单次 locator.count() 等待 SPA 渲染的超时（默认 3s，足够覆盖
            chatgpt.com 主页的初始渲染；过短会把"渲染未完成"误判成"已登录"）。

    Returns:
        True  = 已登录，可直接进入 Upgrade 流程
        False = 未登录或登录态不明（应当走完整登录流程或降级处理）

    设计注意：
      - 本函数**容忍异常** — locator 出错也按"未登录"处理，确保 caller 总能拿到布尔值。
      - 不修改 page 状态（不点击、不 goto、不 evaluate JS），纯只读。
      - 与 ``scripts/verify_warmup_account.py`` 共用同一份 selector 常量，避免双处实现漂移。
    """
    # 1) URL 检查：在 auth.openai.com 域且不在 chatgpt.com → 显然未登录（OAuth 跳转中）
    try:
        cur_url = str(page.url or "")
    except Exception:
        return False
    if "auth" in cur_url and "chatgpt.com" not in cur_url:
        return False

    # 2) DOM 检查：页面有任何 login/signup 按钮 → 访客视图，未登录
    try:
        login_button_count = page.locator(_CHATGPT_LOGIN_BUTTON_LOCATOR).count()
    except Exception as exc:
        # locator 异常 → 保守按"未登录"处理，让 caller 走完整登录路径
        logger.debug("is_chatgpt_logged_in: locator 异常按未登录处理: %s", exc)
        return False
    return login_button_count == 0


def prepare_clean_warmup_page(context: BrowserContext) -> Page:
    """为预热路径准备相对干净的 page，但**保留 ChatGPT 主域 anti-bot cookies**。

    清理范围：
      - 关闭 chatgpt.com / auth.openai.com / auth0.openai.com 残留 tab
      - 仅按域清 cookies：``auth.openai.com`` / ``auth0.openai.com`` /
        ``.openai.com``（chatgpt.com 主域 cookies 保留）
      - 仅在 auth.openai.com 域执行 localStorage / sessionStorage /
        IndexedDB / Cache Storage / Service Worker 清理

    保留 chatgpt.com 主域 cookies / storage 的原因：
      ChatGPT 登录选项页 ``chatgpt.com/auth/login_with`` 是个 React SPA，
      渲染时强依赖 Cloudflare 颁发的 ``_cf_bm`` 与 ChatGPT 自己的 ``oai-did``
      等 anti-bot cookies。一旦清光，SPA 拒绝渲染、整页白屏，
      连 "Continue with email" 选项都没机会点。

    Returns:
        新 Page。调用方应直接用作登录起点（无需再次 new_page）。
    """
    logger.info("执行 warmup clean-start：仅清 auth 域 cookies + storage（保留 chatgpt 主域）。")
    # 第一步：关闭目标域残留 tab（cookie 不再全清）
    for candidate in list(context.pages):
        try:
            candidate_url = str(candidate.url or "")
        except Exception:
            candidate_url = ""
        is_target = any(m in candidate_url for m in ("chatgpt.com", "auth.openai.com", "auth0.openai.com"))
        is_error = candidate_url.startswith("chrome-error://") or "auth/error" in candidate_url
        if is_target or is_error:
            try:
                candidate.close()
            except Exception:
                pass

    # 第二步：按域清 cookies。Playwright 的 ``context.clear_cookies(domain=...)``
    # 用前缀匹配；传 ``.openai.com`` 会清所有 ``*.openai.com`` cookie 但 chatgpt.com
    # 不属于 *.openai.com，所以这一行安全。auth.openai.com / auth0.openai.com 单独
    # 再补两次，是因为有些 cookie 的 domain 字段可能不带前缀点。
    for domain in _WARMUP_AUTH_DOMAINS:
        try:
            context.clear_cookies(domain=domain)
        except TypeError:
            # 老版本 playwright 不支持 domain 参数 —— 直接放弃域过滤是不安全的
            # （会清光所有 cookie），所以这里宁可不清。
            logger.warning(
                "warmup clean: playwright 版本不支持 clear_cookies(domain=...)，"
                "跳过该域 cookie 清理（不再 fallback 到全清避免 SPA 白屏）"
            )
            break
        except Exception as exc:
            logger.warning("warmup clean: 清 %s cookies 异常（继续）: %s", domain, exc)

    page = context.new_page()
    page.set_default_timeout(60000)

    # 第三步：仅在 auth.openai.com 域执行 storage / SW 清理。chatgpt.com 主域不动。
    try:
        page.goto("https://auth.openai.com/", wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        logger.warning("warmup clean: 访问 auth.openai.com 失败（跳过 storage 清理）: %s", exc)
    else:
        try:
            errors = page.evaluate(_WARMUP_STORAGE_CLEAN_JS)
            if errors:
                logger.info("warmup clean auth: storage 清理部分失败（忽略）: %s", errors)
            else:
                logger.info("warmup clean auth: storage / SW 清理完成")
        except Exception as exc:
            logger.warning("warmup clean auth: evaluate 异常（继续）: %s", exc)

    # 第四步：导航到 about:blank，让登录从干净起点开始
    try:
        page.goto("about:blank", timeout=5000)
    except Exception:
        pass

    return page


# ── 注册流程 Handler ──────────────────────────────


def open_signup_entry(page: Page, email: str, *, runtime: AutomationRuntime | None = None) -> None:
    """打开入口页并完成邮箱提交。"""
    logger.info("尝试进入 OpenAI 注册流程...")
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    human_delay(5, 8)
    try:
        if click_first_visible(page, COOKIE_ACCEPT_SELECTORS, description="点击 Cookie 同意按钮"):
            human_delay(1, 2)
        if click_first_visible(page, SIGNUP_SELECTORS, description="点击首页注册入口"):
            human_delay(3, 5)
    except Exception as exc:
        logger.warning("处理弹窗或跳转时发生非致命错误: %s", exc)

    logger.info("寻找邮箱输入框并填写...")
    try:
        page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=20000)
        human_typing(page, EMAIL_SELECTOR, email, runtime=runtime)
        human_delay()
        page.keyboard.press("Enter")
        try:
            click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击邮箱页继续按钮", timeout_ms=3000)
        except Exception:
            pass
    except PlaywrightTimeoutError:
        logger.error("未找到邮箱输入框。")
        page.screenshot(path="error_debug.png")
        raise


def find_auth_page(context: BrowserContext, current_page: Page) -> Page | None:
    """在标签页中寻找 auth 页面。"""
    if is_auth_url(current_page.url):
        return current_page
    for candidate in context.pages:
        try:
            candidate_url = candidate.url
        except Exception:
            continue
        if is_auth_url(candidate_url):
            if candidate is not current_page:
                logger.info("在其他标签页发现 auth 页面，切换过去。")
                candidate.bring_to_front()
            return candidate
    return None


def wait_for_auth_page(context: BrowserContext, page: Page) -> Page:
    """等待跳转到鉴权页。"""
    logger.info("等待页面跳转到注册/登录页面...")
    for wait_i in range(60):
        auth_page = find_auth_page(context, page)
        if auth_page:
            return auth_page
        if wait_i % 10 == 0 and wait_i > 0:
            logger.info("仍在等待跳转... (已等 %d 秒)", wait_i)
        human_delay(0.8, 1.2)
    logger.warning("等待跳转超时 (60秒)，继续尝试...")
    return page


def submit_password(page: Page, password: str, *, runtime: AutomationRuntime | None = None) -> None:
    """填写密码并提交。"""
    logger.info("发现密码输入框，正在填写...")
    human_typing(page, PASSWORD_SELECTOR, password, runtime=runtime)
    human_delay(1, 2)
    page.keyboard.press("Enter")


def handle_email_verification_step(page: Page, mail_api, email: str, *, runtime: AutomationRuntime | None = None) -> bool:
    """处理邮箱验证码提交。"""
    logger.info("发现邮箱验证页面，开始拉取验证码...")
    try:
        mail_code = mail_api.get_verification_code_via_browser(email=email, page=page, wait_timeout=120)
        if mail_code:
            logger.info("拉取成功: %s，正在填入...", mail_code)
            code_selector = 'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
            code_input = page.locator(code_selector).first
            if code_input.is_visible(timeout=3000):
                # 直接 fill：Playwright fill 会自动 focus 并清空再写入，
                # 比先 click+fill("") 更宽松（避开了 click 的全套 actionability 检查，
                # 实测 OpenAI 验证码页 click 会卡 60s timeout）。
                # 设短 timeout (5s)：fill 失败立刻降级到键盘输入，避免一次卡死整轮。
                try:
                    if runtime is not None and _humanize_enabled(runtime):
                        _humanize.type_humanized(page, code_selector, mail_code)
                    else:
                        code_input.fill(mail_code, timeout=5000)
                except Exception as exc:
                    logger.warning("input.fill 失败 (%s)，降级到键盘输入", exc)
                    try:
                        try:
                            code_input.focus(timeout=3000)
                        except Exception:
                            pass
                        page.keyboard.press("Meta+A")
                        page.keyboard.press("Backspace")
                        page.keyboard.type(mail_code)
                    except Exception as exc2:
                        logger.error("键盘输入也失败: %s", exc2)
                        raise
            else:
                page.keyboard.type(mail_code)
            human_delay(2, 3)
            # 提交验证码：先 Enter 兜底，再点 Continue 按钮（任意一个生效都行）。
            # 旧逻辑只点按钮，碰到新版 OpenAI 风控延迟 enable Continue 按钮的场景会
            # 卡在表单页 timeout。Enter 在 OpenAI 表单都支持。
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            try:
                click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击验证码页继续按钮", timeout_ms=3000)
            except Exception:
                pass
            for _ in range(30):
                if "email-verification" not in page.url:
                    logger.info("页面已跳转: %s", page.url)
                    return True
                human_delay(1, 1.5)
            return True
        logger.error("未拉取到邮箱验证码，请手动输入...")
        for _ in range(60):
            if "email-verification" not in page.url:
                logger.info("检测到手动操作成功。")
                return True
            human_delay(2, 3)
        return False
    except Exception as exc:
        # 失败原因透传：让 _classify_failure (warmup.py) 能识别 mailbox-service /
        # 5xx / PROVIDER_NOT_CONFIGURED 等关键词，区分 external_failure 与
        # account_failure，避免邮件抖动连带 disable 整个 pro_warmup 号池。
        # logger.error 走 LogBroadcastHandler 把细节留给上层；同时记录到 runtime
        # 的 last_actions（如有），便于状态机追溯。
        sanitized = _summarize_mail_exception(exc)
        logger.error("邮箱验证步骤失败: %s", sanitized)
        if runtime is not None:
            try:
                runtime.last_actions.append({
                    "action": "handle_email_verification_step",
                    "result": "failed",
                    "reason": sanitized,
                })
            except Exception:
                pass
        return False


# ── mail 异常摘要工具 ──────────────────────────────────
# 用于失败原因透传：把 MailServiceError / MissingProviderConfigError /
# ProviderUpstreamError / ConnectionError 的关键信息提炼成一行字符串，
# 让 warmup._classify_failure 的关键词匹配规则能正确识别 external vs account。
_MAIL_EXCEPTION_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("MissingProviderConfigError", "PROVIDER_NOT_CONFIGURED"),
    ("ProviderUpstreamError", "PROVIDER_UPSTREAM_ERROR"),
    ("MailRuntimeIncompatibleError", "MAILBOX_RUNTIME_INCOMPAT"),
    ("MailServiceError", "MAILBOX_SERVICE_ERROR"),
)


def _summarize_mail_exception(exc: BaseException) -> str:
    """把 mail 异常摘要成一行，供失败 reason 透传。

    格式：``<HINT> mailbox-service: <类名>: <message>``。
    HINT 是 _classify_failure 能识别的关键词（mailbox-service / 5xx 等）。
    """
    type_name = type(exc).__name__
    hint = ""
    for marker, code in _MAIL_EXCEPTION_TYPE_HINTS:
        if marker == type_name or marker in type_name:
            hint = code
            break
    msg = str(exc)
    if len(msg) > 240:
        msg = msg[:237] + "..."
    parts = ["mailbox-service"]
    if hint:
        parts.append(hint)
    parts.append(f"{type_name}: {msg}")
    return " | ".join(parts)


def read_onboarding_metrics(page: Page) -> dict[str, int | bool]:
    """读取 onboarding 问卷结构化信号。"""
    try:
        raw = page.evaluate("""() => {
            const root = document.querySelector('main') || document.body;
            const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const isDisabled = (node) => Boolean(node.disabled || node.getAttribute('aria-disabled') === 'true');
            const clickables = Array.from(root.querySelectorAll('button, [role="button"], [role="radio"]')).filter(isVisible);
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
        }""")
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        return {"prompt_present": False, "option_count": 0, "footer_button_count": 0}
    return {
        "prompt_present": bool(raw.get("prompt_present")),
        "option_count": int(raw.get("option_count", 0) or 0),
        "footer_button_count": int(raw.get("footer_button_count", 0) or 0),
    }


def click_onboarding_option(page: Page) -> str:
    """点击问卷选项区第一项。"""
    try:
        label = page.evaluate("""() => {
            const root = document.querySelector('main') || document.body;
            const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const isDisabled = (node) => Boolean(node.disabled || node.getAttribute('aria-disabled') === 'true');
            const option = Array.from(root.querySelectorAll('button, [role="button"], [role="radio"]'))
                .filter(isVisible)
                .find((node) => {
                    const rect = node.getBoundingClientRect();
                    const text = (node.innerText || node.getAttribute('aria-label') || '').trim();
                    return !isDisabled(node) && rect.top < window.innerHeight * 0.72 && rect.height >= 24 && text.length > 0;
                });
            if (!option) return '';
            option.click();
            return (option.innerText || option.getAttribute('aria-label') || '').trim();
        }""")
        return str(label or "")
    except Exception:
        return ""


def click_onboarding_footer_action(page: Page) -> str:
    """点击底部动作按钮。"""
    try:
        label = page.evaluate("""() => {
            const root = document.querySelector('main') || document.body;
            const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const isDisabled = (node) => Boolean(node.disabled || node.getAttribute('aria-disabled') === 'true');
            const action = Array.from(root.querySelectorAll('button, [role="button"]'))
                .filter(isVisible)
                .find((node) => {
                    const rect = node.getBoundingClientRect();
                    return !isDisabled(node) && rect.top >= window.innerHeight * 0.62 && rect.height >= 24;
                });
            if (!action) return '';
            action.click();
            return (action.innerText || action.getAttribute('aria-label') || '').trim();
        }""")
        return str(label or "")
    except Exception:
        return ""


def fill_about_you_form(page: Page, *, runtime: AutomationRuntime | None = None) -> None:
    """处理年龄/生日确认页。"""
    humanize_on = _humanize_enabled(runtime)
    metrics = read_onboarding_metrics(page)
    if metrics.get("prompt_present"):
        logger.info("检测到 onboarding 问卷，自动完成/跳过。")
        chosen = click_onboarding_option(page)
        if chosen:
            logger.info("已选择 onboarding 选项: %s", chosen)
            human_delay(0.5, 1)
        clicked = click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击 onboarding 底部主按钮", timeout_ms=1200)
        if not clicked:
            footer = click_onboarding_footer_action(page)
            if footer:
                logger.info("已点击 onboarding 底部动作: %s", footer)
        _wait_for_profile_step_transition(page, prompt_name="onboarding")
        return

    logger.info("检测到 '确认年龄' 页面，填写姓名和年龄/生日...")
    if "about-you" not in str(page.url or ""):
        return

    first_name = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 7))).capitalize()
    last_name = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 8))).capitalize()
    full_name = f"{first_name} {last_name}"

    name_selector = 'input[name="name"][type="text"], input[name="name"], input[autocomplete="name"]'
    name_input = page.locator(name_selector).first
    try:
        if "about-you" in str(page.url or "") and name_input.is_visible(timeout=3000) and name_input.is_enabled(timeout=1000):
            # 清空保留原始 fill；正式输入走拟人化通道（若启用）。
            name_input.fill("")
            if humanize_on:
                _humanize.type_humanized(page, name_selector, full_name)
            else:
                name_input.fill(full_name)
            logger.info("已填写姓名: %s", full_name)
    except Exception as exc:
        if "about-you" not in str(page.url or ""):
            return
        logger.warning("姓名填写失败: %s", exc)

    age_selector = 'input[name="age"]'
    age_input = page.locator(age_selector).first
    try:
        has_age = age_input.is_visible(timeout=2000) and age_input.is_enabled(timeout=1000)
    except Exception:
        has_age = False

    if has_age:
        age = str(random.randint(25, 35))
        try:
            age_input.fill("")
            if humanize_on:
                _humanize.type_humanized(page, age_selector, age)
            else:
                age_input.fill(age)
            logger.info("已填写年龄: %s", age)
        except Exception as exc:
            if "about-you" not in str(page.url or ""):
                return
            logger.warning("年龄填写失败: %s", exc)
    else:
        birth_day = str(random.randint(1, 28))
        birth_month = str(random.randint(1, 12))
        birth_year = str(random.randint(1990, 2000))
        formatted = f"{int(birth_day):02d}/{int(birth_month):02d}/{birth_year}"
        try:
            birthdate_selector = (
                'input[name="birthdate"], input[name="birthday"], '
                'input[autocomplete="bday"], input[inputmode="numeric"]'
            )
            birthdate_input = page.locator(birthdate_selector).first
            segment = page.locator('[role="spinbutton"]').first
            if "about-you" in str(page.url or "") and birthdate_input.is_visible(timeout=1000):
                if humanize_on:
                    _humanize.type_humanized(page, birthdate_selector, formatted)
                else:
                    birthdate_input.fill(formatted)
            elif "about-you" in str(page.url or "") and segment.is_visible(timeout=2000):
                segment.focus()
                page.keyboard.type(formatted, delay=100)
        except Exception as exc:
            if "about-you" not in str(page.url or ""):
                return
            logger.warning("日期填写失败: %s", exc)

    human_delay(1, 2)
    submit_selector = 'button[type="submit"]'
    submit_btn = page.locator(submit_selector).first
    try:
        if "about-you" in str(page.url or "") and submit_btn.is_visible(timeout=3000) and submit_btn.is_enabled(timeout=1000):
            if humanize_on:
                _humanize.click_humanized(page, submit_selector)
            else:
                submit_btn.click(timeout=5000)
            _wait_for_profile_step_transition(page, prompt_name="about-you")
            return
    except Exception as exc:
        if "about-you" not in str(page.url or ""):
            return
        logger.warning("about-you 提交按钮点击失败: %s", exc)

    try:
        if "about-you" in str(page.url or ""):
            page.keyboard.press("Enter")
            _wait_for_profile_step_transition(page, prompt_name="about-you")
    except Exception:
        pass


def send_chat_message(page: Page, message: str, *, response_timeout_sec: int = 60) -> bool:
    """在 ChatGPT 主页 composer 发一条消息并等待响应出现。

    用于账号养号流程：模拟真实用户对话以累积 OpenAI 内部 trust 信号。

    Args:
      page: 已登录到 chatgpt.com 主页的 Playwright Page
      message: 要发送的文本（建议 30-200 字符的自然话题）
      response_timeout_sec: 等响应消息出现的超时

    Returns:
      True 表示成功发出且看到响应；False 表示 composer 找不到 / 提交失败 / 响应超时
    """
    composer_selectors = (
        '[contenteditable="true"][data-testid*="prompt-textarea"]',
        '[contenteditable="true"][id*="prompt-textarea"]',
        'textarea[data-testid*="prompt-textarea"]',
        'textarea[placeholder*="Message"]',
        'div[contenteditable="true"]',
        'textarea',
    )
    composer = None
    for sel in composer_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                composer = loc
                logger.info("composer 命中: %s", sel)
                break
        except Exception:
            continue
    if composer is None:
        logger.warning("send_chat_message: composer 未找到，跳过")
        return False

    try:
        composer.click(timeout=5000)
    except Exception as exc:
        logger.warning("composer.click 失败 (%s)，尝试 keyboard 直接 type", exc)
        page.keyboard.type(message)
    else:
        try:
            composer.fill(message, timeout=5000)
        except Exception:
            page.keyboard.type(message)

    # 记录现有 message bubble 数量作为基准，便于检测新响应
    bubble_selector = '[data-message-author-role="assistant"], [data-testid*="conversation-turn"]'
    try:
        before_count = int(page.locator(bubble_selector).count())
    except Exception:
        before_count = 0

    try:
        page.keyboard.press("Enter")
    except Exception as exc:
        logger.error("send_chat_message: Enter 失败: %s", exc)
        return False

    # 轮询等待新 bubble 出现（assistant 响应）
    import time
    start = time.time()
    poll_interval = 1.5
    while time.time() - start < response_timeout_sec:
        try:
            cur = int(page.locator(bubble_selector).count())
            if cur > before_count:
                logger.info("send_chat_message: 新响应已出现 (count %d → %d)", before_count, cur)
                # 给响应再多 2 秒完成流式输出
                time.sleep(2)
                return True
        except Exception:
            pass
        time.sleep(poll_interval)

    logger.warning("send_chat_message: %ds 内未检测到响应", response_timeout_sec)
    return False


def dismiss_home_welcome_modal(page: Page) -> bool:
    """关闭首页欢迎弹窗。"""
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
    except Exception:
        pass
    return False


def recover_from_error_page(runtime: AutomationRuntime, _action) -> bool:
    """遇到 error 页后清理并新开标签页。"""
    logger.warning("检测到异常页，执行恢复流程。")
    try:
        if runtime.page:
            try:
                runtime.page.close()
            except Exception:
                pass
        runtime.context.clear_cookies()
    except Exception as exc:
        logger.warning("恢复流程清理失败: %s", exc)
    runtime.page = prepare_clean_start_page(runtime.context)
    return True


def manual_handoff(runtime: AutomationRuntime, payload: dict) -> bool:
    """人工接管兜底。"""
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


# ── Handler 构建工厂 ──────────────────────────────


def build_runtime_handlers(
    *,
    email: str,
    password: str,
) -> dict[str, Callable[[AutomationRuntime, object], bool]]:
    """构造状态机执行器需要的 handler 字典。"""

    def enter_signup(runtime: AutomationRuntime, _action) -> bool:
        open_signup_entry(runtime.page, email, runtime=runtime)
        runtime.page = wait_for_auth_page(runtime.context, runtime.page)
        return True

    def _submit_password(runtime: AutomationRuntime, _action) -> bool:
        submit_password(runtime.page, password, runtime=runtime)
        return True

    def verify_email(runtime: AutomationRuntime, _action) -> bool:
        return handle_email_verification_step(runtime.page, runtime.mail_api, email, runtime=runtime)

    def _fill_about_you(runtime: AutomationRuntime, _action) -> bool:
        fill_about_you_form(runtime.page, runtime=runtime)
        return True

    def wait_short(runtime: AutomationRuntime, _action) -> bool:
        human_delay(2, 3)
        return True

    return {
        "enter_signup": enter_signup,
        "submit_password": _submit_password,
        "verify_email": verify_email,
        "fill_about_you": _fill_about_you,
        "wait_short": wait_short,
        "recover_error": recover_error,
        "manual_handoff": manual_handoff,
    }


# ── 支付流程 Handler ──────────────────────────────


def wait_for_stripe_form(page: Page, timeout_sec: int = 30) -> bool:
    """等待 Stripe 支付表单加载。"""
    for _ in range(timeout_sec):
        # 检查常见的 Stripe iframe 或输入框
        try:
            if page.locator('iframe[title*="payment" i]').first.is_visible(timeout=1000):
                return True
            if page.locator('input[name="cardnumber"]').first.is_visible(timeout=1000):
                return True
        except Exception:
            pass
        human_delay(1, 1.2)
    return False


def fill_checkout_card(page: Page, card_number: str, expiry: str, cvc: str) -> str:
    """
    定位并填写信用卡信息。

    Stripe React 在 iframe element 出现后**还需要数秒**才把内部 input 渲染出来。
    历史 ``is_visible(timeout=2000)`` 偶尔在 React 还在 mount 时就 timeout 拿空。
    现在每个 input 等待时长拉长到 ``8s``，覆盖正常 React 内部渲染窗口。

    返回所使用的表单变体名称，失败返回空字符串。
    """
    used_variant = ""

    # 变体 1: 分离的 frames (历史 Stripe 分体集成 - card num / exp / cvc 各自独立 iframe)
    # 当前 ChatGPT hosted checkout 是 unified frame，这条变体一般不命中。
    # 用短 timeout (1500ms) 快速判断不存在就转到变体 2，避免无谓等待 8s × 3。
    try:
        card_num_frame = page.frame_locator('iframe[name^="__privateStripeFrame"][title*="Card number" i]').first
        exp_frame = page.frame_locator('iframe[name^="__privateStripeFrame"][title*="Expiration" i]').first
        cvc_frame = page.frame_locator('iframe[name^="__privateStripeFrame"][title*="CVC" i]').first

        if card_num_frame.locator('input[name="cardnumber"]').first.is_visible(timeout=1500):
            human_typing(card_num_frame, 'input[name="cardnumber"]', card_number)
            human_typing(exp_frame, 'input[name="exp-date"]', expiry)
            human_typing(cvc_frame, 'input[name="cvc"]', cvc)
            used_variant = "split_frames"
            return used_variant
    except Exception:
        pass

    # 变体 2: 统一的支付输入 frame (常见于 Hosted Checkout)
    # 实测 Stripe 当前 unified frame 用的 input name：
    #   name='number'  placeholder='1234 1234 1234 1234'  autocomplete='cc-number'
    #   name='expiry'  placeholder='MM / YY'              autocomplete='cc-exp'
    #   name='cvc'     placeholder='CVC'                  autocomplete='cc-csc'
    # 历史 Stripe 集成用过 cardNumber / cardnumber / cardExpiry / exp-date / cardCvc，
    # **逐个 selector 尝试**而不是 OR 组合 — 实测 frame_locator(...).locator(<多 selector>).first
    # 在跨域 iframe 下 is_visible 行为不可靠（即便 input 实际可见）。
    _CARD_NAME_CANDIDATES = ("number", "cardNumber", "cardnumber")
    _EXP_NAME_CANDIDATES = ("expiry", "cardExpiry", "exp-date")
    _CVC_NAME_CANDIDATES = ("cvc", "cardCvc")

    def _first_visible_selector(frame, candidates: tuple[str, ...], *, first_timeout: int = 8000, others_timeout: int = 2000) -> str:
        """逐个尝试 selector 字符串，返回第一个可见的（**字符串本身**，便于传给 human_typing）。

        first_timeout 给第一个 candidate 较长时间（覆盖 Stripe React 渲染窗口），
        后续 candidate 短 timeout（仅做 fallback 探测）。
        """
        for idx, sel in enumerate(candidates):
            t = first_timeout if idx == 0 else others_timeout
            try:
                if frame.locator(sel).first.is_visible(timeout=t):
                    return sel
            except Exception:
                continue
        return ""

    for iframe_sel in (
        'iframe[title="Secure payment input frame"]',
        'iframe[title*="payment" i]',
        'iframe[name*="__privateStripeFrame"]',
    ):
        try:
            stripe_frame = page.frame_locator(iframe_sel).first

            # 找 card number — 实测 Stripe 用 name="number"，selector 候选含历史变种 + autocomplete
            card_sel = _first_visible_selector(
                stripe_frame,
                tuple(f'input[name="{n}"]' for n in _CARD_NAME_CANDIDATES) + ('input[autocomplete="cc-number"]',),
            )
            if not card_sel:
                continue  # 当前 iframe_sel 不是真正的支付 frame，下一个

            logger.info("fill_checkout_card: card number 命中 %s", card_sel)
            human_typing(stripe_frame, card_sel, card_number)

            # 找 expiry
            exp_sel = _first_visible_selector(
                stripe_frame,
                tuple(f'input[name="{n}"]' for n in _EXP_NAME_CANDIDATES) + ('input[autocomplete="cc-exp"]',),
                first_timeout=3000, others_timeout=1500,
            )
            if exp_sel:
                human_typing(stripe_frame, exp_sel, expiry)

            # 找 cvc
            cvc_sel = _first_visible_selector(
                stripe_frame,
                tuple(f'input[name="{n}"]' for n in _CVC_NAME_CANDIDATES) + ('input[autocomplete="cc-csc"]',),
                first_timeout=3000, others_timeout=1500,
            )
            if cvc_sel:
                human_typing(stripe_frame, cvc_sel, cvc)

            used_variant = "unified_frame"
            return used_variant
        except Exception as exc:
            logger.debug("fill_checkout_card: iframe %s 尝试失败 %s", iframe_sel, exc)
            continue

    return used_variant


def handle_checkout_3ds_challenge(page: Page, otp: str) -> bool:
    """处理 3DS 验证弹窗。"""
    acs_frame = page.frame_locator('iframe[name^="acsFrame"]').first
    try:
        input_selector = 'input[type="password"], input[name*="code"], input[name*="challenge"]'
        if acs_frame.locator(input_selector).first.is_visible(timeout=5000):
            human_typing(acs_frame, input_selector, otp)
            btn = acs_frame.locator('button[type="submit"], input[type="submit"]').first
            btn.click()
            return True
    except Exception as exc:
        logger.warning("3DS 回填失败: %s", exc)
    return False


# ── Pro 账号代刷预热（v2 真实 Playwright 适配）────────────────────────


def pro_account_login(
    page: Page,
    email: str,
    password: str,
    *,
    mail_api=None,
    timeout_sec: int = 90,
) -> bool:
    """登录 OpenAI Pro 账号。

    用于卡预热：用 mail_accounts 池中 role=pro_warmup 的账号登录 ChatGPT，
    然后在该账号上发起 Pro Plan 订阅以触发对待绑卡的真实扣款 attempt。

    Args:
      page: 一个全新的 Playwright Page（独立 context，不污染主注册流）
      email: Pro 账号邮箱
      password: Pro 账号密码（仅在 OpenAI 给密码框时使用；现在 OpenAI 多走 magic link
        所以 password 大多数时候用不到，但保留参数兼容）
      mail_api: ``MailManager`` 实例。OpenAI 走 magic link 时调
        ``handle_email_verification_step(page, mail_api, email)`` 拉验证码自动填入。
        不传则只能依赖密码流（清完 cookies 的新 IP 上多半走不通）。
      timeout_sec: 整个登录流程的总超时

    Returns:
      True 表示成功登录到 chatgpt.com 主页（has_app_shell 信号）；False 则失败
    """
    try:
        page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        logger.error("pro_account_login: 导航失败 %s", exc)
        return False
    human_delay(2, 4)

    # Cookie banner / signin 入口
    click_first_visible(page, COOKIE_ACCEPT_SELECTORS, description="点击 Cookie 同意按钮", timeout_ms=2000)
    # ChatGPT 登录页可能要先点 "Log in" 按钮触发 OAuth 跳转到 auth.openai.com
    login_entry_selectors = (
        'button:has-text("Log in")',
        'a:has-text("Log in")',
        'button:has-text("Sign in")',
        'a:has-text("Sign in")',
    )
    click_first_visible(page, login_entry_selectors, description="点击 Log in 入口", timeout_ms=3000)

    # 处理新版 ChatGPT "Log in or sign up" 合一页：邮箱框直接出现在 chatgpt.com/auth/login，
    # 不需要先点 "Continue with email" 跳到 auth 域。统一策略：
    #   1) 优先在当前页等邮箱框（EMAIL_SELECTOR 命中 ``input[type="email"]``）
    #   2) 等不到再尝试旧版"点 Continue with email 跳转 auth 域"路径
    #   3) 都不行才走"直连 auth.openai.com/log-in"兜底
    email_present = False
    try:
        page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=5000)
        email_present = True
        logger.info("pro_account_login: chatgpt.com 当前页已渲染邮箱框，直接填邮箱")
    except Exception:
        pass

    if not email_present:
        # 旧版兼容：试着点 "Continue with email" 类按钮触发跳转
        email_option_selectors = (
            'button:has-text("Continue with email")',
            'button:has-text("Continue with Email")',
            'a:has-text("Continue with email")',
            'a:has-text("Continue with Email")',
            'button:has-text("继续使用邮箱")',
            'button[data-testid*="email" i]:not([data-testid*="google" i]):not([data-testid*="apple" i]):not([data-testid*="microsoft" i])',
        )
        spa_clicked = False
        _spa_deadline = time.time() + 6
        while time.time() < _spa_deadline and not spa_clicked:
            if click_first_visible(
                page, email_option_selectors,
                description="点击 Continue with email 进入密码登录路径",
                timeout_ms=1200,
            ):
                spa_clicked = True
                break
            human_delay(0.6, 1.0)

        # 等跳到 auth.openai.com（旧版流程）
        auth_landed = False
        _start = time.time()
        while time.time() - _start < 8:
            if is_auth_url(getattr(page, "url", "") or ""):
                auth_landed = True
                break
            human_delay(0.5, 0.8)

        if not auth_landed:
            # 终极兜底：直连 auth.openai.com/log-in。仅当前两步都没把邮箱框/auth 域整出来才走。
            logger.info("pro_account_login: 两步都没出邮箱框，尝试直连 auth.openai.com/log-in")
            try:
                page.goto("https://auth.openai.com/log-in", wait_until="domcontentloaded", timeout=20000)
                human_delay(1, 2)
            except Exception as exc:
                logger.error("pro_account_login: 直连 auth.openai.com 失败 %s", exc)
                return False

    # 处理 OpenAI "Your session has ended" 中转页：
    # 清完 auth 域 cookies 后访问 /log-in，OpenAI 会先弹这个页面强制再点一次 "Log in"
    # 才放行到真正的邮箱表单。检测页面上是否有这段标志性文案 + 一个独立的 "Log in"
    # 按钮（不带其他兄弟元素）来判定。
    try:
        session_ended_marker = page.locator(
            'h1:has-text("Your session has ended"), :text("Your session has ended")'
        ).first
        if session_ended_marker.is_visible(timeout=2500):
            logger.info("pro_account_login: 检测到 'Your session has ended' 中转页，再点 Log in 进入邮箱表单")
            click_first_visible(
                page,
                ('button:has-text("Log in")', 'a:has-text("Log in")'),
                description="点击中转页 Log in 按钮",
                timeout_ms=3000,
            )
            human_delay(1.5, 2.5)
    except Exception:
        # 中转页不存在时这段不应该影响后续流程
        pass

    # 邮箱表单 + OpenAI Auth0 偶发 "Oops, an error occurred" 错误页处理：
    # OpenAI 后端可能 ~30s 后才显示 timed out 错误。所以策略是边等邮箱框边
    # 检测错误页 —— 看到错误页就点 Try again，重试最多 2 次（避免死循环）。
    email_input_visible = False
    for attempt in range(3):  # 0=初次，1/2=Try again 重试
        # 先短等：邮箱框正常 5s 内会出现；超过就开始嗅探错误页
        try:
            page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=5000)
            email_input_visible = True
            break
        except Exception:
            pass

        # 检测 OpenAI Auth0 错误页
        err_visible = False
        try:
            err_visible = page.locator(
                ':text("Oops, an error occurred")'
            ).first.is_visible(timeout=2000)
        except Exception:
            err_visible = False

        if err_visible:
            if attempt >= 2:
                logger.error("pro_account_login: 错误页连续 %d 次，放弃", attempt + 1)
                break
            logger.warning(
                "pro_account_login: 检测到 OpenAI Auth0 错误页（第 %d 次），点击 Try again 重试",
                attempt + 1,
            )
            clicked = click_first_visible(
                page,
                ('button:has-text("Try again")', 'a:has-text("Try again")'),
                description="Try again",
                timeout_ms=3000,
            )
            if not clicked:
                logger.error("pro_account_login: Try again 按钮点击失败")
                break
            human_delay(3.0, 5.0)
            continue

        # 没错误页也没邮箱框：再等一轮（共最多 5s + 5s + 5s = 15s）
        try:
            page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=5000)
            email_input_visible = True
            break
        except Exception:
            continue

    if not email_input_visible:
        logger.error("pro_account_login: 邮箱输入框未出现 (url=%s)", getattr(page, "url", ""))
        return False
    logger.info("pro_account_login: 邮箱框就绪，准备填入 (url=%s)", getattr(page, "url", ""))
    try:
        page.locator(EMAIL_SELECTOR).first.fill(email, timeout=5000)
        logger.info("pro_account_login: 邮箱已 fill")
    except Exception as exc:
        logger.warning("pro_account_login: 邮箱 fill 失败 %s，降级 keyboard", exc)
        try:
            page.locator(EMAIL_SELECTOR).first.click(timeout=3000)
        except Exception:
            pass
        page.keyboard.type(email)
        logger.info("pro_account_login: 邮箱已通过 keyboard.type 填入")
    # 验证邮箱真的填进去了
    try:
        actual = page.locator(EMAIL_SELECTOR).first.input_value(timeout=2000)
        logger.info("pro_account_login: 邮箱框当前 value 长度=%d (期望=%d)", len(actual or ""), len(email))
        if not actual:
            logger.warning("pro_account_login: 邮箱框 value 为空，再次降级到 keyboard")
            page.locator(EMAIL_SELECTOR).first.click(timeout=2000)
            page.keyboard.type(email)
    except Exception as exc:
        logger.warning("pro_account_login: 验证邮箱 value 失败 %s", exc)
    human_delay(0.6, 1.2)
    page.keyboard.press("Enter")
    click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击邮箱继续按钮", timeout_ms=3000)
    logger.info("pro_account_login: 邮箱已 submit (url=%s)", getattr(page, "url", ""))

    # 邮箱提交后 OpenAI 会走三条路径之一：
    #   A) 给密码框 → 走密码流（已知设备 / 不强制 magic link 的场景）
    #   B) 给 "Check your inbox" 页 → 走 magic link 流，必须拉邮箱验证码
    #   C) 给 "Oops, an error occurred / Operation timed out" 错误页 → 点 Try again 重试
    # 用 race 模式同时嗅探三个信号。最多重试 2 次错误页。
    inbox_url_landed = False
    password_visible = False
    err_retries = 0
    MAX_ERR_RETRIES = 2
    race_deadline = time.time() + 30  # 邮箱 submit 后 OpenAI 后端可能 ~25s 才显示 timed out
    while time.time() < race_deadline:
        cur_url = getattr(page, "url", "") or ""
        # 路径 A：密码框出现
        try:
            if page.locator(PASSWORD_SELECTOR).first.is_visible(timeout=500):
                password_visible = True
                break
        except Exception:
            pass
        # 路径 B：inbox 页（URL 跳到 email-verification 或 文案命中均认）
        if "email-verification" in cur_url:
            inbox_url_landed = True
            break
        try:
            if page.locator(':text("Check your inbox")').first.is_visible(timeout=500):
                inbox_url_landed = True
                break
        except Exception:
            pass
        # 路径 D：OpenAI "Your session has ended" 中转页 → 点 Log in 后重新走邮箱表单
        # 这个页面会出现在 submit 邮箱后 OpenAI 决定要"重新走一遍登录"时。
        try:
            session_ended_visible = page.locator(':text("Your session has ended")').first.is_visible(timeout=500)
        except Exception:
            session_ended_visible = False
        if session_ended_visible:
            logger.info("pro_account_login: 邮箱 submit 后命中 'Your session has ended' 中转页，点 Log in")
            click_first_visible(
                page,
                ('button:has-text("Log in")', 'a:has-text("Log in")'),
                description="点击中转页 Log in（race 内）",
                timeout_ms=3000,
            )
            human_delay(2.0, 3.0)
            # 中转页后回到邮箱表单，需要重新 fill + submit
            try:
                page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=10000)
                page.locator(EMAIL_SELECTOR).first.fill(email, timeout=5000)
                human_delay(0.5, 1.0)
                page.keyboard.press("Enter")
                click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击邮箱继续按钮（中转页后）", timeout_ms=3000)
                logger.info("pro_account_login: 中转页后重新 submit 邮箱")
            except Exception as exc:
                logger.warning("pro_account_login: 中转页后重新填邮箱失败 %s", exc)
            race_deadline = time.time() + 30
            continue
        # 路径 C：OpenAI Auth0 "Oops, an error occurred" 错误页 → 点 Try again
        try:
            err_visible = page.locator(':text("Oops, an error occurred")').first.is_visible(timeout=500)
        except Exception:
            err_visible = False
        if err_visible:
            if err_retries >= MAX_ERR_RETRIES:
                logger.error(
                    "pro_account_login: 邮箱 submit 后连续 %d 次 OpenAI 后端错误页，放弃",
                    err_retries + 1,
                )
                break
            err_retries += 1
            logger.warning(
                "pro_account_login: 邮箱 submit 后命中 OpenAI 错误页（第 %d/%d 次），点 Try again 重试",
                err_retries, MAX_ERR_RETRIES,
            )
            click_first_visible(
                page,
                ('button:has-text("Try again")', 'a:has-text("Try again")'),
                description="Try again（邮箱 submit 后）",
                timeout_ms=3000,
            )
            human_delay(3.0, 5.0)
            # 重试后页面会回到邮箱框，需要重新 fill + submit
            try:
                page.locator(EMAIL_SELECTOR).first.fill(email, timeout=5000)
                human_delay(0.5, 1.0)
                page.keyboard.press("Enter")
                click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击邮箱继续按钮（重试）", timeout_ms=3000)
                logger.info("pro_account_login: Try again 后重新 submit 邮箱")
            except Exception as exc:
                logger.warning("pro_account_login: Try again 后重新填邮箱失败 %s", exc)
            # 延长 race 截止时间给重试的请求 ~25s 时间
            race_deadline = time.time() + 30
            continue
        human_delay(0.5, 0.9)

    if inbox_url_landed:
        # 走 magic link 路径：复用主注册流的 handle_email_verification_step
        if mail_api is None:
            logger.error(
                "pro_account_login: 命中 'Check your inbox' 页但未注入 mail_api，"
                "无法自动拉验证码。caller 必须传入 mail_api。"
            )
            return False
        logger.info("pro_account_login: 命中 'Check your inbox' 页，复用 handle_email_verification_step 拉码")
        try:
            ok = handle_email_verification_step(page, mail_api, email)
        except Exception as exc:
            logger.error("pro_account_login: 邮箱验证码处理异常 %s", exc)
            return False
        if not ok:
            logger.error("pro_account_login: 邮箱验证码处理失败")
            return False
        human_delay(1, 2)
        return _wait_for_chatgpt_home(page, timeout_sec)

    if not password_visible:
        logger.error(
            "pro_account_login: 既没等到密码框也没等到 inbox 页 (url=%s)",
            getattr(page, "url", ""),
        )
        return False
    # 落到密码框 → 走密码流
    try:
        page.locator(PASSWORD_SELECTOR).first.fill(password, timeout=5000)
    except Exception as exc:
        logger.warning("pro_account_login: 密码 fill 失败 %s，降级 keyboard", exc)
        page.keyboard.type(password)
    human_delay(0.6, 1.2)
    page.keyboard.press("Enter")
    click_first_visible(page, PRIMARY_SUBMIT_SELECTORS, description="点击密码继续按钮", timeout_ms=3000)

    return _wait_for_chatgpt_home(page, timeout_sec, mail_api=mail_api, email=email)


def _wait_for_chatgpt_home(
    page: Page,
    timeout_sec: int,
    *,
    mail_api: object | None = None,
    email: str = "",
) -> bool:
    """轮询等待页面跳到 chatgpt.com 主页（composer 或 nav 出现）。

    pro_account_login 的两条路径（密码流 / inbox 验证码流）都用它判断登录成功。

    若密码流提交后被 OpenAI 二次跳到 magic link 验证码页（"Check your inbox" 或
    /email-verification），且调用方传入了 mail_api，则复用
    handle_email_verification_step 自动拉码后继续等待主页。
    """
    start = time.time()
    inbox_handled = False
    while time.time() - start < timeout_sec:
        try:
            url = str(page.url or "")
            if "chatgpt.com" in url and "auth" not in url and "login" not in url:
                # 二次确认：composer 或 nav 出现
                try:
                    if page.locator('textarea, [contenteditable="true"], [data-testid*="composer"]').first.is_visible(timeout=1000):
                        logger.info("pro_account_login: 登录成功 url=%s", url)
                        return True
                except Exception:
                    pass
                try:
                    if page.locator('aside a, aside button, nav a, nav button').count() >= 6:
                        logger.info("pro_account_login: 登录成功（nav）url=%s", url)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        # 检查是否被丢到 captcha / verify-email
        try:
            cur_url = str(page.url or "")
        except Exception:
            cur_url = ""
        if "challenge" in cur_url or "captcha" in cur_url or "auth/error" in cur_url:
            logger.error("pro_account_login: 被风控拦截 url=%s", cur_url)
            return False
        # 密码流提交后被二次跳转到 magic link 验证码页：复用主流程拉码
        if not inbox_handled and mail_api is not None and email:
            inbox_detected = "email-verification" in cur_url
            if not inbox_detected:
                try:
                    inbox_detected = page.locator(':text("Check your inbox")').first.is_visible(timeout=500)
                except Exception:
                    inbox_detected = False
            if inbox_detected:
                logger.info("pro_account_login: 密码流后跳到 magic link 验证码页，自动拉码")
                inbox_handled = True
                try:
                    ok = handle_email_verification_step(page, mail_api, email)
                except Exception as exc:
                    logger.error("pro_account_login: 二次 magic link 拉码异常 %s", exc)
                    return False
                if not ok:
                    logger.error("pro_account_login: 二次 magic link 拉码失败")
                    return False
                human_delay(1, 2)
                continue
        human_delay(1, 1.5)
    logger.error("pro_account_login: 等待主页超时")
    return False


class WarmupUpgradeNotFound(RuntimeError):
    """ChatGPT UI 上未找到 Upgrade 入口或 Pro 档位 button。"""


# Pro 档位的静态 button id（OpenAI Configure your plan 页 DOM 实测）
_PRO_TIER_BUTTON_IDS = {
    100: "chatgptprolite",   # 5x more usage than Plus, $100/month（默认选中）
    200: "chatgptpro",       # 20x more usage than Plus, $200/month
}


def navigate_to_pro_checkout(page: Page, *, timeout_sec: int = 30) -> None:
    """从登录后的 ChatGPT 主页导航到 Configure your plan 页。

    步骤：
      1. 主页找左下角侧栏 "Claim offer" / "Upgrade plan" 入口，点击 → 弹出 plan modal
      2. plan modal 强制切到 Personal tab（避开 Business 默认）
      3. plan modal 找 Pro 卡里 'Upgrade to Pro' 黑按钮，点击
      4. 等到达 Configure your plan 页（URL 含 /checkout/ 或 #pricing，或 DOM 出现 button#chatgptpro）

    完成后 page 停在 Configure your plan 页（默认选中 $100 档位）。
    失败 raise WarmupUpgradeNotFound。

    注意：不要点主页 "Claim free offer"（Plus 卡内按钮）— 会直接跳 Plus checkout 绕过 plan modal。
    """
    # 第 1 步：主页 → 选 plan 弹窗
    cur_url = str(page.url or "")
    if "/checkout/" in cur_url or "#pricing" in cur_url:
        logger.info(
            "navigate_to_pro_checkout: 已在 checkout/pricing 页，跳过主页入口 (url=%s)；"
            "仍然要确保 Personal tab 激活 + 点 Pro 升级按钮",
            cur_url,
        )
        # 不要 early return — 已在 pricing 弹窗时，仍然需要确保 Personal tab 激活，
        # 并继续走第 1.5+2+3 步（Personal toggle / 点 Upgrade to Pro / 等 plan 页就绪）。
        # 否则之前 Business tab 显示的弹窗会被当成"已就绪"，select_pro_tier 找不到 Pro 卡。
    else:
        # 主页 Upgrade 入口
        # 实测真实 DOM：<button aria-label="Claim offer">Claim offer</button>（左下角侧栏底部）
        # 点击后 → 弹出 plan 选择 modal（Personal/Business toggle + Free/Go/Plus/Pro 卡）
        # 注意 NOT "Free offer"（那是 Plus 卡内 'Claim free offer'，会直接跳 Plus checkout，绕过 plan modal）
        # selector 优先 aria-label 精确匹配（最稳），再 fallback 到文本匹配
        upgrade_entry_selectors = (
            # 1) aria-label 精确匹配（实测稳定，避开 Plus 卡 "Claim free offer"）
            'button[aria-label="Claim offer"]',
            # 2) 文案精确匹配（第二道防线）
            'button:text-is("Claim offer")',
            'a:text-is("Claim offer")',
            # 3) 桌面版侧栏 "Upgrade plan"（其他 A/B 分支）
            'button[aria-label="Upgrade plan"]',
            'button:has-text("Upgrade plan")',
            'a:has-text("Upgrade plan")',
            # 4) 通用 fallback（aria-label 含 upgrade）
            'button[aria-label*="upgrade" i]',
            '[data-testid*="upgrade" i]',
        )
        if not click_first_visible(
            page, upgrade_entry_selectors,
            description="点击主页 Upgrade 入口（左下角 Claim offer / Upgrade plan）",
            timeout_ms=8000,
        ):
            raise WarmupUpgradeNotFound(
                f"主页找不到 Upgrade 入口（Claim offer / Upgrade plan）url={cur_url}"
            )
        human_delay(2, 3)

    # 第 1.5 步：plan 弹窗默认可能停在 Business tab（OpenAI A/B test 给某些账号）。
    # Pro 卡只在 Personal tab，必须强制切过去。
    #
    # 关键：toggle 是 **Radix UI roving focus toggle group**（DOM 特征：
    # `data-radix-collection-item` + 未选中项 `tabindex="-1"`）。Radix 只让当前
    # 选中项可被键盘聚焦，所以普通 `click()` / `focus() + Space` 都常被拦截。
    # 正确切换方式（按可靠性递减）：
    #   1. click(force=True)  — 跳过 Playwright actionability check，Radix 自身仍处理 click
    #   2. 键盘 ArrowLeft     — Radix toggle group 的标准切换（Personal 在左 / Business 在右）
    #   3. JS evaluate click  — 最后兜底
    try:
        personal_toggle = page.locator(
            'button[aria-label="Toggle for switching to Personal plans"]'
        ).first
        if personal_toggle.is_visible(timeout=3000):
            # 已在 Personal 直接跳过
            try:
                cur_state = personal_toggle.get_attribute("aria-checked", timeout=1500)
            except Exception:
                cur_state = None
            if cur_state == "true":
                logger.info("navigate_to_pro_checkout: Personal toggle 已选中 (aria-checked=true)")
            else:
                business_toggle = page.locator(
                    'button[aria-label="Toggle for switching to Business plans"]'
                ).first

                def _arrow_left():
                    # Radix 用键盘箭头切换：先聚焦当前选中项（Business tabindex=0），按 ArrowLeft
                    try:
                        business_toggle.focus(timeout=2000)
                    except Exception:
                        pass
                    page.keyboard.press("ArrowLeft")

                attempts = (
                    ("click_force", lambda: personal_toggle.click(timeout=3000, force=True)),
                    ("keyboard_arrow_left", _arrow_left),
                    ("js_click", lambda: personal_toggle.evaluate("el => el.click()")),
                )
                switched = False
                for label, action in attempts:
                    try:
                        action()
                    except Exception as act_exc:
                        logger.warning("navigate_to_pro_checkout: Personal toggle %s 失败 %s", label, act_exc)
                        continue
                    human_delay(1.0, 2.0)
                    try:
                        cur_state = personal_toggle.get_attribute("aria-checked", timeout=2000)
                    except Exception:
                        cur_state = None
                    if cur_state == "true":
                        logger.info(
                            "navigate_to_pro_checkout: Personal toggle 已切换 (via=%s, aria-checked=true)",
                            label,
                        )
                        switched = True
                        break
                    logger.warning(
                        "navigate_to_pro_checkout: Personal toggle %s 后 aria-checked=%s（期望 true），尝试下一种方式",
                        label, cur_state,
                    )
                if not switched:
                    logger.warning(
                        "navigate_to_pro_checkout: Personal toggle 三种方式都没切过去，仍按当前 tab 继续"
                        "（如果 Pro 卡在 Business tab 不存在，下一步会失败）"
                    )
        else:
            logger.info("navigate_to_pro_checkout: 未发现 Personal/Business toggle（移动版 UI 或单 tab 布局）")
    except Exception as exc:
        logger.warning("navigate_to_pro_checkout: Personal toggle 检测异常 %s", exc)

    # 第 2 步：plan 选择弹窗 → 点 Pro 卡里的升级按钮
    # selector 多套 fallback：testid（最稳）→ aria-label → 文案
    pro_button_selectors = (
        'button[data-testid*="select-plan-button-pro" i]',
        'button[data-testid*="select-plan-button-chatgptpro" i]',
        'button[aria-label="Upgrade to Pro"]',
        'button:has-text("Upgrade to Pro")',
        'a:has-text("Upgrade to Pro")',
    )
    if not click_first_visible(
        page, pro_button_selectors,
        description="点击 Pro 卡 'Upgrade to Pro'",
        timeout_ms=8000,
    ):
        raise WarmupUpgradeNotFound(
            f"plan 弹窗找不到 Pro 升级按钮 url={page.url}"
        )

    # 第 3 步：等 Configure your plan 页真正就绪
    # 实测：点击 Upgrade to Pro 后 URL 会先变 chatgpt.com/#pricing（中间态，Pro 档位 button
    # 还没渲染），数秒后 OpenAI 才生成 cs_live session 并跳到 /checkout/openai_llc/...
    # 完成判定必须看 DOM —— `button#chatgptpro` / `button#chatgptprolite` 都是 Pro 档位
    # 静态 ID，可见即视为页面真就绪。URL 单独命中 /checkout/ 也算 OK（兜底）。
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cur = str(page.url or "")
        # DOM-first：Pro 档位 button 真的渲染完成
        try:
            if page.locator("button#chatgptpro, button#chatgptprolite").first.is_visible(timeout=500):
                logger.info("navigate_to_pro_checkout: plan 页已就绪 (DOM 判定) url=%s", cur)
                human_delay(1, 2)
                return
        except Exception:
            pass
        # URL 兜底：hosted checkout 已生成（cs_live session）
        if "/checkout/" in cur:
            logger.info("navigate_to_pro_checkout: plan 页已就绪 (URL 判定 /checkout/) url=%s", cur)
            human_delay(1, 2)
            return
        # #pricing 是中间态，不算到位 — 继续轮询直到 DOM 渲染完成
        human_delay(0.5, 1.0)
    raise WarmupUpgradeNotFound(
        f"等待 plan 页超时（DOM 无 chatgptpro button 且 URL 不含 /checkout/）url={page.url}"
    )


def select_pro_tier(page: Page, *, amount_usd: int) -> None:
    """在 Configure your plan 页选 Pro 档位（$100 或 $200）。

    依赖真实 DOM：
      <button id="chatgptprolite" role="radio">  → $100/month
      <button id="chatgptpro" role="radio">       → $200/month

    幂等：amount_usd=100 是默认选中，再点一次也 OK。失败 raise WarmupUpgradeNotFound。
    """
    if amount_usd not in _PRO_TIER_BUTTON_IDS:
        raise ValueError(f"select_pro_tier: amount_usd={amount_usd} 不支持，仅支持 {list(_PRO_TIER_BUTTON_IDS.keys())}")

    button_id = _PRO_TIER_BUTTON_IDS[amount_usd]
    selector = f"button#{button_id}"
    try:
        button = page.locator(selector).first
        button.wait_for(state="visible", timeout=10000)
        button.click(timeout=5000)
        human_delay(0.8, 1.5)
        # 校验 aria-checked 切换成功
        try:
            checked = button.get_attribute("aria-checked", timeout=2000)
            if checked == "true":
                logger.info("select_pro_tier: 已选中 Pro $%d 档位 (button#%s aria-checked=true)", amount_usd, button_id)
                return
            logger.warning(
                "select_pro_tier: 点击 button#%s 后 aria-checked=%s（期望 true），可能 React 状态延迟",
                button_id, checked,
            )
        except Exception:
            # 拿不到 aria-checked 不致命
            logger.info("select_pro_tier: 已点击 button#%s（aria-checked 校验跳过）", button_id)
    except Exception as exc:
        raise WarmupUpgradeNotFound(
            f"select_pro_tier: 找不到或无法点击 {selector} ({exc})"
        )


def select_pro_plan(page: Page, amount: int, *, timeout_sec: int = 30) -> bool:
    """**已弃用 — 兼容旧测试用例保留**。新代码用 navigate_to_pro_checkout + select_pro_tier。

    Args:
      page: 已登录的主页 Page
      amount: 200 / 100

    Returns:
      True 表示成功；False 表示失败
    """
    try:
        navigate_to_pro_checkout(page, timeout_sec=timeout_sec)
        select_pro_tier(page, amount_usd=amount)
        if not wait_for_stripe_form(page, timeout_sec=timeout_sec):
            logger.error("select_pro_plan: Stripe 表单等待超时")
            return False
        return True
    except WarmupUpgradeNotFound as exc:
        logger.error("select_pro_plan: %s", exc)
        return False
    except ValueError as exc:
        logger.error("select_pro_plan: %s", exc)
        return False


def submit_pro_and_capture_outcome(
    page: Page,
    card_info: Any,
    *,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """填卡 + 提交 Pro Plan 订阅，等待结果（成功 / decline / 异常）并采集信号。

    Args:
      page: 已在 checkout 表单的 Page
      card_info: CardInfo 对象（card_number / expiry_month / expiry_year / cvv）
      timeout_sec: 等待结果的总超时

    Returns:
      {
        "status": "declined" | "succeeded" | "failed",
        "decline_code": "insufficient_funds" | "card_declined" | "do_not_honor" | "",
        "rationale": "DOM / URL 摘要",
        "raw_signals": {url, error_text, ...},
      }
    """
    expiry = f"{card_info.expiry_month}/{str(card_info.expiry_year)[-2:]}"
    variant = fill_checkout_card(page, card_info.card_number, expiry, str(card_info.cvv))
    if not variant:
        return {
            "status": "failed",
            "decline_code": "",
            "rationale": "fill_checkout_card 未识别 Stripe 表单变体",
            "raw_signals": {"variant": ""},
        }

    # 提交
    submit_selectors = (
        'button:has-text("Subscribe")',
        'button:has-text("Pay")',
        'button[type="submit"]',
        '[data-testid="hosted-payment-submit-button"]',
    )
    if not click_first_visible(page, submit_selectors, description="点击订阅 / 支付按钮", timeout_ms=5000):
        return {
            "status": "failed",
            "decline_code": "",
            "rationale": "找不到提交按钮",
            "raw_signals": {"variant": variant},
        }

    # 轮询 DOM 找结果
    import time
    start = time.time()
    last_error = ""
    last_url = ""
    while time.time() - start < timeout_sec:
        try:
            last_url = str(page.url or "")
        except Exception:
            last_url = ""

        # 成功标志：跳转到 success / 展示 thanks
        if any(token in last_url.lower() for token in ("success", "complete", "thanks")):
            return {
                "status": "succeeded",
                "decline_code": "",
                "rationale": f"checkout 成功跳转 url={last_url}",
                "raw_signals": {"url": last_url, "variant": variant},
            }

        # decline / error 文案
        try:
            err_text = detect_checkout_error(page)
        except Exception:
            err_text = ""
        if err_text:
            last_error = err_text
            decline_code = _classify_decline(err_text)
            return {
                "status": "declined",
                "decline_code": decline_code,
                "rationale": f"DOM 错误信息: {err_text[:200]}",
                "raw_signals": {"url": last_url, "error_text": err_text, "variant": variant},
            }

        human_delay(1, 1.5)

    # 超时未拿到明确结果
    return {
        "status": "failed",
        "decline_code": "",
        "rationale": f"提交后 {timeout_sec}s 未拿到明确结果 url={last_url} last_error={last_error}",
        "raw_signals": {"url": last_url, "last_error": last_error, "variant": variant},
    }


def _classify_decline(error_text: str) -> str:
    """把 DOM 上的错误文字分类成 Stripe decline_code。"""
    txt = (error_text or "").lower()
    if "insufficient" in txt or "not enough" in txt or "balance" in txt:
        return "insufficient_funds"
    if "do not honor" in txt or "issuer declined" in txt or "issuer" in txt:
        return "do_not_honor"
    if "expired" in txt:
        return "expired_card"
    if "incorrect cvc" in txt or "security code" in txt:
        return "incorrect_cvc"
    if "declined" in txt or "decline" in txt:
        return "card_declined"
    if "fraud" in txt or "stolen" in txt:
        return "fraudulent"
    return ""


def detect_checkout_error(page: Page) -> str:
    """检测并返回支付页的错误信息。"""
    selectors = [
        ".CardField-childError",
        "#card-errors",
        ".error-message",
        '[role="alert"]',
        ".FieldError",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                text = loc.text_content() or ""
                if text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


# ── 内部工具 ──────────────────────────────────────


def _wait_for_profile_step_transition(page: Page, *, prompt_name: str) -> None:
    """短等页面切换，避免状态机误判。"""
    for _ in range(15):
        current_url = str(getattr(page, "url", "") or "")
        if prompt_name == "about-you" and "about-you" not in current_url:
            return
        if prompt_name == "onboarding" and not read_onboarding_metrics(page).get("prompt_present", False):
            return
        human_delay(1, 1.4)


def resolve_card_with_retry(
    card_api: Any,
    card_key: str,
    *,
    attempts: int = 3,
    delay_sec: int = 5,
) -> Optional[CardInfo]:
    """通用化的 card 查询重试，吸收 provider API 偶发抖动。

    背景：``card_api.get_card(cdk)`` 单次失败常见于 efuncard 服务端 10s read timeout
    或卡密刚兑换还没在 provider 端落地。所有三家 provider（efuncard / nodecard /
    x988card）的 ``get_card`` 对已激活卡的重复查询都是幂等的，所以重试是安全的。

    Args:
        card_api: 实现 ``CardProvider.get_card(card_key)`` 协议的对象
        card_key: CDK / 卡密
        attempts: 最多调用次数（默认 3，最少 1）
        delay_sec: 每次失败后退避秒数（默认 5，最后一次失败后不再 sleep）

    Returns:
        首个非 None 的 CardInfo；exhaust 后返回 None。
    """
    if attempts < 1:
        attempts = 1
    for i in range(1, attempts + 1):
        try:
            card = card_api.get_card(card_key)
        except Exception as exc:
            logger.warning("resolve_card_with_retry: get_card 抛异常（第 %d/%d 次）: %s", i, attempts, exc)
            card = None
        if card is not None:
            if i > 1:
                logger.info("resolve_card_with_retry: 第 %d/%d 次重试命中卡片", i, attempts)
            return card
        if i < attempts:
            logger.info(
                "resolve_card_with_retry: 第 %d/%d 次未命中，%ds 后重试",
                i, attempts, delay_sec,
            )
            time.sleep(delay_sec)
    logger.warning("resolve_card_with_retry: 连续 %d 次未能获取卡片，放弃", attempts)
    return None
