# -*- coding: utf-8 -*-
"""code_discovery_service 集成测试

mock check_eligibility，验证：
  - 候选生成 / ELIGIBLE 触发建模板 / 重名跳过
  - cancel 标志生效（线程下一条循环退出）
  - 401 / 403 立即终止
  - 与 bulk_verify 全局锁互斥（启动 discover 时 bulk 锁已占用 → BusyError）
  - 没有 completed Run → 任务进入 error 状态
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import EligibilityStatus, LinkTemplate, Run
from src.promo_eligibility.client import EligibilityResult
from src.services import code_discovery_service as discover_mod
from src.services.code_discovery_service import (
    BusyError,
    DiscoveryNotFound,
    cancel_discovery,
    get_discovery_status,
    list_recent_discoveries,
    start_discovery,
)
from src.services.promo_eligibility_service import PROMO_VERIFY_LOCK


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_run_with_token(session: Session, token: str = "valid-token") -> Run:
    r = Run(email="test@example.com", status="success", openai_tokens={"access_token": token})
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def _wait_until_terminal(task_id: str, timeout: float = 5.0) -> dict:
    """轮询等任务进入 completed / cancelled / error。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_discovery_status(task_id)
        if last["final_status"] != "running":
            return last
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内完成；最后状态: {last}")


class _DiscoveryTestBase(unittest.TestCase):
    """复用 in-memory engine + 清空 _RUNNING + 重置锁。"""

    def setUp(self) -> None:
        self._real_engine = engine_mod._engine
        self._fake_engine = _make_engine()
        engine_mod._engine = self._fake_engine
        # 清空任务表
        discover_mod._RUNNING.clear()
        # 确保锁未持有（前序测试可能漏放）
        try:
            PROMO_VERIFY_LOCK.release()
        except RuntimeError:
            pass

    def tearDown(self) -> None:
        engine_mod._engine = self._real_engine
        discover_mod._RUNNING.clear()
        try:
            PROMO_VERIFY_LOCK.release()
        except RuntimeError:
            pass


class TestStartDiscovery(_DiscoveryTestBase):
    def test_no_completed_run_results_in_error_state(self):
        """没 completed Run 时任务进入 error 而非抛异常（异步线程里抛）"""
        # 限制候选只生成几个，加速
        with mock.patch.object(discover_mod, "build_candidates", return_value=["abc"]):
            res = start_discovery("GB", mode="seeds", delay_sec=0.01)
        status = _wait_until_terminal(res["task_id"])
        self.assertEqual(status["final_status"], "error")
        self.assertIn("no_account", status["error_message"])

    def test_invalid_country_raises_value_error(self):
        with self.assertRaises(ValueError):
            start_discovery("XX", mode="seeds")

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            start_discovery("GB", mode="invalid_mode")

    def test_busy_when_bulk_verify_running(self):
        """模拟 bulk_verify 已持锁 → discover 启动应抛 BusyError"""
        PROMO_VERIFY_LOCK.acquire()
        try:
            with self.assertRaises(BusyError):
                start_discovery("GB", mode="seeds")
        finally:
            PROMO_VERIFY_LOCK.release()


class TestDiscoveryRun(_DiscoveryTestBase):
    """完整运行流程（mock check_eligibility）"""

    def _run_with(self, codes: list[str], side_effect):
        """启动一个小任务跑完。"""
        with get_session() as s:
            _seed_run_with_token(s)
        with mock.patch.object(discover_mod, "build_candidates", return_value=codes), \
             mock.patch.object(discover_mod, "check_eligibility", side_effect=side_effect), \
             mock.patch.object(discover_mod, "_resolve_proxy_url_for_country", return_value=(None, False)):
            res = start_discovery("GB", mode="seeds", delay_sec=0.01)
            return _wait_until_terminal(res["task_id"], timeout=5.0)

    def test_eligible_triggers_template_creation(self):
        def fake_check(*, access_token, code, proxy_url=None):
            return EligibilityResult(
                code=code,
                status=EligibilityStatus.ELIGIBLE,
                http_status=200,
            )
        final = self._run_with(["mycode1"], fake_check)
        self.assertEqual(final["final_status"], "completed")
        self.assertEqual(final["eligible_found"], 1)
        # 模板已建
        with get_session() as s:
            from sqlmodel import select
            templates = list(s.exec(select(LinkTemplate).where(LinkTemplate.name == "promo-gb-mycode1")).all())
            self.assertEqual(len(templates), 1)
            self.assertEqual(templates[0].promo_code, "mycode1")
            self.assertEqual(templates[0].aimizy_country, "GB")

    def test_duplicate_template_skipped(self):
        """同一 code 被发现两次（极端情况），第二次重名跳过不报错"""
        call_count = {"n": 0}

        def fake_check(*, access_token, code, proxy_url=None):
            call_count["n"] += 1
            return EligibilityResult(
                code=code, status=EligibilityStatus.ELIGIBLE, http_status=200,
            )
        final = self._run_with(["dupcode", "dupcode"], fake_check)
        # 任务完成（即便第二次落库失败）
        self.assertEqual(final["final_status"], "completed")
        # 两次 ELIGIBLE 都计数
        self.assertEqual(final["eligible_found"], 2)
        # 但 DB 只有一条
        with get_session() as s:
            from sqlmodel import select
            templates = list(s.exec(select(LinkTemplate).where(LinkTemplate.promo_code == "dupcode")).all())
            self.assertEqual(len(templates), 1)

    def test_401_stops_immediately(self):
        """第一次返回 401 应立即停，不扫后续"""
        call_count = {"n": 0}

        def fake_check(*, access_token, code, proxy_url=None):
            call_count["n"] += 1
            return EligibilityResult(
                code=code, status=EligibilityStatus.ERROR,
                http_status=401, error="token 过期",
            )
        final = self._run_with(["c1", "c2", "c3"], fake_check)
        self.assertEqual(final["final_status"], "error")
        self.assertIn("401", final["error_message"])
        # 只扫了 1 条就停
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(final["processed"], 1)

    def test_403_stops_immediately(self):
        def fake_check(*, access_token, code, proxy_url=None):
            return EligibilityResult(
                code=code, status=EligibilityStatus.ERROR,
                http_status=403, error="cloudflare",
            )
        final = self._run_with(["c1", "c2"], fake_check)
        self.assertEqual(final["final_status"], "error")
        self.assertIn("403", final["error_message"])
        self.assertEqual(final["processed"], 1)

    def test_status_counters_aggregate(self):
        def fake_check(*, access_token, code, proxy_url=None):
            mapping = {
                "elig": EligibilityStatus.ELIGIBLE,
                "exi":  EligibilityStatus.EXISTS,
                "nof":  EligibilityStatus.NOT_FOUND,
            }
            return EligibilityResult(
                code=code, status=mapping[code], http_status=200,
            )
        final = self._run_with(["elig", "exi", "nof"], fake_check)
        self.assertEqual(final["final_status"], "completed")
        self.assertEqual(final["eligible_found"], 1)
        self.assertEqual(final["exists_found"], 1)
        self.assertEqual(final["not_found_count"], 1)
        self.assertEqual(final["processed"], 3)

    def test_exception_counted_as_error(self):
        def fake_check(*, access_token, code, proxy_url=None):
            raise RuntimeError("network down")
        final = self._run_with(["c1", "c2"], fake_check)
        # 不会因为异常停（continue），全部当 error
        self.assertEqual(final["final_status"], "completed")
        self.assertEqual(final["error_count"], 2)


class TestCancel(_DiscoveryTestBase):
    def test_cancel_unknown_task_raises(self):
        with self.assertRaises(DiscoveryNotFound):
            cancel_discovery("nonexistent")

    def test_cancel_running_task_sets_flag(self):
        """启动慢任务 → 立刻 cancel → 状态应为 cancelled"""
        with get_session() as s:
            _seed_run_with_token(s)

        def slow_check(*, access_token, code, proxy_url=None):
            time.sleep(0.05)
            return EligibilityResult(
                code=code, status=EligibilityStatus.NOT_FOUND, http_status=200,
            )

        with mock.patch.object(discover_mod, "build_candidates",
                               return_value=["c%d" % i for i in range(20)]), \
             mock.patch.object(discover_mod, "check_eligibility", side_effect=slow_check), \
             mock.patch.object(discover_mod, "_resolve_proxy_url_for_country", return_value=(None, False)):
            res = start_discovery("GB", mode="seeds", delay_sec=0.01)
            time.sleep(0.08)  # 让它处理 1-2 条
            ok = cancel_discovery(res["task_id"])
            self.assertTrue(ok)
            final = _wait_until_terminal(res["task_id"], timeout=3.0)
        self.assertEqual(final["final_status"], "cancelled")
        # 取消时应该没扫完
        self.assertLess(final["processed"], 20)


class TestRegistryAndStatus(_DiscoveryTestBase):
    def test_status_unknown_task(self):
        with self.assertRaises(DiscoveryNotFound):
            get_discovery_status("nope")

    def test_list_recent_empty(self):
        self.assertEqual(list_recent_discoveries(), [])

    def test_concurrent_discover_blocked(self):
        """同时只允许一个 discover 任务"""
        with get_session() as s:
            _seed_run_with_token(s)

        def slow_check(*, access_token, code, proxy_url=None):
            time.sleep(0.2)
            return EligibilityResult(
                code=code, status=EligibilityStatus.NOT_FOUND, http_status=200,
            )

        with mock.patch.object(discover_mod, "build_candidates",
                               return_value=["a", "b", "c"]), \
             mock.patch.object(discover_mod, "check_eligibility", side_effect=slow_check), \
             mock.patch.object(discover_mod, "_resolve_proxy_url_for_country", return_value=(None, False)):
            res1 = start_discovery("GB", mode="seeds", delay_sec=0.01)
            try:
                # 第二个应立即被拒
                with self.assertRaises(BusyError):
                    start_discovery("GB", mode="seeds", delay_sec=0.01)
            finally:
                cancel_discovery(res1["task_id"])
                _wait_until_terminal(res1["task_id"], timeout=3.0)

    def test_running_discover_blocks_bulk_verify(self):
        """反向互斥：discover 在跑时，bulk_verify 应被拒（共用 PROMO_VERIFY_LOCK）。

        这是修复 "discover 只检查不持锁" bug 的回归测试：旧实现 discover 启动后
        立即释放锁，导致 bulk_verify 能并发跑两个 promo 长任务。
        """
        from src.services.promo_eligibility_service import (
            PromoVerifyError as _PVE,
            bulk_verify_all_templates,
        )

        with get_session() as s:
            _seed_run_with_token(s)

        def slow_check(*, access_token, code, proxy_url=None):
            time.sleep(0.2)
            return EligibilityResult(
                code=code, status=EligibilityStatus.NOT_FOUND, http_status=200,
            )

        with mock.patch.object(discover_mod, "build_candidates",
                               return_value=["a", "b", "c"]), \
             mock.patch.object(discover_mod, "check_eligibility", side_effect=slow_check), \
             mock.patch.object(discover_mod, "_resolve_proxy_url_for_country", return_value=(None, False)):
            res1 = start_discovery("GB", mode="seeds", delay_sec=0.01)
            try:
                with self.assertRaises(_PVE) as ctx:
                    bulk_verify_all_templates()
                self.assertEqual(ctx.exception.code, "busy")
            finally:
                cancel_discovery(res1["task_id"])
                _wait_until_terminal(res1["task_id"], timeout=3.0)

    def test_lock_released_on_invalid_country_after_acquire(self):
        """start_discovery 在 ValueError 路径（参数校验失败）不应泄漏锁。

        修复后参数校验在 acquire 之前——本测试确保该顺序不被反复改回。
        失败表现：下一次 start_discovery 立即抛 BusyError 而非 ValueError。
        """
        # 先用一次非法国家码触发 ValueError
        with self.assertRaises(ValueError):
            start_discovery("XX", mode="seeds")
        # 锁应可继续 acquire（未被泄漏）
        self.assertTrue(PROMO_VERIFY_LOCK.acquire(blocking=False))
        PROMO_VERIFY_LOCK.release()


if __name__ == "__main__":
    unittest.main()
