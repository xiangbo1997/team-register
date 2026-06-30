# -*- coding: utf-8 -*-
"""proxy_provider_service 单元测试（CRUD + test_provider mock）。

依赖：测试 DB 走 in-memory SQLite（conftest 已配置）。
test_provider 路径 mock adapter.fetch_one 避免真实网络。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.models import ProxyInfo
from src.services.proxy_provider_service import (
    create_provider,
    delete_provider,
    get_provider,
    get_supported_kinds,
    list_providers,
    probe_provider,
    update_provider,
)


def _base_create_kwargs(**overrides):
    base = {
        "label": "test_provider",
        "kind": "1024proxy",
        "api_url_template": (
            "https://white.1024proxy.com/white/api?"
            "region={country}&num={num}&time=10&format={format}&type=txt&session={session}"
        ),
        "auth_kind": "ip_whitelist",
        "credentials": {"whitelisted_ip": "127.0.0.1"},
        "response_format": "txt_line",
        "country_map": {"GB": "UK"},
        "rotation_per_n_requests": 50,
        "sticky_seconds": 600,
        "default_country": "US",
        "notes": "smoke",
    }
    base.update(overrides)
    return base


def _cleanup(label_prefix: str = "test_"):
    """删除测试 label 前缀的所有 provider，避免脏数据。"""
    for p in list_providers():
        if p["label"].startswith(label_prefix):
            delete_provider(p["id"])


class TestCreateProvider(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_basic_create_with_masked_fields(self):
        result = create_provider(**_base_create_kwargs())
        self.assertEqual(result["label"], "test_provider")
        self.assertEqual(result["kind"], "1024proxy")
        # 默认脱敏
        self.assertNotIn("api_url_template", result)
        self.assertNotIn("credentials", result)
        self.assertTrue(result["api_url_template_masked"].endswith("..."))
        self.assertEqual(
            result["credentials_masked"]["whitelisted_ip"], "127***",
        )

    def test_duplicate_label_rejected(self):
        create_provider(**_base_create_kwargs(label="test_dup"))
        with self.assertRaises(ValueError) as ctx:
            create_provider(**_base_create_kwargs(label="test_dup"))
        self.assertIn("已存在", str(ctx.exception))

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_provider(**_base_create_kwargs(kind="not_a_kind"))
        self.assertIn("kind", str(ctx.exception))

    def test_invalid_auth_kind_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_provider(**_base_create_kwargs(auth_kind="wrong"))
        self.assertIn("auth_kind", str(ctx.exception))

    def test_invalid_response_format_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_provider(**_base_create_kwargs(response_format="xml"))
        self.assertIn("response_format", str(ctx.exception))

    def test_empty_label_rejected(self):
        with self.assertRaises(ValueError):
            create_provider(**_base_create_kwargs(label="   "))

    def test_zero_rotation_rejected(self):
        with self.assertRaises(ValueError):
            create_provider(**_base_create_kwargs(rotation_per_n_requests=0))


class TestGetProvider(unittest.TestCase):
    def setUp(self):
        self.created = create_provider(**_base_create_kwargs(label="test_get"))

    def tearDown(self):
        _cleanup()

    def test_masked_by_default(self):
        result = get_provider(self.created["id"])
        self.assertNotIn("api_url_template", result)
        self.assertNotIn("credentials", result)

    def test_with_secrets_returns_full(self):
        result = get_provider(self.created["id"], with_secrets=True)
        self.assertIn("api_url_template", result)
        self.assertIn("credentials", result)
        self.assertEqual(
            result["credentials"], {"whitelisted_ip": "127.0.0.1"},
        )

    def test_nonexistent_returns_none(self):
        self.assertIsNone(get_provider(999999))


class TestListProviders(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_list_all(self):
        create_provider(**_base_create_kwargs(label="test_a"))
        create_provider(**_base_create_kwargs(label="test_b"))
        rows = list_providers()
        labels = [r["label"] for r in rows if r["label"].startswith("test_")]
        self.assertIn("test_a", labels)
        self.assertIn("test_b", labels)
        # 排序按 label
        self.assertEqual(sorted(labels), labels)

    def test_exclude_inactive(self):
        create_provider(**_base_create_kwargs(label="test_active"))
        create_provider(**_base_create_kwargs(label="test_inactive", is_active=False))
        active_labels = [
            r["label"]
            for r in list_providers(include_inactive=False)
            if r["label"].startswith("test_")
        ]
        self.assertIn("test_active", active_labels)
        self.assertNotIn("test_inactive", active_labels)


class TestUpdateProvider(unittest.TestCase):
    def setUp(self):
        self.p = create_provider(**_base_create_kwargs(label="test_update"))

    def tearDown(self):
        _cleanup()

    def test_update_rotation(self):
        result = update_provider(self.p["id"], rotation_per_n_requests=100)
        self.assertEqual(result["rotation_per_n_requests"], 100)

    def test_update_credentials(self):
        update_provider(
            self.p["id"],
            credentials={"whitelisted_ip": "192.168.1.1"},
        )
        full = get_provider(self.p["id"], with_secrets=True)
        self.assertEqual(full["credentials"]["whitelisted_ip"], "192.168.1.1")

    def test_update_nonexistent_returns_none(self):
        self.assertIsNone(update_provider(999999, is_active=False))

    def test_update_invalid_auth_kind_raises(self):
        with self.assertRaises(ValueError):
            update_provider(self.p["id"], auth_kind="invalid")


class TestDeleteProvider(unittest.TestCase):
    def test_delete_existing(self):
        p = create_provider(**_base_create_kwargs(label="test_delete"))
        ok, msg = delete_provider(p["id"])
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.assertIsNone(get_provider(p["id"]))

    def test_delete_nonexistent(self):
        ok, msg = delete_provider(999999)
        self.assertFalse(ok)
        self.assertIn("不存在", msg)


class TestProbeProvider(unittest.TestCase):
    def setUp(self):
        self.p = create_provider(**_base_create_kwargs(
            label="test_conn", default_country="US",
        ))

    def tearDown(self):
        _cleanup()

    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_success(self, mock_country, mock_get):
        from unittest.mock import MagicMock
        mock_country.return_value = "US"
        mock_get.return_value = MagicMock(status_code=200, text="1.2.3.4:8080")

        result = probe_provider(self.p["id"])
        self.assertTrue(result["success"])
        self.assertEqual(result["ip"], "1.2.3.4:8080")
        self.assertEqual(result["country"], "US")
        self.assertTrue(result["country_match"])
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(result["error"], "")

    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_country_mismatch(self, mock_country, mock_get):
        from unittest.mock import MagicMock
        mock_country.return_value = "JP"  # 期望 US 拿到 JP
        mock_get.return_value = MagicMock(status_code=200, text="5.6.7.8:9090")

        result = probe_provider(self.p["id"])
        self.assertTrue(result["success"])
        self.assertEqual(result["country"], "JP")
        self.assertFalse(result["country_match"])

    @patch("src.proxy_clients.adapters.base.requests.get")
    def test_adapter_returns_none(self, mock_get):
        from unittest.mock import MagicMock
        mock_get.return_value = MagicMock(status_code=500, text="oops")

        result = probe_provider(self.p["id"])
        self.assertFalse(result["success"])
        self.assertEqual(result["ip"], "")
        self.assertIn("None", result["error"])

    def test_nonexistent_provider(self):
        result = probe_provider(999999)
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["error"])


class TestSupportedKinds(unittest.TestCase):
    def test_includes_known_kinds(self):
        kinds = get_supported_kinds()
        kind_names = [k["kind"] for k in kinds]
        self.assertIn("1024proxy", kind_names)
        self.assertIn("generic_http", kind_names)

    def test_each_kind_has_required_fields(self):
        for k in get_supported_kinds():
            self.assertIn("kind", k)
            self.assertIn("label", k)
            self.assertIn("auth_kinds", k)
            self.assertIn("placeholders", k)


if __name__ == "__main__":
    unittest.main()
