# -*- coding: utf-8 -*-
"""EfunCard 模块单元测试"""

import unittest
from unittest.mock import patch, MagicMock

from src.efuncard import EfunCard
from src.models import CardInfo


class TestEfunCardInit(unittest.TestCase):
    """EfunCard 初始化测试"""

    def test_init_with_valid_token(self):
        """正确初始化"""
        client = EfunCard(token="test-token")
        self.assertIsNotNone(client)

    def test_init_with_empty_token_raises(self):
        """空 token 应抛出 ValueError"""
        with self.assertRaises(ValueError):
            EfunCard(token="")


class TestEfunCardRedeem(unittest.TestCase):
    """EfunCard.redeem() 测试"""

    def setUp(self):
        self.client = EfunCard(token="test-token", base_url="http://mock-api")

    @patch("src.efuncard.requests.post")
    def test_redeem_success(self, mock_post: MagicMock):
        """成功激活 CDK 应返回 CardInfo"""
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4111111111111111",
                "expiryMonth": "12",
                "expiry_year": "2028",
                "cvv": "123",
            },
        }

        result = self.client.redeem("CDK-TEST-001")

        self.assertIsInstance(result, CardInfo)
        self.assertEqual(result.card_number, "4111111111111111")
        self.assertEqual(result.cvv, "123")
        self.assertEqual(result.expiry_display, "12/28")

    @patch("src.efuncard.requests.post")
    def test_redeem_success_with_expiry_year_camel_case(self, mock_post: MagicMock):
        """兼容当前接口返回的 expiryYear 字段"""
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4983910011975821",
                "expiryMonth": 4,
                "expiryYear": 2029,
                "cvv": "820",
            },
        }

        result = self.client.redeem("CDK-TEST-NEW")

        self.assertIsInstance(result, CardInfo)
        self.assertEqual(result.card_number, "4983910011975821")
        self.assertEqual(result.cvv, "820")
        self.assertEqual(result.expiry_display, "04/29")

    @patch("src.efuncard.requests.post")
    def test_redeem_api_failure(self, mock_post: MagicMock):
        """API 返回 success=False 应返回 None"""
        mock_post.return_value.json.return_value = {
            "success": False,
            "message": "Invalid CDK",
        }

        result = self.client.redeem("CDK-BAD")
        self.assertIsNone(result)
        self.assertEqual(self.client.last_redeem_meta["status"], "api_failure")

    @patch("src.efuncard.requests.post")
    def test_redeem_malformed_data(self, mock_post: MagicMock):
        """API 返回数据结构异常应返回 None"""
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {"unexpected": "format"},
        }

        result = self.client.redeem("CDK-WEIRD")
        self.assertIsNone(result)
        self.assertEqual(self.client.last_redeem_meta["status"], "shape_mismatch")

    @patch("src.efuncard.requests.post")
    def test_redeem_network_error(self, mock_post: MagicMock):
        """网络异常应返回 None 而非抛出"""
        import requests
        mock_post.side_effect = requests.RequestException("timeout")

        result = self.client.redeem("CDK-NET-ERR")
        self.assertIsNone(result)
        self.assertEqual(self.client.last_redeem_meta["status"], "request_exception")


class TestEfunCardQueryAndLookup(unittest.TestCase):
    """EfunCard.query()/get_card() 测试"""

    def setUp(self):
        self.client = EfunCard(token="test-token", base_url="http://mock-api")

    @patch("src.efuncard.requests.get")
    def test_query_success_returns_active_card(self, mock_get: MagicMock):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4111111111111111",
                "expiryMonth": 12,
                "expiryYear": 2025,
                "cvv": "123",
                "status": "ACTIVE",
                "autoCancelAt": "2099-01-15T11:30:00Z",
                "nameOnCard": "JOHN DOE",
            },
        }

        result = self.client.query("CDK-QUERY-001")

        self.assertIsInstance(result, CardInfo)
        self.assertEqual(result.name_on_card, "JOHN DOE")
        self.assertEqual(self.client.last_query_meta["status"], "success")

    @patch("src.efuncard.requests.get")
    def test_query_404_marks_not_found(self, mock_get: MagicMock):
        mock_get.return_value.status_code = 404
        mock_get.return_value.json.return_value = {
            "success": False,
            "message": "Card not found",
        }

        result = self.client.query("CDK-NOT-FOUND")

        self.assertIsNone(result)
        self.assertEqual(self.client.last_query_meta["status"], "not_found")

    @patch("src.efuncard.requests.get")
    def test_query_expired_card_returns_none(self, mock_get: MagicMock):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4111111111111111",
                "expiryMonth": 12,
                "expiryYear": 2025,
                "cvv": "123",
                "status": "ACTIVE",
                "autoCancelAt": "2000-01-15T11:30:00Z",
            },
        }

        result = self.client.query("CDK-QUERY-EXPIRED")

        self.assertIsNone(result)
        self.assertEqual(self.client.last_query_meta["status"], "expired")

    @patch("src.efuncard.requests.get")
    def test_query_missing_status_returns_none(self, mock_get: MagicMock):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4111111111111111",
                "expiryMonth": 12,
                "expiryYear": 2025,
                "cvv": "123",
                "autoCancelAt": "2099-01-15T11:30:00Z",
            },
        }

        result = self.client.query("CDK-MISSING-STATUS")

        self.assertIsNone(result)
        self.assertEqual(self.client.last_query_meta["status"], "missing_status")

    @patch("src.efuncard.requests.get")
    def test_query_missing_valid_until_returns_none(self, mock_get: MagicMock):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4111111111111111",
                "expiryMonth": 12,
                "expiryYear": 2025,
                "cvv": "123",
                "status": "ACTIVE",
            },
        }

        result = self.client.query("CDK-MISSING-TIME")

        self.assertIsNone(result)
        self.assertEqual(self.client.last_query_meta["status"], "missing_valid_until")

    @patch.object(EfunCard, "redeem")
    @patch.object(EfunCard, "query")
    def test_get_card_prefers_query_before_redeem(self, mock_query: MagicMock, mock_redeem: MagicMock):
        expected = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
        )
        mock_query.return_value = expected

        result = self.client.get_card("CDK-REUSE")

        self.assertEqual(result, expected)
        mock_query.assert_called_once_with("CDK-REUSE", max_age_sec=3600)
        mock_redeem.assert_not_called()
        self.assertEqual(self.client.last_lookup_meta["source"], "query")

    @patch.object(EfunCard, "redeem")
    @patch.object(EfunCard, "query")
    def test_get_card_falls_back_to_redeem_when_query_misses(self, mock_query: MagicMock, mock_redeem: MagicMock):
        expected = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
        )
        mock_query.return_value = None
        mock_redeem.return_value = expected
        self.client.last_query_meta = {"status": "not_found"}
        self.client.last_redeem_meta = {"status": "success"}

        result = self.client.get_card("CDK-FIRST-USE")

        self.assertEqual(result, expected)
        mock_redeem.assert_called_once_with("CDK-FIRST-USE")
        self.assertEqual(self.client.last_lookup_meta["source"], "redeem")

    @patch.object(EfunCard, "redeem")
    @patch.object(EfunCard, "query")
    def test_get_card_does_not_redeem_when_query_reports_expired(self, mock_query: MagicMock, mock_redeem: MagicMock):
        mock_query.return_value = None
        self.client.last_query_meta = {"status": "expired"}

        result = self.client.get_card("CDK-EXPIRED")

        self.assertIsNone(result)
        mock_redeem.assert_not_called()
        self.assertEqual(self.client.last_lookup_meta["source"], "query_only")

    @patch.object(EfunCard, "redeem")
    @patch.object(EfunCard, "query")
    def test_get_card_does_not_redeem_when_query_api_failure(self, mock_query: MagicMock, mock_redeem: MagicMock):
        mock_query.return_value = None
        self.client.last_query_meta = {"status": "api_failure"}

        result = self.client.get_card("CDK-API-FAIL")

        self.assertIsNone(result)
        mock_redeem.assert_not_called()
        self.assertEqual(self.client.last_lookup_meta["source"], "query_only")


class TestEfunCardWaitFor3ds(unittest.TestCase):
    """EfunCard.wait_for_3ds() 测试"""

    def setUp(self):
        self.client = EfunCard(token="test-token", base_url="http://mock-api")

    @patch("src.efuncard.human_delay")
    @patch("src.efuncard.requests.post")
    def test_wait_for_3ds_success(self, mock_post: MagicMock, mock_delay: MagicMock):
        """第一次轮询即返回 OTP — 无需 mock time，超时足够大即可"""
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {
                "verifications": [{"otp": "567890"}],
            },
        }

        result = self.client.wait_for_3ds("CDK-3DS", timeout_sec=60)
        self.assertEqual(result, "567890")

    @patch("src.efuncard.human_delay")
    @patch("src.efuncard.requests.post")
    def test_wait_for_3ds_timeout(self, mock_post: MagicMock, mock_delay: MagicMock):
        """无验证码应超时返回 None — 使用 timeout_sec=0"""
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {"verifications": []},
        }

        result = self.client.wait_for_3ds("CDK-3DS", timeout_sec=0)
        self.assertIsNone(result)


class TestEfunCardCancelAndBilling(unittest.TestCase):
    """EfunCard.cancel() 和 EfunCard.billing() 测试"""

    def setUp(self):
        self.client = EfunCard(token="test-token", base_url="http://mock-api")

    @patch("src.efuncard.requests.post")
    def test_cancel_success(self, mock_post: MagicMock):
        """成功销卡应返回 True"""
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardId": 123,
                "status": "cancelled",
            },
        }

        result = self.client.cancel("CDK-CANCEL-001")
        self.assertTrue(result)
        self.assertEqual(self.client.last_cancel_meta["status"], "success")

    @patch("src.efuncard.requests.post")
    def test_cancel_failure(self, mock_post: MagicMock):
        """API 返回失败应返回 False"""
        mock_post.return_value.json.return_value = {
            "success": False,
            "message": "Card already cancelled",
        }

        result = self.client.cancel("CDK-CANCEL-BAD")
        self.assertFalse(result)
        self.assertEqual(self.client.last_cancel_meta["status"], "api_failure")

    @patch("src.efuncard.requests.get")
    def test_billing_success(self, mock_get: MagicMock):
        """成功查询账单应返回 BillingInfo"""
        mock_get.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardId": 123,
                "code": "CDK-BILL-001",
                "transactions": [
                    {
                        "id": "txn_001",
                        "amount": -25.0,
                        "currency": "USD",
                        "merchant": "Amazon",
                        "status": "completed",
                        "createdAt": "2024-01-15T14:30:00Z",
                    }
                ],
                "totalSpent": 25.0,
                "remainingBalance": 75.0,
            },
        }

        from src.models import BillingInfo
        result = self.client.billing("CDK-BILL-001")
        self.assertIsInstance(result, BillingInfo)
        self.assertEqual(len(result.transactions), 1)
        self.assertEqual(result.transactions[0].merchant, "Amazon")
        self.assertEqual(self.client.last_billing_meta["status"], "success")


class TestEfunCardBinCountry(unittest.TestCase):
    """验证 CardInfo.bin_country 通过 lookup_bin_country 被填充"""

    def setUp(self):
        self.client = EfunCard(token="test-token", base_url="http://mock-api")

    @patch("src.efuncard.lookup_bin_country")
    @patch("src.efuncard.requests.post")
    def test_redeem_populates_bin_country(
        self, mock_post: MagicMock, mock_lookup: MagicMock
    ):
        """redeem 成功时应调用 lookup_bin_country 并把结果写入 CardInfo"""
        mock_lookup.return_value = "HK"
        mock_post.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4085999900001111",
                "expiryMonth": "12",
                "expiryYear": "2028",
                "cvv": "123",
            },
        }

        result = self.client.redeem("CDK-BIN-001")

        self.assertIsInstance(result, CardInfo)
        mock_lookup.assert_called_once_with("4085999900001111")
        self.assertEqual(result.bin_country, "HK")

    @patch("src.efuncard.lookup_bin_country")
    @patch("src.efuncard.requests.get")
    def test_query_populates_bin_country(
        self, mock_get: MagicMock, mock_lookup: MagicMock
    ):
        """query 复用已激活卡时也应填充 bin_country"""
        mock_lookup.return_value = "US"
        future_iso = "2099-12-31T23:59:59+00:00"
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "data": {
                "cardNumber": "4111111111111111",
                "expiryMonth": "12",
                "expiryYear": "2028",
                "cvv": "999",
                "status": "ACTIVE",
                "autoCancelAt": future_iso,
            },
        }

        result = self.client.query("CDK-BIN-002")

        self.assertIsInstance(result, CardInfo)
        mock_lookup.assert_called_once_with("4111111111111111")
        self.assertEqual(result.bin_country, "US")


if __name__ == "__main__":
    unittest.main()
