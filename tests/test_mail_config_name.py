# -*- coding: utf-8 -*-
"""验证 config_name 参数从 mail-default JSON → MailManager → HttpMailProvider → POST payload 的全链路传递。"""

import unittest
from types import SimpleNamespace
from unittest import mock

from src.providers.mail import HttpMailProvider, MailSession


class TestHttpMailProviderConfigName(unittest.TestCase):
    """HttpMailProvider.create_session 把 config_name 写进请求 payload。"""

    def _stub_response(self, body):
        m = mock.Mock()
        m.status_code = 200
        m.raise_for_status = mock.Mock()
        m.json.return_value = body
        return m

    def test_config_name_included_in_payload_when_provided(self):
        provider = HttpMailProvider(base_url="https://x", api_key="k")
        fake = self._stub_response({
            "session_id": "s-1",
            "lease_token": "lt",
            "email": "a@b.c",
        })
        with mock.patch("src.providers.mail.requests.post", return_value=fake) as p:
            provider.create_session(
                provider="cfworker",
                session_mode="managed",
                config_name="mydomain-cfworker",
            )
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["config_name"], "mydomain-cfworker")
        self.assertEqual(body["provider"], "cfworker")
        self.assertEqual(body["session_mode"], "managed")

    def test_config_name_omitted_when_empty(self):
        provider = HttpMailProvider(base_url="https://x", api_key="k")
        fake = self._stub_response({"session_id": "s-1", "lease_token": "lt", "email": ""})
        with mock.patch("src.providers.mail.requests.post", return_value=fake) as p:
            provider.create_session(provider="cfworker", session_mode="managed")
        body = p.call_args.kwargs["json"]
        self.assertNotIn("config_name", body)

    def test_managed_session_uses_managed_endpoint(self):
        provider = HttpMailProvider(base_url="https://x", api_key="k")
        fake = self._stub_response({"session_id": "s-1", "lease_token": "lt", "email": ""})
        with mock.patch("src.providers.mail.requests.post", return_value=fake) as p:
            provider.create_session(
                provider="cfworker",
                session_mode="managed",
                config_name="mydomain-cfworker",
            )
        url = p.call_args.args[0]
        self.assertTrue(url.endswith("/managed-sessions"), url)


class TestMailManagerForwardsConfigName(unittest.TestCase):
    """MailManager 把构造时的 config_name 透传给 HttpMailProvider.create_session。"""

    def test_init_stores_config_name(self):
        from src.mail import MailManager

        mgr = MailManager(
            base_url="https://x",
            api_key="k",
            provider_name="cfworker",
            config_name="mydomain-cfworker",
        )
        self.assertEqual(mgr._config_name, "mydomain-cfworker")

    def test_default_config_name_is_empty(self):
        from src.mail import MailManager

        mgr = MailManager(base_url="https://x", api_key="k", provider_name="applemail")
        self.assertEqual(mgr._config_name, "")


class TestWorkerResolvesConfigName(unittest.TestCase):
    """worker._resolve_runtime_config 从 mail-default JSON 取 config_name 写到 config.mail_config_name。"""

    def test_config_name_extracted_from_mail_payload(self):
        from src.api.worker import _resolve_runtime_config

        # 用 mock 完全替换 ConfigService，避免起真实 DB
        fake_cfg = SimpleNamespace(
            ads_api="",
            ads_api_key="",
            proxy="",
            card_provider="efuncard",
            efuncard_token="",
            nodecard_api_url="",
            nodecard_merchant_id=0,
            nodecard_platform_id=0,
            email_provider_name="applemail",
            mail_config_name="",
            mail_client_id="",
            mail_refresh_token="",
            known_mail_accounts_json="",
            default_browser_provider="",
            default_card_provider="",
            default_mail_provider="",
            default_mail_account_id="",
        )

        fake_mail_profile = SimpleNamespace(config={
            "provider_name": "cfworker",
            "session_mode": "managed",
            "mailbox": "INBOX",
            "config_name": "mydomain-cfworker",
        })

        fake_svc = mock.Mock()
        fake_svc.get_config.return_value = fake_cfg
        # browser 和 card profile 返回 None 跳过
        fake_svc.resolve_provider_config.side_effect = lambda kind, *a, **k: (
            fake_mail_profile if kind == "mail" else None
        )
        fake_svc.get_mail_account.return_value = None

        fake_run = SimpleNamespace(
            browser_provider="",
            card_provider="",
            mail_provider="",
            mail_account_id="",
            email="x@y.z",
        )

        with mock.patch("src.api.deps.get_config_service", return_value=fake_svc):
            cfg = _resolve_runtime_config(fake_run)

        self.assertEqual(cfg.email_provider_name, "cfworker")
        self.assertEqual(cfg.mail_config_name, "mydomain-cfworker")
        self.assertEqual(getattr(cfg, "mail_session_mode_override", ""), "managed")


class TestCfWorkerEmailModes(unittest.TestCase):
    """端到端验证：fixed-name 模式（用户指定）vs auto-allocate 模式（留空）。

    这两个模式在 HttpMailProvider 这一层都体现为 ``email`` 字段是否存在于 payload。
    服务端实现把 ``email`` 转发给 cfworker adapter 的 ``get_email(requested_email=...)``。
    """

    def _stub_response(self, body):
        m = mock.Mock()
        m.status_code = 200
        m.raise_for_status = mock.Mock()
        m.json.return_value = body
        return m

    def test_fixed_name_mode_sends_email_in_payload(self):
        """模式 B：传 email='wangying@gitee.shop' 时 payload 必须有 email 字段。"""
        provider = HttpMailProvider(base_url="https://x", api_key="k")
        fake = self._stub_response({
            "session_id": "s-fix",
            "lease_token": "lt",
            "email": "wangying@gitee.shop",
        })
        with mock.patch("src.providers.mail.requests.post", return_value=fake) as p:
            sess = provider.create_session(
                provider="cfworker",
                session_mode="managed",
                config_name="mydomain-cfworker",
                email="wangying@gitee.shop",
            )
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["email"], "wangying@gitee.shop")
        self.assertEqual(body["config_name"], "mydomain-cfworker")
        self.assertEqual(sess.email, "wangying@gitee.shop")

    def test_auto_allocate_mode_omits_email(self):
        """模式 A：不传 email 时 payload 不应有 email 字段（让服务端自动分配）。"""
        provider = HttpMailProvider(base_url="https://x", api_key="k")
        fake = self._stub_response({
            "session_id": "s-auto",
            "lease_token": "lt",
            "email": "tmpabc@gitee.shop",
        })
        with mock.patch("src.providers.mail.requests.post", return_value=fake) as p:
            sess = provider.create_session(
                provider="cfworker",
                session_mode="managed",
                config_name="mydomain-cfworker",
            )
        body = p.call_args.kwargs["json"]
        self.assertNotIn("email", body)
        # 服务端返回的 tmpxxx 邮箱被透传到 session.email
        self.assertEqual(sess.email, "tmpabc@gitee.shop")


if __name__ == "__main__":
    unittest.main()
