# -*- coding: utf-8 -*-
"""验证 RegistrationStateMachine 在 BLOCKED / 重试超限时调用 triage。"""

import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from src.automation.models import AutomationState, Evidence
from src.automation.runtime import AutomationRuntime, _run_triage
from src.automation.triage import TriageDecision


class _FakeTriage:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def diagnose(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


def _build_runtime(triage_provider=None, page=None):
    return AutomationRuntime(
        page=page or mock.Mock(spec=[]),  # no screenshot method
        context=mock.Mock(),
        config=SimpleNamespace(triage_enabled=True),
        email="x@y.z",
        password="pw",
        mail_api=mock.Mock(),
        logger=logging.getLogger("test"),
        handlers={},
        triage_provider=triage_provider,
        recent_log_buffer=["log-1", "log-2"],
    )


class TestRunTriage(unittest.TestCase):
    def test_no_provider_is_noop(self):
        rt = _build_runtime(triage_provider=None)
        events = []
        rt.emit_event = lambda t, state=None, payload=None: events.append((t, payload))
        _run_triage(rt, Evidence(url="u"), "BLOCKED")
        self.assertEqual(events, [])

    def test_emits_triage_event_when_provider_present(self):
        decision = TriageDecision(
            category="ip_pollution",
            suggested_action="rotate_proxy",
            confidence=0.9,
            rationale="IP 已被标记",
        )
        rt = _build_runtime(triage_provider=_FakeTriage(decision))
        events = []
        rt.emit_event = lambda t, state=None, payload=None: events.append((t, payload))
        _run_triage(rt, Evidence(url="u", signals={"has_role_alert": True}), "BLOCKED")

        self.assertEqual(len(events), 1)
        kind, payload = events[0]
        self.assertEqual(kind, "triage")
        self.assertEqual(payload["category"], "ip_pollution")
        self.assertEqual(payload["suggested_action"], "rotate_proxy")
        self.assertTrue(payload["is_actionable"])

    def test_diagnose_receives_redacted_context(self):
        fake = _FakeTriage(
            TriageDecision(category="unknown", suggested_action="manual_handoff", confidence=0.1)
        )
        rt = _build_runtime(triage_provider=fake)
        _run_triage(rt, Evidence(url="https://x", signals={"a": 1}), "ERR")

        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["page_url"], "https://x")
        self.assertEqual(call["last_error"], "ERR")
        self.assertEqual(call["signals"], {"a": 1})
        self.assertEqual(call["recent_logs"], ["log-1", "log-2"])

    def test_diagnose_exception_is_swallowed(self):
        class _Bad:
            def diagnose(self, **kwargs):
                raise RuntimeError("boom")

        rt = _build_runtime(triage_provider=_Bad())
        events = []
        rt.emit_event = lambda t, state=None, payload=None: events.append((t, payload))
        _run_triage(rt, Evidence(url="u"), "x")
        # 异常被吞，没有事件发出
        self.assertEqual(events, [])

    def test_screenshot_captured_when_page_supports_it(self):
        page = mock.Mock()
        page.screenshot.return_value = b"fakepng"
        fake = _FakeTriage(
            TriageDecision(category="captcha", suggested_action="solve_captcha", confidence=0.8)
        )
        rt = _build_runtime(triage_provider=fake, page=page)
        _run_triage(rt, Evidence(url="u"), "BLOCKED")

        page.screenshot.assert_called_once()
        self.assertEqual(fake.calls[0]["screenshot_bytes"], b"fakepng")

    def test_screenshot_failure_degrades_to_none(self):
        page = mock.Mock()
        page.screenshot.side_effect = RuntimeError("screenshot failed")
        fake = _FakeTriage(
            TriageDecision(category="unknown", suggested_action="manual_handoff", confidence=0.0)
        )
        rt = _build_runtime(triage_provider=fake, page=page)
        _run_triage(rt, Evidence(url="u"), "x")

        self.assertIsNone(fake.calls[0]["screenshot_bytes"])


if __name__ == "__main__":
    unittest.main()
