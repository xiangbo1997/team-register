# -*- coding: utf-8 -*-
"""账号养号模块 (account_warmup.py) 单元测试。"""

import json
import pathlib
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from src.orchestration.account_warmup import (
    NextActionDecision,
    WarmupSessionResult,
    decide_next_warmup_action,
    detect_blocked_signals,
    load_topics,
    pick_messages,
    run_warmup_session,
    _DEFAULT_TOPICS,
)


def _cfg(**kw):
    defaults = dict(
        warmup_min_days=3,
        warmup_max_days=7,
        warmup_blocked_threshold=2,
        warmup_messages_per_day_min=3,
        warmup_messages_per_day_max=5,
        warmup_conversation_topics_path="",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestLoadTopics(unittest.TestCase):
    def test_empty_path_returns_default(self):
        topics = load_topics("")
        self.assertEqual(topics, list(_DEFAULT_TOPICS))

    def test_nonexistent_path_returns_default(self):
        self.assertEqual(load_topics("/nonexistent/file.json"), list(_DEFAULT_TOPICS))

    def test_valid_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(["question A", "question B"], f)
            path = f.name
        try:
            self.assertEqual(load_topics(path), ["question A", "question B"])
        finally:
            pathlib.Path(path).unlink()

    def test_invalid_json_falls_back(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name
        try:
            self.assertEqual(load_topics(path), list(_DEFAULT_TOPICS))
        finally:
            pathlib.Path(path).unlink()


class TestPickMessages(unittest.TestCase):
    def test_count_within_range(self):
        topics = [f"q{i}" for i in range(20)]
        rng = random.Random(42)
        msgs = pick_messages(topics=topics, min_count=3, max_count=10, rng=rng)
        self.assertGreaterEqual(len(msgs), 3)
        self.assertLessEqual(len(msgs), 10)

    def test_unique_messages(self):
        topics = [f"q{i}" for i in range(20)]
        rng = random.Random(7)
        msgs = pick_messages(topics=topics, min_count=10, max_count=10, rng=rng)
        self.assertEqual(len(set(msgs)), len(msgs))

    def test_fewer_topics_than_max(self):
        topics = ["a", "b"]
        rng = random.Random(1)
        msgs = pick_messages(topics=topics, min_count=1, max_count=5, rng=rng)
        self.assertLessEqual(len(msgs), 2)

    def test_zero_topics(self):
        self.assertEqual(pick_messages(topics=[], min_count=3, max_count=5), [])


class TestDetectBlockedSignals(unittest.TestCase):
    def test_no_signal_returns_empty(self):
        self.assertEqual(detect_blocked_signals(page_signals={}), [])

    def test_captcha_widget(self):
        triggered = detect_blocked_signals(page_signals={"has_challenge_widget": True})
        self.assertIn("captcha_widget", triggered)

    def test_phone_re_verify(self):
        triggered = detect_blocked_signals(page_signals={"has_phone_input": True})
        self.assertIn("phone_re_verify", triggered)

    def test_auth_error_url(self):
        triggered = detect_blocked_signals(
            page_signals={}, page_url="https://auth.openai.com/auth/error?code=403"
        )
        self.assertIn("auth_error_url", triggered)

    def test_multiple_signals(self):
        triggered = detect_blocked_signals(
            page_signals={"has_challenge_widget": True, "has_role_alert": True}
        )
        self.assertEqual(set(triggered), {"captcha_widget", "role_alert"})


class TestRunWarmupSession(unittest.TestCase):
    def test_full_path_all_messages_sent(self):
        sent_messages = []
        def fake_send(p, msg):
            sent_messages.append(msg)
            return True

        rng = random.Random(42)
        result = run_warmup_session(
            run_id="r1",
            page=mock.Mock(),
            config=_cfg(),
            rng=rng,
            chat_send_fn=fake_send,
            page_signal_fn=lambda p: {},
            page_url_fn=lambda p: "https://chatgpt.com/",
        )
        self.assertTrue(result.success)
        self.assertGreaterEqual(result.messages_sent, 3)
        self.assertEqual(len(sent_messages), result.messages_sent)
        self.assertEqual(result.blocked_signals, [])

    def test_blocked_signals_collected(self):
        result = run_warmup_session(
            run_id="r1",
            page=mock.Mock(),
            config=_cfg(),
            rng=random.Random(0),
            chat_send_fn=lambda p, m: True,
            page_signal_fn=lambda p: {"has_challenge_widget": True},
            page_url_fn=lambda p: "https://chatgpt.com/",
        )
        self.assertTrue(result.success)
        self.assertIn("captcha_widget", result.blocked_signals)

    def test_send_failure_is_skipped(self):
        attempts = {"n": 0}
        def flaky(p, m):
            attempts["n"] += 1
            return attempts["n"] % 2 == 0  # 隔一个失败

        result = run_warmup_session(
            run_id="r1",
            page=mock.Mock(),
            config=_cfg(),
            rng=random.Random(42),
            chat_send_fn=flaky,
            page_signal_fn=lambda p: {},
            page_url_fn=lambda p: "",
        )
        # 仍是 success（部分成功），messages_sent 为成功条数
        self.assertTrue(result.success)
        self.assertGreater(attempts["n"], result.messages_sent)


class TestDecideNextWarmupAction(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_blocked_threshold_abandoned(self):
        d = decide_next_warmup_action(
            config=_cfg(warmup_blocked_threshold=2),
            days_warmed=2,
            blocked_count=2,
            last_session_blocked=True,
            now=self.now,
        )
        self.assertEqual(d.next_phase, "abandoned")
        self.assertIsNone(d.next_action_at)
        self.assertIn("blocked_count=2", d.reason)

    def test_max_days_force_bind(self):
        d = decide_next_warmup_action(
            config=_cfg(warmup_max_days=7),
            days_warmed=7,
            blocked_count=1,
            last_session_blocked=True,
            now=self.now,
        )
        self.assertEqual(d.next_phase, "binding_team")
        self.assertEqual(d.next_action_at, self.now)
        self.assertIn("max_days=7", d.reason)

    def test_green_after_min_days(self):
        d = decide_next_warmup_action(
            config=_cfg(warmup_min_days=3),
            days_warmed=4,
            blocked_count=0,
            last_session_blocked=False,
            now=self.now,
        )
        self.assertEqual(d.next_phase, "binding_team")
        self.assertEqual(d.next_action_at, self.now)

    def test_under_min_days_continue(self):
        d = decide_next_warmup_action(
            config=_cfg(warmup_min_days=3),
            days_warmed=1,
            blocked_count=0,
            last_session_blocked=False,
            now=self.now,
        )
        self.assertEqual(d.next_phase, "warming_up_account")
        self.assertEqual(d.next_action_at, self.now + timedelta(hours=24))

    def test_yellow_signal_extends(self):
        d = decide_next_warmup_action(
            config=_cfg(warmup_min_days=3, warmup_max_days=7),
            days_warmed=3,
            blocked_count=1,
            last_session_blocked=True,  # 本次有信号 → 不绑卡
            now=self.now,
        )
        # 因为 last_session_blocked=True，min_days 满足也不进入 binding，继续 +24h
        self.assertEqual(d.next_phase, "warming_up_account")
        self.assertEqual(d.next_action_at, self.now + timedelta(hours=24))


if __name__ == "__main__":
    unittest.main()
