# -*- coding: utf-8 -*-
"""RegistrationProfile / RegistrationProfileRevision 模型 CRUD 测试。

仅校验 schema 与 ORM 行为；业务约束（一对一 default、默认组合不可删等）
由 RegistrationProfileService 实现并在 test_registration_profile_service.py 校验。
"""

import os
import unittest

from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

import src.db.engine as engine_mod
from src.db.engine import get_session, init_db
from src.db.models import RegistrationProfile, RegistrationProfileRevision


def _reset_engine() -> None:
    engine_mod._engine = None


class TestRegistrationProfileModel(unittest.TestCase):
    """RegistrationProfile schema + ORM 行为。"""

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

    def test_create_with_full_fields(self):
        with get_session() as session:
            profile = RegistrationProfile(
                name="email-default",
                registration_kind="email",
                provider_bindings={
                    "browser": "browser-default",
                    "card": "card-default",
                    "mail": "mail-cfworker-default",
                },
                description="邮箱注册默认组合",
                is_default=True,
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)

            self.assertIsNotNone(profile.id)
            self.assertEqual(profile.registration_kind, "email")
            self.assertEqual(
                profile.provider_bindings["mail"],
                "mail-cfworker-default",
            )
            self.assertTrue(profile.is_default)
            self.assertTrue(profile.is_active)
            self.assertIsNotNone(profile.created_at)
            self.assertIsNotNone(profile.updated_at)

    def test_provider_bindings_default_empty_dict(self):
        """未传 provider_bindings 时应落空 dict 而不是 None（server_default='{}'）。"""
        with get_session() as session:
            profile = RegistrationProfile(
                name="minimal",
                registration_kind="email",
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            self.assertEqual(profile.provider_bindings, {})

    def test_name_unique_constraint(self):
        """name 列加了 unique=True，重名应该抛 IntegrityError。"""
        with get_session() as session:
            session.add(RegistrationProfile(name="dup", registration_kind="email"))
            session.commit()

        with get_session() as session:
            session.add(RegistrationProfile(name="dup", registration_kind="phone"))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_query_by_registration_kind(self):
        with get_session() as session:
            session.add_all([
                RegistrationProfile(
                    name="email-a", registration_kind="email", is_default=True,
                ),
                RegistrationProfile(
                    name="email-b", registration_kind="email", is_default=False,
                ),
                RegistrationProfile(
                    name="phone-a", registration_kind="phone", is_default=True,
                ),
            ])
            session.commit()

        with get_session() as session:
            stmt = select(RegistrationProfile).where(
                RegistrationProfile.registration_kind == "email"
            )
            rows = list(session.exec(stmt).all())
            self.assertEqual(len(rows), 2)
            names = sorted(row.name for row in rows)
            self.assertEqual(names, ["email-a", "email-b"])

    def test_provider_bindings_json_roundtrip(self):
        """JSON 列存任意嵌套结构应能原样取出。"""
        bindings = {
            "browser": "browser-default",
            "card": "card-efuncard",
            "mail": "mail-outlook-default",
            "sms": "sms-activate",
            "captcha": "captcha-noop",
            "llm": "llm-openai",
        }
        with get_session() as session:
            session.add(RegistrationProfile(
                name="rich", registration_kind="phone",
                provider_bindings=bindings,
            ))
            session.commit()

        with get_session() as session:
            row = session.exec(
                select(RegistrationProfile).where(RegistrationProfile.name == "rich")
            ).first()
            self.assertEqual(row.provider_bindings, bindings)


class TestRegistrationProfileRevisionModel(unittest.TestCase):
    """RegistrationProfileRevision schema + 关联字段。"""

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

    def test_create_revision_with_snapshot(self):
        with get_session() as session:
            rev = RegistrationProfileRevision(
                profile_name="email-default",
                snapshot={
                    "exists": True,
                    "registration_kind": "email",
                    "provider_bindings": {"card": "card-efuncard"},
                    "description": "old desc",
                    "is_default": True,
                    "is_active": True,
                },
                action_log_id="act-abc-123",
                created_by="user-42",
            )
            session.add(rev)
            session.commit()
            session.refresh(rev)

            self.assertIsNotNone(rev.id)
            self.assertEqual(rev.profile_name, "email-default")
            self.assertEqual(rev.snapshot["provider_bindings"]["card"], "card-efuncard")
            self.assertEqual(rev.action_log_id, "act-abc-123")

    def test_revision_optional_audit_fields(self):
        """action_log_id / created_by 都是 Optional[str]，可缺省。"""
        with get_session() as session:
            rev = RegistrationProfileRevision(
                profile_name="phone-default",
                snapshot={"exists": False},
            )
            session.add(rev)
            session.commit()
            session.refresh(rev)
            self.assertIsNone(rev.action_log_id)
            self.assertIsNone(rev.created_by)
            self.assertEqual(rev.snapshot, {"exists": False})


if __name__ == "__main__":
    unittest.main()
