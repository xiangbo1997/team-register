# -*- coding: utf-8 -*-
"""BrowserBorrower 测试

覆盖：
- BORROW_HEADER_NAMES / BORROW_COOKIE_NAMES 白名单过滤
- from_dict 入口（不依赖 Playwright）
- has_critical / is_usable
- build_request_kwargs 合并语义
- payment_link 集成（borrow_headers + borrow_cookies 透传）
"""

import unittest
from unittest.mock import MagicMock, patch

from src.automation.browser_borrow import (
    BORROW_COOKIE_NAMES,
    BORROW_HEADER_NAMES,
    BorrowSnapshot,
    BrowserBorrower,
)


class WhitelistTests(unittest.TestCase):
    def test_critical_header_names_present(self):
        # 反爬关键的 4 个 header 必须在白名单
        for name in ("x-oai-is", "oai-device-id", "oai-session-id", "user-agent"):
            self.assertIn(name, BORROW_HEADER_NAMES)

    def test_critical_cookie_names_present(self):
        # Cloudflare + session 两个最关键的必须在白名单
        for name in ("cf_clearance", "__Secure-next-auth.session-token"):
            self.assertIn(name, BORROW_COOKIE_NAMES)


class FromDictTests(unittest.TestCase):
    def test_unknown_headers_filtered_out(self):
        b = BrowserBorrower.from_dict(
            headers={"x-oai-is": "ois1.abc", "random-noise": "junk", "oai-device-id": "did-1"},
            cookies={},
        )
        self.assertIn("x-oai-is", b.snapshot.headers)
        self.assertIn("oai-device-id", b.snapshot.headers)
        self.assertNotIn("random-noise", b.snapshot.headers)

    def test_unknown_cookies_filtered_out(self):
        b = BrowserBorrower.from_dict(
            headers={},
            cookies={
                "cf_clearance": "cfc",
                "random_cookie": "junk",
                "__Secure-next-auth.session-token": "st",
            },
        )
        self.assertIn("cf_clearance", b.snapshot.cookies)
        self.assertIn("__Secure-next-auth.session-token", b.snapshot.cookies)
        self.assertNotIn("random_cookie", b.snapshot.cookies)

    def test_empty_values_filtered_out(self):
        b = BrowserBorrower.from_dict(
            headers={"x-oai-is": "", "user-agent": "UA"},
            cookies={"cf_clearance": "", "oai-did": "did-1"},
        )
        self.assertNotIn("x-oai-is", b.snapshot.headers)
        self.assertIn("user-agent", b.snapshot.headers)
        self.assertNotIn("cf_clearance", b.snapshot.cookies)
        self.assertIn("oai-did", b.snapshot.cookies)

    def test_header_names_lowercased(self):
        b = BrowserBorrower.from_dict(
            headers={"X-OAI-IS": "abc", "OAI-Device-Id": "did-1"},
            cookies={},
        )
        self.assertIn("x-oai-is", b.snapshot.headers)
        self.assertIn("oai-device-id", b.snapshot.headers)

    def test_source_url_recorded(self):
        b = BrowserBorrower.from_dict(headers={}, cookies={}, source_url="https://chatgpt.com/")
        self.assertEqual(b.snapshot.source_url, "https://chatgpt.com/")

    def test_captured_at_ms_set(self):
        b = BrowserBorrower.from_dict(headers={}, cookies={})
        self.assertGreater(b.snapshot.captured_at_ms, 0)


class CriticalCheckTests(unittest.TestCase):
    def test_has_critical_with_cf_clearance(self):
        b = BrowserBorrower.from_dict(headers={}, cookies={"cf_clearance": "cfc"})
        self.assertTrue(b.snapshot.has_critical())
        self.assertTrue(b.is_usable())

    def test_has_critical_with_session_token(self):
        b = BrowserBorrower.from_dict(
            headers={}, cookies={"__Secure-next-auth.session-token": "st"}
        )
        self.assertTrue(b.snapshot.has_critical())

    def test_no_critical_when_only_cosmetic(self):
        # 只有 device-id / session-id 不构成"已通过 Cloudflare"，is_usable=False
        b = BrowserBorrower.from_dict(
            headers={"x-oai-is": "abc"},
            cookies={"oai-did": "did", "oai-sc": "sc"},
        )
        self.assertFalse(b.snapshot.has_critical())
        self.assertFalse(b.is_usable())


class BuildRequestKwargsTests(unittest.TestCase):
    def test_kwargs_contain_headers_and_cookies(self):
        b = BrowserBorrower.from_dict(
            headers={"x-oai-is": "abc"},
            cookies={"cf_clearance": "cfc"},
        )
        kwargs = b.build_request_kwargs(access_token="tok-1")
        self.assertEqual(kwargs["headers"]["x-oai-is"], "abc")
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer tok-1")
        self.assertEqual(kwargs["cookies"]["cf_clearance"], "cfc")

    def test_extra_headers_override_borrow(self):
        b = BrowserBorrower.from_dict(
            headers={"user-agent": "BorrowUA"},
            cookies={},
        )
        kwargs = b.build_request_kwargs(extra_headers={"User-Agent": "ExtraUA"})
        self.assertEqual(kwargs["headers"]["user-agent"], "ExtraUA")

    def test_empty_snapshot_returns_empty_kwargs(self):
        b = BrowserBorrower(BorrowSnapshot())
        kwargs = b.build_request_kwargs()
        self.assertEqual(kwargs["headers"], {})
        self.assertEqual(kwargs["cookies"], {})


# ---------------------------------------------------------------------------
# payment_link 集成
# ---------------------------------------------------------------------------

class PaymentLinkBorrowIntegrationTests(unittest.TestCase):
    """验证 payment_link 接收 borrow_headers + borrow_cookies 并正确透传给 curl_cffi"""

    @patch("src.payment_link.requests.post")
    def test_borrow_headers_merged_into_request(self, mock_post):
        from src.payment_link import PaymentLinkGenerator

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"url": "https://stripe.com/checkout/xyz"}

        ok, _link = PaymentLinkGenerator.generate_checkout_link(
            "tok-1",
            plan_type="plus",
            return_mode="long",
            borrow_headers={
                "x-oai-is": "ois1.JWE-fake",
                "oai-device-id": "did-fake",
                "user-agent": "Mozilla/5.0 Chrome/142",
            },
        )

        sent_headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(sent_headers.get("x-oai-is"), "ois1.JWE-fake")
        self.assertEqual(sent_headers.get("oai-device-id"), "did-fake")
        # User-Agent 被 borrow 覆盖（注意：borrow 不区分大小写，会覆盖默认的 "User-Agent" 但 dict key 不同会并存）
        # 这里只验证 borrow 的值确实在 headers 里
        self.assertTrue(
            "Mozilla/5.0 Chrome/142" in str(sent_headers.values()),
            f"borrow user-agent should appear, got {sent_headers}",
        )

    @patch("src.payment_link.requests.post")
    def test_borrow_cookies_passed_to_requests(self, mock_post):
        from src.payment_link import PaymentLinkGenerator

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"url": "https://stripe.com/checkout/xyz"}

        PaymentLinkGenerator.generate_checkout_link(
            "tok-1",
            plan_type="plus",
            return_mode="long",
            borrow_cookies={
                "cf_clearance": "cfc-real",
                "__Secure-next-auth.session-token": "session-real",
            },
        )
        sent_cookies = mock_post.call_args.kwargs.get("cookies")
        self.assertIsNotNone(sent_cookies, "cookies kwarg should be set when borrow_cookies provided")
        self.assertEqual(sent_cookies.get("cf_clearance"), "cfc-real")

    @patch("src.payment_link.requests.post")
    def test_no_borrow_means_no_cookies_kwarg(self, mock_post):
        """不传 borrow_cookies 时 cookies kwarg=None，向后兼容现有调用方"""
        from src.payment_link import PaymentLinkGenerator

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"url": "https://stripe.com/checkout/xyz"}

        PaymentLinkGenerator.generate_checkout_link("tok-1", plan_type="plus", return_mode="long")
        # cookies kwarg 应该是 None（curl_cffi 默认）
        self.assertIsNone(mock_post.call_args.kwargs.get("cookies"))


# ---------------------------------------------------------------------------
# promo_eligibility 集成
# ---------------------------------------------------------------------------

class PromoEligibilityBorrowIntegrationTests(unittest.TestCase):
    @patch("src.promo_eligibility.client.requests.get")
    def test_borrow_headers_and_cookies_passed(self, mock_get):
        from src.promo_eligibility.client import check_eligibility

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"is_eligible": True, "ineligible_reason": None}
        resp.text = ""
        # eligibility + metadata 两次 GET
        mock_get.side_effect = [resp, resp]

        check_eligibility(
            access_token="tok-1",
            code="testcode",
            borrow_headers={"x-oai-is": "ois1.JWE", "oai-device-id": "did"},
            borrow_cookies={"cf_clearance": "cfc"},
        )

        # 第一次（eligibility）必须带上借来的 header + cookie
        first_call = mock_get.call_args_list[0]
        sent_headers = first_call.kwargs["headers"]
        sent_cookies = first_call.kwargs.get("cookies")
        self.assertEqual(sent_headers.get("x-oai-is"), "ois1.JWE")
        self.assertEqual(sent_headers.get("oai-device-id"), "did")
        self.assertIsNotNone(sent_cookies)
        self.assertEqual(sent_cookies.get("cf_clearance"), "cfc")

        # 第二次（metadata）也带上同样的 cookies
        second_call = mock_get.call_args_list[1]
        second_cookies = second_call.kwargs.get("cookies")
        self.assertIsNotNone(second_cookies)
        self.assertEqual(second_cookies.get("cf_clearance"), "cfc")


if __name__ == "__main__":
    unittest.main()
