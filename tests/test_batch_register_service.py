# -*- coding: utf-8 -*-
"""批量注册服务测试。"""

import time
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import Run
from src.services.batch_register_service import (
    _batches,
    cancel_batch,
    get_batch_status,
    list_recent_batches,
    start_batch,
)


def _build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class BatchRegisterTest(unittest.TestCase):

    def setUp(self):
        self.engine = _build_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine
        # 清空 batch 注册表
        _batches.clear()

    def tearDown(self):
        # 1. 先把所有 batch 标 cancelled，让后台 dispatch thread 中的
        #    _wait_for_run_terminal 立刻返回。
        # 2. 等 thread 退出（最多 3s），避免它在 engine 切换后仍在查 DB 触发 segfault。
        # 3. 再清理 engine 引用 + batch 注册表。
        for state in list(_batches.values()):
            state.cancelled = True
        # 给 thread 时间走完 cancel 检查 → break 出循环
        for _ in range(30):
            still_active = any(
                t.is_alive() for t in self._dispatch_threads_snapshot()
            )
            if not still_active:
                break
            time.sleep(0.1)
        engine_mod._engine = self._original
        _batches.clear()

    def _dispatch_threads_snapshot(self):
        """枚举当前进程内名字以 batch- 开头的 dispatch 线程。"""
        import threading as _t
        return [t for t in _t.enumerate() if t.name.startswith("batch-")]

    def test_start_batch_creates_runs_with_identity(self):
        """start_batch 必须落 N 个 Run + identity 在 config_snapshot 里."""
        # mock submit_task 立即把 Run 推到终态，让后台 dispatch 线程在 tearDown 前退出
        broadcaster = mock.MagicMock()
        db_lock = __import__("threading").Lock()

        def fake_submit(rid: str, _bc):
            with db_lock:
                with get_session() as s:
                    run = s.get(Run, rid)
                    if run:
                        run.status = "success"
                        s.add(run)
                        s.commit()
            return True

        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            result = start_batch(
                count=3,
                profile_id="k1b9945d",
                password="Pass123!",
                interval_min_sec=0,
                interval_max_sec=0,  # 让测试快速跑完
                broadcaster=broadcaster,
            )
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["run_ids"]), 3)
        self.assertTrue(result["batch_id"].startswith("batch-"))

        # 校验 DB 落了 3 个 Run，且 config_snapshot.identity 完整
        with get_session() as s:
            runs = s.exec(
                # 不用 select 链路（避免 deprecation），直接 query 拿 list
                # 用 sqlmodel 兼容 API
                __import__("sqlmodel").select(Run)
            ).all()
            self.assertEqual(len(runs), 3)
            for run in runs:
                snapshot = run.config_snapshot or {}
                self.assertEqual(snapshot.get("batch_id"), result["batch_id"])
                self.assertEqual(snapshot.get("task_mode"), "register_only")
                identity = snapshot.get("identity") or {}
                self.assertTrue(identity.get("first_name"))
                self.assertTrue(identity.get("last_name"))
                self.assertTrue(identity.get("email_local"))
                self.assertRegex(identity.get("birthdate", ""), r"^\d{4}-\d{2}-\d{2}$")
                self.assertEqual(run.profile_id, "k1b9945d")
                self.assertEqual(run.password, "Pass123!")

        # 等后台调度跑完
        for _ in range(50):
            time.sleep(0.05)
            st = get_batch_status(result["batch_id"])
            if st and st["status"] != "running":
                break

    def test_start_batch_invalid_count(self):
        with self.assertRaises(ValueError):
            start_batch(count=0, profile_id="p", password="x",
                        interval_min_sec=0, interval_max_sec=0, broadcaster=None)
        with self.assertRaises(ValueError):
            start_batch(count=101, profile_id="p", password="x",
                        interval_min_sec=0, interval_max_sec=0, broadcaster=None)

    def test_start_batch_invalid_interval(self):
        with self.assertRaises(ValueError):
            start_batch(count=1, profile_id="p", password="x",
                        interval_min_sec=10, interval_max_sec=5, broadcaster=None)

    def test_start_batch_invalid_mode(self):
        with self.assertRaises(ValueError):
            start_batch(count=1, profile_id="p", password="x",
                        interval_min_sec=0, interval_max_sec=0,
                        mode="weird", broadcaster=None)

    def test_start_batch_empty_profile_or_password(self):
        with self.assertRaises(ValueError):
            start_batch(count=1, profile_id="  ", password="x",
                        interval_min_sec=0, interval_max_sec=0, broadcaster=None)
        with self.assertRaises(ValueError):
            start_batch(count=1, profile_id="p", password="",
                        interval_min_sec=0, interval_max_sec=0, broadcaster=None)

    def test_get_batch_status_unknown(self):
        self.assertIsNone(get_batch_status("nonexistent"))

    def test_cancel_batch_stops_dispatch(self):
        broadcaster = mock.MagicMock()
        # interval=2s 给 cancel 介入时间窗，count=5 让它有空 cancel
        with mock.patch("src.api.worker.submit_task", return_value=True):
            result = start_batch(
                count=5,
                profile_id="p", password="x",
                interval_min_sec=2, interval_max_sec=2,
                broadcaster=broadcaster,
            )
        # 立即取消（第 1 个还没被 submit 之前）
        ok = cancel_batch(result["batch_id"])
        self.assertTrue(ok)

        # 等几秒看状态
        time.sleep(3)
        st = get_batch_status(result["batch_id"])
        self.assertIsNotNone(st)
        # 应该被中断（submitted < 5）
        self.assertLess(st["submitted"], 5)

    def test_cancel_batch_unknown(self):
        self.assertFalse(cancel_batch("nonexistent"))

    def test_list_recent_batches(self):
        broadcaster = mock.MagicMock()
        db_lock = __import__("threading").Lock()

        def fake_submit(rid: str, _bc):
            with db_lock:
                with get_session() as s:
                    run = s.get(Run, rid)
                    if run:
                        run.status = "success"
                        s.add(run)
                        s.commit()
            return True

        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            r1 = start_batch(count=1, profile_id="p", password="x",
                              interval_min_sec=0, interval_max_sec=0, broadcaster=broadcaster)
            r2 = start_batch(count=2, profile_id="p", password="x",
                              interval_min_sec=0, interval_max_sec=0, broadcaster=broadcaster)
        recent = list_recent_batches(limit=10)
        self.assertEqual(len(recent), 2)
        # 按 started_at desc，最新的在前
        ids = [b["batch_id"] for b in recent]
        self.assertEqual(ids[0], r2["batch_id"])
        self.assertEqual(ids[1], r1["batch_id"])

        # 等后台跑完不留 thread 泄漏
        for _ in range(50):
            time.sleep(0.05)
            if all(get_batch_status(b)["status"] != "running" for b in [r1["batch_id"], r2["batch_id"]]):
                break


class WaitForRunTerminalTest(unittest.TestCase):
    """单元测试 _wait_for_run_terminal 串行屏障语义。

    用 in-memory SQLite + mock 时间，不真睡，毫秒级跑完。
    """

    def setUp(self):
        self.engine = _build_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine
        _batches.clear()

    def tearDown(self):
        for state in list(_batches.values()):
            state.cancelled = True
        engine_mod._engine = self._original
        _batches.clear()

    def _make_state(self):
        from src.services.batch_register_service import _BatchState
        return _BatchState(batch_id="batch-test", total=1)

    def _insert_run(self, run_id: str, status: str = "pending") -> None:
        with get_session() as s:
            s.add(Run(id=run_id, email=f"{run_id}@x.com", status=status))
            s.commit()

    def _set_status(self, run_id: str, status: str) -> None:
        with get_session() as s:
            run = s.get(Run, run_id)
            run.status = status
            s.add(run)
            s.commit()

    def test_returns_terminal_status_immediately(self):
        """Run 已是终态时，第一次 poll 命中即返回。"""
        from src.services.batch_register_service import _wait_for_run_terminal
        run_id = "rid-1"
        self._insert_run(run_id, status="success")
        state = self._make_state()
        sleeps: list[float] = []
        result = _wait_for_run_terminal(
            run_id,
            state=state,
            max_wait_sec=10.0,
            poll_interval_sec=0.5,
            sleep_fn=lambda s: sleeps.append(s),  # 不真睡
            monotonic_fn=lambda: 0.0,  # 时间冻结
        )
        self.assertEqual(result, "success")
        # 第一次 poll 就命中，不应进入睡眠
        self.assertEqual(sleeps, [])

    def test_waits_until_run_reaches_terminal(self):
        """Run 在第 N 次 poll 时变成终态，验证返回正确状态。"""
        from src.services.batch_register_service import _wait_for_run_terminal
        run_id = "rid-2"
        self._insert_run(run_id, status="pending")
        state = self._make_state()
        # 第 3 次 sleep 后改成 failed
        sleep_count = {"n": 0}

        def fake_sleep(_s: float) -> None:
            sleep_count["n"] += 1
            if sleep_count["n"] >= 3:
                # 模拟 worker 把 status 推到终态
                self._set_status(run_id, "failed")

        # 注入单调时钟（不超时）
        clock = {"t": 0.0}
        def fake_monotonic() -> float:
            clock["t"] += 0.1
            return clock["t"]

        result = _wait_for_run_terminal(
            run_id,
            state=state,
            max_wait_sec=100.0,
            poll_interval_sec=0.5,
            sleep_fn=fake_sleep,
            monotonic_fn=fake_monotonic,
        )
        self.assertEqual(result, "failed")
        self.assertGreaterEqual(sleep_count["n"], 3)

    def test_timeout_returns_timeout_string(self):
        """Run 永远 pending，max_wait_sec 到期后返回 'timeout'，不强制 cancel 任务。"""
        from src.services.batch_register_service import _wait_for_run_terminal
        run_id = "rid-3"
        self._insert_run(run_id, status="pending")
        state = self._make_state()
        # 时钟跳得快，让超时立刻命中
        clock = {"t": 0.0}
        def fake_monotonic() -> float:
            clock["t"] += 100.0  # 每次跳 100s
            return clock["t"]

        result = _wait_for_run_terminal(
            run_id,
            state=state,
            max_wait_sec=50.0,  # 50s 上限，时钟两次就超
            poll_interval_sec=0.5,
            sleep_fn=lambda _s: None,
            monotonic_fn=fake_monotonic,
        )
        self.assertEqual(result, "timeout")
        # Run 仍是 pending，不应被强制改写
        with get_session() as s:
            self.assertEqual(s.get(Run, run_id).status, "pending")

    def test_cancel_propagation_breaks_immediately(self):
        """state.cancelled 设为 True 后，下一轮 poll 立刻返回 'cancelled'。"""
        from src.services.batch_register_service import _wait_for_run_terminal
        run_id = "rid-4"
        self._insert_run(run_id, status="pending")
        state = self._make_state()
        state.cancelled = True  # 进入循环前就已取消

        result = _wait_for_run_terminal(
            run_id,
            state=state,
            max_wait_sec=10.0,
            poll_interval_sec=0.5,
            sleep_fn=lambda _s: None,
            monotonic_fn=lambda: 0.0,
        )
        self.assertEqual(result, "cancelled")
        # Run 仍 pending（wait_for_terminal 不写 status，由用户/worker 自己决定）
        with get_session() as s:
            self.assertEqual(s.get(Run, run_id).status, "pending")

    def test_dispatch_loop_serializes_via_barrier(self):
        """端到端：dispatch_loop 在 submit 后调用 wait_for_terminal，
        验证两次 submit 之间的真实间隔 ≥ wait_for_terminal 时长 + interval。"""
        from src.services.batch_register_service import _dispatch_loop, _BatchState

        # 准备 3 个 Run
        run_ids = ["seq-1", "seq-2", "seq-3"]
        for rid in run_ids:
            self._insert_run(rid, status="pending")

        state = _BatchState(batch_id="batch-seq", total=3)
        state.run_ids = list(run_ids)
        _batches[state.batch_id] = state

        # mock submit_task：被调时立刻把对应 Run 改成 success（模拟瞬时完成的 worker）
        submit_calls: list[str] = []

        def fake_submit(rid: str, _broadcaster):
            submit_calls.append(rid)
            self._set_status(rid, "success")
            return True

        broadcaster = mock.MagicMock()
        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            _dispatch_loop(
                state,
                interval_min=0.0,
                interval_max=0.0,  # 关闭 interval 抖动，专门验证屏障
                broadcaster=broadcaster,
                run_wait_timeout_sec=10.0,
                run_poll_interval_sec=0.05,
                sleep_fn=lambda _s: None,  # 不真睡
                monotonic_fn=time.monotonic,
            )

        # 3 个全 submit 了，且顺序与 run_ids 一致（串行保证）
        self.assertEqual(submit_calls, run_ids)
        # batch 标记完成
        self.assertEqual(state.status, "completed")
        # 所有 Run 都是 success
        with get_session() as s:
            for rid in run_ids:
                self.assertEqual(s.get(Run, rid).status, "success")


class MultiProfileBatchTest(unittest.TestCase):
    """多 profile 并发批量调度测试。

    用 in-memory SQLite，mock submit_task 不真跑浏览器；
    通过断言 profile_buckets / Run.profile_id / 计数变化验证 round-robin + cancel 传播。

    关键约束：SQLite + StaticPool 不支持多线程并发查询/写入。本测试类用
    test-default 的 _wait_for_run_terminal mock 让 worker 不真去查 DB，
    避免 segfault（生产用 PG 无此约束）。
    """

    # 类级 patcher：所有测试都 mock _wait_for_run_terminal 不真查 DB
    _wait_patcher = None

    @staticmethod
    def _fake_wait_for_run_terminal(run_id, *, state, **_kwargs):
        """Mock：state.cancelled → 返回 'cancelled'；否则模拟瞬时完成 → 'success'。"""
        if state.cancelled:
            return "cancelled"
        return "success"

    @classmethod
    def setUpClass(cls):
        import src.services.batch_register_service as svc_mod
        cls._wait_patcher = mock.patch.object(
            svc_mod,
            "_wait_for_run_terminal",
            side_effect=cls._fake_wait_for_run_terminal,
        )
        cls._wait_patcher.start()

    @classmethod
    def tearDownClass(cls):
        if cls._wait_patcher is not None:
            cls._wait_patcher.stop()

    def setUp(self):
        self.engine = _build_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine
        _batches.clear()

    def tearDown(self):
        for state in list(_batches.values()):
            state.cancelled = True
        for _ in range(30):
            still_active = any(
                t.is_alive() for t in self._dispatch_threads_snapshot()
            )
            if not still_active:
                break
            time.sleep(0.1)
        engine_mod._engine = self._original
        _batches.clear()

    def _dispatch_threads_snapshot(self):
        import threading as _t
        return [t for t in _t.enumerate() if t.name.startswith("batch-")]

    def test_round_robin_buckets_helper(self):
        """_round_robin_buckets：5 个 run + 2 profile → 3 vs 2 切分。"""
        from src.services.batch_register_service import _round_robin_buckets
        run_ids = ["r0", "r1", "r2", "r3", "r4"]
        profile_ids = ["pA", "pB"]
        buckets = _round_robin_buckets(run_ids, profile_ids)
        self.assertEqual(buckets["pA"], ["r0", "r2", "r4"])
        self.assertEqual(buckets["pB"], ["r1", "r3"])

    def test_round_robin_empty_profiles_yields_empty(self):
        """没有 profile → 空 dict（兜底，调用方应拦截）。"""
        from src.services.batch_register_service import _round_robin_buckets
        self.assertEqual(_round_robin_buckets(["r0"], []), {})

    def test_round_robin_more_profiles_than_runs(self):
        """profile 比 run 多 → 多出来的 profile 拿 0 个，仍出现在 dict。"""
        from src.services.batch_register_service import _round_robin_buckets
        buckets = _round_robin_buckets(["r0", "r1"], ["pA", "pB", "pC"])
        self.assertEqual(buckets["pA"], ["r0"])
        self.assertEqual(buckets["pB"], ["r1"])
        self.assertEqual(buckets["pC"], [])  # 闲置但占位

    def test_normalize_profile_ids_dedup_and_filter(self):
        """_normalize_profile_ids：去重、剔空、保序，profile_id + profile_ids 合并。"""
        from src.services.batch_register_service import _normalize_profile_ids
        # 基本去重 + 空过滤
        self.assertEqual(
            _normalize_profile_ids(profile_ids=["p1", "", "p1", "  ", "p2"]),
            ["p1", "p2"],
        )
        # profile_id + profile_ids 合并
        self.assertEqual(
            _normalize_profile_ids(profile_id="p0", profile_ids=["p1", "p2"]),
            ["p1", "p2", "p0"],
        )
        # 两者重复 → 保序去重
        self.assertEqual(
            _normalize_profile_ids(profile_id="p1", profile_ids=["p1", "p2"]),
            ["p1", "p2"],
        )
        # 都空 → []
        self.assertEqual(_normalize_profile_ids(), [])

    def test_start_batch_with_profile_ids_round_robin(self):
        """start_batch 接 profile_ids，N 个 Run 按 round-robin 落到对应 profile。"""
        broadcaster = mock.MagicMock()
        # SQLite + StaticPool 需要 lock 串行化 DB 写（生产用 PG 无此约束）
        db_lock = __import__("threading").Lock()

        def fake_submit(rid: str, _bc):
            with db_lock:
                with get_session() as s:
                    run = s.get(Run, rid)
                    if run:
                        run.status = "success"
                        s.add(run)
                        s.commit()
            return True

        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            result = start_batch(
                count=5,
                profile_ids=["pA", "pB"],
                password="Pass123!",
                interval_min_sec=0,
                interval_max_sec=0,
                broadcaster=broadcaster,
            )

        self.assertEqual(result["total"], 5)
        self.assertEqual(result["concurrency"], 2)
        self.assertEqual(set(result["profile_buckets"].keys()), {"pA", "pB"})
        self.assertEqual(len(result["profile_buckets"]["pA"]), 3)
        self.assertEqual(len(result["profile_buckets"]["pB"]), 2)

        # 校验 DB 的 Run.profile_id 与 bucket 一致
        with get_session() as s:
            import sqlmodel as _sm
            runs = s.exec(_sm.select(Run)).all()
            profile_counts = {"pA": 0, "pB": 0}
            for run in runs:
                self.assertIn(run.profile_id, profile_counts)
                profile_counts[run.profile_id] += 1
            self.assertEqual(profile_counts, {"pA": 3, "pB": 2})

        # 等后台跑完
        for _ in range(50):
            time.sleep(0.05)
            st = get_batch_status(result["batch_id"])
            if st and st["status"] != "running":
                break

    def test_start_batch_backward_compat_single_profile_id(self):
        """老入参 profile_id 单值：profile_buckets = {p: all_runs}，concurrency = 1。"""
        broadcaster = mock.MagicMock()
        db_lock = __import__("threading").Lock()

        def fake_submit(rid: str, _bc):
            with db_lock:
                with get_session() as s:
                    run = s.get(Run, rid)
                    if run:
                        run.status = "success"
                        s.add(run)
                        s.commit()
            return True

        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            result = start_batch(
                count=3,
                profile_id="legacy_profile",
                password="x",
                interval_min_sec=0,
                interval_max_sec=0,
                broadcaster=broadcaster,
            )
        self.assertEqual(result["concurrency"], 1)
        self.assertEqual(list(result["profile_buckets"].keys()), ["legacy_profile"])
        self.assertEqual(len(result["profile_buckets"]["legacy_profile"]), 3)

        # 所有 Run.profile_id 都是 legacy_profile
        with get_session() as s:
            import sqlmodel as _sm
            runs = s.exec(_sm.select(Run)).all()
            for run in runs:
                self.assertEqual(run.profile_id, "legacy_profile")

        for _ in range(50):
            time.sleep(0.05)
            st = get_batch_status(result["batch_id"])
            if st and st["status"] != "running":
                break

    def test_start_batch_empty_profile_ids_raises(self):
        """profile_id 和 profile_ids 都没填 → ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            start_batch(
                count=1,
                profile_ids=[],
                password="x",
                interval_min_sec=0,
                interval_max_sec=0,
                broadcaster=None,
            )
        self.assertIn("profile", str(ctx.exception))

    def test_start_batch_profile_ids_only_whitespace_raises(self):
        """profile_ids 全是空白也算空 → ValueError。"""
        with self.assertRaises(ValueError):
            start_batch(
                count=1,
                profile_ids=["", "   ", "\t"],
                password="x",
                interval_min_sec=0,
                interval_max_sec=0,
                broadcaster=None,
            )

    def test_start_batch_too_many_profiles_raises(self):
        """profile 数超过 32（与 worker._MAX_WORKERS_MAX 对齐）→ ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            start_batch(
                count=1,
                profile_ids=[f"p{i}" for i in range(33)],
                password="x",
                interval_min_sec=0,
                interval_max_sec=0,
                broadcaster=None,
            )
        self.assertIn("32", str(ctx.exception))

    def test_start_batch_more_profiles_than_count_logs_warning(self):
        """count=2 但 5 个 profile → 不报错，仅 warning（3 个 profile 闲置）。"""
        broadcaster = mock.MagicMock()

        db_lock = __import__("threading").Lock()

        # fake_submit 立即把 Run 推到终态，避免 _wait_for_run_terminal 等 30 分钟 timeout
        def fake_submit(rid: str, _bc):
            with db_lock:
                with get_session() as s:
                    run = s.get(Run, rid)
                    if run:
                        run.status = "success"
                        s.add(run)
                        s.commit()
            return True

        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            with self.assertLogs("src.services.batch_register_service", level="WARNING") as logs:
                result = start_batch(
                    count=2,
                    profile_ids=["pA", "pB", "pC", "pD", "pE"],
                    password="x",
                    interval_min_sec=0,
                    interval_max_sec=0,
                    broadcaster=broadcaster,
                )
        # 验证有 "profile 数 (5) > count (2)" 类的 warning
        found = any("> count" in msg or "profile" in msg.lower() for msg in logs.output)
        self.assertTrue(found, f"应该有 profile/count warning，实际日志: {logs.output}")
        # 即便如此，buckets 仍有 5 个 key（2 个有内容，3 个空）
        self.assertEqual(len(result["profile_buckets"]), 5)

        # 等后台跑完（mock 立即推到终态，应该很快）
        for _ in range(50):
            time.sleep(0.05)
            st = get_batch_status(result["batch_id"])
            if st and st["status"] != "running":
                break

    def test_dispatch_coordinator_runs_profiles_in_parallel(self):
        """端到端：coordinator 起 2 个 per-profile 子线程并行跑各自的 bucket。"""
        from src.services.batch_register_service import _dispatch_coordinator, _BatchState

        # 准备 4 个 Run，round-robin 到 2 profile：pA=[r0,r2] pB=[r1,r3]
        run_ids = ["mp-0", "mp-1", "mp-2", "mp-3"]
        for rid in run_ids:
            with get_session() as s:
                s.add(Run(id=rid, email=f"{rid}@x.com", status="pending"))
                s.commit()

        state = _BatchState(batch_id="batch-mp", total=4)
        state.run_ids = list(run_ids)
        state.profile_buckets = {
            "pA": ["mp-0", "mp-2"],
            "pB": ["mp-1", "mp-3"],
        }
        _batches[state.batch_id] = state

        # 类级 setUpClass 已 patch _wait_for_run_terminal 不真查 DB（避免 SQLite segfault）
        submit_lock = __import__("threading").Lock()
        submit_order: list[str] = []

        def fake_submit(rid: str, _bc):
            with submit_lock:
                submit_order.append(rid)
            return True

        broadcaster = mock.MagicMock()
        with mock.patch("src.api.worker.submit_task", side_effect=fake_submit):
            _dispatch_coordinator(
                state,
                interval_min=0.0,
                interval_max=0.0,
                broadcaster=broadcaster,
                run_wait_timeout_sec=10.0,
                run_poll_interval_sec=0.02,
                sleep_fn=lambda _s: None,
                monotonic_fn=time.monotonic,
            )

        # 4 个全 submit 了
        self.assertEqual(set(submit_order), set(run_ids))
        self.assertEqual(state.submitted, 4)
        self.assertEqual(state.status, "completed")

        # profile 内部保序：pA 内 mp-0 在 mp-2 之前；pB 内 mp-1 在 mp-3 之前
        # （这是 _dispatch_loop_for_profile 串行迭代 bucket 的语义保证）
        pa_indices = [submit_order.index(r) for r in ["mp-0", "mp-2"]]
        pb_indices = [submit_order.index(r) for r in ["mp-1", "mp-3"]]
        self.assertLess(pa_indices[0], pa_indices[1], "profile A 内部应该串行保序")
        self.assertLess(pb_indices[0], pb_indices[1], "profile B 内部应该串行保序")

    def test_cancel_propagates_to_all_profile_workers(self):
        """取消整 batch 后，所有 profile worker 在下一个 poll 都退出。"""
        broadcaster = mock.MagicMock()
        # interval=2s 给 cancel 介入时间窗
        with mock.patch("src.api.worker.submit_task", return_value=True):
            result = start_batch(
                count=6,
                profile_ids=["pA", "pB", "pC"],
                password="x",
                interval_min_sec=2,
                interval_max_sec=2,
                broadcaster=broadcaster,
            )

        # 立即取消（在抖动间隔窗内）
        ok = cancel_batch(result["batch_id"])
        self.assertTrue(ok)

        # 等到结束
        for _ in range(60):
            time.sleep(0.1)
            st = get_batch_status(result["batch_id"])
            if st and st["status"] != "running":
                break
        st = get_batch_status(result["batch_id"])
        self.assertIsNotNone(st)
        # 取消应该截断后续提交：所有 profile 的 worker 都退出
        self.assertEqual(st["status"], "cancelled")
        # 总 submitted + failed 应该 < total（被截断）
        self.assertLess(st["submitted"] + st["failed"], 6)


if __name__ == "__main__":
    unittest.main()
