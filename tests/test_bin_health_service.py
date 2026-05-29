# -*- coding: utf-8 -*-
"""BIN 健康度服务测试。覆盖 BIN 归一化、记录、查询、批量列举。"""

import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_engine, get_session, init_db
from src.db.models import Run
from src.services.bin_health_service import (
    BinHealthSnapshot,
    _normalize_bin,
    list_unhealthy_bins,
    query_bin_health,
    record_run_bin,
)


def _reset_engine():
    engine_mod._engine = None


def _make_run(
    *,
    id: str,
    status: str,
    card_bin: str = "",
    created_offset_hours: float = 0.0,
) -> Run:
    """构造一个 Run 实例，created_at 偏移自 now。负值表示过去。"""
    created_at = datetime.now(timezone.utc) + timedelta(hours=created_offset_hours)
    return Run(
        id=id,
        email=f"{id}@x.com",
        status=status,
        card_bin=card_bin,
        created_at=created_at,
        updated_at=created_at,
    )


class NormalizeBinTest(unittest.TestCase):
    def test_full_card_number_returns_first_6(self):
        self.assertEqual(_normalize_bin("4242424242424242"), "424242")

    def test_already_bin_returns_first_6(self):
        self.assertEqual(_normalize_bin("424242"), "424242")

    def test_with_spaces_and_dashes(self):
        self.assertEqual(_normalize_bin("4242 4242-4242"), "424242")

    def test_too_short_returns_empty(self):
        self.assertEqual(_normalize_bin("42424"), "")

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_bin(""), "")
        self.assertEqual(_normalize_bin("   "), "")


class BinHealthSnapshotTest(unittest.TestCase):
    def test_fail_rate_calculation(self):
        snap = BinHealthSnapshot("424242", 24, 10, 4, 5, 1)
        self.assertAlmostEqual(snap.fail_rate, 5 / 9)

    def test_fail_rate_no_completed_runs(self):
        snap = BinHealthSnapshot("424242", 24, 5, 0, 0, 5)
        self.assertEqual(snap.fail_rate, 0.0)

    def test_unhealthy_threshold(self):
        # 3 次尝试，失败率 >=60%
        self.assertTrue(BinHealthSnapshot("x", 24, 3, 1, 2, 0).is_unhealthy)
        # 仅 2 次尝试，不够数据
        self.assertFalse(BinHealthSnapshot("x", 24, 2, 0, 2, 0).is_unhealthy)
        # 失败率不够高
        self.assertFalse(BinHealthSnapshot("x", 24, 5, 3, 2, 0).is_unhealthy)


class BinHealthServiceDBTest(unittest.TestCase):
    """需要 DB 的集成测试。用 in-memory SQLite。"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        cls.engine = init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        SQLModel.metadata.drop_all(self.engine)
        SQLModel.metadata.create_all(self.engine)

    def test_record_run_bin_writes_first_6_digits(self):
        with get_session() as session:
            run = _make_run(id="a" * 16, status="pending")
            session.add(run)
            session.commit()
            self.assertTrue(record_run_bin("a" * 16, "4242424242424242", session=session))
            session.commit()
            stored = session.get(Run, "a" * 16)
            self.assertEqual(stored.card_bin, "424242")

    def test_record_run_bin_returns_false_for_unknown_run(self):
        self.assertFalse(record_run_bin("nonexistent", "4242424242424242"))

    def test_record_run_bin_returns_false_for_short_card(self):
        with get_session() as session:
            run = _make_run(id="b" * 16, status="pending")
            session.add(run)
            session.commit()
            self.assertFalse(record_run_bin("b" * 16, "12345", session=session))

    def test_query_bin_health_aggregates_correctly(self):
        with get_session() as session:
            session.add(_make_run(id="r1" + "a" * 14, status="success", card_bin="424242"))
            session.add(_make_run(id="r2" + "a" * 14, status="success", card_bin="424242"))
            session.add(_make_run(id="r3" + "a" * 14, status="failed", card_bin="424242"))
            session.add(_make_run(id="r4" + "a" * 14, status="pending", card_bin="424242"))
            session.add(_make_run(id="r5" + "a" * 14, status="success", card_bin="555555"))
            session.commit()

        snap = query_bin_health("424242")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.total_runs, 4)
        self.assertEqual(snap.success_runs, 2)
        self.assertEqual(snap.failed_runs, 1)
        self.assertEqual(snap.pending_runs, 1)

    def test_query_bin_health_filters_by_window(self):
        with get_session() as session:
            # 新数据：1 小时前
            session.add(_make_run(id="new" + "a" * 13, status="success", card_bin="424242", created_offset_hours=-1))
            # 旧数据：48 小时前，应被 24h 窗口过滤掉
            session.add(_make_run(id="old" + "a" * 13, status="failed", card_bin="424242", created_offset_hours=-48))
            session.commit()

        snap = query_bin_health("424242", window_hours=24)
        self.assertEqual(snap.total_runs, 1)
        self.assertEqual(snap.failed_runs, 0)

    def test_query_bin_health_returns_none_for_invalid_bin(self):
        self.assertIsNone(query_bin_health(""))
        self.assertIsNone(query_bin_health("12345"))  # too short

    def test_list_unhealthy_bins_filters_by_threshold(self):
        with get_session() as session:
            # BIN A：5 次尝试，4 失败 1 成功 → 失败率 80% → 不健康
            for i in range(4):
                session.add(_make_run(id=f"af{i:03d}" + "a" * 12, status="failed", card_bin="111111"))
            session.add(_make_run(id="as001" + "a" * 11, status="success", card_bin="111111"))
            # BIN B：5 次尝试，3 成功 2 失败 → 失败率 40% → 健康
            for i in range(3):
                session.add(_make_run(id=f"bs{i:03d}" + "a" * 12, status="success", card_bin="222222"))
            for i in range(2):
                session.add(_make_run(id=f"bf{i:03d}" + "a" * 12, status="failed", card_bin="222222"))
            # BIN C：仅 2 次尝试 → 不够 min_attempts，跳过
            session.add(_make_run(id="cf001" + "a" * 11, status="failed", card_bin="333333"))
            session.add(_make_run(id="cf002" + "a" * 11, status="failed", card_bin="333333"))
            session.commit()

        unhealthy = list_unhealthy_bins(min_attempts=3, fail_rate_threshold=0.6)
        bins = {snap.card_bin for snap in unhealthy}
        self.assertIn("111111", bins)  # 应被列出
        self.assertNotIn("222222", bins)  # 失败率不够高
        self.assertNotIn("333333", bins)  # 尝试数不够

    def test_list_unhealthy_bins_skips_empty_bin_field(self):
        """没填 card_bin 的 Run 不应进入聚合（避免空 bin 误聚合）。"""
        with get_session() as session:
            for i in range(5):
                session.add(_make_run(id=f"x{i:03d}" + "a" * 13, status="failed", card_bin=""))
            session.commit()
        self.assertEqual(list_unhealthy_bins(min_attempts=3), [])


if __name__ == "__main__":
    unittest.main()
