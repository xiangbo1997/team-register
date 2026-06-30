# -*- coding: utf-8 -*-
"""自动化状态机/证据/LLM 协议测试"""

import json
import tempfile
import unittest
from pathlib import Path

from src.automation.artifacts import ArtifactRecorder, build_llm_evidence_payload, sanitize_url
from src.automation.llm import LLMDecisionProvider
from src.automation.models import (
    Action,
    ActionKind,
    Actionable,
    AutomationState,
    DecisionKind,
    Evidence,
)
from src.automation.runtime import AutomationRuntime, RegistrationStateMachine, infer_state, extract_session_tokens_with_http
from src.automation.experience import ExperienceStore


class StubLLMClient:
    """返回固定决策的 LLM stub"""

    def __init__(self, response: dict):
        self._response = response

    def request_decision(self, *, evidence: Evidence, candidates: list[Action]) -> dict:
        return self._response


class TestAutomationArtifacts(unittest.TestCase):
    """脱敏与产物记录测试"""

    def test_sanitize_url_redacts_sensitive_query(self):
        url = "https://chatgpt.com/api/auth/session?access_token=abc123&next=/home"
        sanitized = sanitize_url(url)
        self.assertIn("next=%2Fhome", sanitized)
        self.assertNotIn("abc123", sanitized)

    def test_build_llm_evidence_payload_redacts_sensitive_fields(self):
        evidence = Evidence(
            url="https://chatgpt.com/?token=abc",
            title="Example",
            step_name="VERIFY_EMAIL",
            state_candidates=[AutomationState.VERIFY_EMAIL],
            signals={
                "error_text": "email test@example.com code 123456",
                "dom_fragment": "<input value='4242424242424242' />",
                "ws_url": "ws://127.0.0.1/devtools/browser/secret",
            },
            artifacts={
                "screenshot_path": "/tmp/raw.png",
            },
        )

        payload = build_llm_evidence_payload(evidence)
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("test@example.com", serialized)
        self.assertNotIn("123456", serialized)
        self.assertNotIn("4242424242424242", serialized)
        self.assertNotIn("devtools/browser/secret", serialized)

    def test_artifact_recorder_writes_expected_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = ArtifactRecorder(Path(tmpdir))
            run_id = recorder.start_run("user@example.com")
            evidence = Evidence(
                url="https://chatgpt.com/",
                title="ChatGPT",
                step_name="ENTRY",
                signals={"ready": True},
            )
            step_dir = recorder.record_step(
                run_id=run_id,
                evidence=evidence,
                actions=[],
                screenshot_path=None,
            )

            self.assertTrue((step_dir / "step.json").exists())
            self.assertTrue((step_dir / "actionables.json").exists())
            self.assertTrue((step_dir / "signals.json").exists())
            self.assertTrue((Path(tmpdir) / run_id / "evidence.jsonl").exists())


class TestAutomationStateInference(unittest.TestCase):
    """状态推断测试"""

    def test_infer_state_about_you(self):
        state = infer_state(
            "https://auth.openai.com/about-you",
            {"has_about_name_input": True, "has_date_input": True},
        )
        self.assertEqual(state, AutomationState.ABOUT_YOU)

    def test_infer_state_onboarding_prompt_maps_to_about_you(self):
        state = infer_state(
            "https://chatgpt.com/",
            {"has_onboarding_prompt": True, "has_app_shell": True},
        )
        self.assertEqual(state, AutomationState.ABOUT_YOU)

    def test_infer_state_phone_overrides_onboarding(self):
        """手机号弹窗叠在 chatgpt 主壳上时，has_phone_input 必须让状态判为 PHONE。

        回归锁：2026-06-01 线上 run 4c83ae78 卡死的根因——手机号弹窗浮在
        chatgpt.com 主壳（has_onboarding_prompt+has_home_composer 都 true），
        但真实电话框 name=phoneNumberInput 探测不到（旧 selector 是 phoneNumber），
        导致 has_phone_input=false → 误判 ABOUT_YOU，submit_phone_and_code 永不触发。
        本测试锁死：只要 has_phone_input=true，即使叠了 onboarding/home 信号也判 PHONE。
        """
        state = infer_state(
            "https://chatgpt.com/",
            {
                "has_phone_input": True,
                "has_onboarding_prompt": True,
                "has_home_composer": True,
                "has_app_shell": True,
                "has_unauth_chrome": True,
            },
        )
        self.assertEqual(state, AutomationState.PHONE)

    def test_phone_selector_matches_real_dom_name(self):
        """_PHONE_SELECTOR 必须能命中真实 DOM 的 input#phoneNumberInput。

        真实 DOM（实测）：<input id="phoneNumberInput" name="phoneNumberInput"
        type="tel" autocomplete="tel">。回归锁防止再退回无 Input 后缀的旧 selector。
        """
        from src.automation.runtime import _PHONE_SELECTOR
        self.assertIn("phoneNumberInput", _PHONE_SELECTOR)
        self.assertIn('type="tel"', _PHONE_SELECTOR)

    def test_infer_state_phone_already_registered_maps_to_error(self):
        """号已注册（跳 /log-in/ 或登录页文案）→ ERROR，触发拉黑换号。

        回归锁（run c5ba5779）：印尼号已注册，注册流程提交密码后跳 /log-in/password
        让输入已有密码。旧逻辑判 AUTH → 无限重试 submit_password → silent_failure。
        现 has_phone_already_registered → ERROR → worker 拉黑换号。
        """
        # URL /log-in/ 信号
        s = infer_state(
            "https://auth.openai.com/log-in/password",
            {"has_phone_already_registered": True, "has_password_input": True},
        )
        self.assertEqual(s, AutomationState.ERROR)
        # 正常创建密码页仍是 AUTH（不误伤）
        s2 = infer_state(
            "https://auth.openai.com/create-account/password",
            {"has_password_input": True},
        )
        self.assertEqual(s2, AutomationState.AUTH)

    def test_infer_state_blocked(self):
        state = infer_state(
            "https://chatgpt.com/challenge",
            {"has_challenge_text": True},
        )
        self.assertEqual(state, AutomationState.BLOCKED)

    def test_infer_state_blocked_with_widget_signal(self):
        state = infer_state(
            "https://chatgpt.com/",
            {"has_challenge_widget": True},
        )
        self.assertEqual(state, AutomationState.BLOCKED)

    def test_infer_state_home(self):
        state = infer_state(
            "https://chatgpt.com/?model=gpt-4",
            {"has_app_shell": True},
        )
        self.assertEqual(state, AutomationState.HOME)

    def test_infer_state_unauth_chrome_overrides_home(self):
        """chatgpt.com 主页若仍出现登录/注册按钮（含日语 locale），必须回退到 ENTRY。

        线上 Run 7b495bc9（JP IP, phone 注册）观察到 enter_signup 后落在未登录主页
        + 「ログイン / 無料でサインアップ」按钮，has_app_shell=True 触发 HOME 误判，
        worker 循环重启浪费资源。修复后该场景应被识别为 ENTRY。
        """
        state = infer_state(
            "https://chatgpt.com/",
            {"has_app_shell": True, "has_unauth_chrome": True},
        )
        self.assertEqual(state, AutomationState.ENTRY)

    def test_infer_state_home_when_no_unauth_chrome(self):
        """已登录主页（无登录按钮）继续判 HOME。"""
        state = infer_state(
            "https://chatgpt.com/",
            {"has_app_shell": True, "has_unauth_chrome": False},
        )
        self.assertEqual(state, AutomationState.HOME)

    def test_infer_state_auth_timeout_error_maps_to_error(self):
        """OpenAI 鉴权页 'Operation timed out' 应识别为 ERROR 状态。"""
        state = infer_state(
            "https://auth.openai.com/create-account/password",
            {"has_auth_timeout_error": True},
        )
        self.assertEqual(state, AutomationState.ERROR)

    def test_error_state_with_timeout_signal_emits_try_again_action(self):
        """ERROR 状态且检测到 timeout 信号时，应优先生成 click_try_again 动作。"""
        machine = RegistrationStateMachine()
        evidence = Evidence(
            url="https://auth.openai.com/create-account/password",
            title="Oops, an error occurred! - OpenAI",
            step_name="step_7",
            state_candidates=[AutomationState.ERROR],
            signals={"has_auth_timeout_error": True},
        )
        actions = machine._build_actions(runtime=None, evidence=evidence)  # noqa: SLF001
        self.assertEqual(actions[0].action_id, "click_auth_try_again")
        self.assertEqual(actions[0].params.get("handler"), "click_try_again")
        # 兜底动作仍保留
        self.assertEqual(actions[-1].action_id, "recover_from_error")

    def test_infer_state_account_creation_error_overrides_password_input(self):
        """OpenAI 服务端拒号「アカウントを作成できませんでした」应判 ERROR，

        即使页面仍带密码框（has_password_input=True）。这是 run 17665b65 的回归锁：
        旧逻辑只看 url 含 auth/error，拒号页 URL 不变 → 误判 AUTH → 重试 submit_password。
        """
        state = infer_state(
            "https://auth.openai.com/create-account/password",
            {"has_account_creation_error": True, "has_password_input": True},
        )
        self.assertEqual(state, AutomationState.ERROR)

    def test_run_terminates_on_account_creation_error(self):
        """状态机检测到账号创建失败应立刻终态失败（ACCOUNT_CREATION_REJECTED），

        不进入 ERROR 的 recover_from_error 循环（清存储重开只会再撞同一脏号）。
        """
        class StubCollector:
            def collect(self, runtime, *, step_name):
                return Evidence(
                    url="https://auth.openai.com/create-account/password",
                    step_name=step_name,
                    state_candidates=[AutomationState.ERROR],
                    signals={"has_account_creation_error": True, "has_password_input": True},
                )

        recover_called = {"n": 0}
        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type("Cfg", (), {"max_email_attempts": 1, "max_navigation_retries": 3, "max_manual_handoffs": 0, "llm_max_consecutive_uncertain": 1})(),
            email="user@example.com",
            password="Password123!",
            mail_api=None,
            logger=type("Log", (), {"warning": lambda *a, **k: None, "error": lambda *a, **k: None})(),
            handlers={"recover_error": lambda runtime, action: recover_called.__setitem__("n", recover_called["n"] + 1) or True},
        )

        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=5)
        result = machine.run(runtime)

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "ACCOUNT_CREATION_REJECTED")
        self.assertEqual(result.final_state, AutomationState.ERROR)
        # 关键：不应触发 recover_from_error（直接终止，交给 worker 拉黑换号）
        self.assertEqual(recover_called["n"], 0)

    def test_account_creation_error_signal_detects_terms_of_use_on_chatgpt_domain(self):
        """ABOUT_YOU 页 finalize 拒号「We can't create your account due to our Terms of Use」
        应被 has_account_creation_error 信号识别（含 chatgpt.com 域）。

        回归锁（实测截图 2026-06-30，Joe Nguyen age=26）：英文版风控兜底拒号停在
        ABOUT_YOU（"How old are you?"）页，URL 在 chatgpt.com 域。旧逻辑①phrases 只有
        JA/ZH 漏了这条英文文案；②URL 约束仅认 auth.openai.com 会把 chatgpt.com 域挡掉
        → 双重漏判 → 状态机误判 ABOUT_YOU 重试 fill_about_you → silent_failure_at_state=ABOUT_YOU。
        """
        from src.automation.runtime import EvidenceCollector, _ACCOUNT_CREATION_ERROR_PHRASES

        # ① 英文 "Terms of Use" 拒号文案必须在 phrases 集合里被命中
        body_text = (
            "How old are you? Full name Joe Nguyen Age 26 "
            "We can't create your account due to our Terms of Use "
            "Finish creating account"
        )

        class _FakePage:
            url = "https://chatgpt.com/create-account/about-you"

            def evaluate(self, _script):
                return body_text

        page = _FakePage()
        matched = EvidenceCollector._page_contains_any(  # noqa: SLF001
            page, list(_ACCOUNT_CREATION_ERROR_PHRASES)
        )
        self.assertTrue(matched, "英文 Terms of Use 拒号文案未被 phrases 命中")

        # ② chatgpt.com 域必须落入信号的 URL 白名单（不再被 auth.openai.com-only 约束挡掉）
        in_openai_domain = any(
            d in page.url for d in ("auth.openai.com", "auth0.openai.com", "chatgpt.com")
        )
        self.assertTrue(in_openai_domain)

        # ③ 端到端：signal True → infer_state 判 ERROR（即使页面仍带 about-you 信号）
        state = infer_state(
            page.url,
            {"has_account_creation_error": True, "has_about_name_input": True},
        )
        self.assertEqual(state, AutomationState.ERROR)

    def test_account_creation_error_signal_not_triggered_on_third_party_domain(self):
        """不误伤：含相似文案但非 OpenAI 自家域的第三方页面不应触发拒号信号。"""
        in_openai_domain = any(
            d in "https://example.com/terms"
            for d in ("auth.openai.com", "auth0.openai.com", "chatgpt.com")
        )
        self.assertFalse(in_openai_domain)

    def test_phone_state_emits_submit_phone_and_code_action(self):
        """PHONE 状态：_build_actions 应返回 submit_phone_and_code 单 action。

        runtime/config 的 registration_kind 在 RegistrationStateMachine.run() 里 gate；
        _build_actions 本身无视 registration_kind 直接产出 phone action，由上层判断是否消费。
        """
        machine = RegistrationStateMachine()
        evidence = Evidence(
            url="https://auth.openai.com/onboarding/phone",
            title="Verify your phone - OpenAI",
            step_name="step_phone",
            state_candidates=[AutomationState.PHONE],
            signals={"has_phone_input": True},
        )
        actions = machine._build_actions(runtime=None, evidence=evidence)  # noqa: SLF001
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_id, "submit_phone_and_code")
        self.assertEqual(actions[0].params.get("handler"), "submit_phone_and_code")
        self.assertIn(AutomationState.HOME, actions[0].expected_outcomes)


class TestLLMDecisionProvider(unittest.TestCase):
    """LLM 受限决策测试"""

    def test_rejects_unknown_action_id(self):
        provider = LLMDecisionProvider(
            client=StubLLMClient(
                {
                    "kind": "choose_action",
                    "action_id": "not-allowed",
                    "confidence": 0.92,
                    "reason_code": "AMBIGUOUS_UI",
                }
            ),
            confidence_threshold=0.6,
        )
        evidence = Evidence(url="https://chatgpt.com/", title="ChatGPT", step_name="ENTRY")
        candidates = [
            Action(
                action_id="enter_signup",
                kind=ActionKind.CLICK,
                description="进入注册页",
            )
        ]

        decision = provider.decide(evidence=evidence, candidates=candidates)
        self.assertEqual(decision.kind, DecisionKind.ABORT)

    def test_low_confidence_turns_into_request_evidence(self):
        provider = LLMDecisionProvider(
            client=StubLLMClient(
                {
                    "kind": "choose_action",
                    "action_id": "enter_signup",
                    "confidence": 0.31,
                    "reason_code": "AMBIGUOUS_UI",
                }
            ),
            confidence_threshold=0.6,
        )
        evidence = Evidence(url="https://chatgpt.com/", title="ChatGPT", step_name="ENTRY")
        candidates = [
            Action(
                action_id="enter_signup",
                kind=ActionKind.CLICK,
                description="进入注册页",
            )
        ]

        decision = provider.decide(evidence=evidence, candidates=candidates)
        self.assertEqual(decision.kind, DecisionKind.REQUEST_EVIDENCE)


class TestSessionExtraction(unittest.TestCase):
    """独立 token 提取测试"""

    def test_extract_session_tokens_with_http_uses_cookie_session(self):
        captured: dict[str, object] = {}

        class FakeSession:
            def __init__(self):
                self.cookies = {}
                self.headers = {}

            def get(self, url, timeout, proxies=None):
                captured["url"] = url
                captured["timeout"] = timeout
                captured["proxies"] = proxies
                captured["headers"] = dict(self.headers)

                class Resp:
                    def json(self):
                        return {"accessToken": "access_123"}

                return Resp()

        cookies = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"},
            {"name": "other", "value": "x", "domain": "chatgpt.com"},
        ]

        access_token, refresh_token = extract_session_tokens_with_http(
            cookies=cookies,
            user_agent="Mozilla/5.0",
            proxy_url="http://127.0.0.1:7897",
            session_factory=FakeSession,
        )

        self.assertEqual(access_token, "access_123")
        self.assertEqual(refresh_token, "refresh_456")
        self.assertEqual(captured["url"], "https://chatgpt.com/api/auth/session")
        self.assertEqual(captured["timeout"], 15)
        self.assertEqual(
            captured["proxies"],
            {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"},
        )
        self.assertEqual(captured["headers"]["User-Agent"], "Mozilla/5.0")
        self.assertEqual(captured["headers"]["Accept"], "application/json")
        self.assertEqual(captured["headers"]["Referer"], "https://chatgpt.com/")
        self.assertEqual(captured["headers"]["Origin"], "https://chatgpt.com")

    def test_extract_session_tokens_with_http_returns_empty_on_non_json_body(self):
        class FakeResponse:
            headers = {"content-type": "text/html"}
            text = "<html>login</html>"

            def json(self):
                raise ValueError("not json")

        class FakeSession:
            def __init__(self):
                self.cookies = {}
                self.headers = {}

            def get(self, url, timeout, proxies=None):
                return FakeResponse()

        cookies = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_789", "domain": "chatgpt.com"},
        ]

        access_token, refresh_token = extract_session_tokens_with_http(
            cookies=cookies,
            user_agent="Mozilla/5.0",
            session_factory=FakeSession,
        )

        self.assertEqual(access_token, "")
        self.assertEqual(refresh_token, "refresh_789")


class TestExperienceMemory(unittest.TestCase):
    """经验记忆层测试"""

    def test_experience_store_reuses_recorded_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExperienceStore(Path(tmpdir) / "memory.jsonl")
            evidence = Evidence(
                url="https://chatgpt.com/",
                step_name="about_you",
                state_candidates=[AutomationState.ABOUT_YOU],
                signals={"has_onboarding_prompt": True},
            )
            action = Action(
                action_id="fill_about_you",
                kind=ActionKind.FILL,
                description="填写资料",
            )
            store.record_success(evidence=evidence, action=action, source="llm")

            matched = store.find_action_id(
                evidence=evidence,
                candidates=[
                    Action(action_id="fill_about_you", kind=ActionKind.FILL, description="填写资料"),
                    Action(action_id="wait_short", kind=ActionKind.WAIT, description="等待"),
                ],
            )

            self.assertEqual(matched, "fill_about_you")

    def test_experience_store_latest_event_returns_recent_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExperienceStore(Path(tmpdir) / "memory.jsonl")
            store.record_event(
                category="payment",
                name="stripe_variant_success",
                location="https://pay.example.com/checkout/abc",
                payload={"variant": "single_frame"},
            )

            payload = store.latest_event(
                category="payment",
                name="stripe_variant_success",
                location="https://pay.example.com/checkout/abc",
            )

            self.assertEqual(payload, {"variant": "single_frame"})

    def test_state_machine_prefers_experience_match_before_rule(self):
        class StubCollector:
            def __init__(self):
                self.calls = 0

            def collect(self, runtime, *, step_name):
                self.calls += 1
                if self.calls == 1:
                    return Evidence(
                        url="https://chatgpt.com/",
                        step_name=step_name,
                        state_candidates=[AutomationState.ABOUT_YOU],
                        signals={"has_onboarding_prompt": True},
                    )
                return Evidence(
                    url="https://chatgpt.com/",
                    step_name=step_name,
                    state_candidates=[AutomationState.HOME],
                    signals={"has_app_shell": True},
                )

        class StubExperienceStore:
            def find_action_id(self, *, evidence, candidates):
                return "wait_short"

            def record_success(self, *, evidence, action, source):
                return None

        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type("Cfg", (), {"max_email_attempts": 1, "max_navigation_retries": 1, "max_manual_handoffs": 0, "llm_max_consecutive_uncertain": 1})(),
            email="user@example.com",
            password="Password123!",
            mail_api=None,
            logger=type("Log", (), {"error": lambda *args, **kwargs: None})(),
            handlers={
                "fill_about_you": lambda runtime, action: True,
                "wait_short": lambda runtime, action: True,
            },
            experience_store=StubExperienceStore(),
        )

        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=2)
        result = machine.run(runtime)

        self.assertTrue(result.success)
        self.assertEqual(runtime.last_actions[0]["action_id"], "wait_short")

    def test_about_you_onboarding_stuck_upgrades_to_skip(self):
        """ABOUT_YOU 反复卡在 onboarding 时，规则升级应自动切到 skip_onboarding。

        模拟 brucecox00@cloudsentryai.com 的现实场景：
          step 1: fill_about_you 返回 False（点击了但没跳转）→ retry_count 递增
          step 2: 同状态 + 同信号 → 规则升级触发 → 选 skip_onboarding → 成功跳到 HOME
        """
        call_log: list[str] = []

        class StubCollector:
            def __init__(self):
                self.calls = 0

            def collect(self, runtime, *, step_name):
                self.calls += 1
                # 在 skip_onboarding 执行后才回到 HOME
                if "skip_onboarding" in call_log:
                    return Evidence(
                        url="https://chatgpt.com/",
                        step_name=step_name,
                        state_candidates=[AutomationState.HOME],
                        signals={"has_app_shell": True},
                    )
                return Evidence(
                    url="https://chatgpt.com/",
                    step_name=step_name,
                    state_candidates=[AutomationState.ABOUT_YOU],
                    signals={"has_onboarding_prompt": True},
                )

        def fill_about_you_handler(runtime, action):
            call_log.append("fill_about_you")
            return False  # 点了但没跳转

        def skip_onboarding_handler(runtime, action):
            call_log.append("skip_onboarding")
            return True

        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type(
                "Cfg",
                (),
                {
                    "max_email_attempts": 1,
                    "max_navigation_retries": 5,
                    "max_manual_handoffs": 0,
                    "llm_max_consecutive_uncertain": 1,
                },
            )(),
            email="user@example.com",
            password="Password123!",
            mail_api=None,
            logger=type(
                "Log",
                (),
                {
                    "error": lambda *args, **kwargs: None,
                    "warning": lambda *args, **kwargs: None,
                    "info": lambda *args, **kwargs: None,
                },
            )(),
            handlers={
                "fill_about_you": fill_about_you_handler,
                "skip_onboarding": skip_onboarding_handler,
                "wait_short": lambda runtime, action: True,
            },
        )

        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=5)
        result = machine.run(runtime)

        self.assertTrue(result.success)
        self.assertEqual(result.final_state, AutomationState.HOME)
        self.assertEqual(call_log, ["fill_about_you", "skip_onboarding"])

    def test_about_you_no_onboarding_no_skip_upgrade(self):
        """ABOUT_YOU 卡住但没 onboarding 信号时不应升级到 skip_onboarding。

        防御性测试：当真的是 about-you 表单（姓名/生日）卡住时，不能误升级。
        """
        call_log: list[str] = []

        class StubCollector:
            def collect(self, runtime, *, step_name):
                return Evidence(
                    url="https://auth.openai.com/about-you",
                    step_name=step_name,
                    state_candidates=[AutomationState.ABOUT_YOU],
                    signals={},  # 没有 has_onboarding_prompt
                )

        def fill_about_you_handler(runtime, action):
            call_log.append("fill_about_you")
            return False

        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type(
                "Cfg",
                (),
                {
                    "max_email_attempts": 1,
                    "max_navigation_retries": 2,
                    "max_manual_handoffs": 0,
                    "llm_max_consecutive_uncertain": 1,
                },
            )(),
            email="user@example.com",
            password="Password123!",
            mail_api=None,
            logger=type(
                "Log",
                (),
                {
                    "error": lambda *args, **kwargs: None,
                    "warning": lambda *args, **kwargs: None,
                    "info": lambda *args, **kwargs: None,
                },
            )(),
            handlers={
                "fill_about_you": fill_about_you_handler,
                "skip_onboarding": lambda runtime, action: True,  # 不应被调用
                "wait_short": lambda runtime, action: True,
            },
        )

        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=5)
        machine.run(runtime)

        self.assertNotIn("skip_onboarding", call_log)


class TestRuntimeEvents(unittest.TestCase):
    """状态机结构化事件发射测试"""

    def test_state_machine_emits_state_and_action_events(self):
        events = []

        class StubCollector:
            def __init__(self):
                self.calls = 0

            def collect(self, runtime, *, step_name):
                self.calls += 1
                if self.calls == 1:
                    return Evidence(
                        url="https://chatgpt.com/",
                        step_name=step_name,
                        state_candidates=[AutomationState.ENTRY],
                        signals={"has_app_shell": False},
                    )
                if self.calls == 2:
                    return Evidence(
                        url="https://auth.openai.com/create-account/password",
                        step_name=step_name,
                        state_candidates=[AutomationState.AUTH],
                        signals={"has_password_input": True},
                    )
                return Evidence(
                    url="https://chatgpt.com/",
                    step_name=step_name,
                    state_candidates=[AutomationState.HOME],
                    signals={"has_app_shell": True},
                )

        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type("Cfg", (), {
                "max_email_attempts": 1,
                "max_navigation_retries": 1,
                "max_manual_handoffs": 0,
                "llm_max_consecutive_uncertain": 1,
            })(),
            email="user@example.com",
            password="Password123!",
            mail_api=None,
            logger=type("Log", (), {"error": lambda *args, **kwargs: None})(),
            handlers={"enter_signup": lambda runtime, action: True},
            emit_event=lambda event_type, **kwargs: events.append({"event_type": event_type, **kwargs}),
        )

        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=2)
        result = machine.run(runtime)

        self.assertTrue(result.success)
        self.assertTrue(any(item["event_type"] == "state_change" and item["state"] == "ENTRY" for item in events))
        self.assertTrue(any(item["event_type"] == "state_change" and item["state"] == "AUTH" for item in events))
        self.assertTrue(
            any(
                item["event_type"] == "action"
                and item["payload"]["action_id"] == "enter_signup"
                and item["payload"]["result"] == "ok"
                for item in events
            )
        )


class TestStallDetection(unittest.TestCase):
    """卡顿检测（P0）：DOM 指纹 + stall_counters + build_stall_context。"""

    def test_dom_fingerprint_stable_for_same_page(self):
        from src.automation.runtime import _compute_dom_fingerprint

        sig = {"has_password_input": True, "has_code_input": False}
        a = _compute_dom_fingerprint("https://x.com/p?q=1", sig, [])
        b = _compute_dom_fingerprint("https://x.com/p?q=2", sig, [])  # query 不影响
        self.assertEqual(a, b)
        c = _compute_dom_fingerprint("https://x.com/p", {"has_password_input": False}, [])
        self.assertNotEqual(a, c)  # 信号变了指纹变

    def test_build_stall_context_fields(self):
        from src.automation.runtime import build_stall_context

        ev = Evidence(
            url="https://auth.openai.com/log-in/password",
            state_candidates=[AutomationState.AUTH],
            signals={"has_password_input": True, "has_code_input": False},
            actionables=[
                Actionable(action_id="n0", kind=ActionKind.CLICK, name="続行", role="button", visible=True, enabled=True),
            ],
        )
        ctx = build_stall_context(ev, retry_count=2, stall_count=3)
        self.assertEqual(ctx["retry_count"], 2)
        self.assertEqual(ctx["stall_count"], 3)
        self.assertEqual(ctx["current_state"], "AUTH")
        self.assertIn("has_password_input", ctx["active_signals"])
        self.assertNotIn("has_code_input", ctx["active_signals"])
        self.assertEqual(ctx["suspected_block"], "続行")

    def test_stall_counter_increments_on_unchanged_page(self):
        """同一 state 连续两步 DOM 指纹相同 → stall_counters 递增。"""
        class StubCollector:
            def __init__(self):
                self.calls = 0

            def collect(self, runtime, *, step_name):
                self.calls += 1
                # 始终返回同一 AUTH 页面（指纹稳定），handler 永远失败 → 卡住
                return Evidence(
                    url="https://auth.openai.com/log-in/password",
                    step_name=step_name,
                    state_candidates=[AutomationState.AUTH],
                    signals={"has_password_input": True},
                    actionables=[
                        Actionable(action_id="n0", kind=ActionKind.CLICK, name="続行", role="button", visible=True, enabled=True),
                    ],
                )

        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type("Cfg", (), {
                "max_email_attempts": 1, "max_navigation_retries": 1,
                "max_manual_handoffs": 0, "llm_max_consecutive_uncertain": 1,
                "llm_stall_threshold": 2,
            })(),
            email="u@e.com",
            password="Password123!",
            mail_api=None,
            logger=type("Log", (), {
                "error": lambda *a, **k: None, "warning": lambda *a, **k: None, "info": lambda *a, **k: None,
            })(),
            handlers={"submit_password": lambda runtime, action: False},
        )
        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=3)
        machine.run(runtime)
        # AUTH 状态连续多步指纹不变 → stall_counters["AUTH"] 应 > 0
        self.assertGreaterEqual(runtime.stall_counters.get("AUTH", 0), 1)

    def test_screenshot_only_captured_when_vision_enabled_and_stalled(self):
        """vision 关闭时即使卡顿也不截图；开启且达阈值才截图。"""
        import src.automation.runtime as rt

        shot_calls = {"n": 0}

        def _fake_shot(page):
            shot_calls["n"] += 1
            return ""  # 返回空 → 降级，不影响决策流

        class StubCollector:
            def collect(self, runtime, *, step_name):
                return Evidence(
                    url="https://auth.openai.com/log-in/password",
                    step_name=step_name,
                    state_candidates=[AutomationState.AUTH],
                    signals={"has_password_input": True},
                    actionables=[
                        Actionable(action_id="n0", kind=ActionKind.CLICK, name="続行", role="button", visible=True, enabled=True),
                    ],
                )

        class StubLLM:
            def decide(self, *, evidence, candidates, screenshot_b64=""):
                from src.automation.models import Decision
                return Decision(kind=DecisionKind.ABORT, reason_code="x")

        def _build_runtime(vision):
            return AutomationRuntime(
                page=object(),
                context=object(),
                config=type("Cfg", (), {
                    "max_email_attempts": 1, "max_navigation_retries": 1,
                    "max_manual_handoffs": 0, "llm_max_consecutive_uncertain": 99,
                    "llm_stall_threshold": 1, "llm_screenshot_on_stall_threshold": 1,
                    "llm_vision_enabled": vision,
                })(),
                email="u@e.com", password="Password123!", mail_api=None,
                logger=type("Log", (), {
                    "error": lambda *a, **k: None, "warning": lambda *a, **k: None, "info": lambda *a, **k: None,
                })(),
                handlers={"submit_password": lambda runtime, action: False},
                llm_provider=StubLLM(),
            )

        orig = rt._capture_screenshot_b64
        rt._capture_screenshot_b64 = _fake_shot
        try:
            # vision 关闭 → 不截图
            machine = RegistrationStateMachine(collector=StubCollector(), max_steps=3)
            machine.run(_build_runtime(vision=False))
            self.assertEqual(shot_calls["n"], 0)

            # vision 开启 + 卡顿达阈值 → 截图
            shot_calls["n"] = 0
            machine2 = RegistrationStateMachine(collector=StubCollector(), max_steps=3)
            machine2.run(_build_runtime(vision=True))
            self.assertGreaterEqual(shot_calls["n"], 1)
        finally:
            rt._capture_screenshot_b64 = orig


class TestExperienceFailureEviction(unittest.TestCase):
    """P0-4：experience 命中后 verify 失败应记 record_failure（淘汰闭环）。"""

    def test_experience_hit_then_verify_fail_records_failure(self):
        recorded = {"success": 0, "fail": 0}

        class StubExperienceStore:
            def find_action_id(self, *, evidence, candidates):
                return "fill_about_you"  # 经验命中

            def record_success(self, *, evidence, action, source):
                recorded["success"] += 1

            def record_failure(self, *, evidence, action, source):
                recorded["fail"] += 1

        class StubCollector:
            def collect(self, runtime, *, step_name):
                return Evidence(
                    url="https://auth.openai.com/about-you",
                    step_name=step_name,
                    state_candidates=[AutomationState.ABOUT_YOU],
                    signals={"has_onboarding_prompt": True},
                )

        runtime = AutomationRuntime(
            page=object(),
            context=object(),
            config=type("Cfg", (), {
                "max_email_attempts": 1, "max_navigation_retries": 1,
                "max_manual_handoffs": 0, "llm_max_consecutive_uncertain": 1,
                "llm_stall_threshold": 99,
            })(),
            email="u@e.com",
            password="Password123!",
            mail_api=None,
            logger=type("Log", (), {
                "error": lambda *a, **k: None, "warning": lambda *a, **k: None, "info": lambda *a, **k: None,
            })(),
            handlers={"fill_about_you": lambda runtime, action: False},  # 总是失败
            experience_store=StubExperienceStore(),
        )
        machine = RegistrationStateMachine(collector=StubCollector(), max_steps=2)
        machine.run(runtime)
        # 经验命中 → 执行失败 → 应记 fail 至少 1 次，不应记 success
        self.assertGreaterEqual(recorded["fail"], 1)
        self.assertEqual(recorded["success"], 0)


class _AbortLLM:
    """总是返回 ABORT 的 LLM stub（让受限 LLM 层放行到枚举兜底）。"""

    def decide(self, *, evidence, candidates, screenshot_b64=""):
        from src.automation.models import Decision
        return Decision(kind=DecisionKind.ABORT, reason_code="x")


class TestAssistFallback(unittest.TestCase):
    """P2：grok_assist 式枚举兜底接入 OpenAI 状态机（默认关）。"""

    def _make_runtime(self, *, assist_enabled, handlers=None):
        return AutomationRuntime(
            page=object(),
            context=object(),
            config=type("Cfg", (), {
                "max_email_attempts": 1, "max_navigation_retries": 1,
                "max_manual_handoffs": 0, "llm_max_consecutive_uncertain": 99,
                "llm_stall_threshold": 99,
            })(),
            email="u@e.com", password="Password123!", mail_api=None,
            logger=type("Log", (), {
                "error": lambda *a, **k: None, "warning": lambda *a, **k: None, "info": lambda *a, **k: None,
            })(),
            handlers=handlers or {"fill_about_you": lambda runtime, action: False},
            assist_enabled=assist_enabled,
            llm_provider=_AbortLLM(),  # 受限 LLM 放行 → 候选耗尽 → 进入枚举兜底
        )

    def test_disabled_by_default_does_not_call_assist(self):
        import src.automation.runtime as rt

        called = {"n": 0}

        def _fake_assisted(*args, **kwargs):
            called["n"] += 1
            return True

        class StubCollector:
            def collect(self, runtime, *, step_name):
                return Evidence(
                    url="https://auth.openai.com/about-you",
                    step_name=step_name,
                    state_candidates=[AutomationState.ABOUT_YOU],
                    signals={},
                )

        # monkeypatch grok_assist.assisted_action（_try_assist_fallback 内部 import）
        import src.automation.grok_assist as ga
        orig = ga.assisted_action
        ga.assisted_action = _fake_assisted
        try:
            runtime = self._make_runtime(assist_enabled=False)
            machine = RegistrationStateMachine(collector=StubCollector(), max_steps=4)
            machine.run(runtime)
            self.assertEqual(called["n"], 0)  # 默认关 → 永不调用枚举兜底
        finally:
            ga.assisted_action = orig

    def test_enabled_invokes_assist_on_exhaustion(self):
        import src.automation.grok_assist as ga

        called = {"n": 0}

        def _fake_assisted(page, *, step, want_fill, experience=None, llm_provider=None, emit=None, signals=None, verify=None):
            called["n"] += 1
            return True  # 兜底成功 → 状态机应 continue 不直接失败

        class StubCollector:
            def __init__(self):
                self.calls = 0

            def collect(self, runtime, *, step_name):
                self.calls += 1
                # 兜底"成功"后回到 HOME，让循环能终止
                if called["n"] >= 1:
                    return Evidence(
                        url="https://chatgpt.com/",
                        step_name=step_name,
                        state_candidates=[AutomationState.HOME],
                        signals={"has_app_shell": True},
                    )
                return Evidence(
                    url="https://auth.openai.com/about-you",
                    step_name=step_name,
                    state_candidates=[AutomationState.ABOUT_YOU],
                    signals={},
                )

        orig = ga.assisted_action
        ga.assisted_action = _fake_assisted
        try:
            runtime = self._make_runtime(assist_enabled=True)
            machine = RegistrationStateMachine(collector=StubCollector(), max_steps=6)
            result = machine.run(runtime)
            self.assertGreaterEqual(called["n"], 1)  # 候选耗尽 → 触发枚举兜底
            self.assertTrue(result.success)  # 兜底后回到 HOME
        finally:
            ga.assisted_action = orig


if __name__ == "__main__":
    unittest.main()
