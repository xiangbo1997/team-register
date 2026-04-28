# -*- coding: utf-8 -*-
"""Orchestrator 分诊器（Triage）测试。"""

import json
import unittest
from unittest import mock

from src.automation.triage import (
    TRIAGE_CATEGORIES,
    TriageDecision,
    TriageDecisionProvider,
    VisionLLMClient,
    _build_user_text,
)


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.last_call = None

    def request_triage(self, **kwargs):
        self.last_call = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestTriageDecisionDataclass(unittest.TestCase):
    def test_frozen(self):
        d = TriageDecision(category="unknown", suggested_action="manual_handoff", confidence=0.0)
        with self.assertRaises(Exception):
            d.confidence = 0.9  # type: ignore[misc]

    def test_is_actionable_true_for_high_confidence_known(self):
        d = TriageDecision(category="ip_pollution", suggested_action="rotate_proxy", confidence=0.8)
        self.assertTrue(d.is_actionable)

    def test_is_actionable_false_for_low_confidence(self):
        d = TriageDecision(category="ip_pollution", suggested_action="rotate_proxy", confidence=0.3)
        self.assertFalse(d.is_actionable)

    def test_is_actionable_false_for_unknown(self):
        d = TriageDecision(category="unknown", suggested_action="manual_handoff", confidence=0.9)
        self.assertFalse(d.is_actionable)


class TestProviderDiagnose(unittest.TestCase):
    def test_happy_path_maps_category_to_action(self):
        provider = TriageDecisionProvider(
            client=_FakeClient(
                {
                    "category": "card_declined",
                    "confidence": 0.85,
                    "rationale": "看到红字 Card declined",
                    "evidence_summary": "Stripe 3DS 红色提示",
                }
            ),
        )
        decision = provider.diagnose(
            screenshot_bytes=None,
            page_url="https://chat.openai.com/payment",
            recent_logs=["checkout rejected"],
            signals={"has_role_alert": True},
        )
        self.assertEqual(decision.category, "card_declined")
        self.assertEqual(decision.suggested_action, "reorder_card")
        self.assertAlmostEqual(decision.confidence, 0.85)
        self.assertTrue(decision.is_actionable)

    def test_low_confidence_downgrades_to_unknown(self):
        provider = TriageDecisionProvider(
            client=_FakeClient({"category": "ip_pollution", "confidence": 0.3}),
        )
        decision = provider.diagnose(
            screenshot_bytes=None,
            page_url="https://x",
            recent_logs=[],
            signals={},
        )
        self.assertEqual(decision.category, "unknown")
        self.assertEqual(decision.suggested_action, "manual_handoff")
        self.assertFalse(decision.is_actionable)

    def test_unknown_category_string_maps_to_unknown(self):
        provider = TriageDecisionProvider(
            client=_FakeClient({"category": "wtf_is_this", "confidence": 0.99}),
        )
        decision = provider.diagnose(
            screenshot_bytes=None,
            page_url="",
            recent_logs=[],
            signals={},
        )
        self.assertEqual(decision.category, "unknown")

    def test_missing_confidence_defaults_to_zero(self):
        provider = TriageDecisionProvider(
            client=_FakeClient({"category": "captcha"}),
        )
        decision = provider.diagnose(
            screenshot_bytes=None,
            page_url="",
            recent_logs=[],
            signals={},
        )
        self.assertEqual(decision.confidence, 0.0)
        self.assertEqual(decision.category, "unknown")  # 低置信降级

    def test_client_exception_degrades_gracefully(self):
        provider = TriageDecisionProvider(
            client=_FakeClient(RuntimeError("network dead")),
        )
        decision = provider.diagnose(
            screenshot_bytes=None,
            page_url="",
            recent_logs=[],
            signals={},
        )
        self.assertEqual(decision.category, "unknown")
        self.assertIn("LLM 不可用", decision.rationale)

    def test_confidence_clamped_to_0_1(self):
        provider = TriageDecisionProvider(
            client=_FakeClient({"category": "rate_limited", "confidence": 2.5}),
        )
        decision = provider.diagnose(
            screenshot_bytes=None, page_url="", recent_logs=[], signals={},
        )
        self.assertEqual(decision.confidence, 1.0)
        self.assertEqual(decision.category, "rate_limited")

    def test_category_mapping_completeness(self):
        for cat in TRIAGE_CATEGORIES:
            if cat == "unknown":
                continue
            provider = TriageDecisionProvider(
                client=_FakeClient({"category": cat, "confidence": 0.9}),
            )
            d = provider.diagnose(
                screenshot_bytes=None, page_url="", recent_logs=[], signals={},
            )
            self.assertEqual(d.category, cat)
            self.assertEqual(d.suggested_action, TRIAGE_CATEGORIES[cat])


class TestUserTextRedaction(unittest.TestCase):
    def test_redacts_email_phone_token_in_logs(self):
        txt = _build_user_text(
            page_url="https://chat.openai.com/auth?token=abcdef12345",
            recent_logs=[
                "registered user@example.com",
                "Bearer eyJhbGciOiJIUzI1NiJ9.secret",
                "phone +12015550199 verified",
            ],
            signals={"has_role_alert": True, "has_password_input": False},
            last_error="card 4111111111111111 declined",
        )
        data = json.loads(txt)
        # email redacted
        self.assertNotIn("user@example.com", json.dumps(data))
        # bearer redacted
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9.secret", json.dumps(data))
        # card number redacted
        self.assertNotIn("4111111111111111", json.dumps(data))

    def test_logs_trimmed_to_last_10(self):
        logs = [f"line-{i}" for i in range(30)]
        txt = _build_user_text(page_url="", recent_logs=logs, signals={}, last_error="")
        data = json.loads(txt)
        self.assertEqual(len(data["recent_logs"]), 10)
        self.assertIn("line-29", data["recent_logs"][-1])

    def test_signals_strips_nested_values(self):
        txt = _build_user_text(
            page_url="",
            recent_logs=[],
            signals={"has_alert": True, "nested": {"x": 1}, "list": [1, 2]},
            last_error="",
        )
        data = json.loads(txt)
        self.assertIn("has_alert", data["signals"])
        self.assertNotIn("nested", data["signals"])
        self.assertNotIn("list", data["signals"])


class TestVisionLLMClientPayload(unittest.TestCase):
    def test_constructor_rejects_incomplete_config(self):
        with self.assertRaises(ValueError):
            VisionLLMClient(base_url="", api_key="k", model="m")
        with self.assertRaises(ValueError):
            VisionLLMClient(base_url="u", api_key="", model="m")
        with self.assertRaises(ValueError):
            VisionLLMClient(base_url="u", api_key="k", model="")

    def test_request_triage_sends_multipart_when_screenshot_present(self):
        client = VisionLLMClient(base_url="https://x", api_key="k", model="m")
        fake_resp = mock.Mock(status_code=200)
        fake_resp.raise_for_status = mock.Mock()
        fake_resp.json.return_value = {
            "choices": [
                {"message": {"content": json.dumps({"category": "captcha", "confidence": 0.9})}}
            ]
        }
        with mock.patch("src.automation.triage.requests.post", return_value=fake_resp) as mp:
            out = client.request_triage(
                screenshot_bytes=b"fakepngbytes",
                page_url="https://x",
                recent_logs=[],
                signals={},
            )
        self.assertEqual(out["category"], "captcha")
        _, kwargs = mp.call_args
        # 消息体包含 image_url 段
        body = kwargs["json"]
        user_content = body["messages"][1]["content"]
        self.assertTrue(any(item.get("type") == "image_url" for item in user_content))

    def test_request_triage_text_only_when_screenshot_none(self):
        client = VisionLLMClient(base_url="https://x", api_key="k", model="m")
        fake_resp = mock.Mock(status_code=200)
        fake_resp.raise_for_status = mock.Mock()
        fake_resp.json.return_value = {
            "choices": [
                {"message": {"content": json.dumps({"category": "unknown", "confidence": 0.0})}}
            ]
        }
        with mock.patch("src.automation.triage.requests.post", return_value=fake_resp) as mp:
            client.request_triage(
                screenshot_bytes=None,
                page_url="https://x",
                recent_logs=[],
                signals={},
            )
        body = mp.call_args.kwargs["json"]
        user_content = body["messages"][1]["content"]
        self.assertFalse(any(item.get("type") == "image_url" for item in user_content))


if __name__ == "__main__":
    unittest.main()
