# -*- coding: utf-8 -*-
"""DB seed 多 mail ProviderConfig 实例测试。

验证 _seed_runtime_defaults() 按 AppConfig.outlook_enabled / cfworker_enabled
开关条件 seed mail-cfworker-default + mail-outlook-default。

设计立场：
- 使用 in-memory SQLite + 环境变量注入开关
- mail-default 永远 seed（向后兼容）
- 重复 init_db() 必须幂等（不重复 seed 同名记录）
"""
from __future__ import annotations

import os
import unittest

from sqlmodel import Session, select

import src.db.engine as engine_mod
from src.db.engine import init_db
from src.db.models import ProviderConfig


def _reset_engine_and_env(env_overrides: dict[str, str]) -> None:
    """重置 engine 单例 + 注入 env 变量（用于跨测试隔离）。"""
    engine_mod._engine = None
    os.environ["DATABASE_URL"] = "sqlite://"
    # 清理可能影响 seed 的开关
    for k in ("OUTLOOK_ENABLED", "CFWORKER_ENABLED",
              "OUTLOOK_CONFIG_NAME", "CFWORKER_CONFIG_NAME"):
        os.environ.pop(k, None)
    # 注入本测试用 env
    for k, v in env_overrides.items():
        os.environ[k] = v


class TestSeedMailProvidersDisabled(unittest.TestCase):
    """两个开关都关闭 → 只 seed mail-default。"""

    def setUp(self):
        _reset_engine_and_env({"OUTLOOK_ENABLED": "false", "CFWORKER_ENABLED": "false"})
        self.engine = init_db()

    def test_only_mail_default_seeded(self):
        with Session(self.engine) as s:
            mails = s.exec(
                select(ProviderConfig).where(ProviderConfig.provider_type == "mail")
            ).all()
        names = {m.provider_name for m in mails}
        # 仅 mail-default
        self.assertIn("mail-default", names)
        self.assertNotIn("mail-cfworker-default", names)
        self.assertNotIn("mail-outlook-default", names)


class TestSeedMailProvidersBothEnabled(unittest.TestCase):
    """两个开关都启用 → seed 3 条 mail 配置。"""

    def setUp(self):
        _reset_engine_and_env({
            "OUTLOOK_ENABLED": "true",
            "CFWORKER_ENABLED": "true",
            "OUTLOOK_CONFIG_NAME": "outlook-pool-default",
            "CFWORKER_CONFIG_NAME": "mydomain-cfworker",
        })
        self.engine = init_db()

    def test_three_mail_providers_seeded(self):
        with Session(self.engine) as s:
            mails = s.exec(
                select(ProviderConfig).where(ProviderConfig.provider_type == "mail")
            ).all()
        names = {m.provider_name for m in mails}
        self.assertEqual(
            names,
            {"mail-default", "mail-cfworker-default", "mail-outlook-default"},
        )

    def test_cfworker_payload_correct(self):
        with Session(self.engine) as s:
            cf = s.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_type == "mail",
                    ProviderConfig.provider_name == "mail-cfworker-default",
                )
            ).first()
        self.assertIsNotNone(cf)
        self.assertEqual(cf.config["provider_name"], "cfworker")
        self.assertEqual(cf.config["session_mode"], "managed")
        self.assertEqual(cf.config["config_name"], "mydomain-cfworker")
        self.assertIn("zhangxb.xyz", cf.config["supported_domains"])
        self.assertIn("cloudsentryai.com", cf.config["supported_domains"])

    def test_outlook_payload_correct(self):
        with Session(self.engine) as s:
            ol = s.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_type == "mail",
                    ProviderConfig.provider_name == "mail-outlook-default",
                )
            ).first()
        self.assertIsNotNone(ol)
        self.assertEqual(ol.config["provider_name"], "outlook_email_plus")
        self.assertEqual(ol.config["session_mode"], "managed")
        self.assertEqual(ol.config["config_name"], "outlook-pool-default")
        self.assertIn("outlook.com", ol.config["supported_domains"])
        self.assertIn("hotmail.com", ol.config["supported_domains"])


class TestSeedMailProvidersOnlyOutlook(unittest.TestCase):
    """仅启用 outlook → seed mail-default + mail-outlook-default（不 seed cfworker）"""

    def setUp(self):
        _reset_engine_and_env({"OUTLOOK_ENABLED": "true", "CFWORKER_ENABLED": "false"})
        self.engine = init_db()

    def test_only_outlook_added(self):
        with Session(self.engine) as s:
            mails = s.exec(
                select(ProviderConfig).where(ProviderConfig.provider_type == "mail")
            ).all()
        names = {m.provider_name for m in mails}
        self.assertIn("mail-default", names)
        self.assertIn("mail-outlook-default", names)
        self.assertNotIn("mail-cfworker-default", names)


class TestSeedMailProvidersIdempotent(unittest.TestCase):
    """重复 init_db() 必须幂等：不重复 seed、不抛错。"""

    def test_repeated_init_db_idempotent(self):
        _reset_engine_and_env({"OUTLOOK_ENABLED": "true", "CFWORKER_ENABLED": "true"})
        engine = init_db()

        # 第一次 seed 后的 mail 数量
        with Session(engine) as s:
            count_first = len(s.exec(
                select(ProviderConfig).where(ProviderConfig.provider_type == "mail")
            ).all())

        # 第二次 init_db（同一 engine，但内部 seed 会再跑一次）
        from src.db.engine import _seed_runtime_defaults
        _seed_runtime_defaults(engine)

        with Session(engine) as s:
            count_second = len(s.exec(
                select(ProviderConfig).where(ProviderConfig.provider_type == "mail")
            ).all())

        # 数量不能涨 — seed 已是 INSERT IGNORE 语义
        self.assertEqual(count_first, count_second)


class TestSeedMailProvidersDefaults(unittest.TestCase):
    """启用开关但未设 config_name env → 应该用默认值。"""

    def setUp(self):
        _reset_engine_and_env({
            "OUTLOOK_ENABLED": "true",
            "CFWORKER_ENABLED": "true",
            # 不设 OUTLOOK_CONFIG_NAME / CFWORKER_CONFIG_NAME
        })
        self.engine = init_db()

    def test_uses_default_config_names(self):
        with Session(self.engine) as s:
            cf = s.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_name == "mail-cfworker-default"
                )
            ).first()
            ol = s.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_name == "mail-outlook-default"
                )
            ).first()
        self.assertEqual(cf.config["config_name"], "mydomain-cfworker")
        self.assertEqual(ol.config["config_name"], "outlook-pool-default")


if __name__ == "__main__":
    unittest.main()
