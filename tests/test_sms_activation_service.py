# -*- coding: utf-8 -*-
"""SMS 号码复用调度服务测试（sms_activation_service）。

用 in-memory SQLite 跑真实 DB 逻辑（不 mock DB），覆盖：
- record_allocation 落库
- try_reuse_active_number 认领 + use_count 自增
- 用满 max_uses 后不再复用
- 跨 provider / 跨 country 不串号
- invalidate 作废
"""

import os
import unittest

import src.db.engine as engine_mod
from src.db.engine import init_db, get_session
from src.db.models import SmsActivation
from src.services import sms_activation_service as svc


def _reset_engine():
    engine_mod._engine = None


class TestSmsActivationService(unittest.TestCase):

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
        from sqlmodel import SQLModel
        SQLModel.metadata.drop_all(self.engine)
        SQLModel.metadata.create_all(self.engine)

    # ── record_allocation ──────────────────────────────

    def test_record_allocation_persists(self):
        rec = svc.record_allocation(
            "ORDER_1", provider_name="hero_sms", country="182",
            phone_number="81900000000", service="dr",
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec.use_count, 1)
        self.assertEqual(rec.max_uses, 3)
        with get_session() as s:
            stored = s.get(SmsActivation, "ORDER_1")
            self.assertEqual(stored.phone_number, "81900000000")
            self.assertEqual(stored.provider_name, "hero_sms")

    def test_record_allocation_empty_order_id_returns_none(self):
        self.assertIsNone(svc.record_allocation("", provider_name="hero_sms", country="6", phone_number="x"))

    # ── try_reuse_active_number ────────────────────────

    def test_reuse_increments_use_count(self):
        svc.record_allocation("O1", provider_name="hero_sms", country="182", phone_number="P1")
        # 第一次复用：use_count 1 → 2
        rec = svc.try_reuse_active_number("hero_sms", "182")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.order_id, "O1")
        self.assertEqual(rec.use_count, 2)
        # 第二次复用：2 → 3（达上限）
        rec2 = svc.try_reuse_active_number("hero_sms", "182")
        self.assertEqual(rec2.use_count, 3)
        # 第三次：已用满（3>=3）不再复用
        self.assertIsNone(svc.try_reuse_active_number("hero_sms", "182"))

    def test_reuse_no_candidate_returns_none(self):
        self.assertIsNone(svc.try_reuse_active_number("hero_sms", "999"))

    def test_reuse_isolated_by_provider(self):
        """不同 provider 不串号。"""
        svc.record_allocation("O_hero", provider_name="hero_sms", country="182", phone_number="PH")
        # 查 grizzly_sms 应查不到 hero 的号
        self.assertIsNone(svc.try_reuse_active_number("grizzly_sms", "182"))
        # 查 hero_sms 能查到
        self.assertIsNotNone(svc.try_reuse_active_number("hero_sms", "182"))

    def test_reuse_isolated_by_country(self):
        """不同 country 不串号。"""
        svc.record_allocation("O_jp", provider_name="hero_sms", country="182", phone_number="JP")
        self.assertIsNone(svc.try_reuse_active_number("hero_sms", "187"))
        self.assertIsNotNone(svc.try_reuse_active_number("hero_sms", "182"))

    def test_reuse_skips_invalidated(self):
        svc.record_allocation("O_bad", provider_name="hero_sms", country="182", phone_number="B")
        svc.invalidate("O_bad", reason="test")
        self.assertIsNone(svc.try_reuse_active_number("hero_sms", "182"))

    def test_reuse_picks_oldest_first(self):
        """多个活号时认领最早的（先用完再换）。"""
        svc.record_allocation("O_old", provider_name="hero_sms", country="182", phone_number="OLD")
        svc.record_allocation("O_new", provider_name="hero_sms", country="182", phone_number="NEW")
        rec = svc.try_reuse_active_number("hero_sms", "182")
        self.assertEqual(rec.order_id, "O_old")

    # ── invalidate ─────────────────────────────────────

    def test_invalidate(self):
        svc.record_allocation("O_x", provider_name="hero_sms", country="6", phone_number="X")
        self.assertTrue(svc.invalidate("O_x", reason="cancelled"))
        with get_session() as s:
            rec = s.get(SmsActivation, "O_x")
            self.assertTrue(rec.is_invalidated)
            self.assertEqual(rec.invalidate_reason, "cancelled")

    def test_invalidate_missing_returns_false(self):
        self.assertFalse(svc.invalidate("NOPE"))

    # ── 黑名单（号被用过/被拒）──────────────────────────

    def test_is_phone_blacklisted_after_invalidate(self):
        """invalidate 一个 order 后，其 phone_number 应被识别为黑名单。"""
        svc.record_allocation("O_bl", provider_name="hero_sms", country="6", phone_number="6281111")
        self.assertFalse(svc.is_phone_blacklisted("6281111"))  # 未失效前不黑
        svc.invalidate("O_bl", reason="otp_timeout")
        self.assertTrue(svc.is_phone_blacklisted("6281111"))   # 失效后拉黑

    def test_is_phone_blacklisted_unknown_returns_false(self):
        self.assertFalse(svc.is_phone_blacklisted("0000000"))
        self.assertFalse(svc.is_phone_blacklisted(""))

    def test_blacklist_phone_creates_synthetic_record(self):
        """blacklist_phone 给无 order 的号也能落黑名单。"""
        svc.blacklist_phone("6289999", "hero_sms", "6", reason="rejected")
        self.assertTrue(svc.is_phone_blacklisted("6289999"))
        # 合成记录 order_id=blacklist:<phone>
        with get_session() as s:
            rec = s.get(SmsActivation, "blacklist:6289999")
            self.assertIsNotNone(rec)
            self.assertTrue(rec.is_invalidated)

    def test_blacklisted_number_not_reused(self):
        """黑名单号不应被 try_reuse_active_number 复用。"""
        svc.record_allocation("O_r", provider_name="hero_sms", country="6", phone_number="6282222")
        svc.invalidate("O_r", reason="rejected")
        self.assertIsNone(svc.try_reuse_active_number("hero_sms", "6"))


if __name__ == "__main__":
    unittest.main()
