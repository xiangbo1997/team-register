# -*- coding: utf-8 -*-
"""5 项 ambiguity 清理测试集

覆盖：
  - MailAccount.role 字段（schema migration + 默认值）
  - save_mail_account role 白名单校验
  - delete_mail_account default 引用阻止（409）
  - update_config 写 AppSettingRevision
  - list_app_setting_revisions 查询
"""

import os
import unittest
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

import src.db.engine as db_engine
from src.db import crypto
from src.db.models import AppSetting, AppSettingRevision, MailAccount
from src.services.config_service import (
    ConfigService,
    InvalidMailAccountRoleError,
    MailAccountInDefaultUseError,
    VALID_MAIL_ACCOUNT_ROLES,
)


def _build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class _IsolatedDBMixin:
    """每个测试用独立内存 DB，避免污染。"""

    def setUp(self):
        crypto.reset_for_tests()
        self.engine = _build_engine()
        # patch get_session 全部走我们的 engine
        self._engine_patch = mock.patch.object(db_engine, "_engine", self.engine)
        self._engine_patch.start()

    def tearDown(self):
        self._engine_patch.stop()
        crypto.reset_for_tests()


class TestMailAccountRoleSchema(_IsolatedDBMixin, unittest.TestCase):
    def test_role_default_regular(self):
        with Session(self.engine) as s:
            acc = MailAccount(email="x@y.com", client_id="cid", refresh_token="rt")
            s.add(acc)
            s.commit()
            s.refresh(acc)
            self.assertEqual(acc.role, "regular")

    def test_role_can_be_pro_warmup(self):
        with Session(self.engine) as s:
            acc = MailAccount(email="x@y.com", role="pro_warmup", client_id="cid", refresh_token="rt")
            s.add(acc)
            s.commit()
            s.refresh(acc)
            self.assertEqual(acc.role, "pro_warmup")

    def test_valid_roles_constant(self):
        self.assertEqual(VALID_MAIL_ACCOUNT_ROLES, frozenset({"regular", "pro_warmup"}))


class TestSaveMailAccountRole(_IsolatedDBMixin, unittest.TestCase):
    def test_save_with_default_role(self):
        svc = ConfigService()
        acc = svc.save_mail_account(
            label="t", provider_name="applemail",
            email="a@b.c", client_id="cid", refresh_token="rt",
        )
        self.assertEqual(acc.role, "regular")

    def test_save_with_pro_warmup(self):
        svc = ConfigService()
        acc = svc.save_mail_account(
            label="t", provider_name="applemail",
            email="a@b.c", client_id="cid", refresh_token="rt",
            role="pro_warmup",
        )
        self.assertEqual(acc.role, "pro_warmup")

    def test_invalid_role_raises(self):
        svc = ConfigService()
        with self.assertRaises(InvalidMailAccountRoleError):
            svc.save_mail_account(
                label="t", provider_name="applemail",
                email="a@b.c", client_id="cid", refresh_token="rt",
                role="hacker",
            )


class TestDeleteMailAccountDefaultRef(_IsolatedDBMixin, unittest.TestCase):
    def test_delete_blocked_when_referenced_by_default(self):
        svc = ConfigService()
        acc = svc.save_mail_account(
            label="t", provider_name="applemail",
            email="a@b.c", client_id="cid", refresh_token="rt",
        )
        # 通过 update_config 设 default_mail_account_id（走 SAFE_UPDATE_FIELDS 白名单）
        # 直接写 _overrides + _persist_override
        svc.update_config(
            {"default_mail_account_id": acc.id},
            allowed_fields=set(svc.SAFE_UPDATE_FIELDS) | {"default_mail_account_id"},
        )
        with self.assertRaises(MailAccountInDefaultUseError):
            svc.delete_mail_account(acc.id)

    def test_delete_ok_when_not_default(self):
        svc = ConfigService()
        acc = svc.save_mail_account(
            label="t", provider_name="applemail",
            email="a@b.c", client_id="cid", refresh_token="rt",
        )
        ok = svc.delete_mail_account(acc.id)
        self.assertTrue(ok)


class TestAppSettingRevision(_IsolatedDBMixin, unittest.TestCase):
    def test_update_config_writes_revision(self):
        svc = ConfigService()
        # 用 SAFE_UPDATE_FIELDS 内的真字段
        svc.update_config({"max_email_attempts": 5})
        with Session(self.engine) as s:
            revs = s.exec(select(AppSettingRevision).where(AppSettingRevision.key == "max_email_attempts")).all()
            self.assertEqual(len(revs), 1)
            self.assertEqual(revs[0].new_value, 5)

    def test_no_revision_when_value_unchanged(self):
        svc = ConfigService()
        svc.update_config({"max_email_attempts": 5})
        # 第二次同值
        svc.update_config({"max_email_attempts": 5})
        with Session(self.engine) as s:
            revs = s.exec(select(AppSettingRevision).where(AppSettingRevision.key == "max_email_attempts")).all()
            # 仅写 1 次（旧值 None → 5）；第二次旧=新跳过
            self.assertEqual(len(revs), 1)

    def test_list_app_setting_revisions(self):
        svc = ConfigService()
        svc.update_config({"max_email_attempts": 5})
        svc.update_config({"max_email_attempts": 7})
        revs = svc.list_app_setting_revisions(key="max_email_attempts")
        self.assertEqual(len(revs), 2)
        # 按 id desc，最新在前
        self.assertEqual(revs[0].new_value, 7)
        self.assertEqual(revs[1].new_value, 5)

    def test_list_revisions_limit(self):
        svc = ConfigService()
        for i in range(5):
            svc.update_config({"max_email_attempts": i})
        revs = svc.list_app_setting_revisions(limit=3)
        self.assertEqual(len(revs), 3)


# ────────────────────────────────────────────────────
# 卡预热号池调度（select_warmup_account / record_warmup_outcome）
# ────────────────────────────────────────────────────


def _add_warmup_account(
    svc,
    *,
    label: str,
    email: str,
    password: str = "p",
    profile_id: str = "pf",
    is_active: bool = True,
):
    return svc.save_mail_account(
        label=label,
        provider_name="applemail",
        email=email,
        client_id="",
        refresh_token="",
        extra={"password": password, "adspower_profile_id": profile_id},
        is_active=is_active,
        role="pro_warmup",
    )


class TestSelectWarmupAccount(_IsolatedDBMixin, unittest.TestCase):
    def test_empty_pool_returns_none(self):
        svc = ConfigService()
        self.assertIsNone(svc.select_warmup_account())

    def test_skips_regular_role(self):
        """role=regular 的账号不能被选中"""
        svc = ConfigService()
        svc.save_mail_account(
            label="reg", provider_name="applemail", email="r@x.c",
            client_id="c", refresh_token="r",
        )  # role 默认 regular
        self.assertIsNone(svc.select_warmup_account())

    def test_skips_inactive(self):
        svc = ConfigService()
        _add_warmup_account(svc, label="w1", email="w1@x.c", is_active=False)
        self.assertIsNone(svc.select_warmup_account())

    def test_picks_least_recently_used(self):
        """有 2 个账号时，第 1 次挑一个，第 2 次必挑另一个（避免撞号）"""
        svc = ConfigService()
        a1 = _add_warmup_account(svc, label="w1", email="w1@x.c", profile_id="pf1")
        a2 = _add_warmup_account(svc, label="w2", email="w2@x.c", profile_id="pf2")
        first = svc.select_warmup_account()
        second = svc.select_warmup_account()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.id, second.id, "应该挑不同账号")
        self.assertSetEqual({first.id, second.id}, {a1.id, a2.id})

    def test_third_pick_returns_none_when_all_in_cooldown(self):
        svc = ConfigService()
        _add_warmup_account(svc, label="w1", email="w1@x.c")
        _add_warmup_account(svc, label="w2", email="w2@x.c")
        svc.select_warmup_account()
        svc.select_warmup_account()
        # 两个都进了 30 分钟冷却，第 3 次必空
        self.assertIsNone(svc.select_warmup_account())

    def test_select_updates_cooldown_until(self):
        from src.services.config_service import WARMUP_COOLDOWN_MINUTES
        from datetime import datetime, timezone, timedelta
        svc = ConfigService()
        _add_warmup_account(svc, label="w1", email="w1@x.c")
        before = datetime.now(timezone.utc)
        chosen = svc.select_warmup_account()
        self.assertIsNotNone(chosen)
        self.assertIsNotNone(chosen.last_used_at)
        self.assertIsNotNone(chosen.cooldown_until)
        # cooldown_until ≈ now + 30min（容忍 5 秒误差）
        # SQLite 取出来是 naive，比较前补 UTC tz
        cooldown = chosen.cooldown_until
        if cooldown.tzinfo is None:
            cooldown = cooldown.replace(tzinfo=timezone.utc)
        delta = cooldown - before
        expected = timedelta(minutes=WARMUP_COOLDOWN_MINUTES)
        self.assertGreater(delta, expected - timedelta(seconds=5))
        self.assertLess(delta, expected + timedelta(seconds=10))


class TestRecordWarmupOutcome(_IsolatedDBMixin, unittest.TestCase):
    def test_success_resets_failure_counter(self):
        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w1", email="w1@x.c")
        # 先累两次失败
        svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        # 一次成功 → 计数清零
        result = svc.record_warmup_outcome(acc.id, success=True)
        self.assertEqual(result.consecutive_failures, 0)
        self.assertIsNone(result.last_failure_reason)
        self.assertTrue(result.is_active)

    def test_failure_accumulates_and_records_reason(self):
        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w1", email="w1@x.c")
        svc.record_warmup_outcome(acc.id, success=False, reason="adspower_down")
        result = svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        self.assertEqual(result.consecutive_failures, 2)
        self.assertEqual(result.last_failure_reason, "login_failed")
        self.assertTrue(result.is_active, "未到 3 次还不该禁用")

    def test_three_failures_auto_deactivate(self):
        from src.services.config_service import WARMUP_MAX_CONSECUTIVE_FAILURES
        self.assertEqual(WARMUP_MAX_CONSECUTIVE_FAILURES, 3)  # 用户已确认
        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w1", email="w1@x.c")
        for _ in range(3):
            svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        # 重新查 DB 确认禁用
        from src.db.engine import get_session
        from src.db.models import MailAccount
        with Session(self.engine) as s:
            again = s.get(MailAccount, acc.id)
            self.assertFalse(again.is_active, "3 次失败后应自动 is_active=False")
            self.assertEqual(again.consecutive_failures, 3)

    def test_record_outcome_unknown_id_returns_none(self):
        svc = ConfigService()
        result = svc.record_warmup_outcome("does-not-exist", success=True)
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────
# C5（修复后）: 自动禁用清 cooldown + reactivate 自动重置
# ──────────────────────────────────────────────────────────────────────


class TestAutoDeactivateClearsCooldown(_IsolatedDBMixin, unittest.TestCase):
    """修复后：disable 同时清 cooldown_until，reactivate 后立即可用。

    ── 历史 bug（已修） ──
    旧实现 disable 时只设 is_active=False，没清 cooldown_until → 运维 reactivate
    后 select 仍按 cooldown_until > now 跳过；consecutive_failures=3 残留 → 一次
    失败立刻第 4 次累计、再次自动 disable。
    """

    def test_disable_clears_cooldown_until(self):
        """3 次连续失败 → disable + cooldown_until 被清成 None。"""
        from src.db.models import MailAccount

        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-stale", email="stale@x.c")
        svc.select_warmup_account()  # 占位，cooldown_until 填到 now+30min
        with Session(self.engine) as s:
            self.assertIsNotNone(
                s.get(MailAccount, acc.id).cooldown_until,
                "select 后 cooldown_until 必须有值（前置条件）",
            )

        for _ in range(3):
            svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")

        with Session(self.engine) as s:
            after = s.get(MailAccount, acc.id)
            self.assertFalse(after.is_active)
            self.assertEqual(after.consecutive_failures, 3)
            self.assertIsNone(
                after.cooldown_until,
                "修复后：disable 时必须清 cooldown_until=None",
            )

    def test_reactivate_via_save_mail_account_resets_schedule_state(self):
        """通过 save_mail_account 把 is_active=False→True → 自动清 fails 与 cooldown。"""
        from src.db.models import MailAccount

        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-react", email="react@x.c")

        # 1. 走完 select + 3 次失败 disable 流程
        svc.select_warmup_account()
        for _ in range(3):
            svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")

        with Session(self.engine) as s:
            disabled = s.get(MailAccount, acc.id)
            self.assertFalse(disabled.is_active)
            self.assertEqual(disabled.consecutive_failures, 3)

        # 2. 通过 save_mail_account 重新启用（运维 UI 走的就是这条路径）
        svc.save_mail_account(
            account_id=acc.id,
            label="react@x.c",
            provider_name="applemail",
            email="react@x.c",
            client_id="",
            refresh_token="",
            extra={"adspower_profile_id": "p1", "password": "pw"},
            is_active=True,
            role="pro_warmup",
        )

        # 3. 调度状态应该被重置
        with Session(self.engine) as s:
            again = s.get(MailAccount, acc.id)
            self.assertTrue(again.is_active)
            self.assertEqual(again.consecutive_failures, 0, "reactivate 后 fails 应清零")
            self.assertIsNone(again.cooldown_until, "reactivate 后 cooldown 应清空")
            self.assertIsNone(again.last_failure_reason, "reactivate 后失败原因应清空")

        # 4. 立即 select 应当能成功
        chosen = svc.select_warmup_account()
        self.assertIsNotNone(chosen, "reactivate + reset 后 select 应当能选到账号")

    def test_reset_warmup_account_clears_schedule_state(self):
        """新增 reset_warmup_account 直接清调度状态，不动 is_active。"""
        from src.db.models import MailAccount

        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-reset", email="reset@x.c")
        svc.select_warmup_account()
        svc.record_warmup_outcome(acc.id, success=False, reason="something")

        result = svc.reset_warmup_account(acc.id)
        self.assertIsNotNone(result)
        self.assertEqual(result.consecutive_failures, 0)
        self.assertIsNone(result.cooldown_until)
        self.assertIsNone(result.last_failure_reason)
        # is_active 保持原值
        self.assertTrue(result.is_active)

    def test_reset_warmup_account_unknown_returns_none(self):
        svc = ConfigService()
        self.assertIsNone(svc.reset_warmup_account("does-not-exist"))


class TestRecordWarmupOutcomeFailureClass(_IsolatedDBMixin, unittest.TestCase):
    """failure_class 区分外部依赖故障：external_failure 不累计计数。"""

    def test_external_failure_does_not_increment_consecutive_failures(self):
        from src.db.models import MailAccount

        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-ext", email="ext@x.c")

        # 模拟 5 次邮件 5xx 失败
        for _ in range(5):
            svc.record_warmup_outcome(
                acc.id,
                success=False,
                reason="mail_service_5xx",
                failure_class="external_failure",
            )

        with Session(self.engine) as s:
            after = s.get(MailAccount, acc.id)
            self.assertEqual(
                after.consecutive_failures, 0,
                "external_failure 不应累计 consecutive_failures，避免邮件抖动连带 disable",
            )
            self.assertTrue(after.is_active, "external_failure 不应触发 auto-disable")
            self.assertIn("[external]", after.last_failure_reason or "")

    def test_account_failure_default_still_increments(self):
        """default 路径行为不变（向后兼容）。"""
        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-acc", email="acc@x.c")
        svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        result = svc.record_warmup_outcome(acc.id, success=False, reason="login_failed")
        self.assertEqual(result.consecutive_failures, 3)
        self.assertFalse(result.is_active, "3 次 account_failure 仍应触发 disable")

    def test_unknown_failure_class_falls_back_to_account_failure(self):
        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-unk", email="unk@x.c")
        result = svc.record_warmup_outcome(
            acc.id, success=False, reason="x", failure_class="bogus",
        )
        self.assertEqual(result.consecutive_failures, 1, "未知 failure_class 兜底为 account_failure")

    def test_success_after_external_failures_clears_reason(self):
        """external 失败若干次后一次成功，reason 应被清。"""
        svc = ConfigService()
        acc = _add_warmup_account(svc, label="w-mix", email="mix@x.c")
        for _ in range(3):
            svc.record_warmup_outcome(
                acc.id, success=False, reason="mail_5xx",
                failure_class="external_failure",
            )
        result = svc.record_warmup_outcome(acc.id, success=True)
        self.assertEqual(result.consecutive_failures, 0)
        self.assertIsNone(result.last_failure_reason)


if __name__ == "__main__":
    unittest.main()
