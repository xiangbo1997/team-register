# -*- coding: utf-8 -*-
"""init_db() 自动 seed RegistrationProfile 的集成测试。

验证：
  1. 全新库 init_db() 后自动有 email-default / phone-default 两条记录
  2. 第二次 init_db() 不重复 seed（幂等）
  3. seed 失败不阻塞 init_db
"""

import os
import unittest
from unittest import mock

from sqlmodel import SQLModel, select

import src.db.engine as engine_mod
from src.db.engine import get_session, init_db
from src.db.models import RegistrationProfile


def _reset_engine() -> None:
    engine_mod._engine = None


class TestSeedOnInitDB(unittest.TestCase):
    """init_db() 链路里 _seed_registration_profiles 被调用。"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        cls.engine = init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def test_email_and_phone_defaults_seeded(self):
        """init_db() 后默认两条记录已落库。"""
        with get_session() as session:
            stmt = select(RegistrationProfile).where(
                RegistrationProfile.is_default == True  # noqa: E712
            )
            rows = list(session.exec(stmt).all())
        names = sorted(row.name for row in rows)
        self.assertIn("email-default", names)
        self.assertIn("phone-default", names)

    def test_email_default_has_required_slots(self):
        with get_session() as session:
            row = session.exec(
                select(RegistrationProfile).where(
                    RegistrationProfile.name == "email-default"
                )
            ).first()
        self.assertIsNotNone(row)
        self.assertIn("browser", row.provider_bindings)
        self.assertIn("mail", row.provider_bindings)
        self.assertEqual(row.registration_kind, "email")
        self.assertTrue(row.is_default)
        self.assertTrue(row.is_active)

    def test_phone_default_has_sms(self):
        with get_session() as session:
            row = session.exec(
                select(RegistrationProfile).where(
                    RegistrationProfile.name == "phone-default"
                )
            ).first()
        self.assertIsNotNone(row)
        self.assertIn("sms", row.provider_bindings)
        self.assertEqual(row.registration_kind, "phone")


class TestSeedIdempotent(unittest.TestCase):
    """init_db() 第二次调用不重复 seed。"""

    def setUp(self):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"

    def tearDown(self):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def test_double_init_db_idempotent(self):
        # 第一次 init
        init_db()
        with get_session() as session:
            first_count = len(list(session.exec(
                select(RegistrationProfile)
            ).all()))

        # 第二次 init（同一引擎单例 → 同一内存库）
        init_db()
        with get_session() as session:
            second_count = len(list(session.exec(
                select(RegistrationProfile)
            ).all()))

        self.assertEqual(first_count, second_count)
        self.assertGreaterEqual(first_count, 2)


class TestSeedFailureDoesNotBlockInit(unittest.TestCase):
    """seed 内部抛错时，init_db 不应中断。"""

    def setUp(self):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"

    def tearDown(self):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def test_seed_exception_swallowed(self):
        """模拟 RegistrationProfileService.seed_from_appconfig 抛错，init_db 仍成功。"""
        with mock.patch(
            "src.services.registration_profile_service.RegistrationProfileService.seed_from_appconfig",
            side_effect=RuntimeError("simulated seed failure"),
        ):
            # 不应抛
            engine = init_db()
            self.assertIsNotNone(engine)


if __name__ == "__main__":
    unittest.main()
