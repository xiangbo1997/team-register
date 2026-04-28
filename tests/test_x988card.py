# -*- coding: utf-8 -*-
"""X988Card (cards.779.chat) 客户端测试。"""

import unittest
from unittest import mock
from unittest.mock import MagicMock, patch

from src.x988card import X988Card


# 真实 API 响应样例（来自用户提供）
_SAMPLE_RESPONSE = {
    "success": True,
    "card": {
        "key": "1B0198EC6A974AE1",
        "category": "4859",
        "expires_at": "2026-04-25T15:32:41.019Z",
        "activated_at": "2026-04-25T14:32:41.019Z",
        "status": "used",
    },
    "content": {
        "card_number": "4859540155771610",
        "expiry_date": "2030/2",
        "cvv": "055",
        "phone": "+19149980835",
        "sms_api": "http://a.62-us.com/api/get_sms?key=5b686a7945e6cfde5b90e4ad0e96b922",
        "name": "DANIEL SPENCER",
        "address": "69 JOHNSON LANE,SALLY 29137,US",
    },
}


def _ok_response(json_body):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = json_body
    m.text = ""
    return m


def _http_response(status_code: int, body: str = ""):
    m = MagicMock()
    m.status_code = status_code
    m.text = body
    m.json.side_effect = ValueError("not json")
    return m


class TestX988CardInit(unittest.TestCase):
    def test_default_base_url(self):
        c = X988Card()
        self.assertEqual(c._base_url, "https://cards.779.chat")
        self.assertEqual(c._headers["Origin"], "http://card.988.chat")
        self.assertEqual(c._headers["Referer"], "http://card.988.chat/")

    def test_custom_base_url_strips_trailing_slash(self):
        c = X988Card(base_url="https://example.com/api/")
        self.assertEqual(c._base_url, "https://example.com/api")

    def test_request_timeout_min_one(self):
        c = X988Card(request_timeout=0)
        self.assertEqual(c._request_timeout, 1)


class TestX988CardGetCard(unittest.TestCase):
    def setUp(self) -> None:
        self.client = X988Card()

    @patch("src.x988card.requests.post")
    def test_get_card_success(self, mock_post: MagicMock):
        mock_post.return_value = _ok_response(_SAMPLE_RESPONSE)
        info = self.client.get_card("1B0198EC6A974AE1")

        self.assertIsNotNone(info)
        self.assertEqual(info.card_number, "4859540155771610")
        self.assertEqual(info.cvv, "055")
        self.assertEqual(info.expiry_month, "02")
        self.assertEqual(info.expiry_year, "2030")
        self.assertEqual(info.name_on_card, "DANIEL SPENCER")
        self.assertEqual(info.billing_address, "69 JOHNSON LANE,SALLY 29137,US")
        self.assertEqual(info.status, "ACTIVE")
        # _last_meta 缓存已建立（供 wait_for_3ds 使用）
        self.assertEqual(self.client._last_meta["sms_api"],
                         "http://a.62-us.com/api/get_sms?key=5b686a7945e6cfde5b90e4ad0e96b922")
        self.assertEqual(self.client._last_meta["phone"], "+19149980835")
        # 请求体正确
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"], {"key": "1B0198EC6A974AE1"})

    @patch("src.x988card.requests.post")
    def test_get_card_two_digit_year(self, mock_post: MagicMock):
        body = {**_SAMPLE_RESPONSE, "content": {**_SAMPLE_RESPONSE["content"], "expiry_date": "30/2"}}
        mock_post.return_value = _ok_response(body)
        info = self.client.get_card("k")
        self.assertEqual(info.expiry_year, "2030")
        self.assertEqual(info.expiry_month, "02")

    @patch("src.x988card.requests.post")
    def test_get_card_business_failure(self, mock_post: MagicMock):
        mock_post.return_value = _ok_response({"success": False, "error": "invalid key"})
        self.assertIsNone(self.client.get_card("bad"))
        self.assertEqual(self.client.last_lookup_meta["status"], "failed")

    @patch("src.x988card.requests.post")
    def test_get_card_http_4xx(self, mock_post: MagicMock):
        mock_post.return_value = _http_response(404, "not found")
        self.assertIsNone(self.client.get_card("k"))
        self.assertEqual(self.client.last_lookup_meta["reason"], "http_404")

    @patch("src.x988card.requests.post")
    def test_get_card_invalid_json(self, mock_post: MagicMock):
        m = MagicMock()
        m.status_code = 200
        m.text = "<html>oops</html>"
        m.json.side_effect = ValueError("not json")
        mock_post.return_value = m
        self.assertIsNone(self.client.get_card("k"))
        self.assertEqual(self.client.last_lookup_meta["reason"], "invalid_json")

    @patch("src.x988card.requests.post")
    def test_get_card_network_error(self, mock_post: MagicMock):
        import requests as _req
        mock_post.side_effect = _req.ConnectionError("boom")
        self.assertIsNone(self.client.get_card("k"))
        self.assertEqual(self.client.last_lookup_meta["reason"], "network_error")

    @patch("src.x988card.requests.post")
    def test_get_card_malformed_content(self, mock_post: MagicMock):
        body = {"success": True, "card": {}, "content": {"card_number": "1234"}}  # 缺 cvv / expiry
        mock_post.return_value = _ok_response(body)
        self.assertIsNone(self.client.get_card("k"))
        self.assertEqual(self.client.last_lookup_meta["reason"], "parse_error")


class TestX988CardCancel(unittest.TestCase):
    def test_cancel_returns_true(self):
        # X988 不支持销卡（API 没暴露），保持接口契约返回 True
        c = X988Card()
        self.assertTrue(c.cancel_card("any-key"))


class TestX988CardWait3DS(unittest.TestCase):
    def setUp(self) -> None:
        self.client = X988Card()
        # 预设 _last_meta，模拟 get_card 已执行过
        self.client._last_meta = {
            "sms_api": "http://sms.example/api/get?key=abc",
            "phone": "+19149980835",
            "key": "1B0198EC6A974AE1",
        }

    def test_wait_for_3ds_no_sms_api_returns_none(self):
        c = X988Card()
        # 不调 get_card，_last_meta 为空
        self.assertIsNone(c.wait_for_3ds("k", timeout_sec=1))

    @patch("src.x988card.time.sleep", return_value=None)  # 跳过 sleep 加速
    @patch("src.x988card.requests.get")
    def test_wait_for_3ds_extracts_from_text(self, mock_get: MagicMock, _sleep):
        m = MagicMock()
        m.text = "Your verification code is 567890. Do not share."
        m.json.side_effect = ValueError("not json")
        mock_get.return_value = m
        code = self.client.wait_for_3ds("1B0198EC6A974AE1", timeout_sec=10)
        self.assertEqual(code, "567890")

    @patch("src.x988card.time.sleep", return_value=None)
    @patch("src.x988card.requests.get")
    def test_wait_for_3ds_extracts_from_json_sms_field(self, mock_get: MagicMock, _sleep):
        m = MagicMock()
        m.text = '{"sms": "OpenAI: 123456"}'
        m.json.return_value = {"sms": "OpenAI: 123456"}
        mock_get.return_value = m
        code = self.client.wait_for_3ds("k", timeout_sec=10)
        self.assertEqual(code, "123456")

    @patch("src.x988card.time.sleep", return_value=None)
    @patch("src.x988card.requests.get")
    def test_wait_for_3ds_extracts_from_nested_data(self, mock_get: MagicMock, _sleep):
        m = MagicMock()
        m.text = '{"data": {"message": "code 4567"}}'
        m.json.return_value = {"data": {"message": "code 4567"}}
        mock_get.return_value = m
        code = self.client.wait_for_3ds("k", timeout_sec=10)
        self.assertEqual(code, "4567")

    @patch("src.x988card.time.sleep", return_value=None)
    @patch("src.x988card.time.time")
    @patch("src.x988card.requests.get")
    def test_wait_for_3ds_timeout_returns_none(self, mock_get: MagicMock, mock_time: MagicMock, _sleep):
        m = MagicMock()
        m.text = "no code here"
        m.json.side_effect = ValueError()
        mock_get.return_value = m
        # 模拟时间快进直接超时
        mock_time.side_effect = [0.0, 0.0, 9999.0, 9999.0, 9999.0]
        self.assertIsNone(self.client.wait_for_3ds("k", timeout_sec=5))

    @patch("src.x988card.time.sleep", return_value=None)
    @patch("src.x988card.requests.get")
    def test_wait_for_3ds_network_error_keeps_retrying(self, mock_get: MagicMock, _sleep):
        import requests as _req
        # 第一次失败，第二次返回有效短信
        good = MagicMock()
        good.text = "OTP 999888"
        good.json.side_effect = ValueError()
        mock_get.side_effect = [_req.RequestException("boom"), good]
        code = self.client.wait_for_3ds("k", timeout_sec=10)
        self.assertEqual(code, "999888")


class TestX988CardProviderWiring(unittest.TestCase):
    """X988CardProvider 是否正确包装 X988Card。"""

    @patch("src.x988card.requests.post")
    def test_provider_get_card_delegates(self, mock_post: MagicMock):
        from src.providers.card import X988CardProvider
        mock_post.return_value = _ok_response(_SAMPLE_RESPONSE)
        p = X988CardProvider()
        info = p.get_card("1B0198EC6A974AE1")
        self.assertIsNotNone(info)
        self.assertEqual(info.card_number, "4859540155771610")

    def test_provider_cancel_returns_true(self):
        from src.providers.card import X988CardProvider
        self.assertTrue(X988CardProvider().cancel_card("any"))

    def test_provider_get_billing_returns_none(self):
        # X988 verify 已含账单地址但无 transactions，BillingInfo 留空
        from src.providers.card import X988CardProvider
        self.assertIsNone(X988CardProvider().get_billing("any"))


if __name__ == "__main__":
    unittest.main()
