# -*- coding: utf-8 -*-
"""decline_retry_service 单元测试。

覆盖：
- 可重试 / 终态 / 不明 decline 码的分支
- 抖动延迟计算（不真睡，注入 sleep_fn）
- DB 计数累加（in-memory SQLite，沿用 test_bin_health_service.py 同模式）
- 异常静默（submit_callable 抛出 / DB 不可用）
- 首次成功 / 末次成功 / 全部失败
"""

import os
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import src.db.engine as engine_mod
from src.db.engine import get_session, init_db
from src.db.models import Run
from src.services.decline_retry_service import (
    RETRYABLE_DECLINE_CODES,
    TERMINAL_DECLINE_CODES,
    RetryOutcome,
    retry_decline_with_jitter,
)


def _reset_engine():
    engine_mod._engine = None


def _outcome(status: str, code: str = "", rationale: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "decline_code": code,
        "rationale": rationale,
        "raw_signals": {},
    }


def _make_callable(scripted: list[dict[str, Any]]):
    """构造一个按脚本顺序返回结果的 callable，调用次数受 max_attempts 控制。"""
    idx = {"i": 0}

    def _call() -> dict[str, Any]:
        result = scripted[idx["i"]]
        idx["i"] += 1
        return result

    return _call


class DeclineCodeSetsTest(unittest.TestCase):
    def test_retryable_set_intent(self):
        self.assertIn("insufficient_funds", RETRYABLE_DECLINE_CODES)
        self.assertIn("do_not_honor", RETRYABLE_DECLINE_CODES)
        self.assertIn("card_declined", RETRYABLE_DECLINE_CODES)

    def test_terminal_set_intent(self):
        self.assertIn("fraudulent", TERMINAL_DECLINE_CODES)
        self.assertIn("expired_card", TERMINAL_DECLINE_CODES)
        self.assertIn("incorrect_cvc", TERMINAL_DECLINE_CODES)

    def test_no_overlap(self):
        self.assertEqual(RETRYABLE_DECLINE_CODES & TERMINAL_DECLINE_CODES, frozenset())


class RetryNoDBTest(unittest.TestCase):
    """不写 DB 的纯逻辑分支（run_id=None）。"""

    def setUp(self):
        self.sleeps: list[float] = []

    def _sleep(self, secs: float) -> None:
        self.sleeps.append(secs)

    def test_first_attempt_success_no_retry(self):
        cb = _make_callable([_outcome("succeeded")])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.final_status, "succeeded")
        self.assertEqual(out.attempts_made, 1)
        self.assertEqual(out.succeeded_on_attempt, 1)
        self.assertEqual(self.sleeps, [])  # 首次不睡

    def test_retryable_then_success(self):
        cb = _make_callable([
            _outcome("declined", "insufficient_funds"),
            _outcome("succeeded"),
        ])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.final_status, "succeeded")
        self.assertEqual(out.attempts_made, 2)
        self.assertEqual(out.succeeded_on_attempt, 2)
        self.assertEqual(len(self.sleeps), 1)  # 第 2 次前睡 1 次

    def test_retryable_exhausted(self):
        cb = _make_callable([
            _outcome("declined", "insufficient_funds"),
            _outcome("declined", "insufficient_funds"),
        ])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=2,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.final_status, "declined")
        self.assertEqual(out.final_decline_code, "insufficient_funds")
        self.assertEqual(out.attempts_made, 2)
        self.assertIsNone(out.succeeded_on_attempt)

    def test_terminal_decline_stops_immediately(self):
        cb = _make_callable([
            _outcome("declined", "fraudulent"),
            # 不应被调到
            _outcome("succeeded"),
        ])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.final_status, "declined")
        self.assertEqual(out.final_decline_code, "fraudulent")
        self.assertEqual(out.attempts_made, 1)

    def test_ambiguous_status_does_not_retry(self):
        """status=failed + decline_code 空：识别失败，不应无限重试。"""
        cb = _make_callable([
            _outcome("failed", "", rationale="提交后超时"),
            _outcome("succeeded"),  # 不应被调到
        ])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.final_status, "failed")
        self.assertEqual(out.attempts_made, 1)

    def test_submit_callable_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("page closed")

        out = retry_decline_with_jitter(
            submit_callable=boom,
            max_attempts=2,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.final_status, "failed")
        self.assertEqual(out.attempts_made, 1)  # 异常不再重试（视同 ambiguous）
        self.assertIn("submit_callable_exception", out.history[0]["rationale"])
        self.assertIn("page closed", out.history[0]["rationale"])

    def test_max_attempts_zero_or_negative_clamps_to_one(self):
        cb = _make_callable([_outcome("declined", "insufficient_funds")])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=0,
            sleep_fn=self._sleep,
        )
        self.assertEqual(out.attempts_made, 1)

    def test_jitter_delay_grows_with_attempt(self):
        cb = _make_callable([
            _outcome("declined", "do_not_honor"),
            _outcome("declined", "do_not_honor"),
            _outcome("declined", "do_not_honor"),
        ])
        # 用 randint=0 关掉抖动，便于断言基础递增
        with patch("random.randint", return_value=0):
            retry_decline_with_jitter(
                submit_callable=cb,
                max_attempts=3,
                base_delay_ms=1000,
                sleep_fn=self._sleep,
            )
        # attempt 2: base*1=1000ms=1.0s; attempt 3: base*2=2000ms=2.0s
        self.assertEqual(len(self.sleeps), 2)
        self.assertAlmostEqual(self.sleeps[0], 1.0, places=2)
        self.assertAlmostEqual(self.sleeps[1], 2.0, places=2)

    def test_history_preserves_each_attempt(self):
        cb = _make_callable([
            _outcome("declined", "insufficient_funds", rationale="balance too low"),
            _outcome("declined", "do_not_honor", rationale="issuer no"),
            _outcome("succeeded"),
        ])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            sleep_fn=self._sleep,
        )
        self.assertEqual(len(out.history), 3)
        self.assertEqual(out.history[0]["decline_code"], "insufficient_funds")
        self.assertEqual(out.history[1]["decline_code"], "do_not_honor")
        self.assertEqual(out.history[2]["status"], "succeeded")


class RetryWithDBTest(unittest.TestCase):
    """run_id 提供时，应累加 runs.decline_attempts。"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        cls.engine = init_db()

    @classmethod
    def tearDownClass(cls):
        _reset_engine()
        os.environ.pop("DATABASE_URL", None)

    def setUp(self):
        # 每用例插入一个干净的 Run
        with get_session() as s:
            run = Run(
                id="run-test-1",
                email="t@x.com",
                status="pending",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            s.add(run)
            s.commit()

    def tearDown(self):
        with get_session() as s:
            for r in s.exec(__import__("sqlmodel").select(Run)).all():
                s.delete(r)
            s.commit()

    def test_retryable_decline_bumps_counter(self):
        cb = _make_callable([
            _outcome("declined", "insufficient_funds"),
            _outcome("declined", "insufficient_funds"),
        ])
        retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=2,
            run_id="run-test-1",
            sleep_fn=lambda s: None,
        )
        with get_session() as s:
            run = s.get(Run, "run-test-1")
            self.assertEqual(run.decline_attempts, 2)

    def test_terminal_decline_bumps_counter_once(self):
        cb = _make_callable([_outcome("declined", "fraudulent")])
        retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            run_id="run-test-1",
            sleep_fn=lambda s: None,
        )
        with get_session() as s:
            run = s.get(Run, "run-test-1")
            self.assertEqual(run.decline_attempts, 1)

    def test_success_does_not_bump_counter(self):
        cb = _make_callable([_outcome("succeeded")])
        retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=3,
            run_id="run-test-1",
            sleep_fn=lambda s: None,
        )
        with get_session() as s:
            run = s.get(Run, "run-test-1")
            self.assertEqual(run.decline_attempts, 0)

    def test_unknown_run_id_does_not_crash(self):
        cb = _make_callable([_outcome("declined", "insufficient_funds")])
        out = retry_decline_with_jitter(
            submit_callable=cb,
            max_attempts=1,
            run_id="run-does-not-exist",
            sleep_fn=lambda s: None,
        )
        # 主流程不应被破坏
        self.assertEqual(out.final_status, "declined")


if __name__ == "__main__":
    unittest.main()
