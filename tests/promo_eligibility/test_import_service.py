# -*- coding: utf-8 -*-
"""promo_import_service 测试

覆盖：
  - 源文件不存在 → FileNotFoundError
  - dry_run 模式：只返回计划不写库
  - 真实导入：写库 + 幂等（重跑跳过已存在）
  - import_note 落到 last_eligibility_metadata
  - 同名模板已存在 → 跳过不报错
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import LinkTemplate


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


_SAMPLE_PAYLOAD = {
    "last_updated": "2026-05-12",
    "valid": {
        "US": [
            {"code": "talentgeniusus", "price_usd": 25, "duration_months": 48,
             "company": "TalentGenius", "discount_pct": 50},
            {"code": "monicaius", "price_usd": 20, "duration_months": 48,
             "company": "Monica AI", "discount_pct": 60},
        ],
        "GB": [
            {"code": "aibuildgroupgb", "price_local": "£18/月", "price_usd": 25,
             "company": "AI Build Group", "discount_pct": 50},
        ],
    },
    "expired": [
        {"code": "firstfocus", "region": "AU", "company": "First Focus",
         "note": "AU 无后缀码，已失效"},
    ],
}


class PromoImportServiceTests(unittest.TestCase):

    def setUp(self):
        self._old_engine = engine_mod._engine
        engine_mod._engine = _make_engine()
        # 写入临时 known_codes.json
        self._tmp_dir = tempfile.mkdtemp()
        self._src = Path(self._tmp_dir) / "known_codes.json"
        self._src.write_text(json.dumps(_SAMPLE_PAYLOAD), encoding="utf-8")

    def tearDown(self):
        engine_mod._engine = self._old_engine
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_missing_source_raises(self):
        from src.services.promo_import_service import import_from_known_codes

        with self.assertRaises(FileNotFoundError):
            import_from_known_codes(source_path=Path("/nonexistent/path.json"))

    def test_dry_run_does_not_write(self):
        from src.services.promo_import_service import import_from_known_codes

        result = import_from_known_codes(source_path=self._src, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["planned"], 4)  # 3 valid + 1 expired
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["details"]), 4)

        # DB 没写入
        with get_session() as session:
            count = len(list(session.exec(LinkTemplate.__table__.select()).all()))
        self.assertEqual(count, 0)

    def test_apply_writes_all_entries(self):
        from src.services.promo_import_service import import_from_known_codes

        result = import_from_known_codes(source_path=self._src, dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["created"], 4)
        self.assertEqual(len(result["failed"]), 0)

        # DB 内有 4 条且命名规范
        with get_session() as session:
            from sqlmodel import select
            rows = list(session.exec(select(LinkTemplate)).all())
        names = sorted(r.name for r in rows)
        self.assertEqual(names, sorted([
            "promo-us-talentgeniusus",
            "promo-us-monicaius",
            "promo-gb-aibuildgroupgb",
            "promo-au-firstfocus",
        ]))
        # 默认值正确
        for r in rows:
            self.assertEqual(r.plan, "team")
            self.assertEqual(r.seat_quantity, 2)
            self.assertEqual(r.return_mode, "long")

    def test_idempotent_second_run_skips(self):
        from src.services.promo_import_service import import_from_known_codes

        first = import_from_known_codes(source_path=self._src, dry_run=False)
        self.assertEqual(first["created"], 4)

        # 第二次跑：应该跳过全部
        second = import_from_known_codes(source_path=self._src, dry_run=False)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["planned"], 0)
        # DB 仍是 4 条，没重复
        with get_session() as session:
            from sqlmodel import select
            rows = list(session.exec(select(LinkTemplate)).all())
        self.assertEqual(len(rows), 4)

    def test_dedup_by_business_key_skips_alias_named_template(self):
        """已存在 name 不同但 (country, code) 相同的"别名"模板 → 也应视为重复跳过。

        历史背景：早期手工建的模板名是 'US promo talentgeniusus'（自由命名），
        新规范是 'promo-us-talentgeniusus'。导入 known_codes.json 时若只按 name
        去重，会为同一码再建一条 → 产生业务重复。
        """
        from src.services.link_template_service import create_template
        from src.services.promo_import_service import import_from_known_codes

        # 预置一个别名命名的同 (country, code) 模板
        create_template(
            name="US promo talentgeniusus alias",  # 不符合规范命名
            plan="team",
            seat_quantity=2,
            promo_code="talentgeniusus",
            aimizy_country="US",
        )

        result = import_from_known_codes(source_path=self._src, dry_run=False)

        # 应该只创建 3 条（4 - 1 已存在别名）
        self.assertEqual(result["created"], 3)
        # 不能有 talentgeniusus 重复
        with get_session() as session:
            from sqlmodel import select
            rows = list(session.exec(
                select(LinkTemplate).where(LinkTemplate.promo_code == "talentgeniusus")
            ).all())
        self.assertEqual(len(rows), 1, "talentgeniusus 应该只剩 alias 那一条")
        self.assertEqual(rows[0].name, "US promo talentgeniusus alias")

    def test_import_note_persisted_to_metadata(self):
        from src.services.promo_import_service import import_from_known_codes

        import_from_known_codes(source_path=self._src, dry_run=False)

        with get_session() as session:
            from sqlmodel import select
            us_tpl = session.exec(
                select(LinkTemplate).where(LinkTemplate.name == "promo-us-talentgeniusus")
            ).first()
        self.assertIsNotNone(us_tpl)
        self.assertIsNotNone(us_tpl.last_eligibility_metadata)
        note = us_tpl.last_eligibility_metadata.get("import_note", "")
        self.assertIn("source=valid", note)
        self.assertIn("company=TalentGenius", note)
        self.assertIn("price_usd=25", note)
        self.assertIn("discount=50%", note)

    def test_expired_entry_has_note(self):
        from src.services.promo_import_service import import_from_known_codes

        import_from_known_codes(source_path=self._src, dry_run=False)
        with get_session() as session:
            from sqlmodel import select
            au_tpl = session.exec(
                select(LinkTemplate).where(LinkTemplate.name == "promo-au-firstfocus")
            ).first()
        self.assertIsNotNone(au_tpl)
        note = au_tpl.last_eligibility_metadata.get("import_note", "")
        self.assertIn("source=expired", note)
        self.assertIn("AU 无后缀码", note)

    def test_currency_fallback_filled(self):
        from src.services.promo_import_service import import_from_known_codes

        import_from_known_codes(source_path=self._src, dry_run=False)
        with get_session() as session:
            from sqlmodel import select
            us_tpl = session.exec(
                select(LinkTemplate).where(LinkTemplate.name == "promo-us-talentgeniusus")
            ).first()
            gb_tpl = session.exec(
                select(LinkTemplate).where(LinkTemplate.name == "promo-gb-aibuildgroupgb")
            ).first()
        self.assertEqual(us_tpl.aimizy_currency, "USD")
        self.assertEqual(gb_tpl.aimizy_currency, "GBP")


if __name__ == "__main__":
    unittest.main()
