# -*- coding: utf-8 -*-
"""handlers 拟人化 wrapper 门控测试。

验证 ``_click`` / ``_fill`` 根据 ``runtime.config.humanize_enabled``
分别选择 ``humanize.click_humanized`` / ``humanize.type_humanized``
或 Playwright 原生 ``locator.first.click/fill``。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.orchestration import handlers as handlers_module


def _make_runtime(humanize_enabled: bool) -> SimpleNamespace:
    """构造最小 runtime stub：page=MagicMock、config.humanize_enabled 受控。"""
    return SimpleNamespace(
        page=MagicMock(),
        config=SimpleNamespace(humanize_enabled=humanize_enabled),
    )


class ClickWrapperTests(unittest.TestCase):
    """_click wrapper 门控行为。"""

    def test_click_uses_humanize_when_enabled(self) -> None:
        runtime = _make_runtime(humanize_enabled=True)
        with patch.object(handlers_module._humanize, "click_humanized") as mocked:
            handlers_module._click(runtime, "#submit")

        mocked.assert_called_once_with(runtime.page, "#submit")
        # 拟人化启用时，原生路径不应被触达
        runtime.page.locator.assert_not_called()

    def test_click_uses_raw_when_disabled(self) -> None:
        runtime = _make_runtime(humanize_enabled=False)
        with patch.object(handlers_module._humanize, "click_humanized") as mocked:
            handlers_module._click(runtime, "#submit")

        mocked.assert_not_called()
        runtime.page.locator.assert_called_once_with("#submit")
        runtime.page.locator.return_value.first.click.assert_called_once_with()


class FillWrapperTests(unittest.TestCase):
    """_fill wrapper 门控行为。"""

    def test_fill_uses_humanize_when_enabled(self) -> None:
        runtime = _make_runtime(humanize_enabled=True)
        with patch.object(handlers_module._humanize, "type_humanized") as mocked:
            handlers_module._fill(runtime, "#email", "foo@bar.com")

        mocked.assert_called_once_with(runtime.page, "#email", "foo@bar.com")
        runtime.page.locator.assert_not_called()

    def test_fill_uses_raw_when_disabled(self) -> None:
        runtime = _make_runtime(humanize_enabled=False)
        with patch.object(handlers_module._humanize, "type_humanized") as mocked:
            handlers_module._fill(runtime, "#email", "foo@bar.com")

        mocked.assert_not_called()
        runtime.page.locator.assert_called_once_with("#email")
        runtime.page.locator.return_value.first.fill.assert_called_once_with("foo@bar.com")


class HumanizeEnabledDefaultTests(unittest.TestCase):
    """_humanize_enabled 缺省行为：config 无字段时默认启用。"""

    def test_missing_attribute_defaults_to_true(self) -> None:
        runtime = SimpleNamespace(page=MagicMock(), config=SimpleNamespace())
        self.assertTrue(handlers_module._humanize_enabled(runtime))

    def test_none_runtime_returns_false(self) -> None:
        self.assertFalse(handlers_module._humanize_enabled(None))


if __name__ == "__main__":
    unittest.main()
