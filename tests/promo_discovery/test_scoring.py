# -*- coding: utf-8 -*-
"""促销码启发式打分与优先级排序单测（纯函数，零 mock）"""

from __future__ import annotations

import unittest

from src.data.promo_scoring import (
    ScanHistory,
    ScoreWeights,
    prioritize_candidates,
    score_candidate,
    split_suffix,
)
from src.data.promo_seeds import KNOWN_BASES


class TestSplitSuffix(unittest.TestCase):
    def test_strips_single_suffix(self):
        self.assertEqual(split_suffix("datroaiuk", "GB"), ("datroai", "uk"))

    def test_longest_suffix_wins(self):
        # GB 后缀含 uk/gb/couk；couk 比 uk 长，必须最长匹配优先
        self.assertEqual(split_suffix("datroaicouk", "GB"), ("datroai", "couk"))

    def test_bare_word_no_suffix(self):
        self.assertEqual(split_suffix("datroai", "GB"), ("datroai", ""))

    def test_unknown_country_returns_bare(self):
        self.assertEqual(split_suffix("foobar", "ZZ"), ("foobar", ""))

    def test_suffix_only_not_stripped(self):
        # 码就等于后缀本身时不拆（len 必须 > 后缀）
        self.assertEqual(split_suffix("uk", "GB"), ("uk", ""))

    def test_case_insensitive(self):
        self.assertEqual(split_suffix("DatroaiUK", "GB"), ("datroai", "uk"))


class TestScoreCandidate(unittest.TestCase):
    def setUp(self):
        self.bases = frozenset(KNOWN_BASES)

    def test_known_base_outranks_noise(self):
        # codestone ∈ KNOWN_BASES，应远高于随机噪声
        hi = score_candidate(
            "codestone", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=ScanHistory(),
        )
        lo = score_candidate(
            "zzznoise", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=ScanHistory(),
        )
        self.assertGreater(hi, lo)

    def test_history_hit_boosts(self):
        hist = ScanHistory(hit_codes=frozenset({"datroaiuk"}))
        boosted = score_candidate(
            "datroaiuk", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=hist,
        )
        plain = score_candidate(
            "datroaiuk", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=ScanHistory(),
        )
        self.assertGreater(boosted, plain)

    def test_base_match_feedback_loop(self):
        # 命中码 datroaiuk → base=datroai；兄弟码 datroaigb 应因 base_match 加权
        hist = ScanHistory(hit_bases=frozenset({"datroai"}))
        sibling = score_candidate(
            "datroaigb", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=hist,
        )
        unrelated = score_candidate(
            "datroaigb", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=ScanHistory(),
        )
        self.assertGreater(sibling, unrelated)

    def test_exact_company_boost(self):
        mains = frozenset({"madetech"})
        s = score_candidate(
            "madetechuk", "GB",
            known_bases=self.bases, company_mains=mains, history=ScanHistory(),
        )
        s_no = score_candidate(
            "madetechuk", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=ScanHistory(),
        )
        self.assertGreater(s, s_no)

    def test_dead_code_sinks_below_known_base(self):
        # 死码即使是 KNOWN_BASES，dead_penalty(-1000) 也压过 known_base(100)
        hist = ScanHistory(dead_codes=frozenset({"codestone"}))
        dead = score_candidate(
            "codestone", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=hist,
        )
        self.assertLess(dead, 0)

    def test_initials_penalty_on_short_noise(self):
        # 两字母缩写噪声应被降权（负分）
        s = score_candidate(
            "mt", "GB",
            known_bases=self.bases, company_mains=frozenset(), history=ScanHistory(),
        )
        self.assertLess(s, 0)


class TestPrioritizeCandidates(unittest.TestCase):
    def test_known_bases_front_loaded(self):
        codes = ["zzznoise", "codestone", "aaanoise", "datroai"]
        out = prioritize_candidates(codes, "GB")
        # codestone / datroai ∈ KNOWN_BASES，应排在噪声前面
        self.assertLess(out.index("codestone"), out.index("zzznoise"))
        self.assertLess(out.index("datroai"), out.index("aaanoise"))

    def test_drop_dead_false_keeps_length(self):
        codes = ["codestone", "deadcode", "datroai"]
        hist = ScanHistory(dead_codes=frozenset({"deadcode"}))
        out = prioritize_candidates(codes, "GB", history=hist, drop_dead=False)
        self.assertEqual(len(out), 3)
        # 死码沉到最后
        self.assertEqual(out[-1], "deadcode")

    def test_drop_dead_true_removes(self):
        codes = ["codestone", "deadcode", "datroai"]
        hist = ScanHistory(dead_codes=frozenset({"deadcode"}))
        out = prioritize_candidates(codes, "GB", history=hist, drop_dead=True)
        self.assertNotIn("deadcode", out)
        self.assertEqual(len(out), 2)

    def test_tie_break_alphabetical(self):
        # 同分（都是无信号噪声）时保持字母序，保证确定性
        codes = ["zzz123", "aaa123", "mmm123"]
        out = prioritize_candidates(codes, "GB")
        self.assertEqual(out, ["aaa123", "mmm123", "zzz123"])

    def test_empty_history_stable(self):
        codes = ["codestone", "datroai"]
        out = prioritize_candidates(codes, "GB", history=ScanHistory())
        self.assertEqual(set(out), {"codestone", "datroai"})

    def test_custom_weights_injected(self):
        # 把 known_base 权重清零，KNOWN_BASES 不再前置
        codes = ["zzznoise", "codestone"]
        w = ScoreWeights(known_base=0.0, bare_word=0.0, short_penalty=0.0, initials_penalty=0.0)
        out = prioritize_candidates(codes, "GB", weights=w)
        # 无任何信号差异 → 退回字母序
        self.assertEqual(out, ["codestone", "zzznoise"])


if __name__ == "__main__":
    unittest.main()
