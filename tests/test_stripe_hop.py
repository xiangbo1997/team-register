# -*- coding: utf-8 -*-
"""Stripe 二跳单元测试 + client 集成测试。

覆盖 2026-06-04 修复：OpenAI custom session（url=null）→ Stripe 二跳换 pay.openai.com 长链。
"""
import unittest
from unittest.mock import MagicMock, patch

from src.payment_link import PaymentLinkGenerator
from src.payment_link.stripe_hop import fetch_hosted_url_via_stripe


class TestFetchHostedUrlViaStripe(unittest.TestCase):
    """Stripe 二跳纯函数测试"""

    @patch("src.payment_link.stripe_hop.requests.get")
    def test_success_replaces_domain_to_openai(self, mock_get: MagicMock):
        """成功路径：拿到 stripe_hosted_url 并替换域名为 pay.openai.com。"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "management_url": "https://pay.openai.com",
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_abc#fid123",
        }

        link = fetch_hosted_url_via_stripe("cs_live_abc", "pk_live_xyz", proxy=None)

        self.assertEqual(link, "https://pay.openai.com/c/pay/cs_live_abc#fid123")
        # 确认用 publishable_key 做 Bearer 鉴权
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer pk_live_xyz"
        )
        # 确认打的是 payment_pages 端点
        self.assertIn("api.stripe.com/v1/payment_pages/cs_live_abc", mock_get.call_args.args[0])

    @patch("src.payment_link.stripe_hop.requests.get")
    def test_keep_stripe_domain_when_prefer_false(self, mock_get: MagicMock):
        """prefer_openai_domain=False 时保留 checkout.stripe.com。"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_abc#fid",
        }

        link = fetch_hosted_url_via_stripe(
            "cs_live_abc", "pk_live_xyz", prefer_openai_domain=False
        )

        self.assertEqual(link, "https://checkout.stripe.com/c/pay/cs_live_abc#fid")

    def test_missing_credentials_returns_empty(self):
        """缺 cs_id 或 pk → 空串，不发请求。"""
        self.assertEqual(fetch_hosted_url_via_stripe("", "pk_live_xyz"), "")
        self.assertEqual(fetch_hosted_url_via_stripe("cs_live_abc", ""), "")

    @patch("src.payment_link.stripe_hop.requests.get")
    def test_non_200_returns_empty(self, mock_get: MagicMock):
        """Stripe 非 200 → 空串（调用方回退站内短链）。"""
        mock_get.return_value.status_code = 400
        mock_get.return_value.text = "bad request"
        self.assertEqual(fetch_hosted_url_via_stripe("cs_live_abc", "pk_live_xyz"), "")

    @patch("src.payment_link.stripe_hop.requests.get")
    def test_missing_hosted_url_field_returns_empty(self, mock_get: MagicMock):
        """响应缺 stripe_hosted_url → 空串。"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"management_url": "https://pay.openai.com"}
        self.assertEqual(fetch_hosted_url_via_stripe("cs_live_abc", "pk_live_xyz"), "")

    @patch("src.payment_link.stripe_hop.requests.get")
    def test_request_exception_returns_empty(self, mock_get: MagicMock):
        """请求异常（代理不通等）→ 空串，不抛。"""
        mock_get.side_effect = RuntimeError("proxy down")
        self.assertEqual(fetch_hosted_url_via_stripe("cs_live_abc", "pk_live_xyz"), "")


class TestClientStripeSecondHop(unittest.TestCase):
    """client.py 二跳接入集成测试"""

    @patch("src.payment_link.stripe_hop.requests.get")
    @patch("src.payment_link.requests.post")
    def test_custom_session_null_url_triggers_second_hop(
        self, mock_post: MagicMock, mock_stripe_get: MagicMock
    ):
        """OpenAI 回 custom session（url=null）→ 触发二跳拿 pay.openai.com 长链。

        复刻 2026-06-04 实测的 OpenAI 改版响应：url=null + checkout_session_id + publishable_key。
        """
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "tag": "custom_checkout_session",
            "checkout_ui_mode": "custom",
            "url": None,  # OpenAI 改版后恒为 null
            "checkout_session_id": "cs_live_real",
            "publishable_key": "pk_live_openai",
            "client_secret": "cs_live_real_secret_fid",
        }
        # Stripe 二跳返回 hosted url
        mock_stripe_get.return_value.status_code = 200
        mock_stripe_get.return_value.json.return_value = {
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_real#fidABC",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_token",
            plan_type="plus",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://pay.openai.com/c/pay/cs_live_real#fidABC")
        # 确认二跳用了 OpenAI 给的凭证
        self.assertIn("cs_live_real", mock_stripe_get.call_args.args[0])
        self.assertEqual(
            mock_stripe_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer pk_live_openai",
        )

    @patch("src.payment_link.stripe_hop.requests.get")
    @patch("src.payment_link.requests.post")
    def test_second_hop_failure_falls_back_to_app_link(
        self, mock_post: MagicMock, mock_stripe_get: MagicMock
    ):
        """二跳失败 → 回退到站内短链，不报错。"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": None,
            "checkout_session_id": "cs_live_real",
            "publishable_key": "pk_live_openai",
        }
        mock_stripe_get.return_value.status_code = 500  # 二跳挂了
        mock_stripe_get.return_value.text = "stripe error"

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_token",
            plan_type="plus",
            return_mode="long",
        )

        self.assertTrue(success)
        # 回退到 checkout_session_id 拼的站内短链
        self.assertEqual(
            link, "https://chatgpt.com/checkout/openai_llc/cs_live_real"
        )

    @patch("src.payment_link.stripe_hop.requests.get")
    @patch("src.payment_link.requests.post")
    def test_openai_url_present_skips_second_hop(
        self, mock_post: MagicMock, mock_stripe_get: MagicMock
    ):
        """OpenAI 已给 url（旧行为）→ 不触发二跳，直接用。"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": "https://checkout.stripe.com/c/pay/cs_live_old",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_token",
            plan_type="plus",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://checkout.stripe.com/c/pay/cs_live_old")
        mock_stripe_get.assert_not_called()

    @patch("src.payment_link.stripe_hop.requests.get")
    @patch("src.payment_link.requests.post")
    def test_app_mode_skips_second_hop(
        self, mock_post: MagicMock, mock_stripe_get: MagicMock
    ):
        """return_mode=app（要短链）→ 不触发二跳。"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": None,
            "checkout_session_id": "cs_live_real",
            "publishable_key": "pk_live_openai",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_token",
            plan_type="plus",
            return_mode="app",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://chatgpt.com/checkout/openai_llc/cs_live_real")
        mock_stripe_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
