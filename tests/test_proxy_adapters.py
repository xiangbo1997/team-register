# -*- coding: utf-8 -*-
"""Proxy1024Adapter + GenericHttpAdapter 单元测试。

覆盖：
  - URL 渲染（含 country_map 映射、session 每次 UUID 不同）
  - 鉴权 kwargs 构造（api_key/basic_auth/none）
  - fetch_one 模板方法：mock requests.get + _lookup_proxy_country
  - registry get_adapter 工厂分发 / 未知 kind 抛 ValueError
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.models import ProxyInfo
from src.proxy_clients.adapters.generic_http import GenericHttpAdapter
from src.proxy_clients.adapters.proxy1024 import Proxy1024Adapter
from src.proxy_clients.adapters.registry import get_adapter, list_registered_kinds


def _mk_1024_provider(**overrides):
    base = {
        "kind": "1024proxy",
        "api_url_template": (
            "https://x.com/api?region={country}&num={num}"
            "&format={format}&sess={session}"
        ),
        "country_map": {"GB": "UK"},
        "default_country": "Rand",
        "response_format": "txt_line",
        "label": "test_1024",
        "auth_kind": "ip_whitelist",
        "credentials": {"whitelisted_ip": "1.1.1.1"},
    }
    base.update(overrides)
    return base


def _mk_generic_provider(**overrides):
    base = {
        "kind": "generic_http",
        "api_url_template": "https://example.com/proxy?country={country}",
        "country_map": {},
        "default_country": "US",
        "response_format": "txt_line",
        "label": "test_generic",
        "auth_kind": "none",
        "credentials": {},
    }
    base.update(overrides)
    return base


class TestProxy1024AdapterUrlBuild(unittest.TestCase):
    def test_country_map_applied(self):
        adapter = Proxy1024Adapter()
        url = adapter.build_request_url(_mk_1024_provider(), country="GB")
        # GB → UK 经 country_map
        self.assertIn("region=UK", url)

    def test_country_not_in_map_passthrough(self):
        adapter = Proxy1024Adapter()
        url = adapter.build_request_url(_mk_1024_provider(), country="US")
        self.assertIn("region=US", url)

    def test_country_empty_falls_back_to_default(self):
        adapter = Proxy1024Adapter()
        url = adapter.build_request_url(_mk_1024_provider(), country="")
        self.assertIn("region=Rand", url)

    def test_session_differs_per_call(self):
        adapter = Proxy1024Adapter()
        provider = _mk_1024_provider()
        url1 = adapter.build_request_url(provider, country="US")
        url2 = adapter.build_request_url(provider, country="US")
        self.assertNotEqual(url1, url2)
        # 两次 URL 只有 session 段不同
        s1 = url1.split("sess=")[1]
        s2 = url2.split("sess=")[1]
        self.assertNotEqual(s1, s2)
        self.assertEqual(len(s1), 12)  # uuid.uuid4().hex[:12]

    def test_url_template_extra_args_tolerated(self):
        """模板里只用部分占位符也能工作；.format() 多余 kwargs 不报错。

        所以"缺占位符" 不会被框架拒绝；而 KeyError 仅在模板含**未声明**的占位符
        （如 {unknown}）时触发——这个测试覆盖 ValueError 路径。
        """
        adapter = Proxy1024Adapter()
        bad_provider = _mk_1024_provider(
            api_url_template="https://x.com/api?region={country}&unknown={whatever}",
        )
        with self.assertRaises(ValueError) as ctx:
            adapter.build_request_url(bad_provider, country="US")
        self.assertIn("缺少占位符", str(ctx.exception))


class TestProxy1024AdapterKwargs(unittest.TestCase):
    def test_no_headers_no_auth(self):
        adapter = Proxy1024Adapter()
        kwargs = adapter.build_request_kwargs(_mk_1024_provider())
        self.assertEqual(kwargs, {})


class TestGenericHttpAdapterUrlBuild(unittest.TestCase):
    def test_country_passthrough(self):
        adapter = GenericHttpAdapter()
        url = adapter.build_request_url(_mk_generic_provider(), country="US")
        self.assertIn("country=US", url)

    def test_country_map_when_provided(self):
        adapter = GenericHttpAdapter()
        provider = _mk_generic_provider(country_map={"GB": "uk-region"})
        url = adapter.build_request_url(provider, country="GB")
        self.assertIn("country=uk-region", url)

    def test_session_placeholder_tolerated(self):
        # generic 也允许复用 1024 风格模板（{session} 给空串不报错）
        adapter = GenericHttpAdapter()
        provider = _mk_generic_provider(
            api_url_template="https://x.com/api?c={country}&s={session}",
        )
        url = adapter.build_request_url(provider, country="US")
        self.assertIn("c=US", url)
        self.assertIn("s=", url)


class TestGenericHttpAdapterKwargs(unittest.TestCase):
    def test_none_auth(self):
        adapter = GenericHttpAdapter()
        kwargs = adapter.build_request_kwargs(
            _mk_generic_provider(auth_kind="none"),
        )
        self.assertEqual(kwargs, {})

    def test_api_key_default_header(self):
        adapter = GenericHttpAdapter()
        kwargs = adapter.build_request_kwargs(_mk_generic_provider(
            auth_kind="api_key", credentials={"key": "sk-abc"},
        ))
        self.assertEqual(kwargs, {"headers": {"X-API-Key": "sk-abc"}})

    def test_api_key_custom_header(self):
        adapter = GenericHttpAdapter()
        kwargs = adapter.build_request_kwargs(_mk_generic_provider(
            auth_kind="api_key",
            credentials={"key": "sk-xyz", "header_name": "Authorization"},
        ))
        self.assertEqual(kwargs, {"headers": {"Authorization": "sk-xyz"}})

    def test_basic_auth(self):
        adapter = GenericHttpAdapter()
        kwargs = adapter.build_request_kwargs(_mk_generic_provider(
            auth_kind="basic_auth", credentials={"username": "u", "password": "p"},
        ))
        self.assertEqual(kwargs, {"auth": ("u", "p")})


class TestFetchOneIntegration(unittest.TestCase):
    """模板方法 fetch_one：mock requests.get + _lookup_proxy_country。"""

    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_fetch_one_success(self, mock_country, mock_get):
        mock_country.return_value = "US"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "1.2.3.4:8080"
        mock_get.return_value = mock_resp

        adapter = Proxy1024Adapter()
        info = adapter.fetch_one(_mk_1024_provider(), country="US")
        self.assertIsInstance(info, ProxyInfo)
        self.assertEqual(info.host, "1.2.3.4")
        self.assertEqual(info.port, "8080")
        self.assertEqual(info.country, "US")

    @patch("src.proxy_clients.adapters.base.requests.get")
    def test_fetch_one_http_error(self, mock_get):
        import requests
        mock_get.side_effect = requests.ConnectionError("network down")

        adapter = Proxy1024Adapter()
        info = adapter.fetch_one(_mk_1024_provider(), country="US")
        self.assertIsNone(info)

    @patch("src.proxy_clients.adapters.base.requests.get")
    def test_fetch_one_non_200(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "forbidden"
        mock_get.return_value = mock_resp

        adapter = Proxy1024Adapter()
        info = adapter.fetch_one(_mk_1024_provider(), country="US")
        self.assertIsNone(info)

    @patch("src.proxy_clients.adapters.base.requests.get")
    def test_fetch_one_empty_response(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = ""
        mock_get.return_value = mock_resp

        adapter = Proxy1024Adapter()
        info = adapter.fetch_one(_mk_1024_provider(), country="US")
        self.assertIsNone(info)


class TestRegistry(unittest.TestCase):
    def test_get_adapter_known(self):
        a1 = get_adapter("1024proxy")
        self.assertIsInstance(a1, Proxy1024Adapter)
        a2 = get_adapter("generic_http")
        self.assertIsInstance(a2, GenericHttpAdapter)

    def test_get_adapter_case_insensitive(self):
        a = get_adapter("1024PROXY")
        self.assertIsInstance(a, Proxy1024Adapter)

    def test_get_adapter_unknown_raises(self):
        with self.assertRaises(ValueError) as ctx:
            get_adapter("nonexistent")
        self.assertIn("未注册", str(ctx.exception))

    def test_list_registered_kinds(self):
        kinds = list_registered_kinds()
        self.assertIn("1024proxy", kinds)
        self.assertIn("generic_http", kinds)


if __name__ == "__main__":
    unittest.main()
