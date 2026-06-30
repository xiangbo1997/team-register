# -*- coding: utf-8 -*-
"""RegistrationProfileService 单元测试。

覆盖：
  - CRUD 基本路径
  - registration_kind 白名单 / provider_bindings 格式校验
  - set_default 一对一约束（同 kind 自动清零其他 default）
  - 默认组合不可删
  - seed_from_appconfig 幂等 + force 覆盖
  - validate_bindings_against_provider_configs 引用校验
  - 写操作产生 RegistrationProfileRevision
"""

import os
import unittest
from dataclasses import replace

from sqlmodel import SQLModel, select

import src.db.engine as engine_mod
from src.config import AppConfig, load_config
from src.db.engine import get_session, init_db
from src.db.models import (
    ProviderConfig,
    RegistrationProfile,
    RegistrationProfileRevision,
)
from src.services.registration_profile_service import (
    DefaultProfileNotDeletableError,
    InvalidProviderBindingsError,
    InvalidRegistrationKindError,
    RegistrationProfileError,
    RegistrationProfileNameConflictError,
    RegistrationProfileNotFoundError,
    RegistrationProfileService,
    validate_bindings_against_provider_configs,
)


def _reset_engine() -> None:
    engine_mod._engine = None


def _minimal_config(**overrides) -> AppConfig:
    """AppConfig 字段太多，构造一个最小可用对象。"""
    base = load_config()
    if overrides:
        # AppConfig 是 dataclass；用 replace 安全替换字段
        base = replace(base, **overrides)
    return base


class TestServiceCRUD(unittest.TestCase):
    """create / get / list / update / delete 基本路径。"""

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
        self.svc = RegistrationProfileService()

    def test_create_minimal(self):
        profile = self.svc.create(
            name="email-test",
            registration_kind="email",
            provider_bindings={"browser": "browser-default"},
        )
        self.assertEqual(profile.name, "email-test")
        self.assertEqual(profile.registration_kind, "email")
        self.assertEqual(profile.provider_bindings, {"browser": "browser-default"})
        self.assertFalse(profile.is_default)
        self.assertTrue(profile.is_active)

    def test_create_with_is_default_clears_others(self):
        """新建 is_default=True 时同 kind 其他记录的 is_default 必须清零。"""
        a = self.svc.create(
            name="email-a", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        b = self.svc.create(
            name="email-b", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        # 重新查 a，应该已被清零
        a_after = self.svc.get_profile("email-a")
        self.assertFalse(a_after.is_default)
        self.assertTrue(b.is_default)

    def test_name_conflict(self):
        self.svc.create(
            name="dup", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        with self.assertRaises(RegistrationProfileNameConflictError):
            self.svc.create(
                name="dup", registration_kind="phone",
                provider_bindings={"browser": "b"},
            )

    def test_create_invalid_kind(self):
        with self.assertRaises(InvalidRegistrationKindError):
            self.svc.create(
                name="bad", registration_kind="webauthn",
                provider_bindings={"browser": "b"},
            )

    def test_create_invalid_bindings(self):
        with self.assertRaises(InvalidProviderBindingsError):
            self.svc.create(
                name="bad", registration_kind="email",
                provider_bindings={"browser": ""},  # 空 value
            )
        with self.assertRaises(InvalidProviderBindingsError):
            self.svc.create(
                name="bad", registration_kind="email",
                provider_bindings="not-a-dict",  # type: ignore[arg-type]
            )

    def test_update_partial(self):
        self.svc.create(
            name="p1", registration_kind="email",
            provider_bindings={"browser": "old"},
            description="原描述",
        )
        updated = self.svc.update(
            "p1",
            provider_bindings={"browser": "new", "card": "card-x"},
            description="新描述",
        )
        self.assertEqual(updated.provider_bindings["browser"], "new")
        self.assertEqual(updated.provider_bindings["card"], "card-x")
        self.assertEqual(updated.description, "新描述")
        # kind 未传应保持不变
        self.assertEqual(updated.registration_kind, "email")

    def test_update_not_found(self):
        with self.assertRaises(RegistrationProfileNotFoundError):
            self.svc.update("ghost", description="x")

    def test_list_filters(self):
        self.svc.create(
            name="email-1", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        self.svc.create(
            name="email-2", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        self.svc.create(
            name="phone-1", registration_kind="phone",
            provider_bindings={"browser": "b"}, is_active=False,
        )

        emails = self.svc.list_profiles(registration_kind="email")
        self.assertEqual(len(emails), 2)
        # 默认组合排第一（is_default desc）
        self.assertEqual(emails[0].name, "email-1")

        active = self.svc.list_profiles(active_only=True)
        names = sorted(p.name for p in active)
        self.assertEqual(names, ["email-1", "email-2"])

    def test_delete_non_default(self):
        self.svc.create(
            name="p1", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        self.assertTrue(self.svc.delete("p1"))
        self.assertIsNone(self.svc.get_profile("p1"))
        # 重复删返回 False，不抛
        self.assertFalse(self.svc.delete("p1"))

    def test_delete_default_blocked(self):
        self.svc.create(
            name="default-one", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        with self.assertRaises(DefaultProfileNotDeletableError):
            self.svc.delete("default-one")


class TestSetDefault(unittest.TestCase):
    """set_default 互斥性测试。"""

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
        self.svc = RegistrationProfileService()

    def test_set_default_clears_other_same_kind(self):
        self.svc.create(
            name="email-a", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        self.svc.create(
            name="email-b", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        self.svc.set_default("email-b")
        self.assertFalse(self.svc.get_profile("email-a").is_default)
        self.assertTrue(self.svc.get_profile("email-b").is_default)

    def test_set_default_does_not_affect_other_kind(self):
        """email 设默认不应影响 phone 的默认。"""
        self.svc.create(
            name="email-1", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        self.svc.create(
            name="phone-1", registration_kind="phone",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        self.svc.create(
            name="email-2", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        self.svc.set_default("email-2")
        self.assertFalse(self.svc.get_profile("email-1").is_default)
        self.assertTrue(self.svc.get_profile("email-2").is_default)
        # phone 的默认未受影响
        self.assertTrue(self.svc.get_profile("phone-1").is_default)

    def test_set_default_on_inactive_blocked(self):
        self.svc.create(
            name="p1", registration_kind="email",
            provider_bindings={"browser": "b"}, is_active=False,
        )
        with self.assertRaises(RegistrationProfileError):
            self.svc.set_default("p1")

    def test_set_default_not_found(self):
        with self.assertRaises(RegistrationProfileNotFoundError):
            self.svc.set_default("ghost")

    def test_get_default_returns_active_default_only(self):
        self.svc.create(
            name="active", registration_kind="email",
            provider_bindings={"browser": "b"}, is_default=True,
        )
        result = self.svc.get_default("email")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "active")
        # 改为 inactive 后 get_default 应返回 None
        self.svc.update("active", is_active=False)
        self.assertIsNone(self.svc.get_default("email"))


class TestSeed(unittest.TestCase):
    """seed_from_appconfig 幂等 + force 覆盖。"""

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
        self.config = _minimal_config()
        self.svc = RegistrationProfileService(base_config=self.config)

    def test_seed_creates_two_defaults(self):
        result = self.svc.seed_from_appconfig()
        self.assertEqual(sorted(result["seeded"]), ["email-default", "phone-default"])
        self.assertEqual(result["skipped_existing"], [])
        self.assertEqual(result["updated"], [])

        email = self.svc.get_profile("email-default")
        self.assertIsNotNone(email)
        self.assertEqual(email.registration_kind, "email")
        self.assertTrue(email.is_default)
        self.assertIn("browser", email.provider_bindings)
        self.assertIn("mail", email.provider_bindings)

        phone = self.svc.get_profile("phone-default")
        self.assertIsNotNone(phone)
        self.assertEqual(phone.registration_kind, "phone")
        self.assertIn("sms", phone.provider_bindings)

    def test_seed_idempotent(self):
        self.svc.seed_from_appconfig()
        result = self.svc.seed_from_appconfig()
        self.assertEqual(result["seeded"], [])
        self.assertEqual(
            sorted(result["skipped_existing"]),
            ["email-default", "phone-default"],
        )
        self.assertEqual(result["updated"], [])

    def test_seed_force_overwrites(self):
        self.svc.seed_from_appconfig()
        # 手动改动 binding，模拟运维改过的状态
        self.svc.update(
            "email-default",
            provider_bindings={"browser": "custom-browser"},
        )
        result = self.svc.seed_from_appconfig(force=True)
        self.assertEqual(result["seeded"], [])
        self.assertEqual(
            sorted(result["updated"]),
            ["email-default", "phone-default"],
        )
        # bindings 已被覆盖回 seed 默认值
        email = self.svc.get_profile("email-default")
        self.assertNotEqual(email.provider_bindings.get("browser"), "custom-browser")

    def test_seed_respects_cfworker_enabled(self):
        cfg = _minimal_config(cfworker_enabled=True)
        svc = RegistrationProfileService(base_config=cfg)
        svc.seed_from_appconfig()
        email = svc.get_profile("email-default")
        self.assertEqual(email.provider_bindings["mail"], "mail-cfworker-default")

    def test_seed_revisions_written(self):
        """seed 创建记录时也应该写 revision（exists=False）。"""
        self.svc.seed_from_appconfig()
        revs = self.svc.list_revisions("email-default")
        self.assertGreaterEqual(len(revs), 1)
        self.assertFalse(revs[0].snapshot.get("exists"))
        self.assertEqual(revs[0].created_by, "seed")


class TestRevisionsWrittenOnWrites(unittest.TestCase):
    """所有写操作都应产生 revision，把 action_log_id / actor 写入。"""

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
        self.svc = RegistrationProfileService()

    def _count_revisions(self, name: str) -> int:
        with get_session() as session:
            stmt = select(RegistrationProfileRevision).where(
                RegistrationProfileRevision.profile_name == name
            )
            return len(list(session.exec(stmt).all()))

    def test_create_writes_revision(self):
        self.svc.create(
            name="rev-1", registration_kind="email",
            provider_bindings={"browser": "b"},
            action_log_id="act-1", actor="alice",
        )
        revs = self.svc.list_revisions("rev-1")
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0].action_log_id, "act-1")
        self.assertEqual(revs[0].created_by, "alice")
        self.assertFalse(revs[0].snapshot.get("exists"))

    def test_update_writes_revision_with_old_snapshot(self):
        self.svc.create(
            name="rev-2", registration_kind="email",
            provider_bindings={"browser": "old"},
        )
        self.svc.update(
            "rev-2",
            provider_bindings={"browser": "new"},
            action_log_id="act-2", actor="bob",
        )
        revs = self.svc.list_revisions("rev-2")
        self.assertEqual(len(revs), 2)  # create + update
        # 最新一条（update）的 snapshot 是更新前的旧值
        update_rev = revs[0]  # 按 created_at desc 排序
        self.assertTrue(update_rev.snapshot["exists"])
        self.assertEqual(
            update_rev.snapshot["provider_bindings"]["browser"], "old",
        )
        self.assertEqual(update_rev.action_log_id, "act-2")

    def test_set_default_writes_revision(self):
        self.svc.create(
            name="rev-3", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        before = self._count_revisions("rev-3")
        self.svc.set_default("rev-3", action_log_id="act-3", actor="carol")
        self.assertEqual(self._count_revisions("rev-3"), before + 1)

    def test_delete_writes_revision(self):
        self.svc.create(
            name="rev-4", registration_kind="email",
            provider_bindings={"browser": "b"},
        )
        before = self._count_revisions("rev-4")
        self.svc.delete("rev-4", action_log_id="act-4", actor="dave")
        # 删除后记录已不存在，但 revision 应该保留
        self.assertEqual(self._count_revisions("rev-4"), before + 1)


class TestBindingsValidation(unittest.TestCase):
    """validate_bindings_against_provider_configs 引用校验。"""

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
        with get_session() as session:
            session.add_all([
                ProviderConfig(
                    provider_type="browser", provider_name="browser-default",
                    config={}, is_active=True,
                ),
                ProviderConfig(
                    provider_type="mail", provider_name="mail-cfworker-default",
                    config={}, is_active=True,
                ),
                ProviderConfig(
                    provider_type="card", provider_name="card-disabled",
                    config={}, is_active=False,
                ),
            ])
            session.commit()

    def test_all_valid(self):
        errors = validate_bindings_against_provider_configs({
            "browser": "browser-default",
            "mail": "mail-cfworker-default",
        })
        self.assertEqual(errors, [])

    def test_missing_provider(self):
        errors = validate_bindings_against_provider_configs({
            "browser": "ghost-provider",
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("不存在", errors[0])

    def test_inactive_provider(self):
        errors = validate_bindings_against_provider_configs({
            "card": "card-disabled",
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("已停用", errors[0])

    def test_empty_value(self):
        errors = validate_bindings_against_provider_configs({
            "browser": "",
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("未指定", errors[0])

    def test_required_slots(self):
        errors = validate_bindings_against_provider_configs(
            {"browser": "browser-default"},
            required_slots={"browser", "mail"},
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("必填槽位缺失: mail", errors[0])


if __name__ == "__main__":
    unittest.main()
