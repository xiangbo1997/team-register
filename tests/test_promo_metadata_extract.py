# -*- coding: utf-8 -*-
"""promo_metadata_extract 单元测试（P4 多路径容错抽取器）。

覆盖：
  - 各字段多路径命中（discount.percent_off / value / 嵌套 promotion / promo_code_metadata）
  - 类型归一（float→int / Unix 时间戳 / ISO 字符串 / plan CSV）
  - import_note 文本兜底
  - 边界：None / 空 dict / metadata=null / 缺字段
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.services.promo_metadata_extract import extract_promo_fields


class TestExtractPercentOff(unittest.TestCase):
    def test_stripe_style_percent_off(self):
        m = {"metadata": {"discount": {"percent_off": 50}}}
        self.assertEqual(extract_promo_fields(m)["percent_off"], 50)

    def test_float_truncated(self):
        m = {"metadata": {"discount": {"percent_off": 100.0}}}
        self.assertEqual(extract_promo_fields(m)["percent_off"], 100)

    def test_seed_value_fallback(self):
        # 项目 seed / 测试占位结构 metadata.discount.value
        m = {"metadata": {"discount": {"value": 25}}}
        self.assertEqual(extract_promo_fields(m)["percent_off"], 25)

    def test_nested_promotion(self):
        m = {"metadata": {"promotion": {"discount": {"percent_off": 30}}}}
        self.assertEqual(extract_promo_fields(m)["percent_off"], 30)

    def test_promo_code_metadata_variant(self):
        m = {"promo_code_metadata": {"discount": {"percent_off": 40}}}
        self.assertEqual(extract_promo_fields(m)["percent_off"], 40)


class TestExtractDuration(unittest.TestCase):
    def test_duration_in_months(self):
        m = {"metadata": {"discount": {"duration_in_months": 12}}}
        self.assertEqual(extract_promo_fields(m)["duration_months"], 12)

    def test_duration_months_alias(self):
        m = {"metadata": {"discount": {"duration_months": 6}}}
        self.assertEqual(extract_promo_fields(m)["duration_months"], 6)


class TestExtractExpires(unittest.TestCase):
    def test_unix_timestamp(self):
        # 2025-06-29 ~ 1751151600
        m = {"metadata": {"discount": {"expires_at": 1751151600}}}
        dt = extract_promo_fields(m)["expires_at"]
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.year, 2025)

    def test_iso_string(self):
        m = {"metadata": {"discount": {"expires_at": "2026-12-31T00:00:00Z"}}}
        dt = extract_promo_fields(m)["expires_at"]
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 12)

    def test_numeric_string_timestamp(self):
        m = {"metadata": {"discount": {"expires_at": "1751151600"}}}
        dt = extract_promo_fields(m)["expires_at"]
        self.assertEqual(dt.year, 2025)

    def test_invalid_expires(self):
        m = {"metadata": {"discount": {"expires_at": "not-a-date"}}}
        self.assertIsNone(extract_promo_fields(m)["expires_at"])


class TestExtractMaxRedemptions(unittest.TestCase):
    def test_max_redemptions(self):
        m = {"metadata": {"discount": {"max_redemptions": 5000}}}
        self.assertEqual(extract_promo_fields(m)["max_redemptions"], 5000)


class TestExtractPlans(unittest.TestCase):
    def test_chatgpt_team_string(self):
        m = {"metadata": {"discount": {"plan": "chatgpt-team"}}}
        self.assertEqual(extract_promo_fields(m)["applicable_plans"], "team")

    def test_plan_type_nested(self):
        m = {"metadata": {"promotion": {"plan_type": "chatgpt-team-annual"}}}
        self.assertEqual(extract_promo_fields(m)["applicable_plans"], "team")

    def test_list_of_products(self):
        m = {"metadata": {"discount": {"applicable_products": ["chatgpt-team", "chatgpt-plus"]}}}
        # _dig 对 list 取第 0 个 → 只认 team；这是可接受的近似
        self.assertIn("team", extract_promo_fields(m)["applicable_plans"])

    def test_unknown_plan_cleaned(self):
        m = {"metadata": {"discount": {"plan": "chatgpt-enterprise"}}}
        self.assertEqual(extract_promo_fields(m)["applicable_plans"], "enterprise")


class TestImportNoteFallback(unittest.TestCase):
    def test_extract_from_note(self):
        m = {"import_note": "source=valid | company=X | discount=50% | months=12"}
        r = extract_promo_fields(m)
        self.assertEqual(r["percent_off"], 50)
        self.assertEqual(r["duration_months"], 12)

    def test_api_metadata_wins_over_note(self):
        # API metadata 命中时不被 note 覆盖
        m = {
            "metadata": {"discount": {"percent_off": 80}},
            "import_note": "discount=50% | months=12",
        }
        r = extract_promo_fields(m)
        self.assertEqual(r["percent_off"], 80)   # API 赢
        self.assertEqual(r["duration_months"], 12)  # note 兜底（API 没这字段）

    def test_note_without_discount(self):
        m = {"import_note": "source=expired | note=过期了"}
        r = extract_promo_fields(m)
        self.assertIsNone(r["percent_off"])


class TestEdgeCases(unittest.TestCase):
    def test_none(self):
        r = extract_promo_fields(None)
        self.assertIsNone(r["percent_off"])
        self.assertEqual(r["applicable_plans"], "")

    def test_empty_dict(self):
        r = extract_promo_fields({})
        self.assertIsNone(r["percent_off"])

    def test_metadata_null(self):
        # 失效码真实响应：metadata=null
        m = {"metadata": None, "is_eligible": False}
        r = extract_promo_fields(m)
        self.assertIsNone(r["percent_off"])

    def test_real_ineligible_shape(self):
        # P0 拿到的真实失效响应结构
        m = {
            "metadata": None,
            "is_eligible": False,
            "ineligible_reason": {"title": "Promo Unavailable", "code": "invalid_code"},
        }
        r = extract_promo_fields(m)
        self.assertIsNone(r["percent_off"])
        self.assertIsNone(r["expires_at"])


if __name__ == "__main__":
    unittest.main()
