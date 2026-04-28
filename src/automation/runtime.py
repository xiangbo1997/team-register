# -*- coding: utf-8 -*-
"""注册状态机、证据采集、动作执行与 token 提取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from curl_cffi import requests as curl_requests

from src.automation.artifacts import ArtifactRecorder
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
_PHONE_SELECTOR = 'input[name="phoneNumber"]'
_DATE_SELECTOR = 'input[name="birthdate"], input[name="birthday"], input[autocomplete="bday"], [role="spinbutton"]'
_CODE_SELECTOR = 'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
_HOME_COMPOSER_SELECTOR = 'textarea, [contenteditable="true"], [data-testid*="composer"]'
_HOME_NAV_SELECTOR = 'aside a, aside button, nav a, nav button'
_CHALLENGE_WIDGET_SELECTOR = 'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[title*="captcha"], [data-sitekey], input[name*="captcha"]'
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
    recent_log_buffer: list[str] = field(default_factory=list)


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
    if "auth/error" in current or signals.get("has_auth_error") or signals.get("has_auth_timeout_error"):
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
        return AutomationState.HOME
    if "auth.openai.com" in current or "auth0.openai.com" in current:
        return AutomationState.AUTH
    if not current or current.startswith("about:blank") or "chatgpt.com" in current:
        return AutomationState.ENTRY
    return AutomationState.UNKNOWN


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
            "has_auth_timeout_error": self._page_contains_any(
                page, ["operation timed out", "oops, an error occurred"]
            ) and "auth.openai.com" in url,
            "has_chrome_error": url.startswith("chrome-error://"),
            "has_challenge_text": self._page_contains_any(page, ["captcha", "challenge", "verify you are human"]),
            "has_challenge_widget": self._count(page, _CHALLENGE_WIDGET_SELECTOR) > 0,
            "has_home_composer": home_composer_count > 0,
            "has_dense_app_nav": home_nav_count >= 6,
            "has_app_shell": "auth" not in url and (home_composer_count > 0 or home_nav_count >= 6),
            "has_onboarding_prompt": onboarding_metrics["prompt_present"],
            "onboarding_option_count": onboarding_metrics["option_count"],
            "onboarding_footer_button_count": onboarding_metrics["footer_button_count"],
        }
        state = infer_state(url, signals)
        return Evidence(
            url=url,
            title=title,
            timestamp=datetime.now(timezone.utc).isoformat(),
            step_name=step_name,
            state_candidates=[state],
            actionables=self._collect_actionables(page),
            signals=signals,
            last_actions=list(runtime.last_actions[-10:]),
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

            if state in {AutomationState.BLOCKED, AutomationState.PHONE}:
                _run_triage(runtime, evidence, state.value)
                if self._try_manual_handoff(runtime, evidence, state.value):
                    continue
                return MachineResult(
                    success=False,
                    final_state=state,
                    failure_reason=state.value,
                    manual_handoff_used=runtime.manual_handoff_used,
                )

            candidates = self._build_actions(runtime, evidence)
            decision_source = "rule"
            decision = self._rule_provider.decide(evidence=evidence, candidates=candidates)
            retry_count = runtime.retry_counters.get(state.value, 0)
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

            if runtime.llm_provider and (state in {AutomationState.UNKNOWN, AutomationState.ERROR} or retry_count >= 1):
                llm_decision = runtime.llm_provider.decide(evidence=evidence, candidates=candidates)
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
            if verify.ok and runtime.experience_store and decision_source == "llm":
                runtime.experience_store.record_success(
                    evidence=evidence,
                    action=action,
                    source=decision_source,
                )

            if verify.ok:
                runtime.retry_counters[state.value] = 0
                continue

            runtime.retry_counters[state.value] = retry_count + 1
            if runtime.retry_counters[state.value] > self._retry_limit_for(state, runtime):
                _run_triage(runtime, after, verify.reason_code or state.value)
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

        if state == AutomationState.ABOUT_YOU:
            return [
                Action(
                    action_id="fill_about_you",
                    kind=ActionKind.FILL,
                    description="填写 about-you 表单",
                    params={"handler": "fill_about_you"},
                    expected_outcomes=[AutomationState.HOME, AutomationState.PHONE],
                ),
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
                ),
            ]

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
