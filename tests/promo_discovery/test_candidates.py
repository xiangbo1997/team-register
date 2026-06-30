# -*- coding: utf-8 -*-
"""候选词字典与组合算法单测"""

from __future__ import annotations

import unittest
from pathlib import Path

from src.data.promo_seeds import (
    COUNTRY_SUFFIXES,
    KNOWN_BASES,
    build_candidates,
    build_cross_matrix,
    list_supported_countries,
    normalize,
)


class TestNormalize(unittest.TestCase):
    def test_single_word_lowercase(self):
        self.assertEqual(normalize("Made"), ["made"])

    def test_multi_word_generates_initials(self):
        variants = normalize("Made Tech")
        self.assertIn("madetech", variants)
        self.assertIn("made", variants)
        self.assertIn("mt", variants)

    def test_strips_the_prefix(self):
        variants = normalize("The Alloy Network")
        self.assertIn("alloynetwork", variants)  # base 去 "the"
        self.assertIn("thealloynetwork", variants)  # 原始 base 也保留

    def test_strips_company_tail(self):
        variants = normalize("Cera Care Limited")
        # "ceracarelimited" 应该去掉 "limited" 后缀生成 "ceracare"
        self.assertIn("ceracare", variants)
        self.assertIn("ceracarelimited", variants)

    def test_handles_special_chars(self):
        variants = normalize("Kin + Carta")
        self.assertIn("kincarta", variants)
        # 多词所以会生成首字母 kc
        self.assertIn("kc", variants)

    def test_empty_input(self):
        self.assertEqual(normalize(""), [])
        self.assertEqual(normalize("   "), [])

    def test_digits_preserved(self):
        variants = normalize("Trading 212")
        self.assertIn("trading212", variants)


class TestBuildCandidates(unittest.TestCase):
    def test_unknown_country_returns_empty(self):
        self.assertEqual(build_candidates("XX"), [])

    def test_gb_returns_many_candidates(self):
        codes = build_candidates("GB")
        # 应当包含 KNOWN_BASES × 多个后缀 + 公司名变体
        self.assertGreater(len(codes), 100, "GB 含字典应该有大量候选")

    def test_known_bases_included_by_default(self):
        codes = build_candidates("GB")
        for base in KNOWN_BASES:
            with self.subTest(base=base):
                self.assertIn(base, codes, f"无后缀基础码 {base!r} 应在候选中")

    def test_country_suffixes_applied(self):
        codes = build_candidates("GB")
        # GB 的后缀至少包含 uk / gb
        self.assertIn("talentgeniusuk", codes)
        self.assertIn("codestoneuk", codes)

    def test_short_candidates_filtered_out(self):
        codes = build_candidates("GB")
        for c in codes:
            self.assertGreaterEqual(len(c), 3, f"候选 {c!r} 短于 3 字符")

    def test_extra_words_normalized(self):
        codes = build_candidates(
            "GB",
            extra_words=["MyCustomBrand"],
            include_known_bases=False,
        )
        # MyCustomBrand 不带空格只生成 ['mycustombrand']
        self.assertIn("mycustombrand", codes)
        self.assertIn("mycustombranduk", codes)

    def test_include_known_bases_false_excludes_seeds(self):
        codes_with = build_candidates("GB", include_known_bases=True)
        codes_without = build_candidates("GB", include_known_bases=False)
        # 不带种子词时 codestoneuk（来自 KNOWN_BASES）不应出现
        self.assertIn("codestoneuk", codes_with)
        # 不能强断言 codestoneuk 不在 codes_without（因为字典里可能也提到 codestone）
        # 但 codes_with 应至少多出 KNOWN_BASES 数量级
        self.assertGreater(len(codes_with), len(codes_without))

    def test_custom_seeds_dir(self):
        """用空目录覆盖 → 只剩 KNOWN_BASES × 后缀"""
        empty_dir = Path("/tmp/__nonexistent_promo_seeds__")
        codes = build_candidates("GB", seeds_dir=empty_dir)
        # KNOWN_BASES (14) × 后缀 (uk/gb/couk = 3) + 无后缀 = 14*4 = 56
        self.assertGreater(len(codes), 30)
        self.assertLess(len(codes), 100)


class TestBuildCrossMatrix(unittest.TestCase):
    def test_two_countries_grouped(self):
        pairs = build_cross_matrix(["GB", "US"])
        countries = {p[0] for p in pairs}
        self.assertEqual(countries, {"GB", "US"})

    def test_unknown_country_skipped(self):
        pairs = build_cross_matrix(["GB", "XX"])
        countries = {p[0] for p in pairs}
        self.assertEqual(countries, {"GB"})

    def test_empty_input(self):
        self.assertEqual(build_cross_matrix([]), [])


class TestListSupportedCountries(unittest.TestCase):
    def test_returns_gb(self):
        """gb_companies.json 是已经搬过来的字典文件"""
        countries = list_supported_countries()
        self.assertIn("GB", countries)


if __name__ == "__main__":
    unittest.main()
