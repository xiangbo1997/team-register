# -*- coding: utf-8 -*-
"""PaymentLinkGenerator 单元测试"""

import unittest
from unittest.mock import patch, MagicMock

from src.payment_link import PaymentLinkGenerator


class TestPaymentLinkGenerator(unittest.TestCase):
    """支付链接生成测试"""

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_plus_prefers_long_url(self, mock_post: MagicMock):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": "https://checkout.stripe.com/c/pay/cs_live_test_123",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="plus",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://checkout.stripe.com/c/pay/cs_live_test_123")
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {
                "plan_type": "plus",
                "checkout_ui_mode": "hosted",
                "cancel_url": "https://chatgpt.com/",
                "success_url": "https://chatgpt.com/?subscribed=true",
            },
        )

    @patch("src.payment_link.requests.post")
    def test_generate_short_link_plus_converts_to_app_link(self, mock_post: MagicMock):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": "https://checkout.stripe.com/c/pay/cs_live_abc123xyz",
        }

        success, link = PaymentLinkGenerator.generate_short_link(
            "access_123",
            plan_type="plus",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://chatgpt.com/checkout/openai_llc/cs_live_abc123xyz")

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_team_uses_custom_mode(self, mock_post: MagicMock):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "checkout_session_id": "cs_test_team_123",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="team",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://chatgpt.com/checkout/openai_llc/cs_test_team_123")
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {
                "plan_name": "chatgptteamplan",
                "team_plan_data": {
                    "workspace_name": "MyTeam",
                    "price_interval": "month",
                    "seat_quantity": 5,
                },
                "promo_campaign": {
                    "promo_campaign_id": "team-1-month-free",
                    "is_coupon_from_query_param": True,
                },
                "checkout_ui_mode": "custom",
            },
        )

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_team_prefers_hosted_url_when_long_mode_requested(self, mock_post: MagicMock):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": "https://pay.openai.com/team/hosted/session_123",
            "checkout_session_id": "cs_test_team_123",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="team",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://pay.openai.com/team/hosted/session_123")

    @patch("src.payment_link.requests.post")
    def test_generate_short_link_team_returns_app_checkout_link(self, mock_post: MagicMock):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "checkout_session_id": "cs_test_team_456",
        }

        success, link = PaymentLinkGenerator.generate_short_link(
            "access_123",
            plan_type="team",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://chatgpt.com/checkout/openai_llc/cs_test_team_456")
        self.assertEqual(mock_post.call_args.kwargs["json"]["checkout_ui_mode"], "custom")

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_team_aimizy_long_mode_prefers_hosted_url(self, mock_post: MagicMock):
        mock_post.return_value.json.return_value = {
            "success": True,
            "url": "https://pay.openai.com/c/pay/cs_live_free_trial_123",
            "checkout_session_id": "cs_live_free_trial_123",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="team",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://pay.openai.com/c/pay/cs_live_free_trial_123")
        self.assertEqual(mock_post.call_args.kwargs["json"]["country"], "SG")
        self.assertEqual(mock_post.call_args.kwargs["json"]["currency"], "SGD")
        self.assertFalse(mock_post.call_args.kwargs["json"]["is_short_link"])

    def test_generate_checkout_link_rejects_unknown_plan(self):
        success, message = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="enterprise",
        )

        self.assertFalse(success)
        self.assertIn("不支持的支付计划", message)

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_retries_after_proxy_exception(self, mock_post: MagicMock):
        mock_post.side_effect = [
            RuntimeError("proxy closed"),
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"checkout_session_id": "cs_retry_ok"}),
                text="",
            ),
        ]

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="team",
            return_mode="long",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://chatgpt.com/checkout/openai_llc/cs_retry_ok")
        self.assertEqual(mock_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
