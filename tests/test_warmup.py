# -*- coding: utf-8 -*-
"""卡预热模块单元测试（v2 - ChatGPT UI Upgrade 路径，两轮循环）

覆盖：
  - 号池为空 / disabled 等前置 guard
  - 凭据缺失（防御性校验）
  - AdsPower 启动失败 → 写 record_warmup_outcome(failure)
  - pro_account_login 失败 → record_warmup_outcome(failure)
  - 第 1 轮 navigate_to_pro_checkout 失败 → record failure(upgrade_failed_round1)
  - 第 2 轮 navigate_to_pro_checkout 失败 → record failure(upgrade_failed_round2)
  - Stripe iframe 缺失 → 跳过本轮但整体仍继续，两轮都跑完 = success
  - 端到端：两轮 navigate+select+fill+submit 全部跑完 → record success
  - card declined（detect_checkout_error 返回报错）不影响整体成功
  - 资源回收：page.close() 始终被调

Mock 策略：
  - svc.select_warmup_account / record_warmup_outcome 用 MagicMock 注入
  - sync_playwright + get_browser_ws + pro_account_login + navigate_to_pro_checkout
    + select_pro_tier + Stripe handlers 全部 patch
  - page.wait_for_timeout / page.go_back / page.locator 都 mock 掉，避免真等 60s
"""

import unittest
from contextlib import contextmanager
from unittest import mock

from src.config import AppConfig
from src.db.models import MailAccount
from src.models import CardInfo
from src.orchestration.handlers import WarmupUpgradeNotFound
from src.orchestration.warmup import _classify_failure, execute_card_warmup


def _make_card() -> CardInfo:
    return CardInfo(
        card_number="4111111111111111",
        expiry_month="12",
        expiry_year="2030",
        cvv="123",
    )


def _make_config(*, enable_card_warmup: bool = True) -> AppConfig:
    """构造测试用 AppConfig，避免触发 .env 必填校验。"""
    cfg = AppConfig.__new__(AppConfig)
    cfg.enable_card_warmup = enable_card_warmup
    cfg.payment_plan = "team"
    cfg.aimizy_country = "SG"
    cfg.aimizy_currency = "SGD"
    cfg.proxy = ""
    cfg.ads_api = "http://local.adspower.net:50325"
    cfg.ads_api_key = "k"
    return cfg


def _make_account(
    *,
    account_id: str = "acc-1",
    email: str = "warm@example.test",
    password: str = "secret",
    profile_id: str = "pf-1",
) -> MailAccount:
    return MailAccount(
        id=account_id,
        label="warmup-test",
        provider_name="applemail",
        email=email,
        client_id="",
        refresh_token="",
        extra={"password": password, "adspower_profile_id": profile_id},
        role="pro_warmup",
        is_active=True,
    )


def _make_svc(account=None) -> mock.MagicMock:
    """构造 mock ConfigService，select 返回指定账号或 None。"""
    svc = mock.MagicMock()
    svc.select_warmup_account.return_value = account
    # 默认无 mail_api（pro_account_login 走密码流降级）
    svc.mail_api = None
    return svc


@contextmanager
def _patch_playwright_chain(*, page_mock: mock.MagicMock, logged_in: bool = False):
    """统一 mock sync_playwright + AdsPower → context → page。

    Args:
        page_mock: 注入的 page mock 对象
        logged_in: is_chatgpt_logged_in 返回值。默认 False（走完整登录路径，与历史
            行为一致，避免老测试 regress）；True 则模拟"账号已登录"走快路径。

    Yields:
        dict 含可访问的 mocks：{"prepare_clean": <mock>, "is_logged_in": <mock>}，
        便于 caller 在嵌套 with 里直接断言被调次数。
    """
    context_mock = mock.MagicMock()
    context_mock.new_page.return_value = page_mock
    browser_mock = mock.MagicMock()
    browser_mock.contexts = [context_mock]
    pw_mock = mock.MagicMock()
    pw_mock.chromium.connect_over_cdp.return_value = browser_mock
    pw_ctx = mock.MagicMock()
    pw_ctx.__enter__.return_value = pw_mock
    pw_ctx.__exit__.return_value = False
    page_mock.evaluate.return_value = "Mozilla/5.0 (test)"
    # 默认让 prepare_clean_warmup_page 直接返回 page_mock，避免真去清理 cookies
    # 默认让 is_chatgpt_logged_in 返回 False，让现有测试走慢路径（保持兼容）
    with mock.patch("src.orchestration.warmup.sync_playwright", return_value=pw_ctx), \
         mock.patch("src.orchestration.warmup.get_browser_ws", return_value="ws://test"), \
         mock.patch("src.orchestration.warmup.prepare_clean_warmup_page", return_value=page_mock) as mock_prep, \
         mock.patch("src.orchestration.warmup.is_chatgpt_logged_in", return_value=logged_in) as mock_is_logged, \
         mock.patch("src.orchestration.warmup.human_delay", new=lambda *_a, **_kw: None):
        yield {"prepare_clean": mock_prep, "is_logged_in": mock_is_logged}


# ────────────────────────────────────────────────────
# 前置 guard
# ────────────────────────────────────────────────────


class TestExecuteCardWarmupGuards(unittest.TestCase):
    """前置 guard：disabled / 池空 / 凭据缺失"""

    def test_disabled_returns_false_without_picking_account(self):
        svc = _make_svc()
        cfg = _make_config(enable_card_warmup=False)
        with mock.patch("src.orchestration.warmup.get_browser_ws") as mock_ws:
            result = execute_card_warmup(cfg, _make_card(), mock.MagicMock(), "ck", svc=svc)
        self.assertFalse(result)
        svc.select_warmup_account.assert_not_called()
        mock_ws.assert_not_called()

    def test_empty_pool_returns_false(self):
        svc = _make_svc(account=None)
        cfg = _make_config()
        with mock.patch("src.orchestration.warmup.get_browser_ws") as mock_ws:
            result = execute_card_warmup(cfg, _make_card(), mock.MagicMock(), "ck", svc=svc)
        self.assertFalse(result)
        svc.select_warmup_account.assert_called_once()
        mock_ws.assert_not_called()
        svc.record_warmup_outcome.assert_not_called()

    def test_missing_profile_id_records_failure_without_browser(self):
        """缺 adspower_profile_id → 必须失败（运行时 hard requirement）。

        历史曾把"缺 password"也当硬失败，但 password 在 OAuth-only 邮箱
        （applemail + Microsoft Graph 等）下本来就该留空。现在只强制 email +
        adspower_profile_id 必填，password 缺失走 magic link / OAuth 路径。
        """
        bad_acc = _make_account()
        bad_acc.extra = {"password": "pw"}  # 缺 adspower_profile_id
        svc = _make_svc(account=bad_acc)
        cfg = _make_config()
        with mock.patch("src.orchestration.warmup.get_browser_ws") as mock_ws:
            result = execute_card_warmup(cfg, _make_card(), mock.MagicMock(), "ck", svc=svc)
        self.assertFalse(result)
        mock_ws.assert_not_called()
        svc.record_warmup_outcome.assert_called_once()
        _args, kwargs = svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertIn("missing_credentials", kwargs["reason"])
        self.assertIn("profile=False", kwargs["reason"])

    def test_missing_password_no_longer_fails_at_credential_check(self):
        """缺 password 不再阻止预热启动（OAuth-only 邮箱场景）。

        这条 test 进 AdsPower 启动后会 mock get_browser_ws raise 让流程在那里失败 —
        关键断言是 ``get_browser_ws 被调一次``（说明凭据校验已通过）。
        """
        bad_acc = _make_account()
        bad_acc.extra = {"adspower_profile_id": "pf"}  # 缺 password
        svc = _make_svc(account=bad_acc)
        cfg = _make_config()
        with mock.patch(
            "src.orchestration.warmup.get_browser_ws",
            side_effect=RuntimeError("adspower down"),
        ) as mock_ws:
            execute_card_warmup(cfg, _make_card(), mock.MagicMock(), "ck", svc=svc)
        # 缺 password 不在凭据校验阶段拦下，流程会继续到 AdsPower 启动
        mock_ws.assert_called_once()
        _args, kwargs = svc.record_warmup_outcome.call_args
        # 这次失败原因是 adspower_failed，不是 missing_credentials
        self.assertIn("adspower", kwargs["reason"].lower())

    def test_adspower_failure_records_failure(self):
        svc = _make_svc(account=_make_account())
        cfg = _make_config()
        with mock.patch(
            "src.orchestration.warmup.get_browser_ws",
            side_effect=RuntimeError("adspower down"),
        ):
            result = execute_card_warmup(cfg, _make_card(), mock.MagicMock(), "ck", svc=svc)
        self.assertFalse(result)
        svc.record_warmup_outcome.assert_called_once()
        _args, kwargs = svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertIn("adspower_failed", kwargs["reason"])


# ────────────────────────────────────────────────────
# 浏览器路径 v2（ChatGPT UI Upgrade + 两轮循环）
# ────────────────────────────────────────────────────


class TestExecuteCardWarmupBrowserPath(unittest.TestCase):
    """登录 → 两轮 navigate+select+fill+submit 路径各分支"""

    def setUp(self):
        self.cfg = _make_config()
        self.card = _make_card()
        self.card_api = mock.MagicMock()
        # 默认每轮拿到 3DS 验证码（best-effort，不拿到也不算失败）
        self.card_api.wait_for_3ds.return_value = "123456"
        self.account = _make_account()
        self.svc = _make_svc(account=self.account)
        self.page = mock.MagicMock()
        # 默认 locator.first.is_visible 返回 True（让 submit / go_back 后的 plan-page 检测路径都通过）
        self.page.locator.return_value.first.is_visible.return_value = True

    def _run(self):
        with _patch_playwright_chain(page_mock=self.page):
            return execute_card_warmup(
                self.cfg, self.card, self.card_api, "ck", svc=self.svc,
            )

    # — 登录失败 —
    def test_pro_account_login_failure_records_login_failed(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=False):
            result = self._run()
        self.assertFalse(result)
        self.page.close.assert_called()
        self.svc.record_warmup_outcome.assert_called_once()
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertEqual(kwargs["reason"], "login_failed")

    # — 第 1 轮 navigate 失败 —
    def test_round1_upgrade_not_found_records_failure(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch(
                 "src.orchestration.warmup.navigate_to_pro_checkout",
                 side_effect=WarmupUpgradeNotFound("Claim offer 入口缺失"),
             ):
            result = self._run()
        self.assertFalse(result)
        self.page.close.assert_called()
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertIn("upgrade_failed_round1", kwargs["reason"])

    # — 第 1 轮 select_pro_tier 失败 —
    def test_round1_select_tier_failure_records_failure(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout"), \
             mock.patch(
                 "src.orchestration.warmup.select_pro_tier",
                 side_effect=WarmupUpgradeNotFound("button#chatgptpro 找不到"),
             ):
            result = self._run()
        self.assertFalse(result)
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertIn("upgrade_failed_round1", kwargs["reason"])

    # — Stripe iframe 缺失：跳过本轮 fill，但整体两轮跑完仍 success —
    def test_stripe_form_missing_skips_fill_but_still_succeeds(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout"), \
             mock.patch("src.orchestration.warmup.select_pro_tier"), \
             mock.patch("src.orchestration.warmup.wait_for_stripe_form", return_value=False), \
             mock.patch("src.orchestration.warmup.fill_checkout_card") as mock_fill, \
             mock.patch("src.orchestration.warmup.handle_checkout_3ds_challenge"), \
             mock.patch("src.orchestration.warmup.detect_checkout_error", return_value=""):
            result = self._run()
        # iframe 没出现也不算失败 — 两轮 navigate+select 都通过即视作 success
        self.assertTrue(result)
        mock_fill.assert_not_called()
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertTrue(kwargs["success"])

    # — 端到端两轮全跑完：success —
    def test_two_rounds_complete_records_success(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout") as mock_nav, \
             mock.patch("src.orchestration.warmup.select_pro_tier") as mock_tier, \
             mock.patch("src.orchestration.warmup.wait_for_stripe_form", return_value=True), \
             mock.patch("src.orchestration.warmup.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.warmup.handle_checkout_3ds_challenge"), \
             mock.patch("src.orchestration.warmup.detect_checkout_error", return_value=""):
            result = self._run()
        self.assertTrue(result)
        self.page.close.assert_called()
        # navigate 至少被调 1 次（第 1 轮强制；第 2 轮 mock 的 page.locator/go_back 走兜底也可能再调）
        self.assertGreaterEqual(mock_nav.call_count, 1)
        # 两轮都该调 select_pro_tier
        self.assertEqual(mock_tier.call_count, 2)
        # 两轮 amount_usd 顺序：第 1 轮 200，第 2 轮 100
        amounts = [c.kwargs.get("amount_usd") for c in mock_tier.call_args_list]
        self.assertEqual(amounts, [200, 100])
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertTrue(kwargs["success"])

    # — 卡被拒（detect_checkout_error 返回 declined）也算 success —
    def test_card_declined_does_not_block_overall_success(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout"), \
             mock.patch("src.orchestration.warmup.select_pro_tier"), \
             mock.patch("src.orchestration.warmup.wait_for_stripe_form", return_value=True), \
             mock.patch("src.orchestration.warmup.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.warmup.handle_checkout_3ds_challenge"), \
             mock.patch(
                 "src.orchestration.warmup.detect_checkout_error",
                 return_value="Your card was declined.",
             ):
            result = self._run()
        # declined 是预期内（账号余额不足），两轮跑完仍算成功
        self.assertTrue(result)
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertTrue(kwargs["success"])

    # — 异常逃逸仍关 page + record failure —
    def test_unexpected_exception_still_closes_page_and_records_failure(self):
        with mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch(
                 "src.orchestration.warmup.navigate_to_pro_checkout",
                 side_effect=RuntimeError("boom"),
             ):
            result = self._run()
        self.assertFalse(result)
        self.page.close.assert_called()
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertIn("exception", kwargs["reason"])


# ──────────────────────────────────────────────────────────────────────
# 快路径（已登录跳过 pro_account_login）+ 降级 fallback
# ──────────────────────────────────────────────────────────────────────


class TestExecuteCardWarmupQuickPath(unittest.TestCase):
    """新增"先查登录态"分支的 4 种场景：

    1. 已登录 → 跳过 prepare_clean_warmup_page + pro_account_login，直接 navigate
    2. 未登录 → 走原有完整登录路径（已被 TestExecuteCardWarmupBrowserPath 覆盖，本类只补充对照）
    3. 快路径下第 1 轮 navigate 失败 → 自动降级走完整登录重试 → 整体 success
    4. 快路径降级后第 2 轮 navigate 仍失败 → 不再降级，直接 fail
    """

    def setUp(self):
        self.cfg = _make_config()
        self.card = _make_card()
        self.card_api = mock.MagicMock()
        self.card_api.wait_for_3ds.return_value = "123456"
        self.account = _make_account()
        self.svc = _make_svc(account=self.account)
        self.page = mock.MagicMock()
        self.page.locator.return_value.first.is_visible.return_value = True

    # — 1. 已登录 → 跳过登录，直接走 navigate + 两轮成功 —
    def test_quick_path_skips_login_when_already_logged_in(self):
        with _patch_playwright_chain(page_mock=self.page, logged_in=True) as chain_mocks, \
             mock.patch("src.orchestration.warmup.pro_account_login") as mock_login, \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout") as mock_nav, \
             mock.patch("src.orchestration.warmup.select_pro_tier") as mock_tier, \
             mock.patch("src.orchestration.warmup.wait_for_stripe_form", return_value=True), \
             mock.patch("src.orchestration.warmup.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.warmup.handle_checkout_3ds_challenge"), \
             mock.patch("src.orchestration.warmup.detect_checkout_error", return_value=""):
            result = execute_card_warmup(
                self.cfg, self.card, self.card_api, "ck", svc=self.svc,
            )
        self.assertTrue(result)
        # 关键断言：快路径 → 不调 pro_account_login / prepare_clean_warmup_page
        mock_login.assert_not_called()
        chain_mocks["prepare_clean"].assert_not_called()
        chain_mocks["is_logged_in"].assert_called()
        # 两轮 navigate + select 仍照常
        self.assertGreaterEqual(mock_nav.call_count, 1)
        self.assertEqual(mock_tier.call_count, 2)
        # 成功写回号池
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertTrue(kwargs["success"])

    # — 2. 未登录 → 走完整登录路径（与历史行为一致，本测试确认默认 logged_in=False 走老路径）—
    def test_full_path_used_when_not_logged_in(self):
        with _patch_playwright_chain(page_mock=self.page, logged_in=False) as chain_mocks, \
             mock.patch("src.orchestration.warmup.pro_account_login", return_value=True) as mock_login, \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout"), \
             mock.patch("src.orchestration.warmup.select_pro_tier"), \
             mock.patch("src.orchestration.warmup.wait_for_stripe_form", return_value=True), \
             mock.patch("src.orchestration.warmup.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.warmup.handle_checkout_3ds_challenge"), \
             mock.patch("src.orchestration.warmup.detect_checkout_error", return_value=""):
            result = execute_card_warmup(
                self.cfg, self.card, self.card_api, "ck", svc=self.svc,
            )
        self.assertTrue(result)
        # 关键断言：未登录 → pro_account_login + prepare_clean_warmup_page 都被调
        mock_login.assert_called()
        chain_mocks["prepare_clean"].assert_called()

    # — 3. 快路径 navigate 第 1 次失败 → 等 5s 重试 navigate（不清 token） → 后续 OK → 整体 success —
    def test_quick_path_retries_navigate_on_first_failure_without_relogin(self):
        """快路径 navigate 第一次失败 → 重试一次（不调 pro_account_login，不清 cookies）。

        关键约束（用户明确）：两次绑卡之间不能重新登录。所以快路径 navigate 失败
        不再触发"清 cookies + 重新登录"降级，改成纯 navigate 重试。
        """
        nav_calls = {"n": 0}

        def nav_side_effect(*_a, **_kw):
            nav_calls["n"] += 1
            # 第 1 次失败（在快路径里），第 2 次重试 + 后续都成功
            if nav_calls["n"] == 1:
                raise WarmupUpgradeNotFound("Claim offer 入口缺失（快路径首次）")

        with _patch_playwright_chain(page_mock=self.page, logged_in=True) as chain_mocks, \
             mock.patch("src.orchestration.warmup.pro_account_login") as mock_login, \
             mock.patch("src.orchestration.warmup.navigate_to_pro_checkout", side_effect=nav_side_effect), \
             mock.patch("src.orchestration.warmup.select_pro_tier"), \
             mock.patch("src.orchestration.warmup.wait_for_stripe_form", return_value=True), \
             mock.patch("src.orchestration.warmup.fill_checkout_card", return_value="unified_frame"), \
             mock.patch("src.orchestration.warmup.handle_checkout_3ds_challenge"), \
             mock.patch("src.orchestration.warmup.detect_checkout_error", return_value=""):
            result = execute_card_warmup(
                self.cfg, self.card, self.card_api, "ck", svc=self.svc,
            )
        self.assertTrue(result)
        # 关键断言：快路径重试**不**调 pro_account_login，不清 cookies
        mock_login.assert_not_called()
        chain_mocks["prepare_clean"].assert_not_called()
        # navigate 被调 ≥2 次（第 1 次失败 + 第 2 次重试成功 + 第 2 轮可能再 navigate）
        self.assertGreaterEqual(nav_calls["n"], 2)
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertTrue(kwargs["success"])

    # — 4. 快路径下 navigate 持续失败 → 重试一次仍失败，不再重试，直接 fail —
    def test_quick_path_navigate_retry_only_once(self):
        with _patch_playwright_chain(page_mock=self.page, logged_in=True) as chain_mocks, \
             mock.patch("src.orchestration.warmup.pro_account_login") as mock_login, \
             mock.patch(
                 "src.orchestration.warmup.navigate_to_pro_checkout",
                 side_effect=WarmupUpgradeNotFound("一直找不到入口"),
             ):
            result = execute_card_warmup(
                self.cfg, self.card, self.card_api, "ck", svc=self.svc,
            )
        self.assertFalse(result)
        # 仍然不调 pro_account_login（约束：不重新登录）
        mock_login.assert_not_called()
        chain_mocks["prepare_clean"].assert_not_called()
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        # 失败原因含 round1 + after_retry（区分快路径首次失败和重试后仍失败）
        self.assertIn("upgrade_failed_round1", kwargs["reason"])
        self.assertIn("after_retry", kwargs["reason"])


# ──────────────────────────────────────────────────────────────────────
# P0c: 失败原因分类 _classify_failure
# ──────────────────────────────────────────────────────────────────────


class TestClassifyFailure(unittest.TestCase):
    """_classify_failure：把 reason 字符串映射到 account_failure / external_failure。"""

    def test_adspower_failure_classified_external(self):
        self.assertEqual(_classify_failure("adspower_failed:connection refused"), "external_failure")

    def test_mail_5xx_classified_external(self):
        self.assertEqual(
            _classify_failure(
                "exception:创建邮箱会话失败: 500 Server Error for url: https://x/managed-sessions"
            ),
            "external_failure",
        )

    def test_timeout_classified_external(self):
        self.assertEqual(_classify_failure("Page.goto: Timeout 20000ms exceeded"), "external_failure")

    def test_login_failed_classified_account(self):
        # 单纯 login_failed 没邮件/网络关键词 → 账号自身问题（cookies 失效 / 风控）
        self.assertEqual(_classify_failure("login_failed"), "account_failure")

    def test_upgrade_not_found_classified_account(self):
        self.assertEqual(
            _classify_failure("upgrade_failed_round1:Claim offer 入口缺失"),
            "account_failure",
        )

    def test_empty_reason_falls_back_to_account(self):
        self.assertEqual(_classify_failure(""), "account_failure")
        self.assertEqual(_classify_failure(None), "account_failure")

    def test_case_insensitive(self):
        self.assertEqual(_classify_failure("ADSPOWER hung"), "external_failure")
        self.assertEqual(_classify_failure("HTTP 503"), "external_failure")

    # ── 配置错 override 规则（即便走 mail 链路也归 account_failure）──

    def test_provider_not_configured_overrides_external(self):
        """reason 同时含 mailbox-service 和 PROVIDER_NOT_CONFIGURED → 仍归 account_failure。

        这是运维配置错，应当让 consecutive_failures 累计 + 触发自动 disable，
        让运维注意修配置而不是当外部抖动忽略。
        """
        reason = "mailbox-service | PROVIDER_NOT_CONFIGURED | MissingProviderConfigError: config_name 必填"
        self.assertEqual(_classify_failure(reason), "account_failure")

    def test_missing_fields_keyword_classified_account(self):
        reason = "mail-service: missing_fields=['config_name', 'cfworker_api_url']"
        self.assertEqual(_classify_failure(reason), "account_failure")

    def test_provider_upstream_error_classified_external(self):
        """PROVIDER_UPSTREAM_ERROR (cfworker 上游 4xx) 归 external — 不是 account 配置问题。"""
        reason = "mailbox-service | PROVIDER_UPSTREAM_ERROR | ProviderUpstreamError: CF Worker rejected"
        self.assertEqual(_classify_failure(reason), "external_failure")

    def test_mailbox_runtime_incompat_classified_external(self):
        reason = "mailbox-service | MAILBOX_RUNTIME_INCOMPAT | service returned 502"
        self.assertEqual(_classify_failure(reason), "external_failure")


# ──────────────────────────────────────────────────────────────────────
# P0c: 端到端验证 record_warmup_outcome 收到正确的 failure_class
# ──────────────────────────────────────────────────────────────────────


class TestExecuteCardWarmupFailureClassification(unittest.TestCase):
    """端到端验证：execute_card_warmup 失败时调用 record_warmup_outcome 时
    传的 failure_class 与失败原因匹配。"""

    def setUp(self):
        self.cfg = _make_config()
        self.card = _make_card()
        self.card_api = mock.MagicMock()
        self.account = _make_account()
        self.svc = _make_svc(account=self.account)

    def test_adspower_failure_passes_external_failure_class(self):
        with mock.patch(
            "src.orchestration.warmup.get_browser_ws",
            side_effect=RuntimeError("adspower down"),
        ):
            execute_card_warmup(self.cfg, self.card, self.card_api, "ck", svc=self.svc)
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertEqual(
            kwargs.get("failure_class"), "external_failure",
            "AdsPower 启动失败应当传 external_failure，避免远程抖动 disable 号池",
        )

    def test_login_failed_due_to_mail_5xx_passes_external(self):
        """登录失败但 reason 含 mail 5xx 关键词 → 应被识别为 external。"""
        page = mock.MagicMock()
        page.locator.return_value.first.is_visible.return_value = True

        # pro_account_login 抛 mail-related 异常（pro_account_login 内部捕获了，但
        # 我们模拟它把 reason 写成 mail 5xx 形态 — 通过 navigate 抛异常路径）
        with _patch_playwright_chain(page_mock=page), \
             mock.patch("src.orchestration.warmup.pro_account_login", return_value=True), \
             mock.patch(
                 "src.orchestration.warmup.navigate_to_pro_checkout",
                 side_effect=RuntimeError(
                     "creating mailbox-service session failed: 500 Internal Server Error"
                 ),
             ):
            execute_card_warmup(self.cfg, self.card, self.card_api, "ck", svc=self.svc)
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertEqual(
            kwargs.get("failure_class"), "external_failure",
            "邮件 5xx 关键词应被识别为 external_failure",
        )

    def test_pure_login_failed_passes_account_failure(self):
        """登录失败、没有外部关键词 → account_failure，正常累计计数。"""
        page = mock.MagicMock()
        page.locator.return_value.first.is_visible.return_value = True

        with _patch_playwright_chain(page_mock=page), \
             mock.patch("src.orchestration.warmup.pro_account_login", return_value=False):
            execute_card_warmup(self.cfg, self.card, self.card_api, "ck", svc=self.svc)
        _args, kwargs = self.svc.record_warmup_outcome.call_args
        self.assertFalse(kwargs["success"])
        self.assertEqual(
            kwargs.get("failure_class"), "account_failure",
            "纯 login_failed（无外部关键词）应当 account_failure，让 3 次累计 disable 生效",
        )

    def test_missing_credentials_passes_account_failure(self):
        """缺 adspower_profile_id（必填硬条件）→ account_failure。

        password 不再是必填（OAuth-only 邮箱场景），所以这个测试改成测 profile_id
        缺失，那是真正的硬必填项（没有 AdsPower 浏览器无法跑 warmup）。
        """
        bad = _make_account()
        bad.extra = {"password": "pw"}  # 缺 adspower_profile_id
        svc = _make_svc(account=bad)
        with mock.patch("src.orchestration.warmup.get_browser_ws"):
            execute_card_warmup(self.cfg, self.card, self.card_api, "ck", svc=svc)
        _args, kwargs = svc.record_warmup_outcome.call_args
        self.assertEqual(
            kwargs.get("failure_class"), "account_failure",
            "凭据缺失是账号配置问题，应当 account_failure",
        )


if __name__ == "__main__":
    unittest.main()
