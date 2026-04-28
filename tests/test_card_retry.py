# -*- coding: utf-8 -*-
"""resolve_card_with_retry 单元测试

验证 src/orchestration/handlers.py:resolve_card_with_retry 的通用化重试语义，
确保对所有 CardProvider 实现都安全（efuncard / nodecard / x988card）。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.models import CardInfo
from src.orchestration.handlers import resolve_card_with_retry


def _make_card() -> CardInfo:
    return CardInfo(
        card_number="4111111111111111",
        expiry_month="12",
        expiry_year="2030",
        cvv="123",
        last_four="1111",
    )


class TestResolveCardWithRetry(unittest.TestCase):
    def test_first_attempt_hit_no_sleep(self):
        """首次命中应直接返回，不触发 sleep"""
        card = _make_card()
        api = MagicMock()
        api.get_card.return_value = card

        with patch("src.orchestration.handlers.time.sleep") as mock_sleep:
            result = resolve_card_with_retry(api, "CDK-1")

        self.assertIs(result, card)
        self.assertEqual(api.get_card.call_count, 1)
        mock_sleep.assert_not_called()

    def test_retry_then_hit(self):
        """第一次返回 None，第二次返回 CardInfo → 应返回 CardInfo"""
        card = _make_card()
        api = MagicMock()
        api.get_card.side_effect = [None, card]

        with patch("src.orchestration.handlers.time.sleep") as mock_sleep:
            result = resolve_card_with_retry(api, "CDK-1", attempts=3, delay_sec=2)

        self.assertIs(result, card)
        self.assertEqual(api.get_card.call_count, 2)
        # 第一次失败后应 sleep 一次（2s）
        mock_sleep.assert_called_once_with(2)

    def test_all_attempts_return_none(self):
        """全部返回 None → 应返回 None，并退出循环"""
        api = MagicMock()
        api.get_card.return_value = None

        with patch("src.orchestration.handlers.time.sleep") as mock_sleep:
            result = resolve_card_with_retry(api, "CDK-1", attempts=3, delay_sec=1)

        self.assertIsNone(result)
        self.assertEqual(api.get_card.call_count, 3)
        # 应 sleep 2 次（最后一次失败后不 sleep）
        self.assertEqual(mock_sleep.call_count, 2)

    def test_exception_swallowed_and_retried(self):
        """get_card 抛异常应被吞掉并继续重试"""
        card = _make_card()
        api = MagicMock()
        api.get_card.side_effect = [RuntimeError("read timeout"), card]

        with patch("src.orchestration.handlers.time.sleep"):
            result = resolve_card_with_retry(api, "CDK-1", attempts=3, delay_sec=0)

        self.assertIs(result, card)
        self.assertEqual(api.get_card.call_count, 2)

    def test_attempts_one_no_sleep(self):
        """attempts=1 时只调一次，不 sleep"""
        api = MagicMock()
        api.get_card.return_value = None

        with patch("src.orchestration.handlers.time.sleep") as mock_sleep:
            result = resolve_card_with_retry(api, "CDK-1", attempts=1, delay_sec=5)

        self.assertIsNone(result)
        self.assertEqual(api.get_card.call_count, 1)
        mock_sleep.assert_not_called()

    def test_attempts_zero_clamped_to_one(self):
        """attempts<1 应 clamp 到 1，避免死循环或 0 调用"""
        api = MagicMock()
        api.get_card.return_value = None

        with patch("src.orchestration.handlers.time.sleep"):
            result = resolve_card_with_retry(api, "CDK-1", attempts=0)

        self.assertIsNone(result)
        self.assertEqual(api.get_card.call_count, 1)

    def test_provider_agnostic_only_get_card_required(self):
        """provider-agnostic：只需对象实现 get_card 即可（不依赖 ABC 其他方法）"""
        card = _make_card()

        class DuckProvider:
            def __init__(self):
                self.calls = 0

            def get_card(self, key):
                self.calls += 1
                return card

        api = DuckProvider()
        result = resolve_card_with_retry(api, "CDK-1")
        self.assertIs(result, card)
        self.assertEqual(api.calls, 1)

    def test_card_key_passed_through(self):
        """card_key 应原样透传给 get_card"""
        api = MagicMock()
        api.get_card.return_value = _make_card()

        resolve_card_with_retry(api, "MY-SPECIAL-CDK")
        api.get_card.assert_called_with("MY-SPECIAL-CDK")


if __name__ == "__main__":
    unittest.main()
