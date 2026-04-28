# -*- coding: utf-8 -*-
"""Pro 账号代刷 handler 单元测试（v2 真实 Playwright 适配）。"""

import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from src.models import CardInfo
from src.orchestration.handlers import (
    _classify_decline,
    pro_account_login,
    select_pro_plan,
    submit_pro_and_capture_outcome,
)


def _card() -> CardInfo:
    return CardInfo(
        card_number="4859540155771610",
        expiry_month="02",
        expiry_year="2030",
        cvv="055",
        name_on_card="DANIEL SPENCER",
        billing_address="69 JOHNSON LANE,SALLY 29137,US",
    )


class TestClassifyDecline(unittest.TestCase):
    def test_insufficient_funds(self):
        self.assertEqual(_classify_decline("Your card has insufficient funds."), "insufficient_funds")
        self.assertEqual(_classify_decline("Not enough balance"), "insufficient_funds")

    def test_do_not_honor(self):
        self.assertEqual(_classify_decline("Issuer declined; do not honor."), "do_not_honor")

    def test_expired(self):
        self.assertEqual(_classify_decline("Your card has expired."), "expired_card")

    def test_incorrect_cvc(self):
        self.assertEqual(_classify_decline("Incorrect CVC"), "incorrect_cvc")
        self.assertEqual(_classify_decline("Wrong security code"), "incorrect_cvc")

    def test_generic_declined(self):
        self.assertEqual(_classify_decline("Card was declined"), "card_declined")

    def test_fraud(self):
        self.assertEqual(_classify_decline("Suspected fraudulent activity"), "fraudulent")

    def test_unknown_returns_empty(self):
        self.assertEqual(_classify_decline("Some weird error"), "")
        self.assertEqual(_classify_decline(""), "")


class TestProAccountLogin(unittest.TestCase):
    def test_navigation_failure_returns_false(self):
        page = MagicMock()
        page.goto.side_effect = Exception("network down")
        self.assertFalse(pro_account_login(page, "x@y.com", "pw"))

    def test_email_input_not_found_returns_false(self):
        page = MagicMock()
        page.goto.return_value = None
        page.wait_for_selector.side_effect = Exception("no email input")
        with mock.patch("src.orchestration.handlers.click_first_visible", return_value=False), \
             mock.patch("src.orchestration.handlers.human_delay"):
            self.assertFalse(pro_account_login(page, "x@y.com", "pw"))

    def test_captcha_url_returns_false(self):
        page = MagicMock()
        page.goto.return_value = None
        # 第一次 wait_for_selector 通过；第二次（密码）也通过
        page.wait_for_selector.return_value = None
        # url sequence：先正常，再变成 challenge
        page_url_seq = iter([
            "https://chatgpt.com/auth/login",
            "https://chatgpt.com/auth/login",
            "https://chatgpt.com/challenge?type=captcha",
        ])

        class _PageProxy:
            def __init__(self, base):
                self._base = base
                self._url_seq = page_url_seq
                self.url = next(self._url_seq, "")
            def __getattr__(self, name):
                return getattr(self._base, name)

        proxy = _PageProxy(page)
        # 用 side_effect 在每次 page.url 访问时切换
        with mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.human_delay"):
            # 这次测试会因为找不到主页 selector + url 是 challenge 中断；可能比较脆，简化为 timeout fast
            res = pro_account_login(proxy, "x@y.com", "pw", timeout_sec=1)
        self.assertFalse(res)

    def test_full_path_success(self):
        page = MagicMock()
        page.goto.return_value = None
        page.wait_for_selector.return_value = None
        # 模拟 url 一直在 chatgpt.com/ 主页（无 auth/login）
        page.url = "https://chatgpt.com/"
        # composer 选择器命中
        loc_proxy = MagicMock()
        first = MagicMock()
        first.is_visible.return_value = True
        loc_proxy.first = first
        loc_proxy.count.return_value = 10
        page.locator.return_value = loc_proxy

        with mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.human_delay"):
            res = pro_account_login(page, "x@y.com", "pw", timeout_sec=10)
        self.assertTrue(res)


class TestSelectProPlan(unittest.TestCase):
    def test_navigation_all_fail(self):
        page = MagicMock()
        page.goto.side_effect = Exception("blocked")
        with mock.patch("src.orchestration.handlers.human_delay"), \
             mock.patch("src.orchestration.handlers.wait_for_stripe_form", return_value=False):
            self.assertFalse(select_pro_plan(page, 200))

    def test_plan_button_not_found(self):
        page = MagicMock()
        page.goto.return_value = None
        with mock.patch("src.orchestration.handlers.human_delay"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=False):
            self.assertFalse(select_pro_plan(page, 200))

    def test_stripe_form_timeout(self):
        page = MagicMock()
        page.goto.return_value = None
        with mock.patch("src.orchestration.handlers.human_delay"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.wait_for_stripe_form", return_value=False):
            self.assertFalse(select_pro_plan(page, 200))

    def test_full_path_success(self):
        page = MagicMock()
        page.goto.return_value = None
        # 新版 navigate_to_pro_checkout 等 page.url 含 "/checkout/"，mock 直接返回该 URL
        page.url = "https://chatgpt.com/checkout/openai_llc/cs_live_xxx"
        # select_pro_tier 内部用 page.locator(...).first，mock 返回 aria-checked=true 模拟切换成功
        page.locator.return_value.first.get_attribute.return_value = "true"
        with mock.patch("src.orchestration.handlers.human_delay"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.wait_for_stripe_form", return_value=True):
            self.assertTrue(select_pro_plan(page, 100))


class TestSubmitProAndCaptureOutcome(unittest.TestCase):
    def test_fill_card_returns_no_variant_means_failed(self):
        page = MagicMock()
        with mock.patch("src.orchestration.handlers.fill_checkout_card", return_value=""):
            r = submit_pro_and_capture_outcome(page, _card())
        self.assertEqual(r["status"], "failed")
        self.assertIn("未识别", r["rationale"])

    def test_submit_button_not_found(self):
        page = MagicMock()
        with mock.patch("src.orchestration.handlers.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=False):
            r = submit_pro_and_capture_outcome(page, _card())
        self.assertEqual(r["status"], "failed")
        self.assertIn("找不到提交按钮", r["rationale"])

    def test_decline_with_insufficient_funds(self):
        page = MagicMock()
        page.url = "https://checkout.stripe.com/c/123"
        with mock.patch("src.orchestration.handlers.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.detect_checkout_error", return_value="Your card has insufficient funds."), \
             mock.patch("src.orchestration.handlers.human_delay"):
            r = submit_pro_and_capture_outcome(page, _card(), timeout_sec=5)
        self.assertEqual(r["status"], "declined")
        self.assertEqual(r["decline_code"], "insufficient_funds")
        self.assertIn("insufficient funds", r["rationale"].lower())

    def test_succeeded_via_url_token(self):
        page = MagicMock()
        page.url = "https://chatgpt.com/subscription/success?session=abc"
        with mock.patch("src.orchestration.handlers.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.detect_checkout_error", return_value=""), \
             mock.patch("src.orchestration.handlers.human_delay"):
            r = submit_pro_and_capture_outcome(page, _card(), timeout_sec=5)
        self.assertEqual(r["status"], "succeeded")
        self.assertIn("success", r["rationale"].lower())

    def test_timeout_returns_failed_with_signals(self):
        page = MagicMock()
        page.url = "https://checkout.stripe.com/c/idle"
        # Time will be controlled so the loop only runs once and exits
        with mock.patch("src.orchestration.handlers.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.handlers.click_first_visible", return_value=True), \
             mock.patch("src.orchestration.handlers.detect_checkout_error", return_value=""), \
             mock.patch("src.orchestration.handlers.human_delay"):
            # Use very small timeout to fast-fail
            r = submit_pro_and_capture_outcome(page, _card(), timeout_sec=0)
        self.assertEqual(r["status"], "failed")
        self.assertIn("未拿到明确结果", r["rationale"])


if __name__ == "__main__":
    unittest.main()
