# -*- coding: utf-8 -*-
"""促销码扫描 ledger 服务测试（scanned_code_service）。

用 in-memory SQLite 跑真实 DB 逻辑（不 mock DB），覆盖：
- record_scan 落库
- record_scan upsert（同 (country, code) 更新而非新增）
- load_dead_codes 只取 not_found 且新鲜期内
- 跨 country 不串
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

import src.db.engine as engine_mod
from src.db.engine import get_session, init_db
from src.db.models import ScannedCode
from src.services import scanned_code_service as svc


def _reset_engine():
    engine_mod._engine = None


class TestScannedCodeService(unittest.TestCase):

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

    # ── record_scan ──────────────────────────────

    def test_record_scan_persists_lowercase(self):
        svc.record_scan("GB", "DatroaiUK", "not_found")
        with get_session() as s:
            from sqlmodel import select
            row = s.exec(select(ScannedCode)).first()
            self.assertEqual(row.country, "gb")
            self.assertEqual(row.code, "datroaiuk")
            self.assertEqual(row.status, "not_found")

    def test_record_scan_upserts(self):
        svc.record_scan("GB", "datroaiuk", "not_found")
        svc.record_scan("GB", "datroaiuk", "eligible")  # 同码再扫，状态翻转
        with get_session() as s:
            from sqlmodel import select
            rows = s.exec(select(ScannedCode)).all()
            self.assertEqual(len(rows), 1)            # upsert 不新增行
            self.assertEqual(rows[0].status, "eligible")

    def test_record_scan_ignores_empty(self):
        svc.record_scan("", "x", "not_found")
        svc.record_scan("GB", "", "not_found")
        with get_session() as s:
            from sqlmodel import select
            self.assertEqual(len(s.exec(select(ScannedCode)).all()), 0)

    # ── load_dead_codes ──────────────────────────────

    def test_load_dead_codes_only_not_found(self):
        svc.record_scan("GB", "deadone", "not_found")
        svc.record_scan("GB", "liveone", "eligible")
        svc.record_scan("GB", "existsone", "exists")
        dead = svc.load_dead_codes("GB")
        self.assertEqual(dead, frozenset({"deadone"}))

    def test_load_dead_codes_country_scoped(self):
        svc.record_scan("GB", "gbcode", "not_found")
        svc.record_scan("US", "uscode", "not_found")
        self.assertEqual(svc.load_dead_codes("GB"), frozenset({"gbcode"}))
        self.assertEqual(svc.load_dead_codes("US"), frozenset({"uscode"}))

    def test_load_dead_codes_freshness_filter(self):
        # 手动塞一条 40 天前的死码，应被新鲜期（默认 30 天）过滤掉
        old = datetime.now(timezone.utc) - timedelta(days=40)
        with get_session() as s:
            s.add(ScannedCode(country="gb", code="staledead", status="not_found", scanned_at=old))
            s.commit()
        svc.record_scan("GB", "freshdead", "not_found")  # 新鲜的
        dead = svc.load_dead_codes("GB", fresh_within_days=30)
        self.assertIn("freshdead", dead)
        self.assertNotIn("staledead", dead)

    def test_load_dead_codes_empty_country(self):
        self.assertEqual(svc.load_dead_codes(""), frozenset())


if __name__ == "__main__":
    unittest.main()
