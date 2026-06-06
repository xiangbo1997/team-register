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

    def _mock_real_input_page(self, evaluate_side_effect, fill_value="Pwd123!"):
        """构造 page，使 _fill_profile_real_keyboard / _click_submit_real（Playwright locator
        真实键盘路径，isTrusted=true 修复）全部成功，evaluate 仅用于 Turnstile 检测序列。

        每次 page.locator(sel) 返回新 MagicMock：input_value() 回读返回目标值（填充校验通过），
        click/press_sequentially/wait_for/scroll_into_view_if_needed 均为 noop。
        """
        page = self._page(evaluate_side_effect=evaluate_side_effect)

        def _make_locator(selector, *_a, **_k):
            loc = MagicMock()
            loc.first = loc
            # 提交按钮无 input_value 语义；输入框回读返回对应填充值。
            if "submit" in selector:
                return loc
            # 姓名框回读返回 _gen_name 的值无法预知，故让回读始终匹配 press 的值：
            # 用一个会"记住"最后 press 内容的 side_effect。
            state = {"val": ""}
            def _press_seq(value, *_pa, **_pk):
                state["val"] = value
            loc.press_sequentially.side_effect = _press_seq
            loc.input_value.side_effect = lambda: state["val"]
            return loc

        page.locator.side_effect = _make_locator
        return page

    def test_fill_profile_happy_no_turnstile(self):
        # 真实键盘填姓名/密码 → solve state(not-found) → wait_ready(not-found) → 提交确认
        # 提交细节由 _submit_profile_and_confirm 专项测试覆盖，这里 mock 隔离
        page = self._mock_real_input_page(evaluate_side_effect=["not-found", "not-found"])
        solver_rt = MagicMock()
        try_solve = MagicMock(return_value=True)
        with patch("time.sleep"), patch(
            "src.automation.grok_runtime._submit_profile_and_confirm", return_value=True
        ) as mock_submit:
            gr._fill_profile(page, "Pwd123!", solver_rt, try_solve, _noop_emit)
        try_solve.assert_not_called()  # 无 turnstile 不调 solver
        mock_submit.assert_called_once()

    def test_fill_profile_solves_turnstile(self):
        # 真实键盘填充 → solve state(pending) → get token → sync → wait_ready(ready) → 提交
        page = self._mock_real_input_page(evaluate_side_effect=["pending", "tok-abc", True, "ready"])
        solver_rt = MagicMock()
        try_solve = MagicMock(return_value=True)
        with patch("time.sleep"), patch(
            "src.automation.grok_runtime._submit_profile_and_confirm", return_value=True
        ):
            gr._fill_profile(page, "Pwd123!", solver_rt, try_solve, _noop_emit)
        try_solve.assert_called_once()

    def test_submit_waits_for_turnstile_then_submits(self):
        # 你的洞察：轮询确认勾选才点。token ready 后 requestSubmit，验证页面前进
        page = MagicMock()
        # _turnstile_token_present → ready；_profile_left_page → 第一次还在，第二次离开
        states = iter(["ready", "ready"] + ["pending"] * 0)
        present = iter([True, False])  # _JS_PROFILE_STILL_PRESENT: 提交后消失
        def _eval(js, *a):
            if "cf-turnstile-response" in js and "value" in js:
                return "ready"  # _JS_TURNSTILE_STATE
            if "querySelector('input[name=\"cf-turnstile-response\"]')" in js and "return !!" in js:
                return next(present, False)  # _JS_PROFILE_STILL_PRESENT
            if "requestSubmit" in js:
                return True
            return None
        page.evaluate.side_effect = _eval
        with patch("time.sleep"):
            ok = gr._submit_profile_and_confirm(page, _noop_emit)
        self.assertTrue(ok)

    def test_submit_returns_false_when_page_never_advances(self):
        # 提交多次页面都没前进（cf-turnstile-response 一直在）→ 返回 False
        page = MagicMock()
        def _eval(js, *a):
            if "cf-turnstile-response" in js and "value" in js:
                return "ready"
            if "return !!" in js:
                return True  # 一直在资料页
            if "requestSubmit" in js:
                return True
            return None
        page.evaluate.side_effect = _eval
        page.locator.return_value.first = MagicMock()
        with patch("time.sleep"):
            ok = gr._submit_profile_and_confirm(page, _noop_emit, max_attempts=2)
        self.assertFalse(ok)

    def test_submit_polls_3_rounds_before_giving_up_on_token(self):
        # token 一直未就绪 → 轮询 3 轮后仍尝试提交（标 warning），不死等
        page = MagicMock()
        def _eval(js, *a):
            if "cf-turnstile-response" in js and "value" in js:
                return "pending"  # token 永远没勾上
            if "return !!" in js:
                return False  # 但页面前进了（模拟检测延迟）
            if "requestSubmit" in js:
                return True
            return None
        page.evaluate.side_effect = _eval
        with patch("time.sleep"):
            ok = gr._submit_profile_and_confirm(page, _noop_emit)
        self.assertTrue(ok)  # 页面前进即成功

    def test_wait_turnstile_manual_handoff_human_clicks(self):
        # 被动超时后人工接管：第一次 poll pending，第二次 ready（人点了）→ 返回 True
        page = MagicMock()
        page.evaluate.side_effect = ["pending", "ready"]
        with patch("time.sleep"):
            ok = gr._wait_turnstile_manual_handoff(page, _noop_emit, config=None)
        self.assertTrue(ok)

    def test_wait_turnstile_manual_handoff_timeout(self):
        # 无人值守：一直 pending 到超时 → 返回 False（不抛，由上层走失败）
        page = MagicMock()
        page.evaluate.return_value = "pending"
        # time.time 序列：构造 deadline 计算 + 循环判断超时
        with patch("time.sleep"), patch(
            "time.time", side_effect=[0, 0, 0, 1, 200, 200]
        ):
            ok = gr._wait_turnstile_manual_handoff(page, _noop_emit, config=None)
        self.assertFalse(ok)

    def test_wait_turnstile_manual_handoff_respects_config_timeout(self):
        # config.grok_turnstile_manual_handoff_sec 覆盖默认时长
        page = MagicMock()
        page.evaluate.return_value = "ready"
        cfg = MagicMock()
        cfg.grok_turnstile_manual_handoff_sec = 60
        with patch("time.sleep"):
            ok = gr._wait_turnstile_manual_handoff(page, _noop_emit, config=cfg)
        self.assertTrue(ok)

    def test_fill_profile_enters_manual_handoff_on_passive_timeout(self):
        # 被动验证超时（_wait_turnstile_ready 返回 False）→ 触发人工接管轮询
        # evaluate 序列：solve state(not-found) → wait_ready 超时(全 pending) → manual ready
        page = self._mock_real_input_page(
            evaluate_side_effect=["not-found", "pending", "ready"]
        )
        solver_rt = MagicMock()
        try_solve = MagicMock(return_value=True)
        with patch("time.sleep"), patch(
            "src.automation.grok_runtime._wait_turnstile_ready", return_value=False
        ) as mock_ready, patch(
            "src.automation.grok_runtime._wait_turnstile_manual_handoff", return_value=True
        ) as mock_manual:
            gr._fill_profile(page, "Pwd123!", solver_rt, try_solve, _noop_emit)
        mock_ready.assert_called_once()
        mock_manual.assert_called_once()  # 被动超时 → 进人工接管

    def test_fill_profile_skips_manual_handoff_when_passive_ok(self):
        # 被动验证通过（_wait_turnstile_ready 返回 True）→ 不进人工接管
        page = self._mock_real_input_page(evaluate_side_effect=["not-found"])
        solver_rt = MagicMock()
        try_solve = MagicMock(return_value=True)
        with patch("time.sleep"), patch(
            "src.automation.grok_runtime._wait_turnstile_ready", return_value=True
        ), patch(
            "src.automation.grok_runtime._wait_turnstile_manual_handoff"
        ) as mock_manual:
            gr._fill_profile(page, "Pwd123!", solver_rt, try_solve, _noop_emit)
        mock_manual.assert_not_called()  # 被动 OK 不需要人工

    def test_fill_otp_real_keyboard_aggregate_box(self):
        # 单聚合框：press_sequentially 填整个码，回读校验通过
        page = MagicMock()
        agg = MagicMock()
        agg.get_attribute.return_value = "6"  # maxlength=6 → 聚合框
        agg.input_value.return_value = "GE4DGT"
        loc = MagicMock(); loc.first = agg
        page.locator.return_value = loc
        with patch("time.sleep"), patch("random.randint", return_value=50):
            ok = gr._fill_otp_real_keyboard(page, "GE4DGT")
        self.assertTrue(ok)
        agg.press_sequentially.assert_called_once()

    def test_fill_otp_real_keyboard_empty_code(self):
        page = MagicMock()
        self.assertFalse(gr._fill_otp_real_keyboard(page, ""))

    def test_select_clean_page_reuses_existing_grok_tab(self):
        # profile 有 x.ai tab → 复用它，关掉 OpenAI 残留 tab
        grok_pg = MagicMock(); grok_pg.url = "https://accounts.x.ai/sign-up"
        openai_pg = MagicMock(); openai_pg.url = "https://chatgpt.com/"
        ctx = MagicMock(); ctx.pages = [openai_pg, grok_pg]
        result = gr._select_clean_grok_page(ctx, _noop_emit)
        self.assertIs(result, grok_pg)  # 复用 x.ai tab
        openai_pg.close.assert_called_once()  # 关掉 OpenAI tab
        grok_pg.close.assert_not_called()
        ctx.new_page.assert_not_called()  # 不需要新开

    def test_select_clean_page_opens_new_when_no_grok_tab(self):
        # profile 只有 OpenAI 残留 tab → 新开干净 tab，关掉 OpenAI
        openai_pg = MagicMock(); openai_pg.url = "https://accounts.openai.com/"
        new_pg = MagicMock(); new_pg.url = "about:blank"
        ctx = MagicMock(); ctx.pages = [openai_pg]; ctx.new_page.return_value = new_pg
        result = gr._select_clean_grok_page(ctx, _noop_emit)
        self.assertIs(result, new_pg)  # 用新开的
        ctx.new_page.assert_called_once()
        openai_pg.close.assert_called_once()  # 关掉 OpenAI 残留

    def test_select_clean_page_closes_blank_tabs(self):
        # 关掉 about:blank / 非 x.ai 残留 tab
        grok_pg = MagicMock(); grok_pg.url = "https://grok.com/"
        blank_pg = MagicMock(); blank_pg.url = ""
        ctx = MagicMock(); ctx.pages = [grok_pg, blank_pg]
        result = gr._select_clean_grok_page(ctx, _noop_emit)
        self.assertIs(result, grok_pg)
        blank_pg.close.assert_called_once()

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
