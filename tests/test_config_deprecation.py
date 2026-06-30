# -*- coding: utf-8 -*-
"""A 类 provider 字段 deprecation warning 测试。"""

import logging
import os
import unittest
from unittest.mock import patch

import src.config as config_mod
from src.config import load_config


class TestDeprecatedProviderFieldsWarning(unittest.TestCase):

    def setUp(self):
        # 重置一次性标志，让每个测试独立
        config_mod._deprecation_warned = False

    def tearDown(self):
        # 测试后清理可能注入的环境变量
        for var in config_mod._DEPRECATED_ENV_VARS:
            os.environ.pop(var, None)
        config_mod._deprecation_warned = False

    def test_no_warning_when_no_deprecated_vars_set(self):
        """没设任何 A 类字段时不打 warning。"""
        # 先清掉所有可能存在的 A 类环境变量
        for var in config_mod._DEPRECATED_ENV_VARS:
            os.environ.pop(var, None)
        with self.assertLogs("src.config", level="WARNING") as ctx:
            # 加个 dummy warning 防止 assertLogs 没记录任何东西时抛 AssertionError
            logging.getLogger("src.config").warning("dummy")
            load_config(dotenv_path="/tmp/__nonexistent__.env")
        deprecation_msgs = [
            msg for msg in ctx.output
            if "A 类 provider 字段" in msg
        ]
        self.assertEqual(deprecation_msgs, [])

    def test_warning_when_efuncard_token_set(self):
        """设了 EFUNCARD_TOKEN 应触发 warning。"""
        for var in config_mod._DEPRECATED_ENV_VARS:
            os.environ.pop(var, None)
        os.environ["EFUNCARD_TOKEN"] = "test-token"
        with self.assertLogs("src.config", level="WARNING") as ctx:
            load_config(dotenv_path="/tmp/__nonexistent__.env")
        deprecation_msgs = [
            msg for msg in ctx.output
            if "A 类 provider 字段" in msg
        ]
        self.assertEqual(len(deprecation_msgs), 1)
        self.assertIn("EFUNCARD_TOKEN", deprecation_msgs[0])

    def test_warning_only_once_per_process(self):
        """同进程多次 load_config 只 warn 一次。"""
        for var in config_mod._DEPRECATED_ENV_VARS:
            os.environ.pop(var, None)
        os.environ["DEFAULT_BROWSER_PROVIDER"] = "browser-default"

        with self.assertLogs("src.config", level="WARNING") as ctx:
            logging.getLogger("src.config").warning("anchor")
            load_config(dotenv_path="/tmp/__nonexistent__.env")
            load_config(dotenv_path="/tmp/__nonexistent__.env")  # 第二次
            load_config(dotenv_path="/tmp/__nonexistent__.env")  # 第三次
        deprecation_msgs = [
            msg for msg in ctx.output
            if "A 类 provider 字段" in msg
        ]
        self.assertEqual(len(deprecation_msgs), 1)

    def test_warning_lists_all_set_vars(self):
        """warning 内容应该列出所有被显式设置的 A 类字段。"""
        for var in config_mod._DEPRECATED_ENV_VARS:
            os.environ.pop(var, None)
        os.environ["EFUNCARD_TOKEN"] = "tok"
        os.environ["NODECARD_API_URL"] = "https://x"
        os.environ["SMS_API_KEY"] = "sk"

        with self.assertLogs("src.config", level="WARNING") as ctx:
            load_config(dotenv_path="/tmp/__nonexistent__.env")
        deprecation_msgs = [
            msg for msg in ctx.output
            if "A 类 provider 字段" in msg
        ]
        self.assertEqual(len(deprecation_msgs), 1)
        msg = deprecation_msgs[0]
        self.assertIn("EFUNCARD_TOKEN", msg)
        self.assertIn("NODECARD_API_URL", msg)
        self.assertIn("SMS_API_KEY", msg)


if __name__ == "__main__":
    unittest.main()
