# -*- coding: utf-8 -*-
"""Orchestrator 分诊器（Triage）测试。"""

import json
import unittest
from unittest import mock

from src.automation.triage import (
    ENGINEERING_CATEGORIES,
    TRIAGE_CATEGORIES,
    TriageDecision,
    TriageDecisionProvider,
    VisionLLMClient,
    _build_user_text,
    _summarize_actions,
    build_triage_provider,
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


class TestEngineeringCategories(unittest.TestCase):
    """工程类卡点诊断（selector_stale / handler_stuck / dom_drift）。"""

    def test_engineering_categories_map_to_review_code(self):
        for cat in ENGINEERING_CATEGORIES:
            self.assertEqual(TRIAGE_CATEGORIES[cat], "review_code")

    def test_is_engineering_flag(self):
        eng = TriageDecision(category="selector_stale", suggested_action="review_code", confidence=0.9)
        self.assertTrue(eng.is_engineering)
        risk = TriageDecision(category="ip_pollution", suggested_action="rotate_proxy", confidence=0.9)
        self.assertFalse(risk.is_engineering)

    def test_fix_suggestion_kept_for_engineering(self):
        provider = TriageDecisionProvider(
            client=_FakeClient(
                {
                    "category": "selector_stale",
                    "confidence": 0.9,
                    "rationale": "locator 反复超时",
                    "fix_suggestion": "submit_password 的 selector 需补 input[name=...] fallback",
                }
            ),
        )
        decision = provider.diagnose(
            screenshot_bytes=None, page_url="", recent_logs=[], signals={},
        )
        self.assertEqual(decision.category, "selector_stale")
        self.assertTrue(decision.is_engineering)
        self.assertIn("fallback", decision.fix_suggestion)

    def test_fix_suggestion_dropped_for_risk_category(self):
        # 风控类即便 LLM 误填 fix_suggestion 也应丢弃（避免前端把"换代理"当代码建议）。
        provider = TriageDecisionProvider(
            client=_FakeClient(
                {
                    "category": "ip_pollution",
                    "confidence": 0.9,
                    "fix_suggestion": "不该出现的代码建议",
                }
            ),
        )
        decision = provider.diagnose(
            screenshot_bytes=None, page_url="", recent_logs=[], signals={},
        )
        self.assertEqual(decision.category, "ip_pollution")
        self.assertEqual(decision.fix_suggestion, "")


class TestRecentActions(unittest.TestCase):
    """决策序列透传与脱敏（handler_stuck 判定依据）。"""

    def test_summarize_keeps_low_sensitivity_fields(self):
        actions = [
            {"action_id": "a1", "kind": "fill", "description": "填密码", "result": "ok"},
            {"action_id": "a2", "kind": "click", "description": "续行", "result": "noop"},
        ]
        out = _summarize_actions(actions)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["action_id"], "a1")
        self.assertEqual(out[0]["kind"], "fill")
        self.assertEqual(out[0]["result"], "ok")

    def test_summarize_trims_to_last_10(self):
        actions = [{"action_id": f"a{i}", "kind": "click", "description": "x", "result": "ok"} for i in range(20)]
        out = _summarize_actions(actions)
        self.assertEqual(len(out), 10)
        self.assertEqual(out[-1]["action_id"], "a19")

    def test_summarize_redacts_description(self):
        actions = [{"action_id": "a1", "kind": "fill", "description": "填入 user@example.com", "result": "ok"}]
        out = _summarize_actions(actions)
        self.assertNotIn("user@example.com", json.dumps(out, ensure_ascii=False))

    def test_recent_actions_threaded_into_user_text(self):
        txt = _build_user_text(
            page_url="",
            recent_logs=[],
            signals={},
            last_error="",
            recent_actions=[{"action_id": "a1", "kind": "click", "description": "续行", "result": "ok"}],
        )
        data = json.loads(txt)
        self.assertIn("recent_actions", data)
        self.assertEqual(data["recent_actions"][0]["action_id"], "a1")

    def test_recent_actions_passed_to_client(self):
        client = _FakeClient({"category": "handler_stuck", "confidence": 0.9, "fix_suggestion": "x"})
        provider = TriageDecisionProvider(client=client)
        provider.diagnose(
            screenshot_bytes=None,
            page_url="",
            recent_logs=[],
            signals={},
            recent_actions=[{"action_id": "a1", "kind": "click", "description": "续行", "result": "ok"}],
        )
        self.assertIn("recent_actions", client.last_call)
        self.assertEqual(client.last_call["recent_actions"][0]["action_id"], "a1")


class TestBuildTriageProvider(unittest.TestCase):
    """build_triage_provider 工厂（默认关闭 + 失败降级）。"""

    class _Cfg:
        def __init__(self, **kw):
            self.triage_enabled = kw.get("triage_enabled", False)
            self.triage_base_url = kw.get("triage_base_url", "https://x")
            self.triage_api_key = kw.get("triage_api_key", "k")
            self.triage_model = kw.get("triage_model", "m")
            self.triage_timeout_ms = kw.get("triage_timeout_ms", 15000)
            self.triage_confidence_threshold = kw.get("triage_confidence_threshold", 0.6)
            # 回退复用的 LLM_* 端点
            self.llm_base_url = kw.get("llm_base_url", "")
            self.llm_api_key = kw.get("llm_api_key", "")
            self.llm_model = kw.get("llm_model", "")

    def test_disabled_returns_none(self):
        self.assertIsNone(build_triage_provider(self._Cfg(triage_enabled=False)))

    def test_enabled_complete_builds_provider(self):
        cfg = self._Cfg(triage_enabled=True)
        provider = build_triage_provider(cfg)
        self.assertIsInstance(provider, TriageDecisionProvider)

    def test_falls_back_to_llm_endpoint_when_triage_unset(self):
        # TRIAGE_* 全空，但 LLM_* 已配 → 回退复用，构造成功
        cfg = self._Cfg(
            triage_enabled=True,
            triage_base_url="", triage_api_key="", triage_model="",
            llm_base_url="https://proxy/v1", llm_api_key="k", llm_model="gpt-5.4",
        )
        provider = build_triage_provider(cfg)
        self.assertIsInstance(provider, TriageDecisionProvider)

    def test_partial_triage_falls_back_to_llm(self):
        # TRIAGE_* 只配了部分（缺 model）→ 整体回退 LLM_*
        cfg = self._Cfg(
            triage_enabled=True,
            triage_base_url="https://t", triage_api_key="tk", triage_model="",
            llm_base_url="https://proxy/v1", llm_api_key="lk", llm_model="gpt-5.4",
        )
        provider = build_triage_provider(cfg)
        self.assertIsInstance(provider, TriageDecisionProvider)

    def test_both_unset_degrades_to_none(self):
        # TRIAGE_* 和 LLM_* 都空 → 降级 None
        cfg = self._Cfg(
            triage_enabled=True,
            triage_base_url="", triage_api_key="", triage_model="",
            llm_base_url="", llm_api_key="", llm_model="",
        )
        self.assertIsNone(build_triage_provider(cfg))


if __name__ == "__main__":
    unittest.main()
