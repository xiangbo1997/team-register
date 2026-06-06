# -*- coding: utf-8 -*-
"""`src/workflow/humanize` 的单元测试。

通过注入固定种子的 `random.Random(42)` 与 Mock 的 Playwright Page
对象，确保测试完全确定且不触发真实睡眠或浏览器。"""

from __future__ import annotations

import random
import unittest
from unittest.mock import MagicMock, call

from src.workflow.humanize import (
    _MAX_KEY_DELAY_MS,
    _MIN_KEY_DELAY_MS,
    _wpm_to_ms_per_char,
    bezier_path,
    click_humanized,
    sample_keystroke_delays,
    type_humanized,
)


def _make_page(box=None):
    """构造一个 Mock Page，locator 返回的 first 带可配置的 bounding_box。"""
    page = MagicMock()
    locator = MagicMock()
    first = MagicMock()
    first.bounding_box.return_value = box or {
        "x": 100,
        "y": 200,
        "width": 40,
        "height": 20,
    }
    locator.first = first
    # click_humanized 只访问 .first；type_humanized 还会直接对 locator 调 click()
    page.locator.return_value = locator
    return page


class TestBezierPath(unittest.TestCase):
    def test_returns_expected_point_count(self):
        pts = bezier_path((0, 0), (100, 100), steps=10, rng=random.Random(42))
        self.assertEqual(len(pts), 10)

    def test_first_and_last_exact(self):
        pts = bezier_path((5, 7), (123, 456), steps=30, rng=random.Random(42))
        self.assertEqual(pts[0], (5.0, 7.0))
        self.assertEqual(pts[-1], (123.0, 456.0))

    def test_minimum_steps_enforced(self):
        pts = bezier_path((0, 0), (1, 1), steps=1, rng=random.Random(42))
        self.assertEqual(len(pts), 2)

    def test_deterministic_with_seed(self):
        pts_a = bezier_path((0, 0), (200, 50), steps=20, rng=random.Random(42))
        pts_b = bezier_path((0, 0), (200, 50), steps=20, rng=random.Random(42))
        self.assertEqual(pts_a, pts_b)

    def test_different_seeds_differ(self):
        pts_a = bezier_path((0, 0), (200, 50), steps=20, rng=random.Random(1))
        pts_b = bezier_path((0, 0), (200, 50), steps=20, rng=random.Random(2))
        # 中间点必然不同（起止点已被强制对齐）
        self.assertNotEqual(pts_a[5:-5], pts_b[5:-5])

    def test_zero_distance_still_returns_path(self):
        pts = bezier_path((10, 10), (10, 10), steps=5, rng=random.Random(42))
        self.assertEqual(len(pts), 5)
        self.assertEqual(pts[0], (10.0, 10.0))
        self.assertEqual(pts[-1], (10.0, 10.0))


class TestSampleKeystrokeDelays(unittest.TestCase):
    def test_empty_when_length_zero(self):
        self.assertEqual(sample_keystroke_delays(0, rng=random.Random(42)), [])

    def test_length_matches(self):
        out = sample_keystroke_delays(50, rng=random.Random(42))
        self.assertEqual(len(out), 50)

    def test_mean_within_tolerance(self):
        out = sample_keystroke_delays(
            5000, wpm_mean=180, wpm_std=40, rng=random.Random(42)
        )
        expected = _wpm_to_ms_per_char(180)  # ≈ 66.67 ms
        avg = sum(out) / len(out)
        # 允许 ±10% 偏差（样本足够多 + 裁剪影响）
        self.assertAlmostEqual(avg, expected, delta=expected * 0.10)

    def test_values_clamped(self):
        out = sample_keystroke_delays(
            2000, wpm_mean=180, wpm_std=1000, rng=random.Random(42)
        )
        for v in out:
            self.assertGreaterEqual(v, _MIN_KEY_DELAY_MS)
            self.assertLessEqual(v, _MAX_KEY_DELAY_MS)

    def test_deterministic_with_seed(self):
        a = sample_keystroke_delays(100, rng=random.Random(42))
        b = sample_keystroke_delays(100, rng=random.Random(42))
        self.assertEqual(a, b)

    def test_zero_std_returns_constant(self):
        out = sample_keystroke_delays(10, wpm_mean=180, wpm_std=0, rng=random.Random(42))
        expected = _wpm_to_ms_per_char(180)
        self.assertTrue(all(v == expected for v in out))

    def test_rejects_non_positive_wpm(self):
        with self.assertRaises(ValueError):
            sample_keystroke_delays(5, wpm_mean=0, rng=random.Random(42))
        with self.assertRaises(ValueError):
            sample_keystroke_delays(5, wpm_mean=-1, rng=random.Random(42))

    def test_rejects_negative_std(self):
        with self.assertRaises(ValueError):
            sample_keystroke_delays(5, wpm_mean=180, wpm_std=-1, rng=random.Random(42))


class TestClickHumanized(unittest.TestCase):
    def test_moves_multiple_times_then_down_up(self):
        page = _make_page()
        sleeps = []
        click_humanized(
            page,
            "#btn",
            steps=5,
            duration_ms=0,  # 避免除零问题走默认分支，同时保证无 sleep 延迟
            rng=random.Random(42),
            sleep_fn=sleeps.append,
        )

        mouse = page.mouse
        # move 应至少被调用 steps 次（1 次起点锚定 + (steps-1) 次沿路径）
        self.assertGreaterEqual(mouse.move.call_count, 5)
        mouse.down.assert_called_once()
        mouse.up.assert_called_once()

    def test_order_move_then_down_then_up(self):
        page = _make_page()
        events = []
        page.mouse.move.side_effect = lambda x, y: events.append("move")
        page.mouse.down.side_effect = lambda: events.append("down")
        page.mouse.up.side_effect = lambda: events.append("up")

        click_humanized(
            page,
            "#btn",
            steps=4,
            duration_ms=0,
            rng=random.Random(42),
            sleep_fn=lambda _s: None,
        )
        self.assertIn("down", events)
        self.assertIn("up", events)
        # 所有 move 必须在 down 之前
        down_idx = events.index("down")
        up_idx = events.index("up")
        self.assertTrue(all(e == "move" for e in events[:down_idx]))
        self.assertLess(down_idx, up_idx)

    def test_uses_element_center_as_final_point(self):
        page = _make_page(box={"x": 10, "y": 20, "width": 40, "height": 60})
        moves = []
        page.mouse.move.side_effect = lambda x, y: moves.append((x, y))
        click_humanized(
            page,
            "#btn",
            steps=5,
            duration_ms=0,
            rng=random.Random(42),
            sleep_fn=lambda _s: None,
        )
        # 期望中心 = (10+20, 20+30) = (30, 50)
        self.assertEqual(moves[-1], (30.0, 50.0))

    def test_raises_when_bounding_box_missing(self):
        page = _make_page()
        page.locator.return_value.first.bounding_box.return_value = None
        with self.assertRaises(RuntimeError):
            click_humanized(
                page,
                "#btn",
                steps=3,
                duration_ms=0,
                rng=random.Random(42),
                sleep_fn=lambda _s: None,
            )


class TestTypeHumanized(unittest.TestCase):
    def _collect_typed(self, page) -> str:
        """聚合 keyboard.type 的所有调用字符，返回合并字符串。"""
        chunks = []
        for c in page.keyboard.type.call_args_list:
            chunks.append(c.args[0])
        return "".join(chunks)

    def test_types_exact_text_with_zero_typo_rate(self):
        page = _make_page()
        type_humanized(
            page,
            "#email",
            "hello",
            typo_rate=0.0,
            rng=random.Random(42),
            sleep_fn=lambda _s: None,
        )
        # 先聚焦：locator(selector).click()
        page.locator.assert_any_call("#email")
        page.locator.return_value.click.assert_called_once()
        # 每个字符单独 type
        self.assertEqual(self._collect_typed(page), "hello")
        # 无打错 → 无退格
        backspace_calls = [
            c for c in page.keyboard.press.call_args_list if c.args == ("Backspace",)
        ]
        self.assertEqual(backspace_calls, [])

    def test_full_typo_rate_triggers_backspace(self):
        page = _make_page()
        type_humanized(
            page,
            "#email",
            "abcde",
            typo_rate=1.0,
            rng=random.Random(42),
            sleep_fn=lambda _s: None,
        )
        backspace_calls = [
            c for c in page.keyboard.press.call_args_list if c.args == ("Backspace",)
        ]
        # typo_rate=1.0 → 每个字符都应触发一次退格
        self.assertEqual(len(backspace_calls), len("abcde"))
        typed = self._collect_typed(page)
        # 目标字符必须以子序列的方式出现（错字穿插其间，但正确字符依序输入）
        it = iter(typed)
        self.assertTrue(
            all(ch in it for ch in "abcde"),
            f"expected 'abcde' as subsequence of {typed!r}",
        )
        # 总输入长度应为 text + 错字 = 2 * len(text)
        self.assertEqual(len(typed), 2 * len("abcde"))

    def test_final_text_still_contains_target_chars_in_order(self):
        page = _make_page()
        type_humanized(
            page,
            "#email",
            "hello",
            typo_rate=0.0,
            rng=random.Random(42),
            sleep_fn=lambda _s: None,
        )
        # 单独调用顺序应当严格匹配
        typed_calls = [c.args[0] for c in page.keyboard.type.call_args_list]
        self.assertEqual(typed_calls, ["h", "e", "l", "l", "o"])

    def test_rejects_bad_wpm(self):
        page = _make_page()
        with self.assertRaises(ValueError):
            type_humanized(
                page,
                "#email",
                "x",
                wpm_mean=0,
                rng=random.Random(42),
                sleep_fn=lambda _s: None,
            )

    def test_rejects_bad_typo_rate(self):
        page = _make_page()
        with self.assertRaises(ValueError):
            type_humanized(
                page,
                "#email",
                "x",
                typo_rate=1.5,
                rng=random.Random(42),
                sleep_fn=lambda _s: None,
            )
        with self.assertRaises(ValueError):
            type_humanized(
                page,
                "#email",
                "x",
                typo_rate=-0.1,
                rng=random.Random(42),
                sleep_fn=lambda _s: None,
            )

    def test_no_real_sleep_when_sleep_fn_injected(self):
        """确保我们的 sleep_fn 注入真的被用了；未注入真实 time.sleep。"""
        page = _make_page()
        slept = []
        type_humanized(
            page,
            "#email",
            "ab",
            typo_rate=0.0,
            rng=random.Random(42),
            sleep_fn=slept.append,
        )
        # 1 次聚焦后启动停顿 + 2 个字符的键间隔 sleep = 3 次
        # （"ab" 无空格，不触发词间停顿）
        self.assertEqual(len(slept), 3)
        for s in slept:
            self.assertGreater(s, 0)


if __name__ == "__main__":
    unittest.main()
