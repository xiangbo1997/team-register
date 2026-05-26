# -*- coding: utf-8 -*-
"""schemas/ 包独立单测。

每个 (plan, version) 至少 3 个测试：
  - 最小 payload 必填字段都在
  - 字段值正确（防退化到旧版）
  - extra_payload 合并行为
"""
import unittest

from src.payment_link.schemas import get_schema, list_registered, register
from src.payment_link.url_postprocess import apply_locale


class TestRegistry(unittest.TestCase):
    def test_required_schemas_registered(self):
        registered = dict.fromkeys(list_registered())
        self.assertIn(("plus", "v2"), registered)
        self.assertIn(("team", "v1"), registered)
        self.assertIn(("pro", "v1"), registered)
        self.assertIn(("pro_lite", "v1"), registered)

    def test_get_schema_returns_callable(self):
        self.assertTrue(callable(get_schema("plus", "v2")))

    def test_get_schema_unknown_raises(self):
        with self.assertRaises(ValueError) as ctx:
            get_schema("plus", "v999")
        self.assertIn("未注册", str(ctx.exception))
        self.assertIn("plus/v2", str(ctx.exception))  # 错误信息列出可用版本

    def test_double_register_raises(self):
        with self.assertRaises(RuntimeError):
            @register("plus", "v2")
            def _dup(**_):
                return {}


class TestPlusV2Schema(unittest.TestCase):
    def setUp(self):
        self.build = get_schema("plus", "v2")

    def test_minimal_payload_has_plan_name(self):
        payload = self.build(billing_country="US", billing_currency="USD")
        self.assertEqual(payload["plan_name"], "chatgptplusplan")
        self.assertNotIn("plan_type", payload)  # 防退化到 v1

    def test_promo_campaign_contains_id_and_is_coupon(self):
        """v2.1: is_coupon_from_query_param 嵌在 promo_campaign 内（对齐用户成功 cURL）。"""
        payload = self.build(billing_country="US", billing_currency="USD")
        self.assertEqual(payload["promo_campaign"], {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        })

    def test_is_coupon_from_query_param_nested_in_promo_campaign(self):
        """v2.1 关键：is_coupon_from_query_param 在 promo_campaign 内，不在顶层。

        v2 旧版把它放顶层（错误假设「反作弊更诚实」），导致 PayPal 消失。
        实战 cURL 实测 OpenAI 期望嵌套结构。
        """
        payload = self.build(billing_country="US", billing_currency="USD")
        # 关键：顶层不能有
        self.assertNotIn("is_coupon_from_query_param", payload)
        # 关键：嵌入 promo_campaign 内 = False
        self.assertIn("is_coupon_from_query_param", payload["promo_campaign"])
        self.assertEqual(payload["promo_campaign"]["is_coupon_from_query_param"], False)

    def test_payload_does_not_include_success_url(self):
        """v2.2 反退化护栏：不发 success_url（PayPal Auto Filler 工作脚本不带）。"""
        payload = self.build(billing_country="US", billing_currency="USD")
        self.assertNotIn("success_url", payload)

    def test_payload_includes_cancel_url_pricing_anchor(self):
        """v2.2: cancel_url=https://chatgpt.com/#pricing
        参考工作脚本 paypal-auto-filler-*.user.js:1824"""
        payload = self.build(billing_country="US", billing_currency="USD")
        self.assertEqual(payload["cancel_url"], "https://chatgpt.com/#pricing")

    def test_billing_details_required(self):
        payload = self.build()  # 即使没传 country/currency，也要回填默认 SG/SGD
        self.assertIn("billing_details", payload)
        self.assertEqual(payload["billing_details"]["country"], "SG")
        self.assertEqual(payload["billing_details"]["currency"], "SGD")

    def test_promo_code_is_ignored(self):
        """Plus 永远不在 payload 里带 promo_code（防 v3 回归 bug）。"""
        payload = self.build(
            billing_country="US", billing_currency="USD",
            promo_code="datroaiuk",
        )
        self.assertNotIn("promo_code", payload)

    def test_extra_payload_merges(self):
        payload = self.build(
            billing_country="US", billing_currency="USD",
            extra_payload={"locale": "en", "custom_flag": True},
        )
        self.assertEqual(payload["locale"], "en")
        self.assertEqual(payload["custom_flag"], True)
        # 原有字段仍在
        self.assertEqual(payload["plan_name"], "chatgptplusplan")

    def test_explicit_promo_campaign_id_overrides_default(self):
        payload = self.build(
            billing_country="US", billing_currency="USD",
            promo_campaign_id="custom-plus-promo",
        )
        self.assertEqual(payload["promo_campaign"]["promo_campaign_id"], "custom-plus-promo")

    def test_default_checkout_ui_mode_is_hosted(self):
        payload = self.build(billing_country="US", billing_currency="USD")
        self.assertEqual(payload["checkout_ui_mode"], "hosted")

    def test_explicit_checkout_ui_mode_custom(self):
        """Plus 可显式切到 custom 模式（半价 promo 从 pricing modal 弹用）"""
        payload = self.build(
            billing_country="US", billing_currency="USD",
            checkout_ui_mode="custom",
        )
        self.assertEqual(payload["checkout_ui_mode"], "custom")

    def test_invalid_checkout_ui_mode_falls_back_to_hosted(self):
        """非法 ui_mode 安全 fallback 到 hosted，不给 OpenAI 发未知值"""
        payload = self.build(
            billing_country="US", billing_currency="USD",
            checkout_ui_mode="bogus",
        )
        self.assertEqual(payload["checkout_ui_mode"], "hosted")


class TestTeamV1Schema(unittest.TestCase):
    def setUp(self):
        self.build = get_schema("team", "v1")

    def test_plan_name_correct(self):
        payload = self.build()
        self.assertEqual(payload["plan_name"], "chatgptteamplan")

    def test_team_plan_data_required(self):
        payload = self.build(workspace_name="ACME", seat_quantity=10, price_interval="month")
        self.assertEqual(payload["team_plan_data"], {
            "workspace_name": "ACME",
            "price_interval": "month",
            "seat_quantity": 10,
        })

    def test_no_promo_code_uses_custom_mode_with_campaign(self):
        payload = self.build()
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertIn("promo_campaign", payload)
        self.assertEqual(payload["promo_campaign"]["promo_campaign_id"], "team-1-month-free")
        # Team 的 is_coupon_from_query_param 嵌在 promo_campaign 内 = True
        self.assertEqual(payload["promo_campaign"]["is_coupon_from_query_param"], True)

    def test_with_promo_code_uses_hosted_mode_no_campaign(self):
        payload = self.build(promo_code="datroaiuk")
        self.assertEqual(payload["checkout_ui_mode"], "hosted")
        self.assertEqual(payload["promo_code"], "datroaiuk")
        # 显式 promo_code 时不带 promo_campaign，避免双重优惠冲突
        self.assertNotIn("promo_campaign", payload)


class TestProV1Schema(unittest.TestCase):
    def test_pro_uses_plan_type(self):
        build = get_schema("pro", "v1")
        payload = build(billing_country="US", billing_currency="USD")
        # Pro 实验性 schema 仍用 plan_type（待 OpenAI 实测确认）
        self.assertEqual(payload["plan_type"], "pro")
        self.assertEqual(payload["checkout_ui_mode"], "hosted")

    def test_pro_lite_uses_plan_type(self):
        build = get_schema("pro_lite", "v1")
        payload = build()
        self.assertEqual(payload["plan_type"], "pro_lite")


class TestApplyLocale(unittest.TestCase):
    def test_locale_appended_to_url(self):
        url = "https://pay.openai.com/c/pay/cs_live_abc"
        out = apply_locale(url, "en")
        self.assertIn("locale=en", out)

    def test_locale_replaces_existing_query(self):
        url = "https://pay.openai.com/c/pay/cs_live_abc?foo=bar"
        out = apply_locale(url, "ja")
        self.assertIn("locale=ja", out)
        self.assertIn("foo=bar", out)

    def test_none_locale_noop(self):
        url = "https://pay.openai.com/c/pay/cs_live_abc"
        self.assertEqual(apply_locale(url, None), url)
        self.assertEqual(apply_locale(url, ""), url)

    def test_empty_url_noop(self):
        self.assertEqual(apply_locale("", "en"), "")


if __name__ == "__main__":
    unittest.main()
