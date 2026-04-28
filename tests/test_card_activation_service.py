# -*- coding: utf-8 -*-
"""CardActivation 缓存 service 测试

覆盖 8 个核心场景 + 2 个工具函数 + 2 个列表/查询 API。
设计：每个测试用 in-memory SQLite，避免污染主库；mock fetcher 验证调用次数。
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.db.engine as db_engine
from src.db import crypto
from src.db.models import CardActivation
from src.models import CardInfo
from src.services.card_activation_service import (
    WARMUP_CARD_CACHE_MAX_AGE_DAYS,
    _is_card_still_valid,
    get_activation,
    get_or_create_activation,
    get_sms_api,
    invalidate,
    list_activations,
)


def _build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _make_card(
    *,
    card_number: str = "4111111111111111",
    expiry_month: str = "12",
    expiry_year: str = "2030",
    cvv: str = "123",
    name: str = "Test User",
    bin_country: str = "US",
) -> CardInfo:
    return CardInfo(
        card_number=card_number,
        expiry_month=expiry_month,
        expiry_year=expiry_year,
        cvv=cvv,
        name_on_card=name,
        billing_address="123 Main St",
        bin_country=bin_country,
    )


class _IsolatedDBMixin:
    def setUp(self):
        crypto.reset_for_tests()
        self.engine = _build_engine()
        self._engine_patch = mock.patch.object(db_engine, "_engine", self.engine)
        self._engine_patch.start()

    def tearDown(self):
        self._engine_patch.stop()
        crypto.reset_for_tests()


# ────────────────────────────────────────────────────
# get_or_create_activation
# ────────────────────────────────────────────────────


class TestGetOrCreateActivation(_IsolatedDBMixin, unittest.TestCase):
    def test_miss_calls_fetcher_and_persists(self):
        """缓存 miss → fetcher 被调一次 → 数据落 DB"""
        card = _make_card()
        meta = {"sms_api": "https://sms.example/q", "phone": "+1234567890"}
        fetcher = mock.MagicMock(return_value=(card, meta))

        result, returned_meta = get_or_create_activation(
            "CDK_NEW", "x988card", fetcher=fetcher,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.card_number, card.card_number)
        self.assertEqual(returned_meta["sms_api"], meta["sms_api"])
        fetcher.assert_called_once()

        # 落库验证
        with Session(self.engine) as s:
            rec = s.get(CardActivation, "CDK_NEW")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.card_number, card.card_number)
            self.assertEqual(rec.sms_api, meta["sms_api"])
            self.assertEqual(rec.use_count, 0)  # 第一次入库 use_count=0

    def test_hit_skips_fetcher_and_increments_use_count(self):
        """缓存命中 → fetcher 不被调 → use_count++ → last_used_at 更新"""
        card = _make_card()
        meta = {"sms_api": "https://sms.example/q", "phone": ""}
        fetcher = mock.MagicMock(return_value=(card, meta))

        # 第一次
        get_or_create_activation("CDK_HIT", "x988card", fetcher=fetcher)
        # 第二次（应命中缓存）
        result, _ = get_or_create_activation("CDK_HIT", "x988card", fetcher=fetcher)

        self.assertIsNotNone(result)
        fetcher.assert_called_once()  # 仍然只调一次

        with Session(self.engine) as s:
            rec = s.get(CardActivation, "CDK_HIT")
            self.assertEqual(rec.use_count, 1)  # 命中一次
            self.assertIsNotNone(rec.last_used_at)

    def test_card_expired_returns_none_without_fetcher(self):
        """卡本身 expiry 已过 → 返回 None + 不调 fetcher（X988 不能重新 verify）"""
        # 直接落一张过期卡
        with Session(self.engine) as s:
            s.add(CardActivation(
                card_key="CDK_EXP",
                card_provider="x988card",
                card_number="4111000011112222",
                expiry_month="01",
                expiry_year="2020",  # 已过期
                cvv="999",
                sms_api="x",
            ))
            s.commit()

        fetcher = mock.MagicMock()
        result, meta = get_or_create_activation("CDK_EXP", "x988card", fetcher=fetcher)

        self.assertIsNone(result)
        self.assertEqual(meta.get("reason"), "card_expired")
        fetcher.assert_not_called()

    def test_max_age_expiry_returns_none_without_fetcher(self):
        """activated_at 超过 max_age → 缓存失效 + 不调 fetcher"""
        old_activated = datetime.now(timezone.utc) - timedelta(days=WARMUP_CARD_CACHE_MAX_AGE_DAYS + 1)
        with Session(self.engine) as s:
            s.add(CardActivation(
                card_key="CDK_OLD",
                card_provider="x988card",
                card_number="4111000011113333",
                expiry_month="12",
                expiry_year="2099",  # 卡本身没过期
                cvv="999",
                sms_api="x",
                activated_at=old_activated,
            ))
            s.commit()

        fetcher = mock.MagicMock()
        result, meta = get_or_create_activation("CDK_OLD", "x988card", fetcher=fetcher)

        self.assertIsNone(result)
        self.assertEqual(meta.get("reason"), "card_expired")
        fetcher.assert_not_called()

    def test_invalidated_returns_none_without_fetcher(self):
        """is_invalidated=True → 永久失效，绝不再 fetcher"""
        with Session(self.engine) as s:
            s.add(CardActivation(
                card_key="CDK_INV",
                card_provider="x988card",
                card_number="4111000011114444",
                expiry_month="12",
                expiry_year="2099",
                cvv="999",
                sms_api="x",
                is_invalidated=True,
                invalidate_reason="test_reason",
            ))
            s.commit()

        fetcher = mock.MagicMock()
        result, meta = get_or_create_activation("CDK_INV", "x988card", fetcher=fetcher)

        self.assertIsNone(result)
        self.assertEqual(meta.get("reason"), "card_invalidated")
        fetcher.assert_not_called()

    def test_fetcher_returns_none_no_persist(self):
        """fetcher 失败返回 None → 不落库"""
        fetcher = mock.MagicMock(return_value=(None, {"reason": "verify_failed"}))
        result, meta = get_or_create_activation("CDK_FAIL", "x988card", fetcher=fetcher)
        self.assertIsNone(result)
        self.assertEqual(meta.get("reason"), "verify_failed")

        with Session(self.engine) as s:
            self.assertIsNone(s.get(CardActivation, "CDK_FAIL"))


# ────────────────────────────────────────────────────
# get_sms_api
# ────────────────────────────────────────────────────


class TestGetSmsApi(_IsolatedDBMixin, unittest.TestCase):
    def test_hit_returns_sms_api(self):
        with Session(self.engine) as s:
            s.add(CardActivation(
                card_key="CDK_S1",
                card_number="x", expiry_month="12", expiry_year="2099", cvv="x",
                sms_api="https://sms.example/abc",
            ))
            s.commit()
        self.assertEqual(get_sms_api("CDK_S1"), "https://sms.example/abc")

    def test_unknown_returns_empty(self):
        self.assertEqual(get_sms_api("NOPE"), "")

    def test_invalidated_returns_empty(self):
        with Session(self.engine) as s:
            s.add(CardActivation(
                card_key="CDK_S2",
                card_number="x", expiry_month="12", expiry_year="2099", cvv="x",
                sms_api="https://sms.example/q", is_invalidated=True,
            ))
            s.commit()
        self.assertEqual(get_sms_api("CDK_S2"), "")

    def test_expired_returns_empty(self):
        with Session(self.engine) as s:
            s.add(CardActivation(
                card_key="CDK_S3",
                card_number="x", expiry_month="01", expiry_year="2020", cvv="x",
                sms_api="https://sms.example/q",
            ))
            s.commit()
        self.assertEqual(get_sms_api("CDK_S3"), "")


# ────────────────────────────────────────────────────
# invalidate / list / get_activation
# ────────────────────────────────────────────────────


class TestManagementOps(_IsolatedDBMixin, unittest.TestCase):
    def _seed(self, card_key: str, **overrides):
        defaults = dict(
            card_provider="x988card",
            card_number="4111000011115555",
            expiry_month="12",
            expiry_year="2099",
            cvv="111",
            sms_api="https://sms.example/x",
        )
        defaults.update(overrides)
        with Session(self.engine) as s:
            s.add(CardActivation(card_key=card_key, **defaults))
            s.commit()

    def test_invalidate_marks_record(self):
        self._seed("CDK_M1")
        ok = invalidate("CDK_M1", reason="manual_test")
        self.assertTrue(ok)
        with Session(self.engine) as s:
            rec = s.get(CardActivation, "CDK_M1")
            self.assertTrue(rec.is_invalidated)
            self.assertEqual(rec.invalidate_reason, "manual_test")

    def test_invalidate_unknown_returns_false(self):
        self.assertFalse(invalidate("DOES_NOT_EXIST"))

    def test_list_excludes_invalidated_by_default(self):
        self._seed("CDK_A")
        self._seed("CDK_B", is_invalidated=True)
        active_only = list_activations()
        self.assertEqual(len(active_only), 1)
        self.assertEqual(active_only[0].card_key, "CDK_A")

    def test_list_includes_invalidated_when_requested(self):
        self._seed("CDK_A")
        self._seed("CDK_B", is_invalidated=True)
        full = list_activations(include_invalidated=True)
        self.assertEqual(len(full), 2)

    def test_get_activation_unknown_returns_none(self):
        self.assertIsNone(get_activation("UNKNOWN"))

    def test_get_activation_returns_record(self):
        self._seed("CDK_GET")
        rec = get_activation("CDK_GET")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.card_key, "CDK_GET")

    def test_list_filters_by_card_provider(self):
        """list_activations(card_provider='efuncard') 只返回该卡商的记录。"""
        self._seed("CDK_X988", card_provider="x988card")
        self._seed("CDK_EFUN", card_provider="efuncard")
        self._seed("CDK_NODE", card_provider="nodecard")

        x988 = list_activations(card_provider="x988card")
        self.assertEqual(len(x988), 1)
        self.assertEqual(x988[0].card_key, "CDK_X988")

        efun = list_activations(card_provider="efuncard")
        self.assertEqual(len(efun), 1)
        self.assertEqual(efun[0].card_key, "CDK_EFUN")

        all_ = list_activations()
        self.assertEqual(len(all_), 3)


class TestRecordLookup(_IsolatedDBMixin, unittest.TestCase):
    """record_lookup（efuncard / nodecard 审计写入）"""

    def test_first_lookup_inserts_with_use_count_1(self):
        from src.services.card_activation_service import record_lookup
        card = _make_card()
        rec = record_lookup("EFUN_A", "efuncard", card)
        self.assertEqual(rec.card_provider, "efuncard")
        self.assertEqual(rec.use_count, 1)
        self.assertIsNotNone(rec.last_used_at)

    def test_repeated_lookup_increments_use_count(self):
        from src.services.card_activation_service import record_lookup
        card = _make_card()
        record_lookup("EFUN_B", "efuncard", card)
        record_lookup("EFUN_B", "efuncard", card)
        rec = record_lookup("EFUN_B", "efuncard", card)
        self.assertEqual(rec.use_count, 3)

        # 不应插重复行
        with Session(self.engine) as s:
            from sqlmodel import select as sel
            count = len(list(s.exec(sel(CardActivation).where(CardActivation.card_key == "EFUN_B")).all()))
            self.assertEqual(count, 1)

    def test_record_lookup_does_not_overwrite_card_number(self):
        """重复调用不会用新 card 数据覆盖旧记录（防错误数据污染）。"""
        from src.services.card_activation_service import record_lookup
        card1 = _make_card(card_number="4111111111111111")
        record_lookup("EFUN_C", "efuncard", card1)

        # 第二次传不同卡号（理论不会发生，但防御性）
        card2 = _make_card(card_number="4222222222222222")
        rec = record_lookup("EFUN_C", "efuncard", card2)
        self.assertEqual(rec.card_number, "4111111111111111", "保持首次记录的卡号")
        self.assertEqual(rec.use_count, 2)


# ────────────────────────────────────────────────────
# _is_card_still_valid 工具函数边界
# ────────────────────────────────────────────────────


class TestIsCardStillValid(unittest.TestCase):
    def _rec(self, **kwargs):
        defaults = dict(
            card_key="x", card_number="x", cvv="x",
            expiry_month="12", expiry_year="2099",
            sms_api="x",
            activated_at=datetime.now(timezone.utc),
        )
        defaults.update(kwargs)
        return CardActivation(**defaults)

    def test_future_card_valid(self):
        self.assertTrue(_is_card_still_valid(self._rec()))

    def test_past_card_invalid(self):
        self.assertFalse(_is_card_still_valid(self._rec(expiry_year="2020")))

    def test_bad_data_safely_invalid(self):
        self.assertFalse(_is_card_still_valid(self._rec(expiry_year="abc")))
        self.assertFalse(_is_card_still_valid(self._rec(expiry_month="")))

    def test_old_activated_invalid_by_max_age(self):
        old = datetime.now(timezone.utc) - timedelta(days=WARMUP_CARD_CACHE_MAX_AGE_DAYS + 1)
        self.assertFalse(_is_card_still_valid(self._rec(activated_at=old)))

    # ────────────────────────────────────────────────────
    # C3: max_age 边界（6.99 / 7.00 / 7.01 天 + 时区污染）
    # ────────────────────────────────────────────────────

    def test_just_under_max_age_still_valid(self):
        """6.99 天前激活 → days=6 → 仍 valid。"""
        almost = datetime.now(timezone.utc) - timedelta(days=WARMUP_CARD_CACHE_MAX_AGE_DAYS) + timedelta(hours=1)
        self.assertTrue(
            _is_card_still_valid(self._rec(activated_at=almost)),
            f"略小于 {WARMUP_CARD_CACHE_MAX_AGE_DAYS} 天应当 valid",
        )

    def test_exactly_max_age_invalid(self):
        """刚好 7.00 天前激活 → days >= 7 → invalid（边界 inclusive）。"""
        exact = datetime.now(timezone.utc) - timedelta(days=WARMUP_CARD_CACHE_MAX_AGE_DAYS, seconds=1)
        self.assertFalse(
            _is_card_still_valid(self._rec(activated_at=exact)),
            f"刚好 {WARMUP_CARD_CACHE_MAX_AGE_DAYS} 天应当 invalid（>= 判定）",
        )

    def test_naive_activated_at_treated_as_utc(self):
        """SQLite 取出来的 datetime 是 naive；_ensure_aware 应当补 UTC，否则会把"现在"当成"未来"。"""
        # 模拟 SQLite 取出的 naive datetime（无 tzinfo）
        naive_now = datetime.utcnow()  # naive
        rec = self._rec(activated_at=naive_now)
        # 不应当 raise；naive 当 UTC 处理后判定 valid
        self.assertTrue(
            _is_card_still_valid(rec),
            "naive datetime 应当被 _ensure_aware 当 UTC 处理，不应触发异常或误判过期",
        )

    def test_naive_old_activated_at_correctly_expired(self):
        """SQLite naive datetime 8 天前 → 应当判定 invalid（验证 _ensure_aware 没把它误读成新值）。"""
        naive_old = datetime.utcnow() - timedelta(days=WARMUP_CARD_CACHE_MAX_AGE_DAYS + 1)
        self.assertFalse(
            _is_card_still_valid(self._rec(activated_at=naive_old)),
            "naive 老时间应当正确判定为过期",
        )


# ────────────────────────────────────────────────────
# C4: get_or_create_activation 并发竞态（fetcher 被调几次）
# ────────────────────────────────────────────────────


class TestGetOrCreateActivationConcurrency(_IsolatedDBMixin, unittest.TestCase):
    """两个 worker 同时 cache miss → fetcher 被调几次？

    设计意图：当 worker A 第一次 miss 调 fetcher（外部 X988 verify），worker B
    紧随其后也 miss、也调 fetcher 时，X988 verify 会被消耗 2 次（实际只需 1 次）。
    现有"二次防御"（card_activation_service.py:161）只防 INSERT 主键冲突，**不防
    fetcher 被重复调用**。

    本测试用 threading.Barrier 强制两个 worker 在 fetcher 入口同步，记录现状：
    fetcher.call_count == 2（确认 X988 verify 配额会被浪费）。

    修复方向（非本 turn 改）：在 get_or_create_activation 内加 card_key 级
    threading.Lock dict，让第二个 worker 等第一个完成后再读缓存。
    """

    def test_concurrent_miss_calls_fetcher_only_once(self):
        """修复后：两个 worker 同时 cache miss → fetcher 只被调 1 次。

        修复方式：card_activation_service.py 加了 card_key 级 threading.Lock dict
        (_get_card_key_lock)。第一个 worker 抢到 lock 后调 fetcher + 落库；
        第二个 worker 在 lock 处 block，醒来后 session.get 直接命中缓存。

        这同时消掉了：
        - fetcher 被双调 → 浪费 X988 verify 配额
        - 第二个 worker 在 INSERT 时撞 UNIQUE 约束 → 抛 IntegrityError 让 warmup 崩
        """
        import threading
        import time
        from src.services.card_activation_service import _card_key_locks

        # 清理可能残留的 card_key lock（其它测试用过）
        _card_key_locks.clear()

        call_count = {"n": 0}
        # 第一个进 fetcher 的线程会 hold 100ms，确保第二个线程能在 lock 处 block 住
        results: dict[str, object] = {}

        def fetcher():
            call_count["n"] += 1
            time.sleep(0.1)  # 拖延一下，让第二个线程在 lock 处 block 上
            return _make_card(), {"sms_api": "https://x", "phone": "+1"}

        def worker(label: str):
            try:
                ci, _meta = get_or_create_activation(
                    "CDK_RACE_FIXED", "x988card", fetcher=fetcher,
                )
                results[label] = "ok" if ci else "none"
            except Exception as exc:
                results[label] = type(exc).__name__

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        # 给 t1 ~10ms 先进 get_or_create_activation 抢到 card_key lock
        time.sleep(0.01)
        t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        # 修复后：只调 1 次 fetcher
        self.assertEqual(
            call_count["n"], 1,
            "card_key 级 lock 已生效 → fetcher 只该被调 1 次。"
            f"实际 {call_count['n']} 次说明 lock 漏了。",
        )

        # 两个 worker 都应当成功拿到卡（一个走 fetcher 路径，一个走"已存在"路径）
        outcomes = sorted(results.values())
        self.assertEqual(outcomes, ["ok", "ok"], f"两个 worker 都应成功，实际 {outcomes}")

        # DB 里只有 1 条记录
        with Session(self.engine) as s:
            from sqlmodel import select as sel
            rows = list(s.exec(sel(CardActivation).where(CardActivation.card_key == "CDK_RACE_FIXED")).all())
            self.assertEqual(len(rows), 1)
            # 第一次 fetcher 成功后 use_count=0，第二个 worker 走"已存在"分支 use_count++
            self.assertEqual(rows[0].use_count, 1)


if __name__ == "__main__":
    unittest.main()
