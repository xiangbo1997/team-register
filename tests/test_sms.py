# -*- coding: utf-8 -*-
"""SMS 模块单元测试"""

import unittest
from unittest.mock import patch, MagicMock

from src.sms import SMSManager
from src.models import SMSOrder


class TestSMSManagerInit(unittest.TestCase):
    """SMSManager 初始化测试"""

    def test_init_valid(self):
        """正确初始化"""
        mgr = SMSManager(api_key="key123", country="6")
        self.assertIsNotNone(mgr)

    def test_init_empty_key_raises(self):
        """空 API key 应抛出 ValueError"""
        with self.assertRaises(ValueError):
            SMSManager(api_key="")


class TestSMSManagerGetNumber(unittest.TestCase):
    """SMSManager.get_number() 测试"""

    def setUp(self):
        self.mgr = SMSManager(api_key="test-key", country="6", api_url="http://mock-sms")

    @patch("src.sms.requests.get")
    def test_get_number_success(self, mock_get: MagicMock):
        """ACCESS_NUMBER 格式正确应返回 SMSOrder"""
        mock_get.return_value.text = "ACCESS_NUMBER:12345:628123456789"

        result = self.mgr.get_number()

        self.assertIsInstance(result, SMSOrder)
        self.assertEqual(result.order_id, "12345")
        self.assertEqual(result.phone_number, "628123456789")

    @patch("src.sms.requests.get")
    def test_get_number_no_numbers(self, mock_get: MagicMock):
        """无可用号码应返回 None"""
        mock_get.return_value.text = "NO_NUMBERS"

        result = self.mgr.get_number()
        self.assertIsNone(result)

    @patch("src.sms.time.sleep")
    @patch("src.sms.requests.get")
    def test_get_number_network_error(self, mock_get: MagicMock, mock_sleep: MagicMock):
        """网络异常应在重试耗尽后返回 None"""
        import requests
        mock_get.side_effect = requests.RequestException("connection refused")

        result = self.mgr.get_number()
        self.assertIsNone(result)
        # 重试机制：首次 + 4 次重试 = 5 次调用，4 次 sleep
        self.assertEqual(mock_get.call_count, 5)
        self.assertEqual(mock_sleep.call_count, 4)


class TestSMSManagerGetCode(unittest.TestCase):
    """SMSManager.get_code() 测试"""

    def setUp(self):
        self.mgr = SMSManager(api_key="test-key", country="6", api_url="http://mock-sms")

    @patch("src.sms.human_delay")
    @patch("src.sms.requests.get")
    def test_get_code_immediate(self, mock_get: MagicMock, mock_delay: MagicMock):
        """第一次轮询就返回验证码"""
        mock_get.return_value.text = "STATUS_OK:123456"

        result = self.mgr.get_code("12345", max_retries=5)
        self.assertEqual(result, "123456")

    @patch("src.sms.human_delay")
    @patch("src.sms.requests.get")
    def test_get_code_after_wait(self, mock_get: MagicMock, mock_delay: MagicMock):
        """等待两次后返回验证码"""
        response_wait = MagicMock()
        response_wait.text = "STATUS_WAIT_CODE"
        response_ok = MagicMock()
        response_ok.text = "STATUS_OK:789012"

        mock_get.side_effect = [response_wait, response_wait, response_ok]

        result = self.mgr.get_code("12345", max_retries=5)
        self.assertEqual(result, "789012")
        self.assertEqual(mock_delay.call_count, 2)

    @patch("src.sms.human_delay")
    @patch("src.sms.requests.get")
    def test_get_code_timeout(self, mock_get: MagicMock, mock_delay: MagicMock):
        """超过重试次数应返回 None"""
        mock_get.return_value.text = "STATUS_WAIT_CODE"

        result = self.mgr.get_code("12345", max_retries=3)
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)


class TestSmsManagerRetry(unittest.TestCase):
    """SMSManager.get_number() 网络层重试机制"""

    def setUp(self):
        self.mgr = SMSManager(api_key="test-key", country="6", api_url="http://mock-sms")

    @patch("src.sms.time.sleep")
    @patch("src.sms.requests.get")
    def test_get_number_retries_on_ssl_error_then_succeeds(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ):
        """前两次 SSL EOF，第三次成功 -- 验证瞬时网络错误自愈"""
        import requests

        success_resp = MagicMock()
        success_resp.text = "ACCESS_NUMBER:99999:18001234567"
        mock_get.side_effect = [
            requests.exceptions.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING"),
            requests.exceptions.SSLError("SSL: UNEXPECTED_EOF_WHILE_READING"),
            success_resp,
        ]

        result = self.mgr.get_number()

        self.assertIsInstance(result, SMSOrder)
        self.assertEqual(result.order_id, "99999")
        self.assertEqual(result.phone_number, "18001234567")
        self.assertEqual(mock_get.call_count, 3)
        # 2 次重试 = 2 次 backoff（最后一次成功后不再 sleep）
        self.assertEqual(mock_sleep.call_count, 2)
        # 线性退避（封顶 6.0s）：1.5s -> 3.0s
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertEqual(sleep_args, [1.5, 3.0])

    @patch("src.sms.time.sleep")
    @patch("src.sms.requests.get")
    def test_get_number_returns_none_after_all_retries_exhausted(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ):
        """全部 ConnectionError -- 返回 None，不再继续（5 次尝试耗尽）"""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("network down")

        result = self.mgr.get_number()

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 5)
        self.assertEqual(mock_sleep.call_count, 4)
        # 线性退避封顶 6.0s：1.5 -> 3.0 -> 4.5 -> 6.0
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertEqual(sleep_args, [1.5, 3.0, 4.5, 6.0])

    @patch("src.sms.time.sleep")
    @patch("src.sms.requests.get")
    def test_get_number_does_not_retry_on_business_error(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ):
        """业务错误（NO_NUMBERS）应立即 fail-fast，不重试"""
        mock_get.return_value.text = "NO_NUMBERS"

        result = self.mgr.get_number()

        self.assertIsNone(result)
        # 业务层错误：只调用 1 次，零 sleep
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_sleep.call_count, 0)


class TestSMSManagerRequestRetry(unittest.TestCase):
    """号码复用：setStatus=3 请求重新发码。"""

    def setUp(self):
        from src.sms import SMSManager
        self.mgr = SMSManager(api_key="test-key", country="6", api_url="http://mock-sms")

    @patch("src.sms.requests.get")
    def test_request_retry_success(self, mock_get: MagicMock):
        """ACCESS_RETRY_GET → True，且参数带 action=setStatus & status=3。"""
        mock_get.return_value = MagicMock(text="ACCESS_RETRY_GET")
        ok = self.mgr.request_retry("ORDER_1")
        self.assertTrue(ok)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["action"], "setStatus")
        self.assertEqual(kwargs["params"]["status"], "3")
        self.assertEqual(kwargs["params"]["id"], "ORDER_1")

    @patch("src.sms.requests.get")
    def test_request_retry_rejected(self, mock_get: MagicMock):
        """平台返回非 ACCESS_RETRY_GET → False（号已结束/不可复用）。"""
        mock_get.return_value = MagicMock(text="NO_ACTIVATION")
        self.assertFalse(self.mgr.request_retry("ORDER_1"))

    @patch("src.sms.requests.get")
    def test_request_retry_network_error(self, mock_get: MagicMock):
        """网络异常 → False，不抛异常。"""
        import requests as _requests
        mock_get.side_effect = _requests.ConnectionError("boom")
        self.assertFalse(self.mgr.request_retry("ORDER_1"))


class TestSMSManagerCancel(unittest.TestCase):
    """取消号码激活（setStatus=8）。"""

    def setUp(self):
        from src.sms import SMSManager
        self.mgr = SMSManager(api_key="test-key", country="6", api_url="http://mock-sms")

    @patch("src.sms.requests.get")
    def test_cancel_success(self, mock_get: MagicMock):
        """ACCESS_CANCEL → True，参数带 action=setStatus & status=8。"""
        mock_get.return_value = MagicMock(text="ACCESS_CANCEL")
        ok = self.mgr.cancel_number("ORDER_1")
        self.assertTrue(ok)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["action"], "setStatus")
        self.assertEqual(kwargs["params"]["status"], "8")
        self.assertEqual(kwargs["params"]["id"], "ORDER_1")

    @patch("src.sms.requests.get")
    def test_cancel_rejected(self, mock_get: MagicMock):
        mock_get.return_value = MagicMock(text="NO_ACTIVATION")
        self.assertFalse(self.mgr.cancel_number("ORDER_1"))

    @patch("src.sms.requests.get")
    def test_cancel_network_error(self, mock_get: MagicMock):
        import requests as _requests
        mock_get.side_effect = _requests.ConnectionError("boom")
        self.assertFalse(self.mgr.cancel_number("ORDER_1"))


if __name__ == "__main__":
    unittest.main()
