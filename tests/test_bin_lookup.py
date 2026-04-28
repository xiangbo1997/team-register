# -*- coding: utf-8 -*-
"""BIN 查询模块单元测试"""

import unittest
from unittest.mock import MagicMock

from src.fintech.bin_lookup import lookup_bin_country


class TestStaticTableHits(unittest.TestCase):
    """静态表命中场景"""

    def test_4_digit_prefix_hk(self):
        """4 位前缀命中香港（Efuncard）"""
        result = lookup_bin_country("4085999988887777")
        self.assertEqual(result, "HK")

    def test_4_digit_prefix_us(self):
        """4 位前缀命中美国（NodeCard）"""
        result = lookup_bin_country("5577123456789012")
        self.assertEqual(result, "US")

    def test_6_digit_prefix_cn(self):
        """6 位前缀命中中国银联"""
        result = lookup_bin_country("6225880012345678")
        self.assertEqual(result, "CN")

    def test_6_digit_prefix_hk(self):
        """6 位前缀命中香港 Mastercard"""
        result = lookup_bin_country("5302200011223344")
        self.assertEqual(result, "HK")

    def test_longer_prefix_priority(self):
        """长前缀优先：400115 → CN 不能被 4 位笼统规则覆盖
        （注意：4001 并不在表里，但验证 6 位优先的语义）
        """
        result = lookup_bin_country("4001150012345678")
        self.assertEqual(result, "CN")

    def test_4085_not_shadowed_by_shorter_rule(self):
        """4085 → HK，即便存在其他 4 开头规则也应正确命中"""
        result = lookup_bin_country("4085123456789012")
        self.assertEqual(result, "HK")


class TestCardNumberCleaning(unittest.TestCase):
    """卡号清洗逻辑"""

    def test_card_with_spaces(self):
        """带空格卡号"""
        result = lookup_bin_country("4085 9999 8888 7777")
        self.assertEqual(result, "HK")

    def test_card_with_dashes(self):
        """带短横线卡号"""
        result = lookup_bin_country("4085-9999-8888-7777")
        self.assertEqual(result, "HK")

    def test_card_with_mixed_noise(self):
        """混合非数字字符"""
        result = lookup_bin_country("  4085-99 99/8888 7777  ")
        self.assertEqual(result, "HK")


class TestEmptyInput(unittest.TestCase):
    """空输入处理"""

    def test_empty_string(self):
        """空字符串返回空串"""
        result = lookup_bin_country("")
        self.assertEqual(result, "")

    def test_none(self):
        """None 返回空串"""
        result = lookup_bin_country(None)  # type: ignore[arg-type]
        self.assertEqual(result, "")

    def test_only_non_digits(self):
        """只有非数字字符返回空串"""
        result = lookup_bin_country("abc---   xyz")
        self.assertEqual(result, "")

    def test_too_short_no_http(self):
        """位数不足 6 位且未命中静态表 → 不发 HTTP，直接空串"""
        mock_http = MagicMock()
        result = lookup_bin_country("123", http_get=mock_http)
        self.assertEqual(result, "")
        mock_http.assert_not_called()


class TestHttpFallback(unittest.TestCase):
    """HTTP 回源场景"""

    def test_http_fallback_success(self):
        """静态表未命中 + http_get 返回 binlist 格式 → 返回 alpha2"""
        def mock_http(url: str) -> dict:
            return {
                "country": {
                    "alpha2": "gb",
                    "name": "United Kingdom",
                    "numeric": "826",
                },
                "bank": {"name": "Barclays"},
            }

        # 使用未命中静态表的卡号
        result = lookup_bin_country("6000009999888877", http_get=mock_http)
        self.assertEqual(result, "GB")

    def test_http_fallback_raises(self):
        """静态表未命中 + http_get 抛异常 → 返回空串，不传播异常"""
        def mock_http(url: str) -> dict:
            raise ConnectionError("network down")

        result = lookup_bin_country("6000009999888877", http_get=mock_http)
        self.assertEqual(result, "")

    def test_http_fallback_non_200(self):
        """http_get 内部抛 RuntimeError（模拟非 200）→ 返回空串"""
        def mock_http(url: str) -> dict:
            raise RuntimeError("binlist HTTP 400")

        result = lookup_bin_country("6000009999888877", http_get=mock_http)
        self.assertEqual(result, "")

    def test_http_fallback_bad_json_shape(self):
        """http_get 返回结构异常（缺 country 字段）→ 返回空串"""
        def mock_http(url: str) -> dict:
            return {"bank": {"name": "Unknown"}}

        result = lookup_bin_country("6000009999888877", http_get=mock_http)
        self.assertEqual(result, "")

    def test_http_fallback_missing_alpha2(self):
        """country 存在但 alpha2 缺失 → 返回空串"""
        def mock_http(url: str) -> dict:
            return {"country": {"name": "Nowhere"}}

        result = lookup_bin_country("6000009999888877", http_get=mock_http)
        self.assertEqual(result, "")

    def test_http_fallback_invalid_alpha2(self):
        """alpha2 不是合法 2 位字母 → 返回空串"""
        def mock_http(url: str) -> dict:
            return {"country": {"alpha2": "123"}}

        result = lookup_bin_country("6000009999888877", http_get=mock_http)
        self.assertEqual(result, "")

    def test_http_fallback_not_dict(self):
        """http_get 返回非 dict → 返回空串"""
        def mock_http(url: str):
            return ["not", "a", "dict"]

        result = lookup_bin_country("6000009999888877", http_get=mock_http)  # type: ignore[arg-type]
        self.assertEqual(result, "")

    def test_http_not_called_when_static_hits(self):
        """静态表命中时不应调用 HTTP"""
        mock_http = MagicMock()
        result = lookup_bin_country("4085123456789012", http_get=mock_http)
        self.assertEqual(result, "HK")
        mock_http.assert_not_called()

    def test_http_called_with_6_digit_bin(self):
        """HTTP 回源时使用卡号前 6 位拼接 URL"""
        calls: list[str] = []

        def mock_http(url: str) -> dict:
            calls.append(url)
            return {"country": {"alpha2": "DE"}}

        result = lookup_bin_country("6000001234567890", http_get=mock_http)
        self.assertEqual(result, "DE")
        self.assertEqual(len(calls), 1)
        self.assertIn("600000", calls[0])


if __name__ == "__main__":
    unittest.main()
