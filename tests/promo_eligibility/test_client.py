# -*- coding: utf-8 -*-
"""promo_eligibility.client 单元测试

mock curl_cffi.requests.get，覆盖：
  - eligible（is_eligible=True）
  - exists（user_not_eligible）
  - not_found（invalid_code）
  - unknown（其他 reason_code）
  - 网络异常 → error
  - 401 / 403 → error
  - 响应非 JSON → error
  - access_token / code 为空 → error
  - eligible 时是否调用 metadata API
  - not_found 时是否跳过 metadata API
"""

import unittest
from unittest import mock

from src.db.models import EligibilityStatus
from src.promo_eligibility.client import EligibilityResult, check_eligibility


def _mock_response(status_code: int = 200, json_data=None, text: str = ""):
    """快速构造 mock 响应。json_data=Exception 实例时 .json() 会抛该异常。"""
    resp = mock.Mock()
    resp.status_code = status_code
    if isinstance(json_data, Exception):
        resp.json.side_effect = json_data
    else:
        resp.json.return_value = json_data
    resp.text = text
    return resp


class CheckEligibilityTests(unittest.TestCase):
    """check_eligibility 主流程"""

    def setUp(self):
        self.token = "eyJabc.def"
        self.code = "talentgeniusus"

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_eligible_status_returned_and_metadata_fetched(self, mock_get):
        """is_eligible=True → status=eligible，且会顺带拉 metadata"""
        mock_get.side_effect = [
            _mock_response(200, {"is_eligible": True, "ineligible_reason": None}),
            _mock_response(200, {"metadata": {"discount": {"value": 25}}}),
        ]

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)
        self.assertEqual(result.code, self.code)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.metadata_raw, {"metadata": {"discount": {"value": 25}}})
        self.assertEqual(mock_get.call_count, 2)  # eligibility + metadata
        # 第一次调 eligibility URL，第二次 metadata URL
        self.assertIn("/eligibility/", mock_get.call_args_list[0].args[0])
        self.assertIn("/metadata/", mock_get.call_args_list[1].args[0])

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_exists_status_when_user_not_eligible(self, mock_get):
        """user_not_eligible → status=exists，仍会拉 metadata"""
        mock_get.side_effect = [
            _mock_response(200, {
                "is_eligible": False,
                "ineligible_reason": {"code": "user_not_eligible", "message": "wrong region"},
            }),
            _mock_response(200, {"metadata": {"discount": {"value": 18}}}),
        ]

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.EXISTS)
        self.assertEqual(result.reason_code, "user_not_eligible")
        self.assertEqual(result.reason_message, "wrong region")
        self.assertIsNotNone(result.metadata_raw)

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_not_found_skips_metadata_call(self, mock_get):
        """invalid_code → status=not_found，且 NOT 调 metadata（节省请求）"""
        mock_get.return_value = _mock_response(200, {
            "is_eligible": False,
            "ineligible_reason": {"code": "invalid_code", "message": ""},
        })

        result = check_eligibility(access_token=self.token, code="nonsense")

        self.assertEqual(result.status, EligibilityStatus.NOT_FOUND)
        self.assertIsNone(result.metadata_raw)
        self.assertEqual(mock_get.call_count, 1)  # 只调一次 eligibility

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_redeemed_status_when_code_already_redeemed(self, mock_get):
        """code_already_redeemed → status=redeemed（独立于 not_found）

        ChatGPT 端区分两种 ineligible：码不存在 vs 码已被兑换。运营侧的处理
        动作不同（已兑换可能仍有效但 token 用过），所以业务上必须区分。
        """
        mock_get.return_value = _mock_response(200, {
            "is_eligible": False,
            "ineligible_reason": {"code": "code_already_redeemed", "message": "Already used"},
        })

        result = check_eligibility(access_token=self.token, code="usedcode")

        self.assertEqual(result.status, EligibilityStatus.REDEEMED)
        self.assertEqual(result.reason_code, "code_already_redeemed")
        self.assertIsNone(result.metadata_raw)  # 已兑换无需拉 metadata

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_unknown_reason_code(self, mock_get):
        """陌生 reason_code → status=unknown"""
        mock_get.return_value = _mock_response(200, {
            "is_eligible": False,
            "ineligible_reason": {"code": "weird_new_reason", "message": "?"},
        })

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.UNKNOWN)
        self.assertEqual(result.reason_code, "weird_new_reason")

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_network_exception_returns_error(self, mock_get):
        """网络异常（curl_cffi 抛异常）→ status=error"""
        mock_get.side_effect = ConnectionError("proxy timeout")

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.ERROR)
        self.assertIn("proxy timeout", result.error)

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_401_returns_error_with_token_hint(self, mock_get):
        """HTTP 401 → status=error，错误提示明确指向 token 失效"""
        mock_get.return_value = _mock_response(401, {})

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.ERROR)
        self.assertEqual(result.http_status, 401)
        self.assertIn("token", result.error.lower())

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_403_returns_error_with_cloudflare_hint(self, mock_get):
        """HTTP 403 → status=error，错误提示提到 Cloudflare/代理"""
        mock_get.return_value = _mock_response(403, {})

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.ERROR)
        self.assertEqual(result.http_status, 403)
        self.assertTrue("Cloudflare" in result.error or "代理" in result.error)

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_non_json_response_returns_error(self, mock_get):
        """响应非 JSON → status=error"""
        mock_get.return_value = _mock_response(200, ValueError("not json"), text="<html>...")

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.ERROR)
        self.assertIn("JSON", result.error)

    def test_empty_token_returns_error_without_http_call(self):
        """空 token → 直接返回 error，不发 HTTP 请求"""
        with mock.patch("src.promo_eligibility.client.requests.get") as mock_get:
            result = check_eligibility(access_token="", code=self.code)
            mock_get.assert_not_called()

        self.assertEqual(result.status, EligibilityStatus.ERROR)
        self.assertIn("access_token", result.error)

    def test_empty_code_returns_error_without_http_call(self):
        """空 code → 直接返回 error，不发 HTTP 请求"""
        with mock.patch("src.promo_eligibility.client.requests.get") as mock_get:
            result = check_eligibility(access_token=self.token, code="   ")
            mock_get.assert_not_called()

        self.assertEqual(result.status, EligibilityStatus.ERROR)
        self.assertIn("code", result.error)

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_proxy_url_passed_to_requests(self, mock_get):
        """proxy_url 不为 None 时，会作为 proxies={"http":..., "https":...} 传给 curl_cffi"""
        mock_get.return_value = _mock_response(200, {
            "is_eligible": False,
            "ineligible_reason": {"code": "invalid_code", "message": ""},
        })

        check_eligibility(
            access_token=self.token,
            code=self.code,
            proxy_url="socks5h://u:p@host:1080",
        )

        kwargs = mock_get.call_args.kwargs
        self.assertEqual(
            kwargs["proxies"],
            {"http": "socks5h://u:p@host:1080", "https": "socks5h://u:p@host:1080"},
        )
        self.assertEqual(kwargs["impersonate"], "chrome120")

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_no_proxy_means_proxies_none(self, mock_get):
        """proxy_url=None → proxies=None 不传代理"""
        mock_get.return_value = _mock_response(200, {
            "is_eligible": False,
            "ineligible_reason": {"code": "invalid_code", "message": ""},
        })

        check_eligibility(access_token=self.token, code=self.code, proxy_url=None)

        self.assertIsNone(mock_get.call_args.kwargs["proxies"])

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_metadata_failure_does_not_break_main_result(self, mock_get):
        """metadata API 失败时（500/异常）主结果不受影响，只是 metadata_raw=None"""
        mock_get.side_effect = [
            _mock_response(200, {"is_eligible": True, "ineligible_reason": None}),
            _mock_response(500, {}),  # metadata 500
        ]

        result = check_eligibility(access_token=self.token, code=self.code)

        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)
        self.assertIsNone(result.metadata_raw)


class EligibilityResultDataclassTests(unittest.TestCase):
    """EligibilityResult 数据类基础"""

    def test_default_fields(self):
        r = EligibilityResult(code="xx", status=EligibilityStatus.ELIGIBLE)
        self.assertEqual(r.reason_code, "")
        self.assertEqual(r.reason_message, "")
        self.assertIsNone(r.metadata_raw)
        self.assertEqual(r.http_status, 0)
        self.assertEqual(r.error, "")


class SentinelInjectionTests(unittest.TestCase):
    """P0-Sentinel: check_eligibility 的 sentinel_provider 参数行为"""

    def setUp(self):
        self.token = "eyJabc.def"
        self.code = "talentgeniusus"

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_no_provider_means_no_sentinel_header(self, mock_get):
        """不传 sentinel_provider 时 headers 不应包含 openai-sentinel-token（向后兼容）"""
        mock_get.return_value = _mock_response(200, {"is_eligible": True, "ineligible_reason": None})
        # 第二次给 metadata
        mock_get.side_effect = [
            _mock_response(200, {"is_eligible": True, "ineligible_reason": None}),
            _mock_response(200, {"metadata": {}}),
        ]

        check_eligibility(access_token=self.token, code=self.code)

        headers_used = mock_get.call_args_list[0].kwargs["headers"]
        self.assertNotIn("openai-sentinel-token", headers_used)

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_noop_provider_does_not_inject_sentinel_header(self, mock_get):
        """NoOpProvider 等价于不传 provider（注入空 token = 不注入）"""
        from src.automation.sentinel import NoOpProvider

        mock_get.side_effect = [
            _mock_response(200, {"is_eligible": True, "ineligible_reason": None}),
            _mock_response(200, {"metadata": {}}),
        ]

        check_eligibility(
            access_token=self.token,
            code=self.code,
            sentinel_provider=NoOpProvider(),
        )

        headers_used = mock_get.call_args_list[0].kwargs["headers"]
        self.assertNotIn("openai-sentinel-token", headers_used)

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_successful_provider_injects_sentinel_header(self, mock_get):
        """provider 成功生成 token 时 eligibility 与 metadata 两次请求都应带同一个 sentinel header"""
        from src.automation.sentinel import SentinelAttempt, SentinelProvider

        class _FixedProvider(SentinelProvider):
            @property
            def provider_name(self) -> str:
                return "fixed"

            def generate(self, *, flow="authorize_continue", user_agent="", device_id="", proxy=None):
                return SentinelAttempt(
                    success=True,
                    provider_name=self.provider_name,
                    duration_ms=1,
                    token="fixed-sentinel-jwt-xyz",
                    rationale="test",
                )

        mock_get.side_effect = [
            _mock_response(200, {"is_eligible": True, "ineligible_reason": None}),
            _mock_response(200, {"metadata": {}}),
        ]

        result = check_eligibility(
            access_token=self.token,
            code=self.code,
            sentinel_provider=_FixedProvider(),
        )

        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)
        # eligibility 调用带 sentinel header
        elig_headers = mock_get.call_args_list[0].kwargs["headers"]
        self.assertEqual(elig_headers.get("openai-sentinel-token"), "fixed-sentinel-jwt-xyz")
        # metadata 调用复用同一个 sentinel header（防止单次 promo 出现 2 个不同 PoW token）
        meta_headers = mock_get.call_args_list[1].kwargs["headers"]
        self.assertEqual(meta_headers.get("openai-sentinel-token"), "fixed-sentinel-jwt-xyz")

    @mock.patch("src.promo_eligibility.client.requests.get")
    def test_provider_failure_does_not_break_main_flow(self, mock_get):
        """provider.generate 抛异常时主流程不受影响，只是不注入 header"""
        from src.automation.sentinel import SentinelProvider

        class _ExplodingProvider(SentinelProvider):
            @property
            def provider_name(self) -> str:
                return "boom"

            def generate(self, *, flow="authorize_continue", user_agent="", device_id="", proxy=None):
                raise RuntimeError("sentinel.openai.com unreachable")

        mock_get.side_effect = [
            _mock_response(200, {"is_eligible": True, "ineligible_reason": None}),
            _mock_response(200, {"metadata": {}}),
        ]

        result = check_eligibility(
            access_token=self.token,
            code=self.code,
            sentinel_provider=_ExplodingProvider(),
        )

        # 主流程没崩
        self.assertEqual(result.status, EligibilityStatus.ELIGIBLE)
        # header 里没注入（失败静默）
        headers_used = mock_get.call_args_list[0].kwargs["headers"]
        self.assertNotIn("openai-sentinel-token", headers_used)


if __name__ == "__main__":
    unittest.main()
