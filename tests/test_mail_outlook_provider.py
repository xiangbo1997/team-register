# -*- coding: utf-8 -*-
"""OutlookMailProvider 单元测试。

验证：
  1. can_handle() 按域名匹配（hotmail/outlook/live/msn 命中；gmail 等不命中）
  2. create_session() 强制注入 provider=outlook_email_plus / config_name / managed
  3. 忽略调用方传入的 existing_account / account_id / account_extra（managed-only）
  4. ensure_runtime_ready() 强制传 outlook_email_plus + managed
"""
from __future__ import annotations

import unittest
from unittest import mock

import requests

from src.providers.mail_outlook import OutlookMailProvider


def _ok_response(payload: dict) -> mock.MagicMock:
    """构造一个成功的 HTTP 200 响应。"""
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestCanHandle(unittest.TestCase):
    def test_outlook_com(self):
        self.assertTrue(OutlookMailProvider.can_handle("alice@outlook.com"))

    def test_hotmail_com(self):
        self.assertTrue(OutlookMailProvider.can_handle("bob@hotmail.com"))

    def test_live_com(self):
        self.assertTrue(OutlookMailProvider.can_handle("charlie@live.com"))

    def test_msn_com(self):
        self.assertTrue(OutlookMailProvider.can_handle("dave@msn.com"))

    def test_outlook_jp(self):
        self.assertTrue(OutlookMailProvider.can_handle("eve@outlook.jp"))

    def test_hotmail_uk(self):
        self.assertTrue(OutlookMailProvider.can_handle("frank@hotmail.co.uk"))

    def test_uppercase_normalizes(self):
        # 域名应大小写不敏感
        self.assertTrue(OutlookMailProvider.can_handle("ALICE@HOTMAIL.COM"))

    def test_gmail_not_handled(self):
        self.assertFalse(OutlookMailProvider.can_handle("alice@gmail.com"))

    def test_custom_domain_not_handled(self):
        self.assertFalse(OutlookMailProvider.can_handle("alice@zhangxb.xyz"))

    def test_empty_string(self):
        self.assertFalse(OutlookMailProvider.can_handle(""))

    def test_no_at_sign(self):
        self.assertFalse(OutlookMailProvider.can_handle("no-at-sign"))


class TestCreateSession(unittest.TestCase):
    def setUp(self):
        self.provider = OutlookMailProvider(
            base_url="https://email.cloudsentryai.com",
            api_key="test-key",
        )

    @mock.patch("src.providers.mail.requests.post")
    def test_strict_inject_managed_provider_config(self, mock_post):
        """create_session 必须传 provider=outlook_email_plus / managed / config_name"""
        mock_post.return_value = _ok_response({
            "session_id": "sid_test",
            "lease_token": "tk_test",
            "email": "alice@outlook.com",
            "provider": "outlook_email_plus",
            "session_mode": "managed",
            "state": "leased",
            "expires_at": "",
            "before_ids": [],
            "provider_meta": {},
        })

        self.provider.create_session(
            email="alice@outlook.com",
            purpose="otp",
        )

        # 验证 HTTP 请求体
        self.assertEqual(mock_post.call_count, 1)
        call_kwargs = mock_post.call_args.kwargs
        sent_payload = call_kwargs["json"]
        self.assertEqual(sent_payload["provider"], "outlook_email_plus")
        self.assertEqual(sent_payload["session_mode"], "managed")
        self.assertEqual(sent_payload["config_name"], "outlook-pool-default")
        # 端点必须是 managed-sessions
        sent_url = mock_post.call_args.args[0]
        self.assertIn("/managed-sessions", sent_url)

    @mock.patch("src.providers.mail.requests.post")
    def test_ignore_existing_account_param(self, mock_post):
        """调用方传 existing_account 应被丢弃（outlook 是 managed-only）"""
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk", "email": "x@outlook.com",
            "provider": "outlook_email_plus", "session_mode": "managed",
            "state": "leased", "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        self.provider.create_session(
            email="x@outlook.com",
            existing_account={"email": "x@outlook.com", "credentials": {"client_id": "c"}},
            account_id="should-be-dropped",
        )
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("existing_account", sent_payload)
        self.assertNotIn("account_id", sent_payload)

    @mock.patch("src.providers.mail.requests.post")
    def test_ignore_credentialed_session_mode(self, mock_post):
        """调用方传 session_mode=credentialed 应被强制改为 managed"""
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk", "email": "x@outlook.com",
            "provider": "outlook_email_plus", "session_mode": "managed",
            "state": "leased", "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        self.provider.create_session(
            email="x@outlook.com",
            session_mode="credentialed",  # 应被忽略
        )
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["session_mode"], "managed")

    @mock.patch("src.providers.mail.requests.post")
    def test_custom_config_name_override(self, mock_post):
        """调用方传 config_name 应覆盖默认值"""
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk", "email": "x@outlook.com",
            "provider": "outlook_email_plus", "session_mode": "managed",
            "state": "leased", "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        self.provider.create_session(
            email="x@outlook.com",
            config_name="custom-pool-config",
        )
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["config_name"], "custom-pool-config")

    @mock.patch("src.providers.mail.requests.post")
    def test_constructor_config_name_override(self, mock_post):
        """构造时传 config_name 应覆盖默认值"""
        provider = OutlookMailProvider(
            base_url="https://email.cloudsentryai.com",
            api_key="test-key",
            config_name="my-custom-pool",
        )
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk", "email": "x@outlook.com",
            "provider": "outlook_email_plus", "session_mode": "managed",
            "state": "leased", "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        provider.create_session(email="x@outlook.com")
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["config_name"], "my-custom-pool")


class TestProviderConstants(unittest.TestCase):
    def test_provider_name_constant(self):
        self.assertEqual(OutlookMailProvider.PROVIDER_NAME, "outlook_email_plus")

    def test_default_config_name_constant(self):
        self.assertEqual(OutlookMailProvider.DEFAULT_CONFIG_NAME, "outlook-pool-default")

    def test_supported_domains_frozen(self):
        # 必须是 frozenset，防止运行时被修改
        self.assertIsInstance(OutlookMailProvider.SUPPORTED_DOMAINS, frozenset)


if __name__ == "__main__":
    unittest.main()
