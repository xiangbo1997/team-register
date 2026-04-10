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

    @patch("src.sms.requests.get")
    def test_get_number_network_error(self, mock_get: MagicMock):
        """网络异常应返回 None"""
        import requests
        mock_get.side_effect = requests.RequestException("connection refused")

        result = self.mgr.get_number()
        self.assertIsNone(result)


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


if __name__ == "__main__":
    unittest.main()
