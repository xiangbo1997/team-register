# -*- coding: utf-8 -*-
"""CFWorkerMailProvider 单元测试。

验证：
  1. can_handle() 按域名匹配（zhangxb.xyz / cloudsentryai.com 命中；其他不命中）
  2. create_session() 强制注入 provider=cfworker / config_name / managed
  3. 忽略 existing_account 等 credentialed-only 参数
"""
from __future__ import annotations

import unittest
from unittest import mock

import requests

from src.providers.mail_cfworker import CFWorkerMailProvider


def _ok_response(payload: dict) -> mock.MagicMock:
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestCanHandle(unittest.TestCase):
    def test_zhangxb_xyz(self):
        self.assertTrue(CFWorkerMailProvider.can_handle("alice@zhangxb.xyz"))

    def test_cloudsentryai_com(self):
        self.assertTrue(CFWorkerMailProvider.can_handle("bob@cloudsentryai.com"))

    def test_uppercase_normalizes(self):
        self.assertTrue(CFWorkerMailProvider.can_handle("ALICE@ZHANGXB.XYZ"))

    def test_outlook_not_handled(self):
        self.assertFalse(CFWorkerMailProvider.can_handle("alice@outlook.com"))

    def test_gmail_not_handled(self):
        self.assertFalse(CFWorkerMailProvider.can_handle("alice@gmail.com"))

    def test_empty_string(self):
        self.assertFalse(CFWorkerMailProvider.can_handle(""))

    def test_no_at_sign(self):
        self.assertFalse(CFWorkerMailProvider.can_handle("no-at-sign"))


class TestCreateSession(unittest.TestCase):
    def setUp(self):
        self.provider = CFWorkerMailProvider(
            base_url="https://email.cloudsentryai.com",
            api_key="test-key",
        )

    @mock.patch("src.providers.mail.requests.post")
    def test_strict_inject_cfworker_managed(self, mock_post):
        """create_session 必须传 provider=cfworker / managed / config_name"""
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk",
            "email": "tmp@zhangxb.xyz", "provider": "cfworker",
            "session_mode": "managed", "state": "leased",
            "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        self.provider.create_session(purpose="otp")

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["provider"], "cfworker")
        self.assertEqual(sent_payload["session_mode"], "managed")
        self.assertEqual(sent_payload["config_name"], "mydomain-cfworker")
        sent_url = mock_post.call_args.args[0]
        self.assertIn("/managed-sessions", sent_url)

    @mock.patch("src.providers.mail.requests.post")
    def test_ignore_existing_account(self, mock_post):
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk",
            "email": "tmp@zhangxb.xyz", "provider": "cfworker",
            "session_mode": "managed", "state": "leased",
            "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        self.provider.create_session(
            existing_account={"email": "x@zhangxb.xyz"},
            account_id="should-drop",
        )
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("existing_account", sent_payload)
        self.assertNotIn("account_id", sent_payload)

    @mock.patch("src.providers.mail.requests.post")
    def test_constructor_config_name_override(self, mock_post):
        provider = CFWorkerMailProvider(
            base_url="https://email.cloudsentryai.com",
            api_key="key",
            config_name="my-custom-cfworker",
        )
        mock_post.return_value = _ok_response({
            "session_id": "sid", "lease_token": "tk",
            "email": "x@zhangxb.xyz", "provider": "cfworker",
            "session_mode": "managed", "state": "leased",
            "expires_at": "", "before_ids": [], "provider_meta": {},
        })

        provider.create_session()
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["config_name"], "my-custom-cfworker")


class TestProviderConstants(unittest.TestCase):
    def test_provider_name_constant(self):
        self.assertEqual(CFWorkerMailProvider.PROVIDER_NAME, "cfworker")

    def test_default_config_name_constant(self):
        self.assertEqual(CFWorkerMailProvider.DEFAULT_CONFIG_NAME, "mydomain-cfworker")

    def test_supported_domains_frozen(self):
        self.assertIsInstance(CFWorkerMailProvider.SUPPORTED_DOMAINS, frozenset)


if __name__ == "__main__":
    unittest.main()
