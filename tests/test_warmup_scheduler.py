# -*- coding: utf-8 -*-
"""warmup 60s 调度循环单元测试。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.api.worker as worker
from src.db.models import Run


def _build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class TestWarmupSchedulerTick(unittest.TestCase):
    def setUp(self):
        self.engine = _build_engine()
        # 避开真实 worker 的 thread pool
        worker._running_tasks.clear()
        worker._cancel_requests.clear()

    def _seed_run(self, *, phase: str, next_action_at, status: str = "pending") -> str:
        with Session(self.engine) as s:
            run = Run(
                email="x@y.z",
                password="pw",
                profile_id="p1",
                status=status,
                phase=phase,
                next_action_at=next_action_at,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            return run.id

    def _patch_session(self):
        return mock.patch("src.db.engine.get_session", lambda: Session(self.engine))

    def test_no_runs_returns_zero(self):
        with self._patch_session():
            n = worker._warmup_scheduler_tick()
        self.assertEqual(n, 0)

    def test_due_warming_up_card_run_requeued(self):
        run_id = self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        broadcaster = mock.Mock()
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
            n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: broadcaster)
        self.assertEqual(n, 1)
        st.assert_called_once_with(run_id, broadcaster)

    def test_future_run_not_requeued(self):
        self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
            n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
        self.assertEqual(n, 0)
        st.assert_not_called()

    def test_warming_up_account_phase_also_picked(self):
        run_id = self._seed_run(
            phase="warming_up_account",
            next_action_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
            n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
        self.assertEqual(n, 1)
        st.assert_called_once()

    def test_other_phases_ignored(self):
        self._seed_run(
            phase="registration",
            next_action_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        self._seed_run(
            phase="payment",
            next_action_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
            n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
        self.assertEqual(n, 0)
        st.assert_not_called()

    def test_cancelled_run_skipped(self):
        self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            status="cancelled",
        )
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
            n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
        self.assertEqual(n, 0)
        st.assert_not_called()

    def test_no_broadcaster_factory_logs_only(self):
        # 没有 broadcaster 时仅记录，不重入
        self._seed_run(
            phase="warming_up_account",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
            n = worker._warmup_scheduler_tick(broadcaster_factory=None)
        self.assertEqual(n, 0)
        st.assert_not_called()

    def test_already_running_run_skipped(self):
        run_id = self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        worker._running_tasks.add(run_id)
        try:
            with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True) as st:
                n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
            self.assertEqual(n, 0)
            st.assert_not_called()
        finally:
            worker._running_tasks.discard(run_id)

    # ────────────────────────────────────────────────────────
    # C1: scheduler 静默吞异常 — 验证 tick 不让循环挂掉
    # ────────────────────────────────────────────────────────

    def test_tick_swallows_db_exception_and_returns_zero(self):
        """tick 内层 try/except：DB session 抛异常 → tick 返回 0 + 不重新抛 + submit_task 不被调。

        覆盖 worker.py:_warmup_scheduler_tick 的 try/except 兜底分支。
        如果这条路径无效，60s 循环里一次 DB 抖动就能让 scheduler 静默失效。
        """
        # 先 seed 一条本来该被捡起的 due run
        self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

        # 让 get_session 抛异常（模拟 DB 闪断）
        # 注意：worker.py:818 局部 import as _get_session，patch 源头模块即可
        def _failing_session_ctx():
            raise RuntimeError("simulated db outage")

        with mock.patch("src.db.engine.get_session", _failing_session_ctx), \
             mock.patch.object(worker, "submit_task", return_value=True) as st:
            try:
                n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
            except Exception as exc:
                self.fail(f"tick 必须吞掉异常不外抛，实际抛了: {exc!r}")
        self.assertEqual(n, 0)
        st.assert_not_called()

    def test_tick_continues_after_per_run_submit_failure(self):
        """修复后：单个 run submit_task 抛异常 → tick 继续处理后续 due run。

        历史 bug（已修）：循环内单 run 异常会被外层 try/except 抓掉，导致 tick 立刻
        退出 + 第 2/3/N 个 due run 都被漏掉。修复方法：在循环里给 submit_task 包
        独立 try/except，单 run 失败只 logger.error，继续后续 run。
        """
        # 两条都该被捡起
        self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        self._seed_run(
            phase="warming_up_account",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

        # submit_task 第一次调用就炸，第二次成功
        first_call = {"hit": False}

        def boom(*_a, **_kw):
            if not first_call["hit"]:
                first_call["hit"] = True
                raise RuntimeError("submit failed")
            return True

        with self._patch_session(), mock.patch.object(worker, "submit_task", side_effect=boom) as st:
            try:
                n = worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())
            except Exception as exc:
                self.fail(f"tick 必须不外抛单 run 异常，实际抛了: {exc!r}")

        # 修复后：两个 due run 都该被尝试 submit；第 1 个抛异常被吞，第 2 个成功
        self.assertEqual(st.call_count, 2, "两个 due run 都该被尝试 submit_task")
        self.assertEqual(n, 1, "第 1 个抛异常 + 第 2 个成功 → 计数 1")

    # ────────────────────────────────────────────────────────
    # C2: select=None 时 next_action_at 是否被推后
    # ────────────────────────────────────────────────────────

    def test_due_run_remains_due_when_pool_exhausted_no_pushback(self):
        """模拟号池告罄场景：scheduler 仅决定"是否 submit_task"，不会改 next_action_at。

        当前实现观察：scheduler 的责任只是按 next_action_at <= now 把 run 重新提交到
        worker 线程池。是否推后 next_action_at 应由 execute_card_warmup 内部在
        select_warmup_account 返回 None 时负责。如果它没做，这条 run 在每个 60s
        tick 都会再次 due → 重复 submit → worker 重复跑空（虽然 select 也是空）。

        本测试**记录 scheduler 层的现状**：单个 tick 不会把 due 的 run 的
        next_action_at 推后，即便 submit_task 成功也一样。这意味着如果 worker
        没有更新 next_action_at，下一个 tick 60s 后又会再选这条 run。

        验证 scheduler 与 worker 之间的责任分配是否正确（这是设计决策，不一定是 bug，
        但现状必须用测试钉死，以便后续改动时能立刻发现责任迁移）。
        """
        run_id = self._seed_run(
            phase="warming_up_card",
            next_action_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        with self._patch_session(), mock.patch.object(worker, "submit_task", return_value=True):
            worker._warmup_scheduler_tick(broadcaster_factory=lambda: mock.Mock())

        # tick 不动 next_action_at —— 推后责任在 worker 内部，不在 scheduler
        with Session(self.engine) as s:
            row = s.get(Run, run_id)
            # 时间戳容忍：如果 scheduler 改了，这里应当 != 原值
            self.assertIsNotNone(
                row.next_action_at,
                "scheduler 不该把 next_action_at 设成 None（这样 run 永远不会再被捡起）",
            )
            ts = row.next_action_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            self.assertLess(
                ts, datetime.now(timezone.utc),
                "现状：scheduler 不主动推后 next_action_at；如果空池/cooldown 锁死的"
                "推后逻辑没在 worker/execute_card_warmup 里实现，run 会被反复 submit。",
            )


class TestWarmupSchedulerLifecycle(unittest.TestCase):
    def test_start_and_stop_idempotent(self):
        # 启动 + 立刻停止，验证不会卡死
        worker.start_warmup_scheduler(broadcaster_factory=lambda: mock.Mock())
        worker.stop_warmup_scheduler(wait_sec=2.0)
        # 再次启动停止应不抛
        worker.start_warmup_scheduler(broadcaster_factory=lambda: mock.Mock())
        worker.stop_warmup_scheduler(wait_sec=2.0)


if __name__ == "__main__":
    unittest.main()
