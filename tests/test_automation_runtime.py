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


if __name__ == "__main__":
    unittest.main()
