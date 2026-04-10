# -*- coding: utf-8 -*-
"""浏览器控制模块单元测试"""

import unittest
from unittest.mock import patch, MagicMock

from src.browser import fetch_proxy, get_browser_ws, run_preflight_checks
from src.models import ProxyInfo


class TestFetchProxy(unittest.TestCase):
    """fetch_proxy() 测试"""

    @patch("src.browser.requests.get")
    def test_success(self, mock_get: MagicMock):
        """正常返回 IP:Port 格式"""
        mock_get.return_value.text = "1.2.3.4:8080"

        result = fetch_proxy(proxy_url="http://mock-proxy")
        self.assertIsInstance(result, ProxyInfo)
        self.assertEqual(result.host, "1.2.3.4")
        self.assertEqual(result.port, "8080")
        self.assertEqual(str(result), "1.2.3.4:8080")

    @patch("src.browser.requests.get")
    def test_whitelist_error(self, mock_get: MagicMock):
        """白名单未添加应返回 None"""
        mock_get.return_value.text = "Your IP is not added to whitelist"

        result = fetch_proxy(proxy_url="http://mock-proxy")
        self.assertIsNone(result)

    @patch("src.browser.requests.get")
    def test_malformed_response(self, mock_get: MagicMock):
        """无冒号的异常格式应返回 None"""
        mock_get.return_value.text = "error_no_colon"

        result = fetch_proxy(proxy_url="http://mock-proxy")
        self.assertIsNone(result)

    @patch("src.browser.requests.get")
    def test_network_error(self, mock_get: MagicMock):
        """网络异常应返回 None"""
        import requests
        mock_get.side_effect = requests.RequestException("timeout")

        result = fetch_proxy(proxy_url="http://mock-proxy")
        self.assertIsNone(result)


class TestGetBrowserWs(unittest.TestCase):
    """get_browser_ws() 测试"""

    @patch("src.browser.requests.get")
    def test_start_success(self, mock_get: MagicMock):
        """正常启动返回 WebSocket URL"""
        mock_get.return_value.json.return_value = {
            "code": 0,
            "data": {
                "ws": {"puppeteer": "ws://127.0.0.1:9222/devtools/browser/abc"},
            },
        }

        ws = get_browser_ws(ads_api="http://mock-ads", user_id="profile_1")
        self.assertTrue(ws.startswith("ws://"))

    @patch("src.browser.requests.get")
    def test_start_with_proxy(self, mock_get: MagicMock):
        """带代理参数启动"""
        mock_get.return_value.json.return_value = {
            "code": 0,
            "data": {
                "ws": {"puppeteer": "ws://127.0.0.1:9222/devtools/browser/xyz"},
            },
        }
        proxy = ProxyInfo(host="5.6.7.8", port="3128")

        ws = get_browser_ws(
            ads_api="http://mock-ads",
            user_id="profile_1",
            proxy=proxy,
        )
        self.assertIn("devtools/browser", ws)

        # 验证代理参数已传入请求
        call_args = mock_get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params", {})
        self.assertEqual(params.get("proxy_host"), "5.6.7.8")
        self.assertEqual(params.get("proxy_port"), "3128")

    @patch("src.browser.requests.get")
    def test_auth_failure(self, mock_get: MagicMock):
        """鉴权失败应抛出 ConnectionError"""
        mock_get.return_value.json.return_value = {
            "code": -1,
            "msg": "Require api-key",
        }

        with self.assertRaises(ConnectionError):
            get_browser_ws(ads_api="http://mock-ads", user_id="profile_1")

    @patch("src.browser.requests.get")
    def test_start_falls_back_from_local_host_to_loopback(self, mock_get: MagicMock):
        """local.adspower.net 失败时应回退到 127.0.0.1"""
        import requests

        mock_get.side_effect = [
            requests.RequestException("dns fail"),
            MagicMock(json=MagicMock(return_value={
                "code": 0,
                "data": {"ws": {"puppeteer": "ws://127.0.0.1:9222/devtools/browser/fallback"}},
            })),
        ]

        ws = get_browser_ws(ads_api="http://local.adspower.net:50325", user_id="profile_1")

        self.assertIn("devtools/browser/fallback", ws)
        first_url = mock_get.call_args_list[0].args[0]
        second_url = mock_get.call_args_list[1].args[0]
        self.assertIn("local.adspower.net", first_url)
        self.assertIn("127.0.0.1:50325", second_url)


class TestRunPreflightChecks(unittest.TestCase):
    """run_preflight_checks() 测试"""

    @patch("src.browser.requests.get")
    def test_preflight_success(self, mock_get: MagicMock):
        ads_resp = MagicMock(status_code=200)
        target_resp = MagicMock(status_code=200)
        mock_get.side_effect = [ads_resp, target_resp]

        run_preflight_checks(
            ads_api="http://mock-ads",
            target_url="https://chatgpt.com/",
            proxy_url="socks5h://127.0.0.1:7890",
        )

        self.assertEqual(mock_get.call_count, 2)

    @patch("src.browser.time.sleep")
    @patch("src.browser.requests.get")
    def test_preflight_retries_adspower_then_succeeds(self, mock_get: MagicMock, mock_sleep: MagicMock):
        import requests

        ads_resp = MagicMock(status_code=200)
        target_resp = MagicMock(status_code=200)
        mock_get.side_effect = [
            requests.RequestException("remote closed"),
            ads_resp,
            target_resp,
        ]

        run_preflight_checks(
            ads_api="http://mock-ads",
            target_url="https://chatgpt.com/",
        )

        self.assertEqual(mock_get.call_count, 3)
        mock_sleep.assert_called_once()

    @patch("src.browser.time.sleep")
    @patch("src.browser.requests.get")
    def test_preflight_falls_back_from_local_host_to_loopback(self, mock_get: MagicMock, mock_sleep: MagicMock):
        import requests

        target_resp = MagicMock(status_code=200)
        mock_get.side_effect = [
            requests.RequestException("dns fail"),
            MagicMock(status_code=200),
            target_resp,
        ]

        run_preflight_checks(
            ads_api="http://local.adspower.net:50325",
            target_url="https://chatgpt.com/",
            ads_retries=1,
        )

        first_url = mock_get.call_args_list[0].args[0]
        second_url = mock_get.call_args_list[1].args[0]
        self.assertIn("local.adspower.net", first_url)
        self.assertIn("127.0.0.1:50325", second_url)

    @patch("src.browser.requests.get")
    def test_preflight_raises_when_target_unreachable(self, mock_get: MagicMock):
        import requests

        mock_get.side_effect = [
            MagicMock(status_code=200),
            requests.RequestException("network down"),
        ]

        with self.assertRaises(ConnectionError):
            run_preflight_checks(
                ads_api="http://mock-ads",
                target_url="https://chatgpt.com/",
            )

    @patch("src.browser.requests.get")
    def test_network_error(self, mock_get: MagicMock):
        """网络异常应抛出 ConnectionError"""
        import requests
        mock_get.side_effect = requests.RequestException("refused")

        with self.assertRaises(ConnectionError):
            get_browser_ws(ads_api="http://mock-ads", user_id="profile_1")


if __name__ == "__main__":
    unittest.main()
