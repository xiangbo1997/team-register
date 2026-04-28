# -*- coding: utf-8 -*-
"""邮件模块单元测试（HTTP API 集成模式）"""

import unittest
from unittest.mock import patch, MagicMock

from src.mail import MailManager
from src.providers.mail import MailSession


def _make_manager(**kwargs):
    defaults = {"base_url": "http://127.0.0.1:8000", "api_key": "test-key"}
    defaults.update(kwargs)
    return MailManager(**defaults)


def _make_session(**kwargs):
    defaults = {
        "session_id": "sess-1",
        "lease_token": "tok-1",
        "email": "test@example.com",
        "provider": "applemail",
    }
    defaults.update(kwargs)
    return MailSession(**defaults)


class TestMailManagerInit(unittest.TestCase):
    """MailManager 初始化测试"""

    def test_init_valid(self):
        mgr = _make_manager()
        self.assertIsNotNone(mgr)

    def test_init_base_url_required(self):
        """base_url 可为空（HttpMailProvider 有默认值）"""
        mgr = _make_manager(base_url="")
        self.assertIsNotNone(mgr)

    def test_init_backward_compatible_params(self):
        """保留旧参数不报错"""
        mgr = MailManager(
            base_url="http://x",
            api_key="k",
            refresh_token="rt",
            client_id="cid",
        )
        self.assertIsNotNone(mgr)


class TestMailManagerHTTPIntegration(unittest.TestCase):
    """MailManager 与 HttpMailProvider 的交互测试"""

    def setUp(self):
        self.mgr = _make_manager(proxy="socks5h://127.0.0.1:7890")

    def test_get_latest_mail_returns_none_with_warning(self):
        with self.assertLogs("src.mail", level="WARNING") as captured:
            result = self.mgr.get_latest_mail("test@example.com")
        self.assertIsNone(result)
        self.assertTrue(any("已废弃" in m for m in captured.output))

    @patch.object(MailManager, "_poll_code_via_api", return_value="482910")
    def test_get_verification_code_success(self, mock_poll):
        result = self.mgr.get_verification_code("test@example.com", wait_timeout=30)
        self.assertEqual(result, "482910")
        mock_poll.assert_called_once_with("test@example.com", 30)

    @patch.object(MailManager, "_poll_code_via_api", return_value=None)
    def test_get_verification_code_timeout(self, mock_poll):
        result = self.mgr.get_verification_code("test@example.com", wait_timeout=5)
        self.assertIsNone(result)

    @patch.object(MailManager, "_poll_code_via_api", return_value="193847")
    def test_get_verification_code_via_browser(self, mock_poll):
        """浏览器模式也走 HTTP API"""
        result = self.mgr.get_verification_code_via_browser(
            email="test@example.com", page=MagicMock(), wait_timeout=20,
        )
        self.assertEqual(result, "193847")
        mock_poll.assert_called_once_with("test@example.com", 20)

    def test_clear_mailbox_returns_true(self):
        """HTTP API 模式下 clear_mailbox 始终返回 True"""
        result = self.mgr.clear_mailbox("test@example.com")
        self.assertTrue(result)

    def test_ensure_runtime_ready_resolves_mode_and_calls_provider(self):
        mgr = _make_manager(refresh_token="rt-1", client_id="cid-1")
        mock_provider = MagicMock()
        mgr._provider = mock_provider

        session_mode = mgr.ensure_runtime_ready("known@example.com")

        self.assertEqual(session_mode, "credentialed")
        mock_provider.ensure_runtime_ready.assert_called_once_with(
            "applemail",
            session_mode="credentialed",
        )


class TestMailManagerPollCodeViaAPI(unittest.TestCase):
    """测试 _poll_code_via_api 内部逻辑"""

    def setUp(self):
        self.mgr = _make_manager(provider_name="luckmail")
        self.mock_provider = MagicMock()
        self.mgr._provider = self.mock_provider

    def test_poll_success(self):
        session = _make_session()
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.return_value = "123456"

        result = self.mgr._poll_code_via_api("test@example.com", 60)

        self.assertEqual(result, "123456")
        self.mock_provider.create_session.assert_called_once()
        self.mock_provider.poll_code.assert_called_once_with(session, timeout_seconds=60)
        self.mock_provider.complete.assert_called_once_with(session, result="success", reason="")

    def test_poll_builds_applemail_extra_from_legacy_fields(self):
        mgr = _make_manager(refresh_token="rt-1", client_id="cid-1")
        mgr._provider = self.mock_provider
        session = _make_session(email="known@example.com")
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.return_value = None

        result = mgr._poll_code_via_api("known@example.com", 10)

        self.assertIsNone(result)
        self.mock_provider.create_session.assert_called_once_with(
            provider="applemail",
            purpose="otp",
            session_mode="credentialed",
            email="known@example.com",
            proxy="",
            extra=None,
            existing_account={
                "email": "known@example.com",
                "account_id": "",
                "extra": {},
                "preserve_existing_mail": True,
                "credentials": {
                    "client_id": "cid-1",
                    "refresh_token": "rt-1",
                    "password": "unused",
                },
            },
            config_name="",
        )
        self.mock_provider.complete.assert_called_once_with(session, result="failed", reason="timeout")

    def test_poll_uses_known_applemail_account_json(self):
        mgr = _make_manager(
            known_accounts={
                "applemail": [
                    {
                        "email": "known@example.com",
                        "client_id": "cid-known",
                        "refresh_token": "rt-known",
                        "password": "pw-known",
                        "preserve_existing_mail": False,
                    }
                ]
            }
        )
        mgr._provider = self.mock_provider
        session = _make_session(email="known@example.com")
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.return_value = "654321"

        result = mgr._poll_code_via_api("known@example.com", 15)

        self.assertEqual(result, "654321")
        self.mock_provider.create_session.assert_called_once_with(
            provider="applemail",
            purpose="otp",
            session_mode="credentialed",
            email="known@example.com",
            proxy="",
            extra=None,
            existing_account={
                "email": "known@example.com",
                "account_id": "",
                "extra": {},
                "preserve_existing_mail": False,
                "credentials": {
                    "client_id": "cid-known",
                    "refresh_token": "rt-known",
                    "password": "pw-known",
                },
            },
            config_name="",
        )
        self.mock_provider.complete.assert_called_once_with(session, result="success", reason="")

    def test_applemail_known_accounts_mismatch_fail_fast(self):
        mgr = _make_manager(
            known_accounts={
                "applemail": [
                    {"email": "other@example.com", "client_id": "cid", "refresh_token": "rt"}
                ]
            }
        )
        mgr._provider = self.mock_provider

        with self.assertRaises(RuntimeError):
            mgr._poll_code_via_api("missing@example.com", 10)

        self.mock_provider.create_session.assert_not_called()

    def test_applemail_known_accounts_mismatch_falls_back_to_legacy_credentials(self):
        mgr = _make_manager(
            known_accounts={
                "applemail": [
                    {"email": "other@example.com", "client_id": "cid-other", "refresh_token": "rt-other"}
                ]
            },
            refresh_token="rt-legacy",
            client_id="cid-legacy",
        )
        mgr._provider = self.mock_provider
        session = _make_session(email="fallback@example.com")
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.return_value = None

        mgr._poll_code_via_api("fallback@example.com", 10)

        self.mock_provider.create_session.assert_called_once_with(
            provider="applemail",
            purpose="otp",
            session_mode="credentialed",
            email="fallback@example.com",
            proxy="",
            extra=None,
            existing_account={
                "email": "fallback@example.com",
                "account_id": "",
                "extra": {},
                "preserve_existing_mail": True,
                "credentials": {
                    "client_id": "cid-legacy",
                    "refresh_token": "rt-legacy",
                    "password": "unused",
                },
            },
            config_name="",
        )

    def test_non_applemail_defaults_to_managed_mode(self):
        mgr = _make_manager(provider_name="luckmail")
        mgr._provider = self.mock_provider
        session = _make_session(provider="luckmail", session_mode="managed")
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.return_value = None

        mgr._poll_code_via_api("managed@example.com", 10)

        self.mock_provider.create_session.assert_called_once_with(
            provider="luckmail",
            purpose="otp",
            session_mode="managed",
            email="managed@example.com",
            proxy="",
            extra=None,
            existing_account=None,
            config_name="",
        )

    def test_poll_returns_none_on_timeout(self):
        session = _make_session()
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.return_value = None

        result = self.mgr._poll_code_via_api("test@example.com", 10)

        self.assertIsNone(result)
        self.mock_provider.complete.assert_called_once_with(session, result="failed", reason="timeout")

    def test_poll_connection_error(self):
        self.mock_provider.create_session.side_effect = ConnectionError("refused")

        with self.assertRaises(ConnectionError):
            self.mgr._poll_code_via_api("test@example.com", 10)

    def test_poll_generic_error(self):
        session = _make_session()
        self.mock_provider.create_session.return_value = session
        self.mock_provider.poll_code.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.mgr._poll_code_via_api("test@example.com", 10)
        self.mock_provider.complete.assert_called_once()

    def test_complete_session_swallows_errors(self):
        """_complete_session 不应抛异常"""
        session = _make_session()
        self.mock_provider.complete.side_effect = RuntimeError("fail")
        # 不抛异常即通过
        self.mgr._complete_session(session, "failed", "test")


if __name__ == "__main__":
    unittest.main()
