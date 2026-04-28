# -*- coding: utf-8 -*-
"""Preflight 集成模块测试。"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from src.orchestration.preflight import (
    PreflightReport,
    _resolve_min_score,
    _resolve_mode,
    check_coherence,
    run_preflight,
)


def _cfg(**kw):
    defaults = dict(
        sms_country="187",  # US in our mapping
        billing_country="US",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _fake_fingerprint(verdict="pass", score=95):
    return SimpleNamespace(verdict=verdict, score=score)


class TestModeResolution(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.dict(os.environ, {}, clear=False)
        self._patch.start()
        os.environ.pop("PREFLIGHT_MODE", None)

    def tearDown(self):
        self._patch.stop()

    def test_mode_env_wins(self):
        with mock.patch.dict(os.environ, {"PREFLIGHT_MODE": "block"}):
            self.assertEqual(_resolve_mode(_cfg()), "block")

    def test_mode_config_fallback(self):
        self.assertEqual(_resolve_mode(_cfg(preflight_mode="off")), "off")

    def test_mode_default_warn(self):
        self.assertEqual(_resolve_mode(_cfg()), "warn")

    def test_mode_invalid_falls_back_to_warn(self):
        self.assertEqual(_resolve_mode(_cfg(preflight_mode="garbage")), "warn")


class TestMinScoreResolution(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FINGERPRINT_MIN_SCORE", None)

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"FINGERPRINT_MIN_SCORE": "95"}):
            self.assertEqual(_resolve_min_score(_cfg()), 95)

    def test_config_fallback(self):
        self.assertEqual(_resolve_min_score(_cfg(fingerprint_min_score=70)), 70)

    def test_default(self):
        self.assertEqual(_resolve_min_score(_cfg()), 80)

    def test_invalid_value_fallback(self):
        with mock.patch.dict(os.environ, {"FINGERPRINT_MIN_SCORE": "notanumber"}):
            self.assertEqual(_resolve_min_score(_cfg()), 80)


class TestCheckCoherence(unittest.TestCase):
    def test_all_match(self):
        rep = check_coherence(
            config=_cfg(sms_country="187", billing_country="US"),
            card_bin_country="US",
            proxy_country="US",
        )
        self.assertTrue(rep.ok)
        self.assertEqual(rep.severity, "ok")

    def test_card_mismatch_blocks(self):
        rep = check_coherence(
            config=_cfg(sms_country="187", billing_country="US"),
            card_bin_country="HK",
            proxy_country="US",
        )
        # 1 field deviates → warn per Fintech agent's semantics
        self.assertFalse(rep.ok)
        self.assertIn(rep.severity, ("warn", "block"))


class TestRunPreflight(unittest.TestCase):
    def setUp(self):
        for k in ("PREFLIGHT_MODE", "FINGERPRINT_MIN_SCORE"):
            os.environ.pop(k, None)

    def test_mode_off_short_circuits(self):
        emitted = []
        rep = run_preflight(
            config=_cfg(preflight_mode="off"),
            card_bin_country="",
            proxy_country="",
            page=None,
            emit_event=lambda t, p: emitted.append((t, p)),
        )
        self.assertTrue(rep.ok)
        self.assertEqual(rep.mode, "off")
        self.assertFalse(rep.should_abort)
        # off 模式完全跳过，不发事件
        self.assertEqual(emitted, [])

    def test_warn_mode_never_aborts(self):
        rep = run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="",  # empty → block severity in coherence
            proxy_country="",
            page=None,
        )
        self.assertFalse(rep.should_abort)  # warn 永远不 abort
        self.assertEqual(rep.mode, "warn")
        self.assertIn("block", rep.coherence.severity)  # 一致性已经是 block 级

    def test_block_mode_aborts_on_coherence(self):
        rep = run_preflight(
            config=_cfg(preflight_mode="block"),
            card_bin_country="",
            proxy_country="",
            page=None,
        )
        self.assertTrue(rep.should_abort)
        self.assertFalse(rep.ok)

    def test_fingerprint_fail_aborts_in_block(self):
        fake_scorer = mock.Mock(return_value=_fake_fingerprint(verdict="fail", score=20))
        rep = run_preflight(
            config=_cfg(preflight_mode="block"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=fake_scorer,
        )
        self.assertTrue(rep.should_abort)
        self.assertEqual(rep.fingerprint.verdict, "fail")
        fake_scorer.assert_called_once()

    def test_fingerprint_pass_in_block(self):
        fake_scorer = mock.Mock(return_value=_fake_fingerprint(verdict="pass", score=95))
        rep = run_preflight(
            config=_cfg(preflight_mode="block"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=fake_scorer,
        )
        self.assertFalse(rep.should_abort)
        self.assertTrue(rep.ok)

    def test_event_emission_shape(self):
        captured = []
        fake_scorer = mock.Mock(return_value=_fake_fingerprint(verdict="warn", score=75))
        run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=fake_scorer,
            emit_event=lambda t, p: captured.append((t, p)),
        )
        self.assertEqual(len(captured), 1)
        event_type, payload = captured[0]
        self.assertEqual(event_type, "preflight")
        self.assertEqual(payload["mode"], "warn")
        self.assertEqual(payload["fingerprint_verdict"], "warn")
        self.assertEqual(payload["fingerprint_score"], 75)
        self.assertFalse(payload["will_abort"])

    def test_fingerprint_scorer_exception_degrades_gracefully(self):
        def bad_scorer(*args, **kwargs):
            raise RuntimeError("boom")

        rep = run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=bad_scorer,
        )
        # 异常 → fingerprint 为 None，coherence 为 ok → 整体 ok
        self.assertIsNone(rep.fingerprint)
        self.assertTrue(rep.ok)

    def test_event_emit_failure_does_not_crash(self):
        def bad_emit(t, p):
            raise RuntimeError("emit blew up")

        rep = run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="US",
            proxy_country="US",
            page=None,
            emit_event=bad_emit,
        )
        # 事件失败不影响主流程，依然返回结果
        self.assertIsNotNone(rep)


class TestPreflightReportImmutable(unittest.TestCase):
    def test_report_is_frozen(self):
        rep = PreflightReport(ok=True, mode="warn")
        with self.assertRaises(Exception):
            rep.ok = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
