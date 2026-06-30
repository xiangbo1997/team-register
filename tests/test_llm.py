# -*- coding: utf-8 -*-
"""LLMDecisionProvider 多模态（按需截图）+ vision 降级测试。"""

import unittest

from src.automation.llm import LLMDecisionProvider
from src.automation.models import Action, ActionKind, AutomationState, DecisionKind, Evidence


def _evidence():
    return Evidence(
        url="https://auth.openai.com/log-in/password",
        state_candidates=[AutomationState.AUTH],
        signals={"has_password_input": True},
    )


def _candidates():
    return [Action(action_id="submit_password", kind=ActionKind.FILL, description="填密码")]


class RecordingClient:
    """记录 request_decision 收到的参数。"""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def request_decision(self, *, evidence, candidates, screenshot_b64="__absent__"):
        self.calls.append({"screenshot_b64": screenshot_b64})
        return self.response


_GOOD = {"kind": "choose_action", "action_id": "submit_password", "confidence": 0.9}


class TestVisionGating(unittest.TestCase):
    def test_vision_disabled_never_passes_screenshot(self):
        client = RecordingClient(_GOOD)
        provider = LLMDecisionProvider(client=client, vision_enabled=False)
        decision = provider.decide(
            evidence=_evidence(), candidates=_candidates(), screenshot_b64="ABC123"
        )
        self.assertEqual(decision.kind, DecisionKind.CHOOSE_ACTION)
        # vision 关 → 走纯文本分支，request_decision 不带 screenshot_b64
        self.assertEqual(client.calls[0]["screenshot_b64"], "__absent__")

    def test_vision_enabled_passes_screenshot(self):
        client = RecordingClient(_GOOD)
        provider = LLMDecisionProvider(client=client, vision_enabled=True)
        provider.decide(evidence=_evidence(), candidates=_candidates(), screenshot_b64="ABC123")
        self.assertEqual(client.calls[0]["screenshot_b64"], "ABC123")

    def test_vision_enabled_but_no_screenshot_falls_back_to_text(self):
        client = RecordingClient(_GOOD)
        provider = LLMDecisionProvider(client=client, vision_enabled=True)
        provider.decide(evidence=_evidence(), candidates=_candidates(), screenshot_b64="")
        self.assertEqual(client.calls[0]["screenshot_b64"], "__absent__")

    def test_client_exception_degrades_to_abort(self):
        class BadClient:
            def request_decision(self, **kwargs):
                raise RuntimeError("model does not support image_url")

        provider = LLMDecisionProvider(client=BadClient(), vision_enabled=True)
        decision = provider.decide(
            evidence=_evidence(), candidates=_candidates(), screenshot_b64="ABC"
        )
        self.assertEqual(decision.kind, DecisionKind.ABORT)
        self.assertEqual(decision.reason_code, "LLM_UNAVAILABLE")


class TestMultimodalPayload(unittest.TestCase):
    """OpenAICompatibleLLMClient 截图时构造 image_url 数组 content。"""

    def test_request_decision_builds_image_content(self):
        from src.automation.llm import OpenAICompatibleLLMClient

        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '{"kind":"abort"}'}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return FakeResp()

        import src.automation.llm as llm_mod

        orig = llm_mod.requests.post
        llm_mod.requests.post = fake_post
        try:
            client = OpenAICompatibleLLMClient(
                base_url="http://x", api_key="k", model="gpt-4o"
            )
            client.request_decision(
                evidence=_evidence(), candidates=_candidates(), screenshot_b64="IMG64"
            )
        finally:
            llm_mod.requests.post = orig

        content = captured["payload"]["messages"][1]["content"]
        self.assertIsInstance(content, list)
        kinds = {item["type"] for item in content}
        self.assertIn("image_url", kinds)
        self.assertIn("text", kinds)

    def test_request_decision_text_only_when_no_screenshot(self):
        from src.automation.llm import OpenAICompatibleLLMClient

        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '{"kind":"abort"}'}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return FakeResp()

        import src.automation.llm as llm_mod

        orig = llm_mod.requests.post
        llm_mod.requests.post = fake_post
        try:
            client = OpenAICompatibleLLMClient(base_url="http://x", api_key="k", model="m")
            client.request_decision(evidence=_evidence(), candidates=_candidates())
        finally:
            llm_mod.requests.post = orig

        content = captured["payload"]["messages"][1]["content"]
        self.assertIsInstance(content, str)  # 纯文本


if __name__ == "__main__":
    unittest.main()
