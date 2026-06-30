# -*- coding: utf-8 -*-
"""build_runtime_handlers 烟雾测试。

存在意义：
PhaseOrchestrator 在 _phase_registration 必经路径上调用 build_runtime_handlers()
（src/orchestration/orchestrator.py:257），其返回的 dict 字面量在构造时立即解析所有
名字。曾经发生过 ``"recover_error": recover_error`` 引用未定义名字的回归
（2026-04-29 修复），导致整个注册阶段启动即崩。

本套测试固化以下不变量：
- build_runtime_handlers() 不抛异常
- 返回 dict 包含状态机所需的所有 handler key
- 每个 value 都是可调用对象（callable）

如果未来又有人加新 handler 但漏定义函数，pytest 会立刻报错——而不是等到
production 注册任务启动时才 NameError。
"""

from __future__ import annotations

import unittest

from src.orchestration.handlers import build_runtime_handlers


# 状态机消费的 handler key 集合。
# 来源：src/automation/runtime.py 状态推断 + 决策动作枚举。
# 任何状态机里 emit 的动作 name 必须在这个集合里有对应 handler。
EXPECTED_HANDLER_KEYS = frozenset(
    {
        "enter_signup",
        "submit_password",
        "verify_email",
        "fill_about_you",
        "wait_short",
        "recover_error",
        "manual_handoff",
        # Mode B（手机号注册）handler，feat/mode-phone-registration 2026-05-27 引入
        "submit_phone_and_code",
    }
)


class BuildRuntimeHandlersSmokeTest(unittest.TestCase):
    """构造期不变量：dict 字面量解析所有名字时不应崩。"""

    def test_does_not_raise(self):
        """必经路径：调用即崩等于全线瘫痪，必须先确保不抛异常。"""
        handlers = build_runtime_handlers(email="x@y.com", password="pw")
        self.assertIsInstance(handlers, dict)

    def test_returns_all_expected_keys(self):
        """状态机依赖的 handler key 一个不能少。"""
        handlers = build_runtime_handlers(email="x@y.com", password="pw")
        actual = frozenset(handlers.keys())
        missing = EXPECTED_HANDLER_KEYS - actual
        self.assertFalse(missing, f"缺失 handler: {missing}")

    def test_all_values_are_callable(self):
        """防止某 key 被误赋值成字符串/None 等非函数。"""
        handlers = build_runtime_handlers(email="x@y.com", password="pw")
        for key, value in handlers.items():
            self.assertTrue(
                callable(value),
                f"handler '{key}' 不是 callable，实际类型: {type(value).__name__}",
            )

    def test_recover_error_specifically_resolved(self):
        """专项护栏：recover_error 曾经引用未定义名字（2026-04-29 修复），
        本测试明确锁住它必须存在且 callable。"""
        handlers = build_runtime_handlers(email="x@y.com", password="pw")
        self.assertIn("recover_error", handlers)
        self.assertTrue(callable(handlers["recover_error"]))


class SubmitPhoneAndCodeHandlerTest(unittest.TestCase):
    """Mode B handler `submit_phone_and_code` 行为护栏。

    OTP input 选择器是基于历史 DOM 的占位（input[name="code"]），实施 PR 跑真机时
    可能需要校准；本测试不锁死 page.fill 的具体调用形参，只锁 handler 在「号 + sms_api」
    都齐时调通流程、否则返回 False 让上层做 manual_handoff。
    """

    def _make_runtime(self, *, phone: str = "+14155551212", order_id: str = "ord-1",
                     sms_code: str | None = "123456", sms_api=None):
        from unittest.mock import MagicMock
        from src.orchestration.handlers import submit_phone_and_code  # noqa: F401

        config = MagicMock()
        config.requested_phone = phone
        config.sms_order_id = order_id

        if sms_api is None:
            sms_api = MagicMock()
            sms_api.get_code.return_value = sms_code

        # 新实现走 page.locator(sel).first.{count,fill,click}；
        # 让 first.count() 返回 1（可见），fill/click 记录调用次数。
        page = MagicMock()
        self._loc_first = MagicMock()
        self._loc_first.count.return_value = 1
        page.locator.return_value.first = self._loc_first

        config.sms_country = "182"  # 日本（默认国，国家选择匹配 +81）

        runtime = MagicMock()
        runtime.page = page
        runtime.config = config
        runtime.sms_api = sms_api
        runtime.logger = MagicMock()
        return runtime, page, sms_api

    def test_returns_false_when_phone_missing(self):
        from src.orchestration.handlers import submit_phone_and_code
        runtime, _, _ = self._make_runtime(phone="")
        self.assertFalse(submit_phone_and_code(runtime, None))

    def test_phone_handler_fills_and_submits_without_waiting_otp(self):
        """重构后：submit_phone_and_code 只选国家+填号+提交，不等 OTP（不调 get_code）。

        回归锁：2026-06-01 run 11d63e00 卡死——填号后死等 OTP，但 OpenAI 要求中间
        先创建密码才发短信 → 双方互等。修复后此 handler 只推进一步即 return。
        """
        from src.orchestration.handlers import submit_phone_and_code
        runtime, page, sms_api = self._make_runtime(phone="+14155551212", order_id="ord-1")
        self.assertTrue(submit_phone_and_code(runtime, None))
        # 关键：不再调 get_code（OTP 移到 submit_sms_code）
        sms_api.get_code.assert_not_called()
        # 填了手机号（至少一次 fill）+ 点了提交
        self.assertGreaterEqual(self._loc_first.fill.call_count, 1)
        self.assertGreaterEqual(self._loc_first.click.call_count, 1)

    def test_sms_code_handler_polls_and_fills_otp(self):
        """submit_sms_code：轮询 get_code → 键盘真实输入填验证码（isTrusted）→ 提交。"""
        from src.orchestration.handlers import submit_sms_code
        runtime, page, sms_api = self._make_runtime(order_id="ord-1", sms_code="123456")
        # 让 input_value 返回非目标值，触发键盘输入路径
        self._loc_first.input_value.return_value = ""
        self.assertTrue(submit_sms_code(runtime, None))
        sms_api.get_code.assert_called_once()
        args, _ = sms_api.get_code.call_args
        self.assertEqual(args[0], "ord-1")
        # 走了键盘真实输入（press_sequentially 或 type）
        called = (self._loc_first.press_sequentially.call_count
                  + self._loc_first.type.call_count)
        self.assertGreaterEqual(called, 1)

    def test_sms_code_handler_returns_false_when_sms_api_missing(self):
        from src.orchestration.handlers import submit_sms_code
        runtime, _, _ = self._make_runtime(order_id="ord-1")
        runtime.sms_api = None
        self.assertFalse(submit_sms_code(runtime, None))

    def test_sms_code_handler_returns_false_when_otp_timeout(self):
        from src.orchestration.handlers import submit_sms_code
        runtime, _, _ = self._make_runtime(order_id="ord-1", sms_code=None)
        self.assertFalse(submit_sms_code(runtime, None))


class PhoneCountryAndDialCodeTest(unittest.TestCase):
    """国家选择 + 国家码剥离（真实 DOM：隐藏 select value=ISO + 前缀显示 +区号）。"""

    def test_strip_country_dial_code(self):
        from src.orchestration.handlers import _strip_country_dial_code
        # 日本号带 81 前缀 → 剥离
        self.assertEqual(_strip_country_dial_code("8190123456", "182"), "90123456")
        # 带 + 和分隔符
        self.assertEqual(_strip_country_dial_code("+81 90-1234", "182"), "901234")
        # 美国号带 1 前缀
        self.assertEqual(_strip_country_dial_code("14155551212", "187"), "4155551212")
        # 不带前缀 → 原样
        self.assertEqual(_strip_country_dial_code("901234", "182"), "901234")
        # 未知国家 → 原样（只清理符号）
        self.assertEqual(_strip_country_dial_code("123456", "999"), "123456")

    def test_select_phone_country_uses_iso_select_option(self):
        """主路径：对隐藏 <select> 调 select_option(value=ISO)。"""
        from unittest.mock import MagicMock
        from src.orchestration.handlers import _select_phone_country

        select_loc = MagicMock()
        select_loc.count.return_value = 1
        select_loc.evaluate.return_value = "SELECT"  # tagName

        page = MagicMock()
        page.locator.return_value.first = select_loc
        runtime = MagicMock()

        ok = _select_phone_country(page, "182", runtime=runtime)  # 日本 → JP
        self.assertTrue(ok)
        select_loc.select_option.assert_called_once()
        _, kwargs = select_loc.select_option.call_args
        self.assertEqual(kwargs.get("value"), "JP")

    def test_select_phone_country_unknown_country_returns_false(self):
        from unittest.mock import MagicMock
        from src.orchestration.handlers import _select_phone_country
        ok = _select_phone_country(MagicMock(), "999", runtime=MagicMock())
        self.assertFalse(ok)


class SubmitPasswordTest(unittest.TestCase):
    """submit_password 用键盘真实输入填充（v3：isTrusted 触发 react-aria 校验）。"""

    def test_uses_keyboard_real_input(self):
        """主路径走键盘真实输入（press_sequentially，isTrusted）让续行按钮 enable。"""
        from unittest.mock import MagicMock
        from src.orchestration.handlers import submit_password
        page = MagicMock()
        loc = MagicMock()
        loc.count.return_value = 1
        loc.input_value.return_value = ""   # 未填，触发输入
        page.locator.return_value.first = loc

        submit_password(page, "MyPassw0rd123", runtime=None)

        # 走了键盘真实输入（press_sequentially 或 type）
        called = loc.press_sequentially.call_count + loc.type.call_count
        self.assertGreaterEqual(called, 1)

    def test_skips_input_when_already_filled(self):
        """已填对（input_value==password）→ 跳过输入只提交（防叠加）。"""
        from unittest.mock import MagicMock
        from src.orchestration.handlers import submit_password
        page = MagicMock()
        loc = MagicMock()
        loc.count.return_value = 1
        loc.input_value.return_value = "MyPassw0rd123"  # 已填对
        page.locator.return_value.first = loc

        submit_password(page, "MyPassw0rd123", runtime=None)
        # 不再输入
        loc.press_sequentially.assert_not_called()
        loc.type.assert_not_called()

    def test_set_react_input_value_idempotent_helper(self):
        """_set_react_input_value：evaluate ok=True → 返回 True。"""
        from unittest.mock import MagicMock
        from src.orchestration.handlers import _set_react_input_value
        page = MagicMock()
        page.evaluate.return_value = {"ok": True}
        self.assertTrue(_set_react_input_value(page, "input#x", "v"))
        page.evaluate.return_value = {"ok": False}
        self.assertFalse(_set_react_input_value(page, "input#x", "v"))


class SummarizeMailExceptionTest(unittest.TestCase):
    """_summarize_mail_exception 业务错误码识别护栏。

    背景：2026-05-28 线上 run=fe656a2153a1 在邮箱验证阶段失败，远程
    email-provider 返回 `... (ACCOUNT_NOT_AVAILABLE)`（号池竞争或项目去重门控）。
    此前所有 MailServiceError 都被打成同一个 MAILBOX_SERVICE_ERROR hint，
    运维难以分辨"号池问题 vs 基础设施故障 vs 代码 bug"。

    本测试锁定：业务码升级只在 MailServiceError 上触发，不污染已有更精确 hint。
    """

    def test_account_not_available_upgrades_to_pool_contention(self):
        """ACCOUNT_NOT_AVAILABLE 应升级为 MAILBOX_POOL_CONTENTION 并保留原码。"""
        from src.orchestration.handlers import _summarize_mail_exception
        from src.providers.mail import MailServiceError

        exc = MailServiceError(
            "outlookEmailPlus 请求失败: 邮箱 X 当前不可领取"
            "（可能项目去重门控触发或竞争失败） (ACCOUNT_NOT_AVAILABLE)"
        )
        reason = _summarize_mail_exception(exc)
        self.assertIn("MAILBOX_POOL_CONTENTION", reason)
        self.assertIn("ACCOUNT_NOT_AVAILABLE", reason)
        # mailbox-service 前缀仍在 → warmup._classify_failure 仍归 external_failure
        self.assertTrue(reason.startswith("mailbox-service"))
        # 不应残留原通用 hint
        self.assertNotIn("MAILBOX_SERVICE_ERROR", reason)

    def test_generic_5xx_keeps_service_error_hint(self):
        """普通 5xx / 没业务码的消息应保留 MAILBOX_SERVICE_ERROR，不被误升级。"""
        from src.orchestration.handlers import _summarize_mail_exception
        from src.providers.mail import MailServiceError

        exc = MailServiceError("轮询验证码失败：email-provider 返回 503")
        reason = _summarize_mail_exception(exc)
        self.assertIn("MAILBOX_SERVICE_ERROR", reason)
        self.assertNotIn("MAILBOX_POOL_CONTENTION", reason)

    def test_provider_not_configured_not_overwritten(self):
        """MissingProviderConfigError 已有更精确 hint，业务码逻辑不能覆盖它。"""
        from src.orchestration.handlers import _summarize_mail_exception
        from src.providers.mail import MissingProviderConfigError

        # 即使消息里偶然带类似业务码的括号，也不应升级
        exc = MissingProviderConfigError("缺少 provider 配置 (POOL_EXHAUSTED)")
        reason = _summarize_mail_exception(exc)
        self.assertIn("PROVIDER_NOT_CONFIGURED", reason)
        self.assertNotIn("MAILBOX_POOL_CONTENTION", reason)

    def test_unknown_business_code_is_recorded_but_not_upgraded(self):
        """未列入号池竞争集合的业务码（如 UNKNOWN_FOO）：保留原码透传供排障，
        但 hint 保持 MAILBOX_SERVICE_ERROR（不盲目升级）。"""
        from src.orchestration.handlers import _summarize_mail_exception
        from src.providers.mail import MailServiceError

        exc = MailServiceError("某 provider 返回未识别错误 (UNKNOWN_FOO)")
        reason = _summarize_mail_exception(exc)
        self.assertIn("MAILBOX_SERVICE_ERROR", reason)
        self.assertIn("UNKNOWN_FOO", reason)
        self.assertNotIn("MAILBOX_POOL_CONTENTION", reason)


if __name__ == "__main__":
    unittest.main()
