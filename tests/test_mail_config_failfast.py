# -*- coding: utf-8 -*-
"""4 层 fail-fast 契约测试 — 防止 config_name 缺失漏到服务端。

层级：
  L1 admin UI 字段校验  → upsert_provider 写入前 422
  L2 任务创建 preflight → create_task 入队前 422
  L3 运行时 ensure_runtime_ready → 不发 HTTP 直接 raise MissingProviderConfigError
  L4 .env load_config 后置 warning（非 raise，向后兼容）

详见 docs/architecture/mail-provider-contract.md。
"""
from __future__ import annotations

import logging
import unittest
from unittest import mock

from fastapi import HTTPException

from src.api.routes.config import _validate_mail_provider_config


# ──────────────────────────────────────────────────────────────────────
# L1: admin UI 字段校验
# ──────────────────────────────────────────────────────────────────────


class TestL1AdminUIValidation(unittest.TestCase):
    """_validate_mail_provider_config — admin 创建/更新 mail-* ProviderConfig 前校验。"""

    def test_cfworker_managed_no_config_name_raises_422(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_mail_provider_config(
                "mail-default",
                {"provider_name": "cfworker", "session_mode": "managed"},
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["code"], "PROVIDER_NOT_CONFIGURED")
        self.assertIn("config_name", ctx.exception.detail["missing_fields"])

    def test_cfworker_managed_with_config_name_passes(self):
        _validate_mail_provider_config(
            "mail-default",
            {"provider_name": "cfworker", "session_mode": "managed", "config_name": "mydomain-cfworker"},
        )  # 不抛即通过

    def test_skymail_managed_no_config_name_also_raises(self):
        """skymail 同样在白名单里，缺 config_name 应当也被拦下。"""
        with self.assertRaises(HTTPException) as ctx:
            _validate_mail_provider_config(
                "mail-sky",
                {"provider_name": "skymail", "session_mode": "managed"},
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_freemail_managed_not_in_whitelist_passes(self):
        """auto-allocate 类 provider 不需要 config_name。"""
        _validate_mail_provider_config(
            "mail-free",
            {"provider_name": "freemail", "session_mode": "managed"},
        )  # 不抛

    def test_credentialed_mode_skips_validation(self):
        """credentialed 模式不在校验范围内（凭据走另一条路）。"""
        _validate_mail_provider_config(
            "mail-default",
            {"provider_name": "cfworker", "session_mode": "credentialed"},
        )  # 不抛

    def test_session_mode_default_is_managed(self):
        """session_mode 字段缺失 → 按 managed 处理（与 .env.example 默认一致）。"""
        with self.assertRaises(HTTPException):
            _validate_mail_provider_config(
                "mail-default",
                {"provider_name": "cfworker"},
            )

    def test_provider_name_falls_back_to_route_param(self):
        """payload 没 provider_name 字段 → 用 URL 路径里的 provider_name 推断。

        当前 admin UI 创建 mail-default 时，`config.provider_name` 字段
        通常是空的（实际 cfworker 名字在 config_name='mydomain-cfworker' 里），
        所以白名单按 URL 路径推断不合适 → 这种情况不强制校验，向后兼容。
        """
        # 路径名是 mail-default（不在白名单），且 payload 也没指定 provider_name → 跳过
        _validate_mail_provider_config(
            "mail-default",
            {"session_mode": "managed"},
        )  # 不抛

    def test_empty_config_name_string_treated_as_missing(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_mail_provider_config(
                "mail-default",
                {"provider_name": "cfworker", "session_mode": "managed", "config_name": "   "},
            )
        self.assertIn("config_name", ctx.exception.detail["missing_fields"])


# ──────────────────────────────────────────────────────────────────────
# L3: 运行时 ensure_runtime_ready 本地 fail-fast
# ──────────────────────────────────────────────────────────────────────


class TestL3RuntimeFailFast(unittest.TestCase):
    """MailManager.ensure_runtime_ready managed + 空 config_name → 立即 raise。

    关键：不发任何 HTTP 请求就 fail。这样即便 admin UI 校验被绕过、用户直接跑
    main.py，也不会让 worker 在 110s retry 上空转 cfworker 的 422 响应。
    """

    def _make_manager(self, *, provider_name: str, config_name: str = ""):
        from src.mail import MailManager
        # 用最小参数构造 MailManager，避免触发实际 HTTP
        with mock.patch("src.mail.HttpMailProvider") as _:
            return MailManager(
                base_url="http://example.test",
                api_key="k",
                provider_name=provider_name,
                config_name=config_name,
            )

    def test_cfworker_managed_no_config_name_raises_without_http(self):
        from src.providers.mail import MissingProviderConfigError
        mgr = self._make_manager(provider_name="cfworker", config_name="")
        # 关键：不该调底层 _provider.ensure_runtime_ready
        with mock.patch.object(mgr._provider, "ensure_runtime_ready") as mock_ready:
            with self.assertRaises(MissingProviderConfigError) as ctx:
                mgr.ensure_runtime_ready("foo@gitee.shop")
        mock_ready.assert_not_called()
        self.assertEqual(ctx.exception.error_code, "PROVIDER_NOT_CONFIGURED")
        self.assertIn("config_name", ctx.exception.missing_fields)

    def test_cfworker_managed_with_config_name_proceeds_to_provider(self):
        mgr = self._make_manager(provider_name="cfworker", config_name="mydomain-cfworker")
        with mock.patch.object(mgr._provider, "ensure_runtime_ready", return_value={}) as mock_ready:
            result = mgr.ensure_runtime_ready("foo@gitee.shop")
        self.assertEqual(result, "managed")
        mock_ready.assert_called_once_with("cfworker", session_mode="managed")

    def test_freemail_no_config_name_does_not_fail_fast(self):
        """白名单外的 provider 不强制 config_name；让底层处理。"""
        mgr = self._make_manager(provider_name="freemail", config_name="")
        with mock.patch.object(mgr._provider, "ensure_runtime_ready", return_value={}) as mock_ready:
            mgr.ensure_runtime_ready("foo@example.com")
        mock_ready.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# L4: .env load_config 后置 warning（非 raise）
# ──────────────────────────────────────────────────────────────────────


class TestL4EnvLoadWarning(unittest.TestCase):
    """白名单 provider 但 MAIL_CONFIG_NAME 为空 → logger.warning（不 raise）。

    向后兼容：admin 可能通过 UI /providers 配 ProviderConfig 而不依赖 .env，
    这种情况 .env 里 MAIL_CONFIG_NAME 留空是正常的 — 所以 L4 只 warn，不 raise。
    """

    def test_warns_for_cfworker_without_env_config_name(self):
        from src.config import _warn_if_mail_config_incomplete, AppConfig
        config = mock.MagicMock(spec=AppConfig)
        config.email_provider_name = "cfworker"
        config.mail_config_name = ""
        with self.assertLogs("src.config", level="WARNING") as logs:
            _warn_if_mail_config_incomplete(config)
        self.assertTrue(any("MAIL_CONFIG_NAME 未配置" in line for line in logs.output))

    def test_does_not_warn_for_cfworker_with_env_config_name(self):
        from src.config import _warn_if_mail_config_incomplete, AppConfig
        config = mock.MagicMock(spec=AppConfig)
        config.email_provider_name = "cfworker"
        config.mail_config_name = "mydomain-cfworker"
        # logger.warning 不应被触发；用 assertNoLogs 替代（Python 3.10+）
        # 兼容：用 patch 监听
        with mock.patch("src.config.logger.warning") as mock_warn:
            _warn_if_mail_config_incomplete(config)
        mock_warn.assert_not_called()

    def test_does_not_warn_for_applemail(self):
        """applemail 不在白名单（用 KNOWN_MAIL_ACCOUNTS_JSON）→ 不警告。"""
        from src.config import _warn_if_mail_config_incomplete, AppConfig
        config = mock.MagicMock(spec=AppConfig)
        config.email_provider_name = "applemail"
        config.mail_config_name = ""
        with mock.patch("src.config.logger.warning") as mock_warn:
            _warn_if_mail_config_incomplete(config)
        mock_warn.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# 跨层一致性：L1 / L3 / L4 用同一份白名单
# ──────────────────────────────────────────────────────────────────────


class TestWhitelistConsistency(unittest.TestCase):
    """保证 L1 / L3 / L4 三处白名单同步，不会出现某一层漏拦。"""

    def test_three_layers_share_same_whitelist(self):
        from src.api.routes.config import _MAIL_MANAGED_REQUIRED_FIELDS
        from src.mail import _PROVIDERS_REQUIRING_CONFIG_NAME
        from src.config import _MAIL_PROVIDERS_REQUIRING_CONFIG_NAME

        l1 = set(_MAIL_MANAGED_REQUIRED_FIELDS.keys())
        l3 = set(_PROVIDERS_REQUIRING_CONFIG_NAME)
        l4 = set(_MAIL_PROVIDERS_REQUIRING_CONFIG_NAME)
        self.assertEqual(
            l1, l3,
            "L1 (config.py routes) 和 L3 (mail.py) 必须用同一份 provider 白名单",
        )
        self.assertEqual(
            l3, l4,
            "L3 (mail.py) 和 L4 (config.py load_config) 必须同步",
        )


if __name__ == "__main__":
    unittest.main()
