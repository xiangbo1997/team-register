# -*- coding: utf-8 -*-
"""浏览器控制模块单元测试"""

import unittest
from unittest.mock import patch, MagicMock

from src.browser import (
    _lookup_proxy_country,
    fetch_proxy,
    get_browser_ws,
    run_preflight_checks,
)
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


class TestFetchProxyCountry(unittest.TestCase):
    """fetch_proxy() 的国家字段填充测试"""

    @patch("src.browser._lookup_proxy_country")
    @patch("src.browser.requests.get")
    def test_fetch_proxy_populates_country(
        self,
        mock_get: MagicMock,
        mock_lookup: MagicMock,
    ):
        """成功提取代理时，country 字段应由 _lookup_proxy_country 填充"""
        mock_get.return_value.text = "1.2.3.4:8080"
        mock_lookup.return_value = "US"

        result = fetch_proxy(proxy_url="http://mock-proxy")

        self.assertIsInstance(result, ProxyInfo)
        self.assertEqual(result.host, "1.2.3.4")
        self.assertEqual(result.port, "8080")
        self.assertEqual(result.country, "US")
        mock_lookup.assert_called_once_with("1.2.3.4", "8080")

    @patch("src.browser._lookup_proxy_country")
    @patch("src.browser.requests.get")
    def test_fetch_proxy_country_empty_when_lookup_fails(
        self,
        mock_get: MagicMock,
        mock_lookup: MagicMock,
    ):
        """国家查询失败时，仍返回 ProxyInfo（country=""），并记 warning 日志"""
        mock_get.return_value.text = "9.9.9.9:3128"
        mock_lookup.return_value = ""

        with self.assertLogs("src.browser", level="WARNING") as log_ctx:
            result = fetch_proxy(proxy_url="http://mock-proxy")

        self.assertIsInstance(result, ProxyInfo)
        self.assertEqual(result.host, "9.9.9.9")
        self.assertEqual(result.port, "3128")
        self.assertEqual(result.country, "")
        joined = "\n".join(log_ctx.output)
        self.assertIn("未能确认代理出口国家", joined)


class TestLookupProxyCountry(unittest.TestCase):
    """_lookup_proxy_country() 单元测试"""

    def test_lookup_proxy_country_parses_ipapi_response(self):
        """ipapi.co 正常响应，返回大写国家码"""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"country_code": "US", "ip": "1.2.3.4"}

        def fake_http_get(url: str, proxies: dict):
            self.assertEqual(url, "https://ipapi.co/json/")
            self.assertEqual(proxies.get("http"), "http://1.2.3.4:8080")
            self.assertEqual(proxies.get("https"), "http://1.2.3.4:8080")
            return fake_resp

        result = _lookup_proxy_country("1.2.3.4", "8080", http_get=fake_http_get)
        self.assertEqual(result, "US")

    def test_lookup_proxy_country_timeout_returns_empty(self):
        """requests 异常时应返回空字符串"""
        import requests as _requests

        def fake_http_get(url: str, proxies: dict):
            raise _requests.RequestException("timeout")

        with self.assertLogs("src.browser", level="WARNING"):
            result = _lookup_proxy_country("1.2.3.4", "8080", http_get=fake_http_get)
        self.assertEqual(result, "")

    def test_lookup_proxy_country_missing_field_returns_empty(self):
        """响应 JSON 缺少 country_code 字段时返回空"""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"ip": "1.2.3.4"}

        with self.assertLogs("src.browser", level="WARNING"):
            result = _lookup_proxy_country(
                "1.2.3.4",
                "8080",
                http_get=lambda url, proxies: fake_resp,
            )
        self.assertEqual(result, "")

    def test_lookup_proxy_country_lowercases_to_upper(self):
        """小写国家码应被规范化为大写"""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"country_code": "us"}

        result = _lookup_proxy_country(
            "1.2.3.4",
            "8080",
            http_get=lambda url, proxies: fake_resp,
        )
        self.assertEqual(result, "US")

    def test_lookup_proxy_country_non_200_returns_empty(self):
        """非 200 状态码应返回空"""
        fake_resp = MagicMock()
        fake_resp.status_code = 429
        fake_resp.json.return_value = {"country_code": "US"}

        with self.assertLogs("src.browser", level="WARNING"):
            result = _lookup_proxy_country(
                "1.2.3.4",
                "8080",
                http_get=lambda url, proxies: fake_resp,
            )
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
