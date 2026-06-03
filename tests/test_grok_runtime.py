# -*- coding: utf-8 -*-
"""Grok 注册状态机 + worker 分叉测试（feat/grok-register）。

mock Playwright page / mail_api / solver，不需真实浏览器或 API。
覆盖：5 步 happy path、收码超时、sso 三法提取、Turnstile pending→solve、
worker 按 platform=grok 分叉、终态 sso 判定。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import src.db.engine as engine_mod
from src.automation import grok_runtime as gr
from src.automation.grok_runtime import GrokRegistrationError
from src.db.engine import get_engine, get_session, init_db
from src.db.models import Run
from sqlmodel import SQLModel


def _reset_engine():
    engine_mod._engine = None


def _noop_emit(*_args, **_kwargs):
    pass


# ── grok_runtime 单步测试（mock page）─────────────────────────────────


class TestGrokSteps(unittest.TestCase):
    def _page(self, evaluate_side_effect=None, evaluate_return=None):
        page = MagicMock()
        if evaluate_side_effect is not None:
            page.evaluate.side_effect = evaluate_side_effect
        elif evaluate_return is not None:
            page.evaluate.return_value = evaluate_return
        # locator 路径默认不可见 → 强制走 JS fallback
        loc = MagicMock()
        loc.is_visible.return_value = False
        page.get_by_text.return_value.first = loc
        page.url = "https://accounts.x.ai/sign-up"
        return page

    def test_open_signup_clicks_email_button(self):
        # evaluate 序列：_dismiss_cookie 先跑（返回 "none" ×2，会重试），
        # 再 has_email_input(False) → click_email_signup(True)。
        # 用 callable side_effect 按 JS 内容路由，避免顺序耦合 _dismiss_cookie 的调用次数。
        def _route(js, *a, **k):
            if "cookie" in js.lower() or "acceptKw" in js or "accept all" in js.lower():
                return "none"  # _dismiss_cookie：未处理
            if "input[type=\"email\"]" in js or "autocomplete=\"email\"" in js:
                return False  # has_email_input：否
            return True  # click_email_signup：点中

        page = self._page(evaluate_side_effect=_route)
        with patch("time.sleep"):
            gr._open_signup(page, _noop_emit)
        page.goto.assert_called_once()

    def test_open_signup_skips_when_already_email_page(self):
        # 直接就是填邮箱页 → has_email_input(True) → 跳过点击
        page = self._page(evaluate_return=True)
        with patch("time.sleep"):
            gr._open_signup(page, _noop_emit)
        page.goto.assert_called_once()

    def test_open_signup_fails_when_button_missing(self):
        page = self._page(evaluate_return=False)  # 既无 email input 也点不到入口
        with patch("time.sleep"), patch("time.time", side_effect=[0, 0, 100]):
            with self.assertRaises(GrokRegistrationError):
                gr._open_signup(page, _noop_emit)

    def test_fill_email_happy(self):
        # evaluate 序列：fill_email(filled) → click_submit(True)
        page = self._page(evaluate_side_effect=["filled", True])
        with patch("time.sleep"):
            gr._fill_email(page, "user@hotmail.com", _noop_emit)

    def test_fill_email_timeout(self):
        page = self._page(evaluate_return="not-ready")
        with patch("time.sleep"), patch("time.time", side_effect=[0, 0, 100]):
            with self.assertRaises(GrokRegistrationError):
                gr._fill_email(page, "user@hotmail.com", _noop_emit)

    def test_wait_and_fill_code_happy(self):
        mail = MagicMock()
        mail.get_verification_code.return_value = "123456"
        # evaluate 序列：has_profile_form(False) → fill_code(filled) → click_submit(True) → has_profile_form(True)
        page = self._page(evaluate_side_effect=[False, "filled", True, True])
        with patch("time.sleep"):
            gr._wait_and_fill_code(page, mail, "user@hotmail.com", 10, _noop_emit)
        mail.get_verification_code.assert_called_once()

    def test_wait_and_fill_code_no_code(self):
        mail = MagicMock()
        mail.get_verification_code.return_value = None
        page = self._page()
        with self.assertRaises(GrokRegistrationError):
            gr._wait_and_fill_code(page, mail, "user@hotmail.com", 10, _noop_emit)

    def test_fill_profile_happy_no_turnstile(self):
        # fill_profile(filled) → solve 检测 state(not-found) → _wait_turnstile_ready state(not-found) → click_submit(True)
        page = self._page(evaluate_side_effect=["filled", "not-found", "not-found", True])
        solver_rt = MagicMock()
        try_solve = MagicMock(return_value=True)
        with patch("time.sleep"):
            gr._fill_profile(page, "Pwd123!", solver_rt, try_solve, _noop_emit)
        try_solve.assert_not_called()  # 无 turnstile 不调 solver

    def test_fill_profile_solves_turnstile(self):
        # fill_profile(filled) → solve state(pending) → get token → sync token
        # → _wait_turnstile_ready state(ready) → click_submit(True)
        page = self._page(evaluate_side_effect=["filled", "pending", "tok-abc", True, "ready", True])
        solver_rt = MagicMock()
        try_solve = MagicMock(return_value=True)
        with patch("time.sleep"):
            gr._fill_profile(page, "Pwd123!", solver_rt, try_solve, _noop_emit)
        try_solve.assert_called_once()

    def test_extract_sso_via_cookies(self):
        page = MagicMock()
        page.evaluate.return_value = ""
        context = MagicMock()
        context.cookies.return_value = [{"name": "sso", "value": "eyJ0eXAi.a8f"}]
        sso = gr._extract_sso(page, context, 5, _noop_emit)
        self.assertEqual(sso, "eyJ0eXAi.a8f")

    def test_extract_sso_via_js_cookie(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: "sso=jstoken123; other=x" if "document.cookie" in js else ""
        context = MagicMock()
        context.cookies.return_value = []
        sso = gr._extract_sso(page, context, 5, _noop_emit)
        self.assertEqual(sso, "jstoken123")

    def test_extract_sso_timeout_returns_empty(self):
        page = MagicMock()
        page.evaluate.return_value = ""
        context = MagicMock()
        context.cookies.return_value = []
        with patch("time.sleep"), patch("time.time", side_effect=[0, 0, 100]):
            sso = gr._extract_sso(page, context, 5, _noop_emit)
        self.assertEqual(sso, "")


# ── worker 分叉测试 ───────────────────────────────────────────────────


class TestWorkerGrokFork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        SQLModel.metadata.drop_all(get_engine())
        SQLModel.metadata.create_all(get_engine())

    def _grok_run(self) -> str:
        with get_session() as session:
            run = Run(
                email="alice@hotmail.com",
                password="Pwd123!",
                profile_id="prof-grok",
                mail_provider="mail-default",
                platform="grok",
                status="pending",
                config_snapshot={"platform": "grok", "registration_kind": "grok"},
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run.id

    def test_grok_success_writes_sso(self):
        from src.api.worker import _execute_grok_inner
        run_id = self._grok_run()
        broadcaster = MagicMock()
        config = MagicMock()
        mail_api = MagicMock()
        with patch("src.automation.grok_runtime.run_grok_task", return_value="eyJ.sso.token"):
            _execute_grok_inner(run_id, broadcaster, config, mail_api, "prof-grok", "alice@hotmail.com", "Pwd123!")
        with get_session() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "success")
            self.assertEqual(run.sso_token, "eyJ.sso.token")
            self.assertEqual(run.account_tier, "registered")

    def test_grok_failure_marks_failed(self):
        from src.api.worker import _execute_grok_inner
        run_id = self._grok_run()
        broadcaster = MagicMock()
        with patch(
            "src.automation.grok_runtime.run_grok_task",
            side_effect=GrokRegistrationError("未提取到 sso token"),
        ):
            _execute_grok_inner(run_id, broadcaster, MagicMock(), MagicMock(), "prof-grok", "alice@hotmail.com", "Pwd")
        with get_session() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "failed")
            self.assertIn("grok_register_failed", run.error_reason or "")

    def test_grok_auto_allocates_email_when_blank(self):
        from src.api.worker import _execute_grok_inner
        # 空邮箱 + managed → 自动分配并回写 run.email
        with get_session() as session:
            run = Run(
                email="", password="Pwd", profile_id="prof-grok",
                mail_provider="mail-cfworker-default", platform="grok", status="pending",
                config_snapshot={"platform": "grok", "registration_kind": "grok"},
            )
            session.add(run); session.commit(); session.refresh(run)
            run_id = run.id
        mail_api = MagicMock()
        mail_api.ensure_runtime_ready.return_value = "managed"
        allocated = MagicMock()
        allocated.email = "auto.alloc@example.com"
        mail_api._provider.create_session.return_value = allocated
        captured = {}
        def _capture_email(**kwargs):
            captured["email"] = kwargs.get("email")
            return "eyJ.sso"
        with patch("src.api.worker._resolve_requested_email_from_identity", return_value=""), \
             patch("src.automation.grok_runtime.run_grok_task", side_effect=_capture_email):
            _execute_grok_inner(run_id, MagicMock(), MagicMock(), mail_api, "prof-grok", "", "Pwd")
        # 分配的邮箱回写 + 传给 run_grok_task
        self.assertEqual(captured.get("email"), "auto.alloc@example.com")
        with get_session() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.email, "auto.alloc@example.com")
            self.assertEqual(run.status, "success")

    def test_grok_mail_preflight_failure(self):
        from src.api.worker import _execute_grok_inner
        run_id = self._grok_run()
        mail_api = MagicMock()
        mail_api.ensure_runtime_ready.side_effect = RuntimeError("email-provider 500")
        with patch("src.automation.grok_runtime.run_grok_task") as mock_run:
            _execute_grok_inner(run_id, MagicMock(), MagicMock(), mail_api, "prof-grok", "alice@hotmail.com", "Pwd")
            mock_run.assert_not_called()  # 预检失败不应进入 run_grok_task
        with get_session() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "failed")
            self.assertIn("grok_mail_preflight_failed", run.error_reason or "")


class TestGrokCodePattern(unittest.TestCase):
    """Grok 验证码 pattern 正则正确性（防回归）。

    根因：Grok(x.ai) 码是 `810-XC2`（数字-字母混合带连字符），email-provider
    通用提取器只认 OpenAI 纯 6 位数字。team-register 透传 _GROK_CODE_PATTERN 修复。
    用真实邮件正文片段（run ae34564d 实测）断言能提取出码。
    """

    def setUp(self):
        import re
        self.re = re
        self.pat = gr._GROK_CODE_PATTERN

    def _extract(self, text):
        m = self.re.search(self.pat, text)
        return m.group(1) if m else None

    def test_extracts_numeric_prefix_code(self):
        # 实测样本 1（run ae34564d 邮件 22609）：前段纯数字
        text = "Please use the code below to validate your email address. 810-XC2 If you did not"
        self.assertEqual(self._extract(text), "810-XC2")

    def test_extracts_alpha_prefix_code(self):
        # 实测样本 2（run 82b1a4f8 邮件 22610）：前段字母数字混合 —— pattern 不能写死 \d{3}
        text = "Please use the code below to validate your email address. E52-GXZ If you did not"
        self.assertEqual(self._extract(text), "E52-GXZ")

    def test_extracts_with_html_noise_prefix(self):
        # 正文前常有 CSS/HTML 噪声，pattern 应仍命中（code 锚定）
        text = "#outlook a { padding: 0; } ... your code is 9X3-AB7 thanks"
        self.assertEqual(self._extract(text), "9X3-AB7")

    def test_no_false_match_on_pure_numeric(self):
        # 不应把纯数字（OpenAI 6 位码 / 年份）误当 Grok 码
        text = "your verification code is 482910 valid for 2026"
        self.assertIsNone(self._extract(text))

    def test_no_match_without_code_keyword(self):
        # 无 code 锚定时不乱抓随机 数字-字母 串（如颜色/编号）
        text = "ticket 100-AAA was created; ref 200-BBB"
        self.assertIsNone(self._extract(text))


if __name__ == "__main__":
    unittest.main()
