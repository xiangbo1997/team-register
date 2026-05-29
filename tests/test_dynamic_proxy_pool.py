# -*- coding: utf-8 -*-
"""DynamicProxyPool 单元测试。

覆盖：
  - 首次 next_proxy_url 触发 _rotate
  - rotation_per_n_requests 阈值达到时自动轮换
  - force_rotate("403") 计数 + 立即换 IP
  - stats 字段
  - fetch_one 失败时保留旧 IP（不抛异常）
  - 跨国不同实例独立
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.models import ProxyInfo
from src.proxy_clients.dynamic_pool import DynamicProxyPool


def _mk_provider(**overrides):
    base = {
        "kind": "1024proxy",
        "label": "test",
        "api_url_template": "https://x.com/?c={country}&s={session}&n={num}&f={format}",
        "country_map": {},
        "default_country": "Rand",
        "response_format": "txt_line",
        "auth_kind": "ip_whitelist",
        "credentials": {},
        "rotation_per_n_requests": 3,  # 测试用小值
        "sticky_seconds": 600,
        "is_active": True,
    }
    base.update(overrides)
    return base


class TestDynamicProxyPool(unittest.TestCase):
    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_first_call_triggers_rotation(self, mock_country, mock_get):
        mock_country.return_value = "US"
        from unittest.mock import MagicMock
        resp = MagicMock(status_code=200, text="1.2.3.4:8080")
        mock_get.return_value = resp

        pool = DynamicProxyPool(provider=_mk_provider(), country="US")
        self.assertIsNone(pool._current_proxy_info)

        url, country = pool.next_proxy_url()
        self.assertEqual(url, "http://1.2.3.4:8080")
        self.assertEqual(country, "US")
        self.assertEqual(pool.stats()["total_rotations"], 1)
        self.assertEqual(pool.stats()["current_ip_used_count"], 1)

    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_threshold_triggers_rotation(self, mock_country, mock_get):
        from unittest.mock import MagicMock
        mock_country.return_value = "US"
        # 模拟连续 4 次拉 IP 返回不同
        resps = [
            MagicMock(status_code=200, text=f"1.1.1.{i}:80")
            for i in range(1, 5)
        ]
        mock_get.side_effect = resps

        pool = DynamicProxyPool(
            provider=_mk_provider(rotation_per_n_requests=3),
            country="US",
        )
        # 调 6 次 → 2 次轮换（第 1 次 + 第 4 次）
        urls = [pool.next_proxy_url()[0] for _ in range(6)]
        self.assertEqual(urls[0], "http://1.1.1.1:80")
        self.assertEqual(urls[1], "http://1.1.1.1:80")  # 同 IP
        self.assertEqual(urls[2], "http://1.1.1.1:80")
        self.assertEqual(urls[3], "http://1.1.1.2:80")  # 阈值到，换
        self.assertEqual(urls[4], "http://1.1.1.2:80")
        self.assertEqual(urls[5], "http://1.1.1.2:80")
        self.assertEqual(pool.stats()["total_rotations"], 2)

    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_force_rotate_403_counts(self, mock_country, mock_get):
        from unittest.mock import MagicMock
        mock_country.return_value = "US"
        resps = [
            MagicMock(status_code=200, text="1.1.1.1:80"),
            MagicMock(status_code=200, text="2.2.2.2:80"),
        ]
        mock_get.side_effect = resps

        pool = DynamicProxyPool(provider=_mk_provider(), country="US")
        pool.next_proxy_url()
        pool.force_rotate(reason="403")
        self.assertEqual(pool.stats()["total_rotations"], 2)
        self.assertEqual(pool.stats()["rotations_by_403"], 1)
        # 验证下一次 next_proxy_url 拿到的是新 IP
        url, _ = pool.next_proxy_url()
        self.assertEqual(url, "http://2.2.2.2:80")

    @patch("src.proxy_clients.adapters.base.requests.get")
    def test_rotation_failure_keeps_old_ip(self, mock_get):
        """fetch_one 失败时保留 _current_proxy_info，调用方按 stats 判断。"""
        from unittest.mock import MagicMock
        # 第一次成功
        resp_ok = MagicMock(status_code=200, text="1.1.1.1:80")
        # 第二次失败（500）
        resp_fail = MagicMock(status_code=500, text="oops")
        mock_get.side_effect = [resp_ok, resp_fail]

        with patch("src.browser._lookup_proxy_country", return_value="US"):
            pool = DynamicProxyPool(
                provider=_mk_provider(rotation_per_n_requests=1),
                country="US",
            )
            pool.next_proxy_url()
            self.assertEqual(pool.stats()["current_ip"], "1.1.1.1:80")
            # 触发轮换但失败 → 保留旧 IP
            pool.next_proxy_url()
            self.assertEqual(pool.stats()["current_ip"], "1.1.1.1:80")
            self.assertTrue(pool.stats()["last_rotation_failed"])

    @patch("src.proxy_clients.adapters.base.requests.get")
    @patch("src.browser._lookup_proxy_country")
    def test_initial_failure_returns_empty(self, mock_country, mock_get):
        """首次拉失败 → next_proxy_url 返 ("", "")，调用方降级直连。"""
        from unittest.mock import MagicMock
        mock_country.return_value = ""
        mock_get.return_value = MagicMock(status_code=500, text="")

        pool = DynamicProxyPool(provider=_mk_provider(), country="US")
        url, country = pool.next_proxy_url()
        self.assertEqual(url, "")
        self.assertEqual(country, "")

    def test_unknown_kind_does_not_crash(self):
        """get_adapter ValueError → 不抛，仅标记 last_rotation_failed。"""
        pool = DynamicProxyPool(
            provider=_mk_provider(kind="nonexistent"),
            country="US",
        )
        url, country = pool.next_proxy_url()
        self.assertEqual(url, "")
        self.assertTrue(pool.stats()["last_rotation_failed"])


if __name__ == "__main__":
    unittest.main()
