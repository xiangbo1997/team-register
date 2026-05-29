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


if __name__ == "__main__":
    unittest.main()
