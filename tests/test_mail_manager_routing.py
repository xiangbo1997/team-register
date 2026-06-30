# -*- coding: utf-8 -*-
"""MailManager 按邮箱域名路由到 provider 子类的测试。

验证：
  1. 不传 providers → 完全走旧路径（self._providers 为空列表）
  2. 传 providers → _select_provider_for_email() 按 can_handle() 路由
  3. 列表里第一个命中的 provider 被选中
  4. 都不命中 → 返回 None（调用方走旧 fallback）
  5. provider.can_handle 抛异常 → 安全跳过继续尝试下一个
  6. build_mail_providers(config) 工厂按 enabled 开关生成实例列表
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest import mock

from src.mail import MailManager, build_mail_providers
from src.providers.mail_cfworker import CFWorkerMailProvider
from src.providers.mail_outlook import OutlookMailProvider


class TestMailManagerProvidersAttr(unittest.TestCase):
    def test_no_providers_arg_means_empty_list(self):
        m = MailManager(base_url="https://x.com", api_key="k", provider_name="applemail")
        self.assertEqual(m._providers, [])

    def test_none_providers_means_empty_list(self):
        m = MailManager(base_url="https://x.com", api_key="k", providers=None)
        self.assertEqual(m._providers, [])

    def test_providers_list_preserved(self):
        o = OutlookMailProvider(base_url="https://x.com")
        c = CFWorkerMailProvider(base_url="https://x.com")
        m = MailManager(base_url="https://x.com", api_key="k", providers=[o, c])
        self.assertEqual(len(m._providers), 2)
        self.assertIs(m._providers[0], o)
        self.assertIs(m._providers[1], c)


class TestSelectProviderForEmail(unittest.TestCase):
    def setUp(self):
        self.outlook = OutlookMailProvider(base_url="https://x.com")
        self.cfworker = CFWorkerMailProvider(base_url="https://x.com")
        self.manager = MailManager(
            base_url="https://x.com",
            api_key="k",
            providers=[self.outlook, self.cfworker],
        )

    def test_hotmail_routes_to_outlook(self):
        result = self.manager._select_provider_for_email("alice@hotmail.com")
        self.assertIs(result, self.outlook)

    def test_outlook_com_routes_to_outlook(self):
        result = self.manager._select_provider_for_email("bob@outlook.com")
        self.assertIs(result, self.outlook)

    def test_zhangxb_xyz_routes_to_cfworker(self):
        result = self.manager._select_provider_for_email("alice@zhangxb.xyz")
        self.assertIs(result, self.cfworker)

    def test_cloudsentryai_com_routes_to_cfworker(self):
        result = self.manager._select_provider_for_email("alice@cloudsentryai.com")
        self.assertIs(result, self.cfworker)

    def test_gmail_no_match_returns_none(self):
        result = self.manager._select_provider_for_email("alice@gmail.com")
        self.assertIsNone(result)

    def test_empty_email_returns_none(self):
        self.assertIsNone(self.manager._select_provider_for_email(""))

    def test_no_at_sign_returns_none(self):
        self.assertIsNone(self.manager._select_provider_for_email("no-at-sign"))

    def test_no_providers_returns_none(self):
        m = MailManager(base_url="https://x.com", api_key="k")
        self.assertIsNone(m._select_provider_for_email("alice@hotmail.com"))

    def test_provider_can_handle_exception_skipped(self):
        """can_handle 抛异常应安全跳过继续下一个 provider"""
        # 构造一个会抛异常的伪 provider
        class BrokenProvider:
            @classmethod
            def can_handle(cls, email):
                raise RuntimeError("broken provider")

        broken = BrokenProvider()
        # 把 broken 放在前面，outlook 放后面，hotmail 邮箱应路由到 outlook
        m = MailManager(
            base_url="https://x.com", api_key="k",
            providers=[broken, self.outlook],
        )
        result = m._select_provider_for_email("alice@hotmail.com")
        self.assertIs(result, self.outlook)


class TestBuildMailProviders(unittest.TestCase):
    @dataclass
    class FakeConfig:
        email_provider_base_url: str = "https://email.cloudsentryai.com"
        email_provider_api_key: str = "test-key"
        outlook_enabled: bool = False
        outlook_config_name: str = "outlook-pool-default"
        cfworker_enabled: bool = False
        cfworker_config_name: str = "mydomain-cfworker"

    def test_both_disabled_returns_empty_list(self):
        providers = build_mail_providers(self.FakeConfig())
        self.assertEqual(providers, [])

    def test_outlook_only(self):
        config = self.FakeConfig(outlook_enabled=True)
        providers = build_mail_providers(config)
        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], OutlookMailProvider)

    def test_cfworker_only(self):
        config = self.FakeConfig(cfworker_enabled=True)
        providers = build_mail_providers(config)
        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], CFWorkerMailProvider)

    def test_both_enabled_outlook_first(self):
        """两个都启用时 outlook 在列表前（按 build_mail_providers 实现顺序）"""
        config = self.FakeConfig(outlook_enabled=True, cfworker_enabled=True)
        providers = build_mail_providers(config)
        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], OutlookMailProvider)
        self.assertIsInstance(providers[1], CFWorkerMailProvider)

    def test_custom_config_names_propagated(self):
        config = self.FakeConfig(
            outlook_enabled=True, outlook_config_name="my-outlook-config",
            cfworker_enabled=True, cfworker_config_name="my-cfworker-config",
        )
        providers = build_mail_providers(config)
        self.assertEqual(providers[0]._config_name, "my-outlook-config")
        self.assertEqual(providers[1]._config_name, "my-cfworker-config")

    def test_missing_attrs_uses_getattr_defaults(self):
        """旧版 AppConfig 没 outlook_enabled 字段也不应崩"""
        @dataclass
        class OldConfig:
            email_provider_base_url: str = "https://x.com"
            email_provider_api_key: str = "k"

        # OldConfig 没有 outlook_enabled / cfworker_enabled 字段
        providers = build_mail_providers(OldConfig())
        self.assertEqual(providers, [])


if __name__ == "__main__":
    unittest.main()
