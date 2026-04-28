# -*- coding: utf-8 -*-
"""
身份一致性校验器单元测试

覆盖：
- 4 维全等通过
- 单一错配降级 warn
- 多处错配升级 block
- 字段缺失/未知 block
- SMS 数字编码归一化
- CoherenceReport 不可变性
"""

import unittest
from dataclasses import FrozenInstanceError

from src.fintech.coherence import CoherenceReport, validate_identity_coherence


class TestValidateIdentityCoherence(unittest.TestCase):
    """一致性校验主逻辑测试"""

    def test_all_match_returns_ok(self) -> None:
        """4 维国家完全一致 → ok=True, severity=ok"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="187",  # "187" → US
            billing_country="US",
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.severity, "ok")
        self.assertEqual(report.mismatches, [])
        self.assertEqual(report.card_country, "US")
        self.assertEqual(report.proxy_country, "US")
        self.assertEqual(report.sms_country, "US")
        self.assertEqual(report.billing_country, "US")
        self.assertIn("一致", report.rationale)

    def test_all_match_case_insensitive(self) -> None:
        """大小写不敏感，小写应该被归一化"""
        report = validate_identity_coherence(
            card_bin_country="us",
            proxy_country="Us",
            sms_country_code="us",
            billing_country="US",
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.severity, "ok")

    def test_single_mismatch_returns_warn(self) -> None:
        """仅 SMS 一个字段偏离多数国家（3 US + 1 ID）→ severity=warn, ok=False"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="6",  # "6" → ID
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "warn")
        # 偏离多数的字段只有 sms，视为"1 处错配"
        self.assertIn("sms", report.rationale.lower())

    def test_single_card_deviation_returns_warn(self) -> None:
        """仅 card 偏离多数（card=HK，其余 US）→ warn"""
        report = validate_identity_coherence(
            card_bin_country="HK",
            proxy_country="US",
            sms_country_code="187",
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "warn")
        # 两两错配对数 = 3（HK-US 出现 3 次），但字段维度只有 card 偏离
        self.assertEqual(len(report.mismatches), 3)

    def test_two_vs_two_split_returns_block(self) -> None:
        """2:2 分组（card+proxy=HK, sms+billing=US）→ block（2 个字段偏离多数）"""
        report = validate_identity_coherence(
            card_bin_country="HK",
            proxy_country="HK",
            sms_country_code="187",  # US
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "block")

    def test_double_mismatch_returns_block(self) -> None:
        """多处错配 → severity=block"""
        report = validate_identity_coherence(
            card_bin_country="HK",
            proxy_country="JP",
            sms_country_code="187",  # US
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "block")
        # HK/JP/US/US → card_vs_proxy, card_vs_sms, card_vs_billing,
        # proxy_vs_sms, proxy_vs_billing = 5 对
        self.assertGreaterEqual(len(report.mismatches), 2)
        self.assertIn("错配", report.rationale)

    def test_empty_card_country_blocks(self) -> None:
        """card 国家未知 → severity=block"""
        report = validate_identity_coherence(
            card_bin_country="",
            proxy_country="US",
            sms_country_code="187",
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "block")
        self.assertIn("card", report.rationale)

    def test_empty_proxy_country_blocks(self) -> None:
        """proxy 国家未知 → severity=block"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="",
            sms_country_code="187",
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "block")
        self.assertIn("proxy", report.rationale)

    def test_unknown_sms_code_blocks(self) -> None:
        """未知 SMS 数字代码 → 归一化为空 → block"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="99999",  # 未知
            billing_country="US",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "block")
        self.assertEqual(report.sms_country, "")
        self.assertIn("sms", report.rationale)

    def test_empty_billing_blocks(self) -> None:
        """billing 为空 → block"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="187",
            billing_country="",
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.severity, "block")
        self.assertIn("billing", report.rationale)


class TestSmsCountryNormalization(unittest.TestCase):
    """SMS 国家代码归一化测试"""

    def test_sms_code_187_maps_to_us(self) -> None:
        """"187" → "US" """
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="187",
            billing_country="US",
        )
        self.assertEqual(report.sms_country, "US")

    def test_sms_code_6_maps_to_id(self) -> None:
        """"6" → "ID" """
        report = validate_identity_coherence(
            card_bin_country="ID",
            proxy_country="ID",
            sms_country_code="6",
            billing_country="ID",
        )
        self.assertEqual(report.sms_country, "ID")
        self.assertTrue(report.ok)

    def test_sms_code_0_maps_to_ru(self) -> None:
        """"0" → "RU" """
        report = validate_identity_coherence(
            card_bin_country="RU",
            proxy_country="RU",
            sms_country_code="0",
            billing_country="RU",
        )
        self.assertEqual(report.sms_country, "RU")

    def test_sms_code_16_maps_to_gb(self) -> None:
        """"16" → "GB" """
        report = validate_identity_coherence(
            card_bin_country="GB",
            proxy_country="GB",
            sms_country_code="16",
            billing_country="GB",
        )
        self.assertEqual(report.sms_country, "GB")

    def test_sms_code_36_maps_to_ca(self) -> None:
        """"36" → "CA" """
        report = validate_identity_coherence(
            card_bin_country="CA",
            proxy_country="CA",
            sms_country_code="36",
            billing_country="CA",
        )
        self.assertEqual(report.sms_country, "CA")

    def test_sms_alpha_code_passes_through(self) -> None:
        """已经是 ISO alpha-2 的输入直接大写"""
        report = validate_identity_coherence(
            card_bin_country="DE",
            proxy_country="DE",
            sms_country_code="de",
            billing_country="DE",
        )
        self.assertEqual(report.sms_country, "DE")

    def test_unknown_sms_code_returns_empty(self) -> None:
        """未知数字编码归一化为空字符串"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="8888",
            billing_country="US",
        )
        self.assertEqual(report.sms_country, "")

    def test_invalid_alpha_sms_returns_empty(self) -> None:
        """长度不是 2 的字母串视为未知"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="USA",
            billing_country="US",
        )
        self.assertEqual(report.sms_country, "")


class TestCoherenceReportImmutability(unittest.TestCase):
    """CoherenceReport 是 frozen dataclass，应当不可变"""

    def test_report_is_frozen(self) -> None:
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="187",
            billing_country="US",
        )
        with self.assertRaises(FrozenInstanceError):
            report.ok = False  # type: ignore[misc]

    def test_report_severity_is_frozen(self) -> None:
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="187",
            billing_country="US",
        )
        with self.assertRaises(FrozenInstanceError):
            report.severity = "block"  # type: ignore[misc]

    def test_report_fields_populated(self) -> None:
        """所有字段都应被填充"""
        report = validate_identity_coherence(
            card_bin_country="US",
            proxy_country="US",
            sms_country_code="187",
            billing_country="US",
        )
        self.assertIsInstance(report, CoherenceReport)
        self.assertIsInstance(report.ok, bool)
        self.assertIsInstance(report.mismatches, list)
        self.assertIsInstance(report.severity, str)
        self.assertIsInstance(report.rationale, str)


if __name__ == "__main__":
    unittest.main()
