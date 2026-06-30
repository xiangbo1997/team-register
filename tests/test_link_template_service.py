# -*- coding: utf-8 -*-
"""链接模板服务测试。覆盖 create / list / delete + 验证规则。"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.services.link_template_service import (
    create_template,
    delete_template,
    get_template,
    list_templates,
)


def _build_threadsafe_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class LinkTemplateServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def test_create_and_list_template(self):
        """创建后能在 list 里看到，所有字段被持久化。"""
        created = create_template(
            name="UK promo datroaiuk",
            plan="team",
            seat_quantity=2,
            promo_code="datroaiuk",
            promo_campaign_id="team-1-month-free",
            aimizy_country="GB",
            aimizy_currency="GBP",
            workspace_name="MyTeam UK",
            return_mode="long",
        )
        self.assertGreater(created["id"], 0)
        self.assertEqual(created["name"], "UK promo datroaiuk")
        self.assertEqual(created["plan"], "team")
        self.assertEqual(created["promo_code"], "datroaiuk")
        self.assertEqual(created["aimizy_country"], "GB")

        rows = list_templates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], created["id"])

    def test_template_name_unique(self):
        """同名模板会被拒绝。"""
        create_template(name="dup", plan="plus")
        with self.assertRaises(ValueError) as cm:
            create_template(name="dup", plan="team")
        self.assertIn("已存在", str(cm.exception))

    def test_delete_template(self):
        created = create_template(name="to-delete", plan="plus")
        self.assertTrue(delete_template(created["id"]))
        # 第二次删 → False（已不存在）
        self.assertFalse(delete_template(created["id"]))
        self.assertEqual(list_templates(), [])

    def test_get_template_returns_none_when_missing(self):
        self.assertIsNone(get_template(99999))

    def test_invalid_plan_rejected(self):
        with self.assertRaises(ValueError):
            create_template(name="bad-plan", plan="enterprise")

    def test_invalid_return_mode_rejected(self):
        with self.assertRaises(ValueError):
            create_template(name="bad-mode", plan="team", return_mode="weird")

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            create_template(name="   ", plan="team")

    def test_template_supports_all_4_plans(self):
        """plan 白名单包含 team / plus / pro / pro_lite 四种。"""
        for plan in ("team", "plus", "pro", "pro_lite"):
            t = create_template(name=f"tpl-{plan}", plan=plan)
            self.assertEqual(t["plan"], plan)


if __name__ == "__main__":
    unittest.main()
