# -*- coding: utf-8 -*-
"""Captcha 自愈框架测试。覆盖 SolverProvider 抽象、默认实现、注入点行为。"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.automation.captcha_solver import (
    ManualFallbackSolver,
    NoCaptchaSolver,
    NoOpSolver,
    SolveAttempt,
    SolverProvider,
    build_solver_from_config,
    try_solve_captcha,
)


class _StubSolver(SolverProvider):
    """测试用：返回预设结果的 solver。"""

    def __init__(self, *, success: bool, token: str = "", raise_exc: bool = False):
        self._success = success
        self._token = token
        self._raise = raise_exc
        self.calls: list[dict] = []

    @property
    def provider_name(self) -> str:
        return "stub"

    def try_solve(self, *, page_url, site_key="", challenge_type="turnstile"):
        self.calls.append(
            {
                "page_url": page_url,
                "site_key": site_key,
                "challenge_type": challenge_type,
            }
        )
        if self._raise:
            raise RuntimeError("simulated solver failure")
        return SolveAttempt(
            success=self._success,
            provider_name=self.provider_name,
            duration_ms=42,
            token=self._token if self._success else None,
            rationale="ok" if self._success else "no",
        )


def _make_runtime(*, solver=None, page=None, emit_calls=None):
    """构造一个最小 runtime stub（避免真实 AutomationRuntime 依赖 page/handlers/...）。"""
    return SimpleNamespace(
        captcha_solver=solver,
        page=page,
        emit_event=(lambda evt, payload=None: emit_calls.append((evt, payload))) if emit_calls is not None else None,
    )


def _make_evidence(url: str = "https://chat.openai.com/auth", **signals):
    return SimpleNamespace(url=url, signals=signals)


class NoOpSolverTest(unittest.TestCase):
    def test_provider_name(self):
        self.assertEqual(NoOpSolver().provider_name, "noop")

    def test_always_returns_failure(self):
        attempt = NoOpSolver().try_solve(page_url="https://example.com", site_key="abc")
        self.assertFalse(attempt.success)
        self.assertEqual(attempt.provider_name, "noop")
        self.assertIsNone(attempt.token)


class ManualFallbackSolverTest(unittest.TestCase):
    def test_provider_name_distinct(self):
        self.assertEqual(ManualFallbackSolver().provider_name, "manual_fallback")

    def test_always_returns_failure(self):
        attempt = ManualFallbackSolver().try_solve(page_url="https://example.com")
        self.assertFalse(attempt.success)
        self.assertEqual(attempt.provider_name, "manual_fallback")


class TrySolveCaptchaTest(unittest.TestCase):
    def test_no_solver_configured_returns_false(self):
        runtime = _make_runtime(solver=None)
        evidence = _make_evidence()
        self.assertFalse(try_solve_captcha(runtime, evidence))

    def test_noop_solver_returns_false(self):
        runtime = _make_runtime(solver=NoOpSolver())
        evidence = _make_evidence()
        self.assertFalse(try_solve_captcha(runtime, evidence))

    def test_successful_solve_returns_true(self):
        emit_calls: list = []
        solver = _StubSolver(success=True, token="cf-token-xyz")
        runtime = _make_runtime(solver=solver, emit_calls=emit_calls)
        evidence = _make_evidence(url="https://chat.openai.com/auth", has_challenge_widget=True)
        self.assertTrue(try_solve_captcha(runtime, evidence))
        # solver 收到正确参数
        self.assertEqual(len(solver.calls), 1)
        self.assertEqual(solver.calls[0]["page_url"], "https://chat.openai.com/auth")
        # 事件正确上报
        self.assertEqual(len(emit_calls), 1)
        self.assertEqual(emit_calls[0][0], "captcha_solver")
        self.assertTrue(emit_calls[0][1]["success"])
        self.assertEqual(emit_calls[0][1]["provider"], "stub")

    def test_failed_solve_returns_false(self):
        emit_calls: list = []
        solver = _StubSolver(success=False)
        runtime = _make_runtime(solver=solver, emit_calls=emit_calls)
        evidence = _make_evidence()
        self.assertFalse(try_solve_captcha(runtime, evidence))
        # 失败也要上报事件，便于观测
        self.assertEqual(len(emit_calls), 1)
        self.assertFalse(emit_calls[0][1]["success"])

    def test_solver_exception_silently_returns_false(self):
        emit_calls: list = []
        solver = _StubSolver(success=False, raise_exc=True)
        runtime = _make_runtime(solver=solver, emit_calls=emit_calls)
        evidence = _make_evidence()
        # 不应抛出
        self.assertFalse(try_solve_captcha(runtime, evidence))
        # 异常也要上报，rationale 包含异常信息
        self.assertEqual(len(emit_calls), 1)
        self.assertIn("solver_exception", emit_calls[0][1]["rationale"])

    def test_emit_event_failure_does_not_break_main_flow(self):
        """事件上报失败必须降级，不影响 solver 决策。"""
        def broken_emit(*args, **kwargs):
            raise RuntimeError("event broker down")
        solver = _StubSolver(success=True, token="t")
        runtime = SimpleNamespace(
            captcha_solver=solver,
            page=None,
            emit_event=broken_emit,
        )
        evidence = _make_evidence()
        # 应仍返回 True（solver 决策为准）
        self.assertTrue(try_solve_captcha(runtime, evidence))

    def test_site_key_extracted_from_page(self):
        """page 提供 [data-sitekey] 时应自动提取。"""
        class StubLocator:
            def __init__(self, value):
                self._value = value
                self.first = self
            def get_attribute(self, name, timeout=None):
                if name == "data-sitekey":
                    return self._value
                return None
        class StubPage:
            def locator(self, selector):
                return StubLocator("0x4AAAAAAA_test_sitekey")

        solver = _StubSolver(success=True, token="t")
        runtime = _make_runtime(solver=solver, page=StubPage())
        evidence = _make_evidence()
        try_solve_captcha(runtime, evidence)
        self.assertEqual(solver.calls[0]["site_key"], "0x4AAAAAAA_test_sitekey")

    def test_missing_page_attribute_does_not_crash(self):
        """page=None 应优雅降级，site_key 传空串。"""
        solver = _StubSolver(success=True, token="t")
        runtime = _make_runtime(solver=solver, page=None)
        evidence = _make_evidence()
        self.assertTrue(try_solve_captcha(runtime, evidence))
        self.assertEqual(solver.calls[0]["site_key"], "")


class AutomationRuntimeIntegrationTest(unittest.TestCase):
    """验证 AutomationRuntime dataclass 默认 captcha_solver=None，向后兼容。"""

    def test_default_captcha_solver_is_none(self):
        from src.automation.runtime import AutomationRuntime
        runtime = AutomationRuntime(
            page=None, context=None, config=None,
            email="e@x.com", password="p", mail_api=None, logger=None, handlers={},
        )
        self.assertIsNone(runtime.captcha_solver)


class NoCaptchaSolverTest(unittest.TestCase):
    """NoCaptchaSolver: HTTP 适配器边界、降级、超时、上游错误。"""

    def test_provider_name(self):
        self.assertEqual(NoCaptchaSolver(user_token="t").provider_name, "nocaptcha")

    def test_empty_token_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            NoCaptchaSolver(user_token="")
        with self.assertRaises(ValueError):
            NoCaptchaSolver(user_token="   ")

    def test_unsupported_challenge_type_returns_failure(self):
        solver = NoCaptchaSolver(user_token="t")
        attempt = solver.try_solve(page_url="https://x.com", challenge_type="hcaptcha")
        self.assertFalse(attempt.success)
        self.assertIn("unsupported", attempt.rationale)

    def test_empty_page_url_returns_failure(self):
        solver = NoCaptchaSolver(user_token="t")
        attempt = solver.try_solve(page_url="")
        self.assertFalse(attempt.success)
        self.assertEqual(attempt.rationale, "empty page_url")

    def test_successful_solve_returns_token(self):
        solver = NoCaptchaSolver(user_token="my-token", timeout_ms=5000)
        mock_resp = MagicMock(status_code=200, content=b"{}")
        mock_resp.json.return_value = {"status": 1, "data": {"token": "cf-tok-123"}}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            attempt = solver.try_solve(
                page_url="https://chat.openai.com/auth",
                site_key="0x4AAAAAAA",
            )
        self.assertTrue(attempt.success)
        self.assertEqual(attempt.token, "cf-tok-123")
        self.assertEqual(attempt.provider_name, "nocaptcha")
        # 验证发出去的请求
        args, kwargs = mock_post.call_args
        self.assertIn("nocaptcha.io", args[0])
        self.assertEqual(kwargs["headers"]["User-Token"], "my-token")
        self.assertEqual(kwargs["json"]["href"], "https://chat.openai.com/auth")
        self.assertEqual(kwargs["json"]["sitekey"], "0x4AAAAAAA")

    def test_http_error_returns_failure(self):
        solver = NoCaptchaSolver(user_token="t")
        mock_resp = MagicMock(status_code=500, content=b"")
        with patch("requests.post", return_value=mock_resp):
            attempt = solver.try_solve(page_url="https://x.com")
        self.assertFalse(attempt.success)
        self.assertIn("http_500", attempt.rationale)

    def test_upstream_status_zero_returns_failure(self):
        solver = NoCaptchaSolver(user_token="t")
        mock_resp = MagicMock(status_code=200, content=b"{}")
        mock_resp.json.return_value = {"status": 0, "msg": "rate_limit"}
        with patch("requests.post", return_value=mock_resp):
            attempt = solver.try_solve(page_url="https://x.com")
        self.assertFalse(attempt.success)
        self.assertIn("upstream_decline", attempt.rationale)
        self.assertIn("rate_limit", attempt.rationale)

    def test_empty_token_in_success_returns_failure(self):
        """upstream 返回 status=1 但 token 为空，应视为失败。"""
        solver = NoCaptchaSolver(user_token="t")
        mock_resp = MagicMock(status_code=200, content=b"{}")
        mock_resp.json.return_value = {"status": 1, "data": {"token": ""}}
        with patch("requests.post", return_value=mock_resp):
            attempt = solver.try_solve(page_url="https://x.com")
        self.assertFalse(attempt.success)
        self.assertEqual(attempt.rationale, "empty_token_in_success_response")

    def test_request_exception_silently_returns_failure(self):
        solver = NoCaptchaSolver(user_token="t")
        with patch("requests.post", side_effect=RuntimeError("network down")):
            attempt = solver.try_solve(page_url="https://x.com")
        self.assertFalse(attempt.success)
        self.assertIn("RuntimeError", attempt.rationale)
        self.assertIn("network down", attempt.rationale)


class BuildSolverFromConfigTest(unittest.TestCase):
    """工厂函数路由规则。"""

    def _config(self, **kwargs):
        defaults = {
            "captcha_solver_kind": "noop",
            "nocaptcha_user_token": "",
            "captcha_solver_timeout_ms": 30000,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_default_returns_noop(self):
        solver = build_solver_from_config(self._config())
        self.assertIsInstance(solver, NoOpSolver)

    def test_manual_kind(self):
        solver = build_solver_from_config(self._config(captcha_solver_kind="manual"))
        self.assertIsInstance(solver, ManualFallbackSolver)

    def test_nocaptcha_with_token(self):
        solver = build_solver_from_config(
            self._config(captcha_solver_kind="nocaptcha", nocaptcha_user_token="abc123")
        )
        self.assertIsInstance(solver, NoCaptchaSolver)

    def test_nocaptcha_without_token_degrades_to_noop(self):
        """缺 token 时不应崩溃，应降级 noop。"""
        solver = build_solver_from_config(
            self._config(captcha_solver_kind="nocaptcha", nocaptcha_user_token="")
        )
        self.assertIsInstance(solver, NoOpSolver)

    def test_unknown_kind_falls_back_to_noop(self):
        solver = build_solver_from_config(
            self._config(captcha_solver_kind="capsolver", nocaptcha_user_token="x")
        )
        self.assertIsInstance(solver, NoOpSolver)

    def test_case_insensitive(self):
        solver = build_solver_from_config(
            self._config(captcha_solver_kind="NoCaptcha", nocaptcha_user_token="t")
        )
        self.assertIsInstance(solver, NoCaptchaSolver)

    def test_missing_attrs_falls_back_to_noop(self):
        """config 完全缺 captcha 字段时应安全降级，不抛异常。"""
        solver = build_solver_from_config(SimpleNamespace())
        self.assertIsInstance(solver, NoOpSolver)


if __name__ == "__main__":
    unittest.main()
