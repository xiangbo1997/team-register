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
        # feat: 2026-05-25 Plus 真实 schema v2 — 双 ground truth (payurl server.py + 用户 JS):
        # - plan_name=chatgptplusplan
        # - cancel_url 带 #pricing 锚点
        # - success_url=https://chatgpt.com/（用户 JS 同款，server.py 不带但加上对 Stripe 更完整）
        # - is_coupon_from_query_param 顶层 = False（用户 JS 同款，避免 OpenAI 反作弊触发）
        # - promo_campaign 内只留 promo_campaign_id
        # v2.2: 对齐 PayPal Auto Filler 工作脚本（带 cancel_url + 不带 success_url）
        self.assertEqual(
            mock_post.call_args.kwargs["json"],
            {
                "entry_point": "all_plans_pricing_modal",
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": "SG", "currency": "SGD"},
                "cancel_url": "https://chatgpt.com/#pricing",
                "promo_campaign": {
                    "promo_campaign_id": "plus-1-month-free",
                    "is_coupon_from_query_param": False,
                },
                "checkout_ui_mode": "hosted",
            },
        )

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_plus_with_promo_code_still_uses_trial(self, mock_post: MagicMock):
        """回归护栏：Plus + promo_code(datroaiuk) 时仍带 plus-1-month-free 试用 campaign，
        不带 promo_code 字段（promo_code 是 Team 折扣码语义，对 Plus 无效）。
        防止 2026-05-25 $20 付费链 bug 复发。
        """
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "url": "https://pay.openai.com/c/pay/cs_live_trial_xyz",
        }

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="plus",
            return_mode="long",
            promo_code="datroaiuk",  # 用户填了 URL 优惠码
            aimizy_country="US",
            aimizy_currency="USD",
        )

        self.assertTrue(success)
        self.assertEqual(link, "https://pay.openai.com/c/pay/cs_live_trial_xyz")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["plan_name"], "chatgptplusplan")
        self.assertNotIn("plan_type", payload)
        self.assertNotIn("promo_code", payload)
        # v2.2: 不发 success_url；带 cancel_url（参考工作脚本）
        self.assertNotIn("success_url", payload)
        self.assertEqual(payload["cancel_url"], "https://chatgpt.com/#pricing")
        # is_coupon_from_query_param 在 promo_campaign 内（不在顶层）
        self.assertNotIn("is_coupon_from_query_param", payload)
        self.assertEqual(payload["promo_campaign"], {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        })
        self.assertEqual(payload["billing_details"], {"country": "US", "currency": "USD"})

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
        # Team payload 自带 cancel_url + billing_details（_build_payload 行 377-380）
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
                "cancel_url": "https://chatgpt.com/",
                "billing_details": {"country": "SG", "currency": "SGD"},
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

    @patch("src.payment_link.requests.post")
    def test_generate_checkout_link_fails_fast_on_connection_timeout(self, mock_post: MagicMock):
        """连接层超时（curl(28)）应 fail-fast：只打一次、不重试满 3 次、返回中文提示。"""
        mock_post.side_effect = RuntimeError(
            "Failed to perform, curl: (28) Connection timed out after 20000 milliseconds"
        )

        success, link = PaymentLinkGenerator.generate_checkout_link(
            "access_123",
            plan_type="plus",
            return_mode="long",
        )

        self.assertFalse(success)
        self.assertIn("代理", link)  # 中文友好提示
        # 关键：连接超时不重试，只打一次（对比上面的偶发异常会重试到 2 次）
        self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
