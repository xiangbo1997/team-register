# -*- coding: utf-8 -*-
"""邮件模块单元测试（对齐当前 email-provider 直连实现）"""

import unittest
from unittest.mock import patch, MagicMock

from src.mail import MailManager


class TestMailManagerInit(unittest.TestCase):
    """MailManager 初始化测试"""

    def test_init_valid(self):
        """refresh_token/client_id 完整即可初始化"""
        mgr = MailManager(
            base_url="https://mail.test",
            refresh_token="rt_test",
            client_id="cid_test",
        )
        self.assertIsNotNone(mgr)

    def test_init_base_url_optional(self):
        """base_url 作为兼容字段，可为空"""
        mgr = MailManager(base_url="", refresh_token="rt_test", client_id="cid_test")
        self.assertIsNotNone(mgr)

    def test_init_missing_token_raises(self):
        """缺少 refresh_token 应抛出 ValueError"""
        with self.assertRaises(ValueError):
            MailManager(base_url="https://x", refresh_token="", client_id="cid")

    def test_init_missing_client_id_raises(self):
        """缺少 client_id 应抛出 ValueError"""
        with self.assertRaises(ValueError):
            MailManager(base_url="https://x", refresh_token="rt", client_id="")


class TestMailManagerProviderIntegration(unittest.TestCase):
    """MailManager 与 email-provider 的交互测试"""

    def setUp(self):
        self.mgr = MailManager(
            base_url="https://mail.test",
            refresh_token="rt_test",
            client_id="cid_test",
            proxy="socks5h://127.0.0.1:7890",
        )

    @patch("src.mail.create_local_mailbox")
    def test_get_mailbox_builds_applemail_account(self, mock_create_local_mailbox: MagicMock):
        """应按 AppleMail 账户格式构造 provider 参数"""
        mailbox = MagicMock()
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr._get_mailbox("test@example.com")

        self.assertIs(result, mailbox)
        mock_create_local_mailbox.assert_called_once_with(
            provider="applemail",
            extra={
                "applemail_accounts": "test@example.com----dummy_pass----cid_test----rt_test"
            },
            proxy="socks5h://127.0.0.1:7890",
        )

    def test_get_latest_mail_returns_none_with_warning(self):
        """兼容占位接口当前应返回 None 并记录 warning"""
        with self.assertLogs("src.mail", level="WARNING") as captured:
            result = self.mgr.get_latest_mail("test@example.com")

        self.assertIsNone(result)
        self.assertTrue(any("已废弃" in message for message in captured.output))

    @patch("src.mail.create_local_mailbox")
    def test_get_verification_code_success(self, mock_create_local_mailbox: MagicMock):
        """provider 返回验证码时应原样返回"""
        mailbox = MagicMock()
        mailbox.wait_for_code.return_value = "482910"
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr.get_verification_code("test@example.com", wait_timeout=30)

        self.assertEqual(result, "482910")
        kwargs = mailbox.wait_for_code.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["account"].email, "test@example.com")
        self.assertEqual(kwargs["account"].account_id, "test@example.com")

    @patch("src.mail.create_local_mailbox")
    def test_get_verification_code_timeout(self, mock_create_local_mailbox: MagicMock):
        """provider 超时时应返回 None"""
        mailbox = MagicMock()
        mailbox.wait_for_code.side_effect = TimeoutError("timeout")
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr.get_verification_code("test@example.com", wait_timeout=5)

        self.assertIsNone(result)

    @patch("src.mail.create_local_mailbox")
    def test_get_verification_code_error(self, mock_create_local_mailbox: MagicMock):
        """provider 异常时应返回 None"""
        mailbox = MagicMock()
        mailbox.wait_for_code.side_effect = RuntimeError("boom")
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr.get_verification_code("test@example.com", wait_timeout=5)

        self.assertIsNone(result)

    @patch("src.mail.create_local_mailbox")
    def test_get_verification_code_via_browser_reuses_provider_flow(
        self, mock_create_local_mailbox: MagicMock
    ):
        """浏览器模式当前也应走 email-provider 轮询"""
        mailbox = MagicMock()
        mailbox.wait_for_code.return_value = "193847"
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr.get_verification_code_via_browser(
            email="test@example.com",
            page=MagicMock(),
            wait_timeout=20,
        )

        self.assertEqual(result, "193847")
        self.assertEqual(mailbox.wait_for_code.call_args.kwargs["timeout"], 20)

    @patch("src.mail.create_local_mailbox")
    def test_clear_mailbox_success(self, mock_create_local_mailbox: MagicMock):
        """clear_mailbox 应触发 provider 的 get_email 流程"""
        mailbox = MagicMock()
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr.clear_mailbox("test@example.com")

        self.assertTrue(result)
        mailbox.get_email.assert_called_once_with()

    @patch("src.mail.create_local_mailbox")
    def test_clear_mailbox_failure(self, mock_create_local_mailbox: MagicMock):
        """clear_mailbox 捕获 provider 异常并返回 False"""
        mailbox = MagicMock()
        mailbox.get_email.side_effect = RuntimeError("clear failed")
        mock_create_local_mailbox.return_value = mailbox

        result = self.mgr.clear_mailbox("test@example.com")

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
