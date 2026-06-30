# -*- coding: utf-8 -*-
"""卡池服务测试。覆盖入池 / 列表 / 选熟卡 / 热卡触发与状态写回。"""

import os
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import CardActivation
from src.models import CardInfo
from src.services.card_pool_service import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    add_card,
    card_activation_to_info,
    get_warmup_status,
    list_pool,
    select_for_task,
    trigger_warmup,
)


def _build_threadsafe_engine():
    """in-memory SQLite + StaticPool，让多线程共享同一份内存 DB。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _reset_engine():
    engine_mod._engine = None


def _fake_card_info(last4="4242", bin_country="US") -> CardInfo:
    return CardInfo(
        card_number="4242424242424242"[:-4] + last4,
        expiry_month="12",
        expiry_year="2030",
        cvv="123",
        last_four=last4,
        name_on_card="John Doe",
        status="active",
        created_at="",
        billing_address="123 Main St",
        bin_country=bin_country,
    )


class _StubCardApi:
    def __init__(self, info=None, raise_exc=False):
        self._info = info
        self._raise = raise_exc
        self.calls: list[str] = []

    def get_card(self, card_key: str):
        self.calls.append(card_key)
        if self._raise:
            raise RuntimeError("simulated card api failure")
        return self._info


class CardActivationToInfoTest(unittest.TestCase):
    def test_basic_field_mapping(self):
        rec = CardActivation(
            card_key="key1",
            card_provider="efuncard",
            card_number="4111111111111111",
            expiry_month="06",
            expiry_year="2028",
            cvv="999",
            name_on_card="Alice",
            billing_address="addr1",
            bin_country="GB",
            activated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        )
        info = card_activation_to_info(rec)
        self.assertEqual(info.card_number, "4111111111111111")
        self.assertEqual(info.last_four, "1111")
        self.assertEqual(info.bin_country, "GB")
        self.assertEqual(info.name_on_card, "Alice")


class CardPoolDBTest(unittest.TestCase):
    """需要 in-memory DB 的集成测试。

    用 StaticPool 让多线程（trigger_warmup 起的后台 thread）共享同一个内存 DB，
    否则后台 thread 用 get_session() 拿到的是新 connection = 空 DB。
    """

    def setUp(self):
        # 每个 case 独立 engine，避免 case 之间互相污染
        self.engine = _build_threadsafe_engine()
        # monkey-patch engine module 里的 _engine 单例
        self._original_engine = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original_engine

    # ── add_card ──────────────────────────────────────────
    def test_add_card_success(self):
        api = _StubCardApi(info=_fake_card_info())
        rec, meta = add_card(
            card_key="cdk-001", card_provider="efuncard",
            target_warmup_count=5, card_api=api,
        )
        self.assertIsNotNone(rec)
        self.assertEqual(meta, {})
        self.assertEqual(rec.card_provider, "efuncard")
        self.assertEqual(rec.target_warmup_count, 5)
        self.assertEqual(rec.warmup_count, 0)
        self.assertEqual(rec.last_warmup_status, STATUS_PENDING)
        self.assertEqual(api.calls, ["cdk-001"])

    def test_add_card_invalid_provider(self):
        rec, meta = add_card(
            card_key="cdk-x", card_provider="unknown",
            target_warmup_count=5, card_api=_StubCardApi(),
        )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "invalid_provider")

    def test_add_card_negative_target(self):
        rec, meta = add_card(
            card_key="cdk-x", card_provider="efuncard",
            target_warmup_count=-1, card_api=_StubCardApi(),
        )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "invalid_target")

    def test_add_card_empty_key(self):
        rec, meta = add_card(
            card_key="   ", card_provider="efuncard",
            target_warmup_count=3, card_api=_StubCardApi(),
        )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "empty_card_key")

    def test_add_card_duplicate(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="dup", card_provider="efuncard", target_warmup_count=3, card_api=api)
        rec, meta = add_card(
            card_key="dup", card_provider="efuncard",
            target_warmup_count=3, card_api=api,
        )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "duplicate")
        # 卡商应该只被调一次（第二次入池前发现重复直接返回）
        self.assertEqual(api.calls, ["dup"])

    def test_add_card_api_failure(self):
        rec, meta = add_card(
            card_key="apifail", card_provider="nodecard",
            target_warmup_count=3, card_api=_StubCardApi(raise_exc=True),
        )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "card_api_error")

    def test_add_card_api_returns_none(self):
        rec, meta = add_card(
            card_key="empty-card", card_provider="x988card",
            target_warmup_count=3, card_api=_StubCardApi(info=None),
        )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "card_not_found")

    def test_add_card_persists_x988_last_meta(self):
        api = _StubCardApi(info=_fake_card_info())
        api._last_meta = {"sms_api": "https://sms.example/q?secret=1", "phone": "+15551234567"}
        rec, meta = add_card(
            card_key="x988-meta", card_provider="x988card",
            target_warmup_count=1, card_api=api,
        )
        self.assertIsNotNone(rec)
        self.assertEqual(meta, {})
        with get_session() as s:
            stored = s.get(CardActivation, "x988-meta")
            self.assertEqual(stored.sms_api, "https://sms.example/q?secret=1")
            self.assertEqual(stored.phone, "+15551234567")

    def test_add_card_integrity_error_maps_duplicate_race(self):
        api = _StubCardApi(info=_fake_card_info())
        with mock.patch(
            "src.services.card_pool_service.Session.commit",
            side_effect=IntegrityError("insert", {}, Exception("unique")),
        ):
            rec, meta = add_card(
                card_key="race", card_provider="efuncard",
                target_warmup_count=1, card_api=api,
            )
        self.assertIsNone(rec)
        self.assertEqual(meta["reason"], "duplicate_race")

    # ── list_pool / select_for_task ───────────────────────
    def test_list_pool_excludes_invalidated_by_default(self):
        api = _StubCardApi(info=_fake_card_info(last4="1111"))
        add_card(card_key="alive", card_provider="efuncard", target_warmup_count=1, card_api=api)
        api2 = _StubCardApi(info=_fake_card_info(last4="2222"))
        add_card(card_key="dead", card_provider="efuncard", target_warmup_count=1, card_api=api2)
        # 把第二张作废
        with get_session() as s:
            rec = s.get(CardActivation, "dead")
            rec.is_invalidated = True
            s.add(rec)
            s.commit()
        rows = list_pool()
        self.assertEqual([r.card_key for r in rows], ["alive"])
        # include_invalidated=True 应能拿到全部
        rows = list_pool(include_invalidated=True)
        self.assertEqual({r.card_key for r in rows}, {"alive", "dead"})

    def test_list_pool_only_ready_filter(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="ripe", card_provider="efuncard", target_warmup_count=2, card_api=api)
        api2 = _StubCardApi(info=_fake_card_info(last4="9999"))
        add_card(card_key="green", card_provider="efuncard", target_warmup_count=5, card_api=api2)
        # 让 ripe 达到 target
        with get_session() as s:
            rec = s.get(CardActivation, "ripe")
            rec.warmup_count = 2
            rec.last_warmup_status = STATUS_SUCCESS
            s.add(rec)
            s.commit()
        rows = list_pool(only_ready=True)
        self.assertEqual([r.card_key for r in rows], ["ripe"])

    def test_select_for_task_returns_ready_card(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="ready1", card_provider="efuncard", target_warmup_count=2, card_api=api)
        with get_session() as s:
            rec = s.get(CardActivation, "ready1")
            rec.warmup_count = 3  # 超额
            s.add(rec)
            s.commit()
        chosen = select_for_task()
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.card_key, "ready1")

    def test_select_for_task_returns_none_when_pool_empty(self):
        self.assertIsNone(select_for_task())

    def test_select_for_task_returns_none_when_no_ready_card(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="green-only", card_provider="efuncard", target_warmup_count=10, card_api=api)
        # warmup_count=0 < target=10 → 不算 ready
        self.assertIsNone(select_for_task())

    # ── get_warmup_status ─────────────────────────────────
    def test_get_warmup_status_returns_none_for_missing(self):
        self.assertIsNone(get_warmup_status("nonexistent"))

    def test_get_warmup_status_returns_pending_for_new_card(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="newone", card_provider="efuncard", target_warmup_count=3, card_api=api)
        st = get_warmup_status("newone")
        self.assertEqual(st["last_warmup_status"], STATUS_PENDING)
        self.assertEqual(st["warmup_count"], 0)
        self.assertEqual(st["target_warmup_count"], 3)
        self.assertFalse(st["is_ready"])

    # ── trigger_warmup ────────────────────────────────────
    def test_trigger_warmup_sets_running_and_writes_success(self):
        """验证后台 thread 跑完后 DB 字段正确写回。"""
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="warm-me", card_provider="efuncard", target_warmup_count=5, card_api=api)

        # mock execute_card_warmup 返回 True
        with mock.patch("src.orchestration.warmup.execute_card_warmup", return_value=True) as mock_warm:
            ret = trigger_warmup(
                "warm-me",
                config=mock.MagicMock(),
                card_api=mock.MagicMock(),
                svc_for_warmup_pool=mock.MagicMock(),
                proxy_url="",
            )
            self.assertEqual(ret["status"], STATUS_RUNNING)
            self.assertEqual(ret["warmup_count"], 0)
            self.assertEqual(ret["target_warmup_count"], 5)

            # 等后台线程跑完
            for _ in range(50):
                time.sleep(0.05)
                st = get_warmup_status("warm-me")
                if st["last_warmup_status"] in (STATUS_SUCCESS, STATUS_FAILED):
                    break
            self.assertEqual(st["last_warmup_status"], STATUS_SUCCESS)
            self.assertEqual(st["warmup_count"], 1)
            mock_warm.assert_called_once()

    def test_trigger_warmup_writes_failed_on_false_return(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="fail-me", card_provider="efuncard", target_warmup_count=3, card_api=api)
        svc = mock.MagicMock()
        svc.last_card_warmup_reason = "upgrade_failed_round1_after_retry:主页找不到 Upgrade 入口"
        with mock.patch("src.orchestration.warmup.execute_card_warmup", return_value=False):
            trigger_warmup(
                "fail-me",
                config=mock.MagicMock(),
                card_api=mock.MagicMock(),
                svc_for_warmup_pool=svc,
                proxy_url="",
            )
            for _ in range(50):
                time.sleep(0.05)
                st = get_warmup_status("fail-me")
                if st["last_warmup_status"] in (STATUS_SUCCESS, STATUS_FAILED):
                    break
            self.assertEqual(st["last_warmup_status"], STATUS_FAILED)
            self.assertEqual(st["warmup_count"], 0)
            self.assertIn("upgrade_failed_round1_after_retry", st["last_warmup_reason"])

    def test_trigger_warmup_writes_failed_on_exception(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="boom", card_provider="efuncard", target_warmup_count=3, card_api=api)
        with mock.patch(
            "src.orchestration.warmup.execute_card_warmup",
            side_effect=RuntimeError("simulated boom"),
        ):
            trigger_warmup(
                "boom",
                config=mock.MagicMock(),
                card_api=mock.MagicMock(),
                svc_for_warmup_pool=mock.MagicMock(),
                proxy_url="",
            )
            for _ in range(50):
                time.sleep(0.05)
                st = get_warmup_status("boom")
                if st["last_warmup_status"] in (STATUS_SUCCESS, STATUS_FAILED):
                    break
            self.assertEqual(st["last_warmup_status"], STATUS_FAILED)
            self.assertIn("simulated boom", st["last_warmup_reason"])

    def test_trigger_warmup_rejects_when_already_running(self):
        from src.services.card_pool_service import _AlreadyRunningError
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="busy", card_provider="efuncard", target_warmup_count=3, card_api=api)
        # 手动把状态设为 running 模拟有线程在跑
        with get_session() as s:
            rec = s.get(CardActivation, "busy")
            rec.last_warmup_status = STATUS_RUNNING
            s.add(rec)
            s.commit()
        with self.assertRaises(_AlreadyRunningError):
            trigger_warmup(
                "busy",
                config=mock.MagicMock(),
                card_api=mock.MagicMock(),
                svc_for_warmup_pool=mock.MagicMock(),
                proxy_url="",
            )

    def test_trigger_warmup_rejects_invalidated(self):
        api = _StubCardApi(info=_fake_card_info())
        add_card(card_key="dead", card_provider="efuncard", target_warmup_count=3, card_api=api)
        with get_session() as s:
            rec = s.get(CardActivation, "dead")
            rec.is_invalidated = True
            s.add(rec)
            s.commit()
        with self.assertRaises(ValueError):
            trigger_warmup(
                "dead",
                config=mock.MagicMock(),
                card_api=mock.MagicMock(),
                svc_for_warmup_pool=mock.MagicMock(),
                proxy_url="",
            )

    def test_trigger_warmup_rejects_unknown_card(self):
        with self.assertRaises(ValueError):
            trigger_warmup(
                "nonexistent",
                config=mock.MagicMock(),
                card_api=mock.MagicMock(),
                svc_for_warmup_pool=mock.MagicMock(),
                proxy_url="",
            )


if __name__ == "__main__":
    unittest.main()
