# -*- coding: utf-8 -*-
"""注册状态机、证据采集、动作执行与 token 提取。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from curl_cffi import requests as curl_requests

from src.automation.artifacts import ArtifactRecorder
from src.automation.captcha_solver import try_solve_captcha
from src.automation.experience import ExperienceStore
from src.automation.llm import LLMDecisionProvider
from src.automation.models import (
    Action,
    ActionKind,
    Actionable,
    AutomationState,
    Decision,
    DecisionKind,
    Evidence,
    MachineResult,
    VerificationResult,
)
from src.providers.mail import MailServiceError

_PASSWORD_SELECTOR = 'input#password, input[name="password"], input[type="password"]'
# 真实 DOM（2026-06-01 实测 chatgpt.com 登录弹窗）：
#   <input id="phoneNumberInput" name="phoneNumberInput" type="tel" autocomplete="tel">
# 历史用 input[name="phoneNumber"]（无 Input 后缀）探测不到 → has_phone_input 恒 false
# → 状态机进不了 PHONE state。多重 selector 兜底未来 DOM 变化。
_PHONE_SELECTOR = (
    'input#phoneNumberInput, input[name="phoneNumberInput"], '
    'input[name="phoneNumber"], input[type="tel"], input[autocomplete="tel"]'
)
_DATE_SELECTOR = 'input[name="birthdate"], input[name="birthday"], input[autocomplete="bday"], [role="spinbutton"]'
_CODE_SELECTOR = 'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
_HOME_COMPOSER_SELECTOR = 'textarea, [contenteditable="true"], [data-testid*="composer"]'
_HOME_NAV_SELECTOR = 'aside a, aside button, nav a, nav button'
_CHALLENGE_WIDGET_SELECTOR = 'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[title*="captcha"], [data-sitekey], input[name*="captcha"]'
# 未登录主页特征按钮 (多语言)：登录/注册按钮存在 = 一定未登录
# 用 accessible name 部分匹配 (Playwright 大小写不敏感)，覆盖 EN/ZH/JA/ES/FR/DE 常见 locale
_UNAUTH_BUTTON_NAMES = (
    "log in", "login", "sign up", "signup", "sign in",
    "登录", "登陆", "注册",
    "ログイン", "サインアップ", "サインイン", "無料でサインアップ", "新規登録",
    "iniciar sesión", "registrarse",
    "se connecter", "s'inscrire",
    "anmelden", "registrieren",
)
# OpenAI 服务端「账号创建失败」内联错误文案（多语言）。
# 这类错误停在 /create-account/password 等表单页内联显示，URL 不变（不会跳 auth/error），
# 旧逻辑只看 url 含 "auth/error" 的 has_auth_error 永远抓不到 → 状态机误判为「密码没填对」
# → 重试 submit_password 多次 → 最终 silent_failure_at_state=AUTH（实战 run 17665b65）。
# 真因多为脏号被风控拒（印尼 +62 循环号），命中后应立刻判 ERROR 终止 + 拉黑换号止损，
# 而不是傻填密码。文案以实测截图为准，覆盖 EN/JA/ZH 常见 locale。
_ACCOUNT_CREATION_ERROR_PHRASES = (
    "アカウントを作成できませんでした",  # JA：无法创建账号（实测 run 17665b65）
    "couldn't create your account",
    "could not create your account",
    "unable to create your account",
    "we were unable to create",
    "无法创建你的账户",
    "无法创建您的账户",
    "无法创建帐号",
)
# 「手机号已注册」信号（实测 run c5ba5779）：注册流程提交后 OpenAI 认出号已有账号
# → 跳登录页让输入「已有密码」。对注册任务 = 此号已死，需换号。检测登录页特征文案。
_PHONE_ALREADY_REGISTERED_PHRASES = (
    "incorrect phone number or password",   # EN
    "電話番号またはパスワードが正しくありません",  # JA
    "手机号或密码不正确",                     # ZH
    "手機號碼或密碼不正確",
    "lupa kata sandi",                       # ID「忘记密码」（仅登录页有 → 号已注册）
    "forgot password",                       # EN 忘记密码链接
    "忘记密码",
)
_SESSION_URL = "https://chatgpt.com/api/auth/session"
_TARGET_HOME_URL = "https://chatgpt.com/"


@dataclass
class AutomationRuntime:
    """运行时上下文。"""

    page: Any
    context: Any
    config: Any
    email: str
    password: str
    mail_api: Any
    logger: Any
    handlers: dict[str, Callable[["AutomationRuntime", Action], Any]]
    # SMSManager 注入：phone 模式 handler `submit_phone_and_code` 用它申领 / 轮询 OTP；
    # email 模式 handler 不读这个字段。默认 None 让大部分单测 / 历史调用站不必传。
    sms_api: Any = None
    artifact_recorder: Optional[ArtifactRecorder] = None
    run_id: str = ""
    llm_provider: Optional[LLMDecisionProvider] = None
    experience_store: Optional[ExperienceStore] = None
    emit_event: Optional[Callable[..., Any]] = None
    cancel_check: Optional[Callable[[str], Any]] = None
    last_actions: list[dict[str, Any]] = field(default_factory=list)
    retry_counters: dict[str, int] = field(default_factory=dict)
    llm_uncertain_counters: dict[str, int] = field(default_factory=dict)
    manual_handoff_used: bool = False
    triage_provider: Optional[Any] = None
    captcha_solver: Optional[Any] = None
    recent_log_buffer: list[str] = field(default_factory=list)
    # 卡顿检测（P0）：state.value -> 上一步 DOM 指纹 / 连续无进展步数。
    dom_fingerprints: dict[str, str] = field(default_factory=dict)
    stall_counters: dict[str, int] = field(default_factory=dict)
    # grok_assist 式枚举兜底（P2）：默认关闭，开启后作为硬失败前最后一层。
    assist_enabled: bool = False
    assist_experience: Optional[ExperienceStore] = None


def _emit_runtime_event(
    runtime: AutomationRuntime,
    event_type: str,
    *,
    state: AutomationState | str | None = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    emitter = getattr(runtime, "emit_event", None)
    if not callable(emitter):
        return
    normalized_state = state.value if isinstance(state, AutomationState) else state
    try:
        emitter(event_type, state=normalized_state, payload=payload or {})
    except Exception:
        return


def _safe_call(func: Callable[[], Any], default: Any) -> Any:
    try:
        return func()
    except Exception:
        return default


def _capture_screenshot_b64(page: Any) -> str:
    """抓一张 PNG 截图并 base64 编码（卡顿升级时供多模态 LLM 看）。失败返回 ""（降级纯文本）。"""
    import base64

    try:
        if page is None or not hasattr(page, "screenshot"):
            return ""
        raw = page.screenshot(type="png", full_page=False)
        if not raw:
            return ""
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def _run_triage(runtime: AutomationRuntime, evidence: Any, last_error: str) -> None:
    """
    在 BLOCKED / ERROR / 重试超限时，调分诊器给出类别建议。

    设计约束（见 triage.py system prompt）：
      - 只观察不动作：结果仅落盘到事件流，不自动换代理/换卡/跳状态
      - 失败静默：任何异常降级为 no-op，绝不打断主流程
    """
    provider = getattr(runtime, "triage_provider", None)
    if provider is None:
        return

    # 抓一张截图（可选，供 Vision 模型看）
    screenshot_bytes: Optional[bytes] = None
    try:
        page = runtime.page
        if page is not None and hasattr(page, "screenshot"):
            screenshot_bytes = page.screenshot(type="png", full_page=False)
    except Exception:
        screenshot_bytes = None

    try:
        decision = provider.diagnose(
            screenshot_bytes=screenshot_bytes,
            page_url=getattr(evidence, "url", ""),
            recent_logs=list(getattr(runtime, "recent_log_buffer", []))[-10:],
            signals=dict(getattr(evidence, "signals", {}) or {}),
            last_error=str(last_error or ""),
        )
    except Exception as exc:
        runtime.logger.warning("分诊器异常，跳过: %s", exc)
        return

    _emit_runtime_event(
        runtime,
        "triage",
        payload={
            "category": decision.category,
            "suggested_action": decision.suggested_action,
            "confidence": decision.confidence,
            "rationale": decision.rationale,
            "evidence_summary": decision.evidence_summary,
            "is_actionable": decision.is_actionable,
        },
    )


def _check_runtime_active(runtime: AutomationRuntime, checkpoint: str) -> None:
    checker = getattr(runtime, "cancel_check", None)
    if callable(checker):
        checker(checkpoint)


def infer_state(url: str, signals: dict[str, Any]) -> AutomationState:
    """根据 URL 与结构化信号推断页面状态。"""
    current = str(url or "")

    if current.startswith("chrome-error://") or signals.get("has_chrome_error"):
        return AutomationState.ERROR
    if signals.get("has_challenge_text") or signals.get("has_challenge_widget") or "challenge" in current:
        return AutomationState.BLOCKED
    if (
        "auth/error" in current
        or signals.get("has_auth_error")
        or signals.get("has_auth_timeout_error")
        or signals.get("has_account_creation_error")
        or signals.get("has_phone_already_registered")
    ):
        # has_account_creation_error / has_phone_already_registered 优先于下方
        # has_password_input → AUTH 的分支：这些页仍带密码框，但已是终态错误（号被拒/
        # 号已注册跳登录页），必须判 ERROR 否则会无限重试 submit_password 到 silent_failure。
        # → worker 终态 _blacklist_phone_on_failure 拉黑该号换号。
        return AutomationState.ERROR
    if signals.get("has_phone_input") or "phone" in current or "onboarding" in current:
        return AutomationState.PHONE
    if "about-you" in current or signals.get("has_about_name_input"):
        return AutomationState.ABOUT_YOU
    if signals.get("has_onboarding_prompt"):
        return AutomationState.ABOUT_YOU
    if "email-verification" in current or signals.get("has_code_input"):
        return AutomationState.VERIFY_EMAIL
    if "/password" in current or signals.get("has_password_input"):
        return AutomationState.AUTH
    if "chatgpt.com" in current and "auth" not in current and signals.get("has_app_shell"):
        # 关键护栏：chatgpt.com 主页含登录/注册按钮 → 仍未登录，回退到 ENTRY
        # 防止 enter_signup 触发后落在「未登录主页 + 登录弹窗」被误判为成功
        if signals.get("has_unauth_chrome"):
            return AutomationState.ENTRY
        return AutomationState.HOME
    if "auth.openai.com" in current or "auth0.openai.com" in current:
        return AutomationState.AUTH
    if not current or current.startswith("about:blank") or "chatgpt.com" in current:
        return AutomationState.ENTRY
    return AutomationState.UNKNOWN


def _compute_dom_fingerprint(url: str, signals: dict[str, Any], actionables: list[Any]) -> str:
    """对当前页面算稳定指纹，用于卡顿检测（同 state 连续多步指纹不变 = 没进展）。

    构成：url path（去掉 query/fragment 噪声）+ 排序后的布尔信号 + 可操作元素 name 列表。
    刻意不含 timestamp / 动态 token，保证「同一页面没动」时指纹稳定。
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(str(url or ""))
    url_part = f"{parsed.netloc}{parsed.path}"
    signal_part = "|".join(
        f"{k}={int(bool(v))}" for k, v in sorted(signals.items()) if isinstance(v, bool)
    )
    name_part = "|".join(str(getattr(a, "name", "") or "")[:40] for a in actionables[:25])
    raw = f"{url_part}#{signal_part}#{name_part}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_stall_context(
    evidence: Evidence, *, retry_count: int, stall_count: int
) -> dict[str, Any]:
    """把「卡在哪里 / 为什么卡」结构化，供 LLM 卡顿升级决策 + 事件流可观测。

    纯函数，不读页面（只消费已采集的 evidence），便于单测。
    """
    signals = dict(getattr(evidence, "signals", {}) or {})
    active_signals = [k for k, v in signals.items() if isinstance(v, bool) and v]
    actionables = list(getattr(evidence, "actionables", []) or [])
    actionable_summary = [
        {
            "name": str(getattr(a, "name", "") or "")[:60],
            "role": str(getattr(a, "role", "") or ""),
            "enabled": bool(getattr(a, "enabled", True)),
        }
        for a in actionables[:15]
    ]
    # 启发式「最可能卡住的元素」：第一个可见可用元素的 name；都没有则标 url_unchanged。
    suspected_block = "url_unchanged"
    for a in actionables:
        if getattr(a, "enabled", True) and getattr(a, "visible", True):
            suspected_block = str(getattr(a, "name", "") or "")[:60] or "unknown_element"
            break
    return {
        "retry_count": retry_count,
        "stall_count": stall_count,
        "current_state": evidence.primary_state.value,
        "url": getattr(evidence, "url", ""),
        "active_signals": active_signals,
        "actionable_summary": actionable_summary,
        "last_actions": list(getattr(evidence, "last_actions", []))[-3:],
        "suspected_block": suspected_block,
    }


def extract_session_tokens_with_http(
    *,
    cookies: list[dict[str, Any]],
    user_agent: str,
    proxy_url: str = "",
    session_factory: Optional[Callable[[], Any]] = None,
    session_url: str = _SESSION_URL,
) -> tuple[str, str]:
    """使用复制的 cookie jar 通过独立 HTTP 通道提取 session。"""
    factory = session_factory or curl_requests.Session
    session = factory()
    headers = getattr(session, "headers", {})
    if isinstance(headers, dict):
        headers.setdefault("User-Agent", user_agent)
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Referer", _TARGET_HOME_URL)
        headers.setdefault("Origin", _TARGET_HOME_URL.rstrip("/"))

    cookie_jar = getattr(session, "cookies", None)
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if not name:
            continue
        domain = cookie.get("domain")
        path = cookie.get("path", "/")
        if hasattr(cookie_jar, "set"):
            cookie_jar.set(name, value, domain=domain, path=path)
        elif isinstance(cookie_jar, dict):
            cookie_jar[name] = value

    refresh_token = ""
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        if "next-auth.session-token" in name:
            refresh_token = str(cookie.get("value", ""))
            break

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    response = session.get(session_url, timeout=15, proxies=proxies)
    response_headers = getattr(response, "headers", {}) or {}
    content_type = str(response_headers.get("content-type", "")).lower()
    body_preview = str(getattr(response, "text", "") or "").strip().lower()[:120]
    looks_like_json = (
        "json" in content_type
        or body_preview.startswith("{")
        or body_preview.startswith("[")
        or not body_preview
    )
    if not looks_like_json:
        return "", refresh_token

    try:
        session_data = response.json()
    except Exception:
        return "", refresh_token

    access_token = str(session_data.get("accessToken", ""))
    return access_token, refresh_token


class EvidenceCollector:
    """从当前页面采集结构化证据。"""

    def collect(self, runtime: AutomationRuntime, *, step_name: str) -> Evidence:
        page = runtime.page
        url = _safe_call(lambda: str(page.url), "")
        title = _safe_call(page.title, "")
        home_composer_count = self._count(page, _HOME_COMPOSER_SELECTOR)
        home_nav_count = self._count(page, _HOME_NAV_SELECTOR)
        onboarding_metrics = self._collect_onboarding_metrics(page)
        signals = {
            "has_phone_input": self._count(page, _PHONE_SELECTOR) > 0,
            "has_password_input": self._is_visible(page, _PASSWORD_SELECTOR),
            "has_code_input": self._count(page, _CODE_SELECTOR) > 0,
            "has_about_name_input": self._count(page, 'input[name="name"], input[autocomplete="name"]') > 0,
            "has_age_input": self._count(page, 'input[name="age"]') > 0,
            "has_date_input": self._count(page, _DATE_SELECTOR) > 0,
            "has_role_alert": self._count(page, '[role="alert"]') > 0,
            "has_auth_error": "auth/error" in url,
            # 内联「账号创建失败」错误（URL 不变，停在表单页）：OpenAI 服务端拒号信号。
            # 限定 auth.openai.com 避免误伤其他页面的相似文案。
            "has_account_creation_error": self._page_contains_any(
                page, list(_ACCOUNT_CREATION_ERROR_PHRASES)
            ) and "auth.openai.com" in url,
            # 手机号已注册：注册流程中途跳到登录页（/log-in/）或出现登录页特征文案。
            # 仅 phone 注册场景有意义（email 注册不会跳 phone 登录）。
            "has_phone_already_registered": (
                ("/log-in/" in url and "auth.openai.com" in url)
                or self._page_contains_any(page, list(_PHONE_ALREADY_REGISTERED_PHRASES))
            ),
            "has_auth_timeout_error": self._page_contains_any(
                page, ["operation timed out", "oops, an error occurred"]
            ) and "auth.openai.com" in url,
            "has_chrome_error": url.startswith("chrome-error://"),
            "has_challenge_text": self._page_contains_any(page, ["captcha", "challenge", "verify you are human"]),
            "has_challenge_widget": self._count(page, _CHALLENGE_WIDGET_SELECTOR) > 0,
            "has_home_composer": home_composer_count > 0,
            "has_dense_app_nav": home_nav_count >= 6,
            "has_app_shell": "auth" not in url and (home_composer_count > 0 or home_nav_count >= 6),
            "has_unauth_chrome": self._has_unauth_chrome(page),
            "has_onboarding_prompt": onboarding_metrics["prompt_present"],
            "onboarding_option_count": onboarding_metrics["option_count"],
            "onboarding_footer_button_count": onboarding_metrics["footer_button_count"],
        }
        state = infer_state(url, signals)
        actionables = self._collect_actionables(page)
        # DOM 指纹：用于卡顿检测（同一 state 连续多步指纹不变 = 页面没动 = 卡住）。
        # 复用已采集的 url path + 布尔信号 + 可操作元素 name，不额外 evaluate。
        # 不进 signals 字典，避免污染经验匹配的 _signal_signature 语义。
        fingerprint = _compute_dom_fingerprint(url, signals, actionables)
        return Evidence(
            url=url,
            title=title,
            timestamp=datetime.now(timezone.utc).isoformat(),
            step_name=step_name,
            state_candidates=[state],
            actionables=actionables,
            signals=signals,
            last_actions=list(runtime.last_actions[-10:]),
            artifacts={"dom_fingerprint": fingerprint},
        )

    @staticmethod
    def _count(page: Any, selector: str) -> int:
        return int(_safe_call(lambda: page.locator(selector).count(), 0))

    @staticmethod
    def _is_visible(page: Any, selector: str) -> bool:
        return bool(_safe_call(lambda: page.locator(selector).first.is_visible(timeout=1000), False))

    @staticmethod
    def _page_contains_any(page: Any, phrases: list[str]) -> bool:
        body_text = str(_safe_call(lambda: page.evaluate("() => document.body.innerText"), "")).lower()
        return any(phrase.lower() in body_text for phrase in phrases)

    @staticmethod
    def _has_unauth_chrome(page: Any) -> bool:
        """检测未登录主页特征按钮（多语言）。

        ChatGPT 主页对未登录用户始终在右上角/弹窗中渲染 Log in / Sign up 类按钮，
        即使整体外观接近已登录主页（home_composer/app_shell 仍存在）。
        命中任一关键词即视为未登录，避免被误判为 HOME。
        """
        names = "|".join(_UNAUTH_BUTTON_NAMES)

        def _probe() -> bool:
            body_text = str(page.evaluate("() => (document.body && document.body.innerText) || ''")).lower()
            return any(name.lower() in body_text for name in _UNAUTH_BUTTON_NAMES)

        # 优先 JS 文本扫描（一次评估），失败回退到 false（保守，避免误阻塞已登录主页）
        _ = names  # placeholder for static analyzers
        return bool(_safe_call(_probe, False))

    @staticmethod
    def _collect_onboarding_metrics(page: Any) -> dict[str, Any]:
        result = _safe_call(
            lambda: page.evaluate(
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
                    const text = (node.innerText || node.getAttribute('aria-label') || '').trim();
                    return !isDisabled(node) && rect.top < window.innerHeight * 0.72 && rect.height >= 24 && text.length > 0;
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
            ),
            {},
        )
        if not isinstance(result, dict):
            return {"prompt_present": False, "option_count": 0, "footer_button_count": 0}
        return {
            "prompt_present": bool(result.get("prompt_present")),
            "option_count": int(result.get("option_count", 0) or 0),
            "footer_button_count": int(result.get("footer_button_count", 0) or 0),
        }

    @staticmethod
    def _collect_actionables(page: Any) -> list[Actionable]:
        actionables = _safe_call(
            lambda: page.evaluate(
                """
                () => Array.from(document.querySelectorAll('button, [role="button"], input, textarea, select, a'))
                  .slice(0, 25)
                  .map((node, index) => {
                    const text = (node.innerText || node.value || node.getAttribute('aria-label') || '').trim();
                    const rect = node.getBoundingClientRect();
                    return {
                      action_id: `dom-${index}-${node.tagName.toLowerCase()}`,
                      kind: node.tagName.toLowerCase() === 'input' || node.tagName.toLowerCase() === 'textarea' ? 'fill' : 'click',
                      locator_ref: `dom:${index}`,
                      role: node.getAttribute('role') || node.tagName.toLowerCase(),
                      name: text.slice(0, 80),
                      visible: rect.width > 0 && rect.height > 0,
                      enabled: !node.disabled
                    };
                  });
                """
            ),
            [],
        )
        result: list[Actionable] = []
        for item in actionables:
            try:
                kind = ActionKind(item["kind"])
            except Exception:
                kind = ActionKind.CLICK
            result.append(
                Actionable(
                    action_id=str(item.get("action_id", "")),
                    kind=kind,
                    locator_ref=str(item.get("locator_ref", "")),
                    role=str(item.get("role", "")),
                    name=str(item.get("name", "")),
                    visible=bool(item.get("visible", True)),
                    enabled=bool(item.get("enabled", True)),
                )
            )
        return result


class RuleDecisionProvider:
    """默认规则决策：候选动作存在时，选优先级最高的一个。"""

    def decide(self, *, evidence: Evidence, candidates: list[Action]) -> Decision:
        if not candidates:
            return Decision(
                kind=DecisionKind.REQUEST_EVIDENCE,
                requested_evidence=["signals", "actionables"],
                confidence=0.0,
                reason_code="NO_RULE_ACTION",
            )
        return Decision(
            kind=DecisionKind.CHOOSE_ACTION,
            action_id=candidates[0].action_id,
            confidence=1.0,
            reason_code="RULE_MATCH",
        )


class ActionExecutor:
    """执行内部动作。"""

    _ALLOWED_KINDS = {
        ActionKind.CLICK,
        ActionKind.FILL,
        ActionKind.PRESS,
        ActionKind.SELECT,
        ActionKind.WAIT,
        ActionKind.REFRESH,
        ActionKind.BACK,
        ActionKind.NEW_TAB,
        ActionKind.CLOSE_TAB,
        ActionKind.CLEAR_TARGET_STORAGE,
        ActionKind.RECONNECT_PROFILE,
    }

    def execute(self, runtime: AutomationRuntime, action: Action) -> VerificationResult:
        _check_runtime_active(runtime, f"before_action:{action.action_id}")
        if action.kind not in self._ALLOWED_KINDS:
            return VerificationResult(ok=False, reason_code="ACTION_NOT_ALLOWED")

        handler_name = str(action.params.get("handler", "")).strip()
        if not handler_name:
            return VerificationResult(ok=False, reason_code="HANDLER_NOT_CONFIGURED")

        handler = runtime.handlers.get(handler_name)
        if handler is None:
            return VerificationResult(ok=False, reason_code="HANDLER_NOT_FOUND")

        try:
            result = handler(runtime, action)
            runtime.last_actions.append(
                {
                    "action_id": action.action_id,
                    "kind": action.kind.value,
                    "description": action.description,
                    "result": "ok" if result is not False else "noop",
                }
            )
            _emit_runtime_event(
                runtime,
                "action",
                payload={
                    "message": action.description,
                    "action_id": action.action_id,
                    "handler": handler_name,
                    "result": "ok" if result is not False else "noop",
                },
            )
            return VerificationResult(ok=result is not False, reason_code="EXECUTED")
        except MailServiceError:
            raise
        except Exception as exc:  # pragma: no cover - 运行时异常通过集成验证
            runtime.last_actions.append(
                {
                    "action_id": action.action_id,
                    "kind": action.kind.value,
                    "description": action.description,
                    "result": "error",
                    "error": str(exc),
                }
            )
            _emit_runtime_event(
                runtime,
                "action",
                payload={
                    "message": action.description,
                    "action_id": action.action_id,
                    "handler": handler_name,
                    "result": "error",
                    "error": str(exc),
                },
            )
            runtime.logger.error("动作执行失败 %s: %s", action.action_id, exc)
            return VerificationResult(ok=False, reason_code="HANDLER_EXCEPTION", details={"error": str(exc)})


class Verifier:
    """基于期望状态验证动作是否生效。"""

    def verify(self, action: Action, after: Evidence) -> VerificationResult:
        if not action.expected_outcomes:
            return VerificationResult(ok=True, reason_code="NO_EXPECTATION")
        if after.primary_state in action.expected_outcomes:
            return VerificationResult(ok=True, reason_code="EXPECTED_STATE_REACHED")
        return VerificationResult(
            ok=False,
            reason_code="UNEXPECTED_STATE",
            details={
                "expected": [state.value for state in action.expected_outcomes],
                "actual": after.primary_state.value,
            },
        )


class RegistrationStateMachine:
    """规则优先、LLM 兜底的注册状态机。"""

    def __init__(
        self,
        *,
        collector: Optional[EvidenceCollector] = None,
        rule_provider: Optional[RuleDecisionProvider] = None,
        executor: Optional[ActionExecutor] = None,
        verifier: Optional[Verifier] = None,
        max_steps: int = 18,
    ) -> None:
        self._collector = collector or EvidenceCollector()
        self._rule_provider = rule_provider or RuleDecisionProvider()
        self._executor = executor or ActionExecutor()
        self._verifier = verifier or Verifier()
        self._max_steps = max_steps

    def run(self, runtime: AutomationRuntime) -> MachineResult:
        last_emitted_state: AutomationState | None = None
        for index in range(1, self._max_steps + 1):
            _check_runtime_active(runtime, f"state_machine_step_{index}")
            evidence = self._collector.collect(runtime, step_name=f"step_{index}")
            self._record(runtime, evidence, [])
            state = evidence.primary_state
            if state != last_emitted_state:
                _emit_runtime_event(
                    runtime,
                    "state_change",
                    state=state,
                    payload={
                        "message": f"进入状态 {state.value}",
                        "step_name": evidence.step_name,
                        "url": evidence.url,
                        "signals": evidence.signals,
                    },
                )
                last_emitted_state = state

            if state == AutomationState.HOME:
                return MachineResult(success=True, final_state=state, manual_handoff_used=runtime.manual_handoff_used)

            # 账号创建失败（OpenAI 服务端拒号）：终态错误，立刻放弃。
            # 不走 ERROR 的 recover_from_error（清存储重开只会再撞同一脏号），
            # 直接返回让 worker 走 _blacklist_phone_on_failure 拉黑换号止损。
            # 实战 run 17665b65：印尼 +62 脏号被拒，旧逻辑误判 AUTH 重试 submit_password 4 次。
            if evidence.signals.get("has_account_creation_error"):
                runtime.logger.warning(
                    "检测到 OpenAI 账号创建失败（疑似拒号），终止本轮并标记失败以便换号。"
                )
                return MachineResult(
                    success=False,
                    final_state=AutomationState.ERROR,
                    failure_reason="ACCOUNT_CREATION_REJECTED",
                    manual_handoff_used=runtime.manual_handoff_used,
                )

            # 手机号已注册（跳登录页让输入已有密码）：同样是终态，立刻放弃换号。
            # 不重试（重试只会再撞同一号），返回让 worker 拉黑该号 + 换下一个。
            if evidence.signals.get("has_phone_already_registered"):
                runtime.logger.warning(
                    "检测到手机号已在 OpenAI 注册（跳登录页），终止本轮并标记失败以便换号。"
                )
                return MachineResult(
                    success=False,
                    final_state=AutomationState.ERROR,
                    failure_reason="PHONE_ALREADY_REGISTERED",
                    manual_handoff_used=runtime.manual_handoff_used,
                )

            if state == AutomationState.BLOCKED:
                # BLOCKED 前先给一次自愈机会
                if try_solve_captcha(runtime, evidence):
                    continue
                _run_triage(runtime, evidence, state.value)
                if self._try_manual_handoff(runtime, evidence, state.value):
                    continue
                return MachineResult(
                    success=False,
                    final_state=state,
                    failure_reason=state.value,
                    manual_handoff_used=runtime.manual_handoff_used,
                )

            if state == AutomationState.PHONE:
                # 仅 registration_kind="phone" 模式才进入 PHONE handler（_build_actions 注入 submit_phone_and_code）；
                # 默认 email 模式仍然走原来的硬失败 → triage → manual_handoff 路径，避免破坏既有行为。
                registration_kind = str(getattr(runtime.config, "registration_kind", "email") or "email").strip().lower()
                if registration_kind != "phone":
                    _run_triage(runtime, evidence, state.value)
                    if self._try_manual_handoff(runtime, evidence, state.value):
                        continue
                    return MachineResult(
                        success=False,
                        final_state=state,
                        failure_reason=state.value,
                        manual_handoff_used=runtime.manual_handoff_used,
                    )
                # phone 模式：放行让下面 _build_actions 拿到 PHONE 的 submit_phone_and_code 候选

            candidates = self._build_actions(runtime, evidence)
            decision_source = "rule"
            decision = self._rule_provider.decide(evidence=evidence, candidates=candidates)
            retry_count = runtime.retry_counters.get(state.value, 0)

            # 卡顿检测（P0）：同一 state 连续多步 DOM 指纹不变 = 页面没动 = 卡住。
            # 与 retry_count（动作未达预期）互补——卡顿能更早发现「点了没反应」类问题。
            # EvidenceCollector.collect 通常已填指纹；自定义 collector 未填时这里兜底现算，
            # 保证卡顿检测对任何 collector 实现都生效。
            fingerprint = str(evidence.artifacts.get("dom_fingerprint", "")) or _compute_dom_fingerprint(
                evidence.url, evidence.signals, evidence.actionables
            )
            if fingerprint and fingerprint == runtime.dom_fingerprints.get(state.value):
                runtime.stall_counters[state.value] = runtime.stall_counters.get(state.value, 0) + 1
            else:
                runtime.stall_counters[state.value] = 0
            runtime.dom_fingerprints[state.value] = fingerprint
            stall_count = runtime.stall_counters.get(state.value, 0)

            if runtime.experience_store:
                learned_action_id = runtime.experience_store.find_action_id(evidence=evidence, candidates=candidates)
                if learned_action_id:
                    decision = Decision(
                        kind=DecisionKind.CHOOSE_ACTION,
                        action_id=learned_action_id,
                        confidence=0.98,
                        reason_code="EXPERIENCE_MATCH",
                    )
                    decision_source = "experience"

            # 规则升级：onboarding 卡住时（retry_count >= 1）自动切换到 skip_onboarding，
            # 不依赖 LLM 是否启用——LLM 未配置时这是唯一兜底。
            if (
                state == AutomationState.ABOUT_YOU
                and retry_count >= 1
                and evidence.signals.get("has_onboarding_prompt")
                and any(c.action_id == "skip_onboarding" for c in candidates)
                and decision.kind == DecisionKind.CHOOSE_ACTION
                and decision.action_id == "fill_about_you"
            ):
                runtime.logger.warning(
                    "ABOUT_YOU 卡住 retry=%d 且存在 onboarding 信号，规则升级为 skip_onboarding。",
                    retry_count,
                )
                decision = Decision(
                    kind=DecisionKind.CHOOSE_ACTION,
                    action_id="skip_onboarding",
                    confidence=0.9,
                    reason_code="RULE_UPGRADE_ONBOARDING_SKIP",
                )
                decision_source = "rule_upgrade"

            stall_threshold = int(getattr(runtime.config, "llm_stall_threshold", 2))
            should_llm = runtime.llm_provider and (
                state in {AutomationState.UNKNOWN, AutomationState.ERROR}
                or retry_count >= 1
                or stall_count >= stall_threshold
            )
            if should_llm:
                # 把「卡在哪/为什么卡」结构化喂给 LLM（build_llm_evidence_payload 会脱敏）。
                evidence.artifacts["stall_context"] = build_stall_context(
                    evidence, retry_count=retry_count, stall_count=stall_count
                )
                # 按需截图：仅卡顿达阈值且 vision 开启时附截图调多模态（默认纯文本省 token）。
                screenshot_b64 = ""
                shot_threshold = int(getattr(runtime.config, "llm_screenshot_on_stall_threshold", 3))
                if (
                    bool(getattr(runtime.config, "llm_vision_enabled", False))
                    and stall_count >= shot_threshold
                ):
                    screenshot_b64 = _capture_screenshot_b64(runtime.page)
                    if screenshot_b64:
                        _emit_runtime_event(
                            runtime,
                            "warning",
                            state=state,
                            payload={
                                "message": f"卡顿升级（stall={stall_count}）：附截图调多模态 LLM",
                                "stall_context": evidence.artifacts["stall_context"],
                            },
                        )
                llm_decision = runtime.llm_provider.decide(
                    evidence=evidence, candidates=candidates, screenshot_b64=screenshot_b64
                )
                if llm_decision.kind == DecisionKind.CHOOSE_ACTION:
                    decision = llm_decision
                    decision_source = "llm"
                    runtime.llm_uncertain_counters[state.value] = 0
                else:
                    runtime.llm_uncertain_counters[state.value] = runtime.llm_uncertain_counters.get(state.value, 0) + 1
                    if runtime.llm_uncertain_counters[state.value] >= runtime.config.llm_max_consecutive_uncertain:
                        if self._try_manual_handoff(runtime, evidence, "LLM_UNCERTAIN"):
                            runtime.llm_uncertain_counters[state.value] = 0
                            continue
                        return MachineResult(
                            success=False,
                            final_state=state,
                            failure_reason="LLM_UNCERTAIN",
                            manual_handoff_used=runtime.manual_handoff_used,
                        )

            action = next((item for item in candidates if item.action_id == decision.action_id), None)
            if action is None:
                runtime.retry_counters[state.value] = retry_count + 1
                if runtime.retry_counters[state.value] > self._retry_limit_for(state, runtime):
                    # 结构化候选耗尽（规则/经验/受限LLM 都没救回来）→ 枚举兜底最后一搏，再 manual_handoff。
                    if self._try_assist_fallback(runtime, evidence, state):
                        runtime.retry_counters[state.value] = 0
                        continue
                    if self._try_manual_handoff(runtime, evidence, "NO_ACTION"):
                        runtime.retry_counters[state.value] = 0
                        continue
                    return MachineResult(
                        success=False,
                        final_state=state,
                        failure_reason="NO_ACTION",
                        manual_handoff_used=runtime.manual_handoff_used,
                    )
                continue

            exec_result = self._executor.execute(runtime, action)
            after = self._collector.collect(runtime, step_name=f"{state.value.lower()}_after")
            verify = self._verifier.verify(action, after) if exec_result.ok else exec_result
            self._record(runtime, after, [action])
            if after.primary_state != last_emitted_state:
                _emit_runtime_event(
                    runtime,
                    "state_change",
                    state=after.primary_state,
                    payload={
                        "message": f"进入状态 {after.primary_state.value}",
                        "step_name": after.step_name,
                        "url": after.url,
                        "signals": after.signals,
                    },
                )
                last_emitted_state = after.primary_state
            # 自进化记账（P0-4）：llm/experience 来源的决策记成败，rule/rule_upgrade 不记
            # （硬编码规则无需进化）。关键：experience 命中后 verify 失败也记 fail——
            # 这是淘汰闭环，否则坏经验（0.98 置信度直接选）会永久复用。
            if runtime.experience_store and decision_source in {"llm", "experience"}:
                try:
                    if verify.ok:
                        runtime.experience_store.record_success(
                            evidence=evidence, action=action, source=decision_source
                        )
                    else:
                        recorder = getattr(runtime.experience_store, "record_failure", None)
                        if callable(recorder):
                            recorder(evidence=evidence, action=action, source=decision_source)
                except Exception:
                    pass

            if verify.ok:
                runtime.retry_counters[state.value] = 0
                continue

            runtime.retry_counters[state.value] = retry_count + 1
            if runtime.retry_counters[state.value] > self._retry_limit_for(state, runtime):
                _run_triage(runtime, after, verify.reason_code or state.value)
                # 动作执行了但 verify 反复失败 → 枚举兜底最后一搏，再 manual_handoff。
                if self._try_assist_fallback(runtime, after, state):
                    runtime.retry_counters[state.value] = 0
                    continue
                if self._try_manual_handoff(runtime, after, verify.reason_code or state.value):
                    runtime.retry_counters[state.value] = 0
                    continue
                return MachineResult(
                    success=False,
                    final_state=after.primary_state,
                    failure_reason=verify.reason_code or state.value,
                    manual_handoff_used=runtime.manual_handoff_used,
                )

        return MachineResult(
            success=False,
            final_state=AutomationState.UNKNOWN,
            failure_reason="STEP_LIMIT_EXCEEDED",
            manual_handoff_used=runtime.manual_handoff_used,
        )

    def _build_actions(self, runtime: AutomationRuntime, evidence: Evidence) -> list[Action]:
        state = evidence.primary_state
        if state == AutomationState.ENTRY:
            # 按 registration_kind 派不同入口动作：
            #   phone 模式 → enter_signup_phone（点「電話番号で続行」按钮）
            #   email/默认 → enter_signup（填邮箱 → Enter）
            registration_kind = "email"
            if runtime is not None and runtime.config is not None:
                registration_kind = str(
                    getattr(runtime.config, "registration_kind", "email") or "email"
                ).strip().lower()
            if registration_kind == "phone":
                return [
                    Action(
                        action_id="enter_signup_phone",
                        kind=ActionKind.CLICK,
                        description="进入手机号注册分支（点「電話番号で続行」）",
                        params={"handler": "enter_signup_phone"},
                        expected_outcomes=[
                            AutomationState.PHONE,
                            AutomationState.AUTH,
                            AutomationState.ABOUT_YOU,
                            AutomationState.HOME,
                        ],
                    )
                ]
            return [
                Action(
                    action_id="enter_signup",
                    kind=ActionKind.CLICK,
                    description="进入注册页并提交邮箱",
                    params={"handler": "enter_signup"},
                    expected_outcomes=[
                        AutomationState.AUTH,
                        AutomationState.VERIFY_EMAIL,
                        AutomationState.ABOUT_YOU,
                        AutomationState.HOME,
                    ],
                )
            ]

        if state == AutomationState.AUTH:
            if evidence.signals.get("has_password_input"):
                return [
                    Action(
                        action_id="submit_password",
                        kind=ActionKind.FILL,
                        description="填写密码并提交",
                        params={"handler": "submit_password"},
                        expected_outcomes=[
                            AutomationState.VERIFY_EMAIL,
                            AutomationState.ABOUT_YOU,
                            AutomationState.HOME,
                            AutomationState.PHONE,
                        ],
                    )
                ]
            return [
                Action(
                    action_id="wait_auth_navigation",
                    kind=ActionKind.WAIT,
                    description="等待鉴权页面继续跳转",
                    params={"handler": "wait_short"},
                    expected_outcomes=[
                        AutomationState.VERIFY_EMAIL,
                        AutomationState.ABOUT_YOU,
                        AutomationState.HOME,
                    ],
                )
            ]

        if state == AutomationState.VERIFY_EMAIL:
            return [
                Action(
                    action_id="submit_email_code",
                    kind=ActionKind.FILL,
                    description="拉取并填写邮箱验证码",
                    params={"handler": "verify_email"},
                    expected_outcomes=[
                        AutomationState.ABOUT_YOU,
                        AutomationState.HOME,
                        AutomationState.PHONE,
                    ],
                )
            ]

        if state == AutomationState.PHONE:
            # 仅 phone 模式才会到这里（上面 PHONE state 处理已 gate）；
            # handler 只填号+提交，不等 OTP（见 handlers.py:submit_phone_and_code）。
            # 真实流程：填号 → AUTH(创建密码页) → VERIFY(SMS OTP页) → ABOUT_YOU/HOME。
            return [
                Action(
                    action_id="submit_phone_and_code",
                    kind=ActionKind.FILL,
                    description="选国家 → 填手机号 → 提交（创建密码页留给 AUTH 状态）",
                    params={"handler": "submit_phone_and_code"},
                    expected_outcomes=[
                        AutomationState.AUTH,          # 创建密码页（OpenAI 填号后下一步）
                        AutomationState.VERIFY_EMAIL,  # SMS OTP 页（密码后才发短信）
                        AutomationState.ABOUT_YOU,
                        AutomationState.HOME,
                    ],
                )
            ]

        if state == AutomationState.ABOUT_YOU:
            actions: list[Action] = [
                Action(
                    action_id="fill_about_you",
                    kind=ActionKind.FILL,
                    description="填写 about-you 表单",
                    params={"handler": "fill_about_you"},
                    expected_outcomes=[AutomationState.HOME, AutomationState.PHONE],
                ),
            ]
            # 仅当检测到注册后 onboarding 问卷信号时才暴露 skip_onboarding 候选，
            # 作为 LLM 兜底的有效选项（点底部最后一个按钮 = 跳过/Skip，绕开
            # primary 按钮可能 disabled 的情况）。
            if evidence.signals.get("has_onboarding_prompt"):
                actions.append(
                    Action(
                        action_id="skip_onboarding",
                        kind=ActionKind.CLICK,
                        description="强制点击 onboarding 问卷的跳过/最后一个底部按钮",
                        params={"handler": "skip_onboarding"},
                        expected_outcomes=[AutomationState.HOME, AutomationState.PHONE],
                    )
                )
            actions.append(
                Action(
                    action_id="wait_short",
                    kind=ActionKind.WAIT,
                    description="页面可能正在提交或跳转，短暂等待后再判定",
                    params={"handler": "wait_short"},
                    expected_outcomes=[
                        AutomationState.ABOUT_YOU,
                        AutomationState.HOME,
                        AutomationState.PHONE,
                    ],
                )
            )
            return actions

        if state == AutomationState.ERROR:
            actions: list[Action] = []
            if evidence.signals.get("has_auth_timeout_error"):
                actions.append(
                    Action(
                        action_id="click_auth_try_again",
                        kind=ActionKind.CLICK,
                        description="OpenAI 鉴权超时，点击 Try again 重试",
                        params={"handler": "click_try_again"},
                        expected_outcomes=[
                            AutomationState.AUTH,
                            AutomationState.VERIFY_EMAIL,
                            AutomationState.ABOUT_YOU,
                            AutomationState.HOME,
                            AutomationState.PHONE,
                        ],
                    )
                )
            actions.append(
                Action(
                    action_id="recover_from_error",
                    kind=ActionKind.CLEAR_TARGET_STORAGE,
                    description="清理目标域状态并重新打开入口",
                    params={"handler": "recover_error"},
                    expected_outcomes=[
                        AutomationState.ENTRY,
                        AutomationState.AUTH,
                        AutomationState.VERIFY_EMAIL,
                        AutomationState.ABOUT_YOU,
                    ],
                )
            )
            return actions

        return [
            Action(
                action_id="wait_short",
                kind=ActionKind.WAIT,
                description="等待页面稳定",
                params={"handler": "wait_short"},
                expected_outcomes=[
                    AutomationState.ENTRY,
                    AutomationState.AUTH,
                    AutomationState.VERIFY_EMAIL,
                    AutomationState.ABOUT_YOU,
                    AutomationState.HOME,
                    AutomationState.ERROR,
                ],
            )
        ]

    def _retry_limit_for(self, state: AutomationState, runtime: AutomationRuntime) -> int:
        if state == AutomationState.VERIFY_EMAIL:
            return runtime.config.max_email_attempts
        if state == AutomationState.ERROR:
            return runtime.config.max_navigation_retries
        return 2

    def _try_assist_fallback(
        self, runtime: AutomationRuntime, evidence: Evidence, state: AutomationState
    ) -> bool:
        """grok_assist 式枚举兜底：硬失败前最后一层，让 LLM 自由选页面任意元素。

        默认关闭（assist_enabled=False）→ 立即返回 False，行为与升级前完全一致。
        开启后：枚举当前页面所有可交互元素 → 查兜底经验 → LLM 自由选 → 按 idx 执行 →
        verify（primary_state 是否变化）→ 成功则固化为兜底经验（独立 jsonl + DB）。

        风险高于预定义候选（自由点击），故只在结构化路径全部失败后启用，符合
        「规则优先状态机 + LLM 兜底」哲学。
        """
        if not getattr(runtime, "assist_enabled", False):
            return False
        if runtime.llm_provider is None and runtime.assist_experience is None:
            return False

        try:
            from src.automation.grok_assist import assisted_action
        except Exception:
            return False

        # 经验隔离维度：用 state 名编进 step（assisted_action 内部编进 url fragment）。
        step = f"openai_{state.value.lower()}"

        def _emit(event_type: str, st: Any, msg: str, **extra: Any) -> None:
            _emit_runtime_event(
                runtime, event_type, state=state, payload={"message": msg, **extra}
            )

        def _verify() -> bool:
            # 「前进信号」：兜底动作执行后页面 primary_state 是否离开当前 state。
            try:
                after = self._collector.collect(runtime, step_name="assist_verify")
                return after.primary_state != state
            except Exception:
                return False

        try:
            # 先尝试点击类（多数卡点是按钮没点对）；填值类暂不在通用兜底里盲填（避免乱填）。
            return bool(
                assisted_action(
                    runtime.page,
                    step=step,
                    want_fill=False,
                    experience=runtime.assist_experience,
                    llm_provider=runtime.llm_provider,
                    emit=_emit,
                    signals=dict(getattr(evidence, "signals", {}) or {}),
                    verify=_verify,
                )
            )
        except Exception as exc:
            runtime.logger.warning("枚举兜底异常（降级 manual_handoff）: %s", exc)
            return False

    def _try_manual_handoff(self, runtime: AutomationRuntime, evidence: Evidence, reason: str) -> bool:
        if runtime.manual_handoff_used and runtime.config.max_manual_handoffs <= 1:
            return False
        if runtime.retry_counters.get("MANUAL_HANDOFF", 0) >= runtime.config.max_manual_handoffs:
            return False

        handler = runtime.handlers.get("manual_handoff")
        if handler is None:
            return False

        payload = {
            "reason": reason,
            "state": evidence.primary_state.value,
            "url": evidence.url,
            "signals": evidence.signals,
        }
        _emit_runtime_event(
            runtime,
            "action",
            state=evidence.primary_state,
            payload={
                "message": f"进入人工接管：state={evidence.primary_state.value}, reason={reason}",
                "action_id": "manual_handoff",
                "result": "started",
                "reason": reason,
                "url": evidence.url,
            },
        )
        if runtime.artifact_recorder and runtime.run_id:
            runtime.artifact_recorder.record_handoff(run_id=runtime.run_id, payload=payload)

        runtime.retry_counters["MANUAL_HANDOFF"] = runtime.retry_counters.get("MANUAL_HANDOFF", 0) + 1
        accepted = bool(handler(runtime, payload))
        runtime.manual_handoff_used = runtime.manual_handoff_used or accepted
        return accepted

    @staticmethod
    def _record(runtime: AutomationRuntime, evidence: Evidence, actions: list[Action]) -> None:
        if runtime.artifact_recorder and runtime.run_id:
            runtime.artifact_recorder.record_step(
                run_id=runtime.run_id,
                evidence=evidence,
                actions=actions,
                screenshot_path=None,
            )
