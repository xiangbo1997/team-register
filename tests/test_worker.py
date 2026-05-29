# -*- coding: utf-8 -*-
"""Worker 线程上下文与结构化事件测试。"""

import logging
import os
import unittest
from unittest.mock import MagicMock, patch

import src.db.engine as engine_mod
from src.api.worker import (
    LogBroadcastHandler,
    _execute_task_inner,
    _resolve_runtime_config,
    ensure_current_task_active,
    emit_current_task_event,
    set_current_task_state,
    update_current_task_run,
    update_current_task_tokens,
)
from src.db.engine import get_engine, get_session, init_db
from src.db.models import Run, RunEvent
from src.services.config_service import ConfigService
from src.services.event_service import EventBroadcaster
from sqlmodel import SQLModel, select


def _reset_engine():
    engine_mod._engine = None


class TestWorkerTaskBridge(unittest.TestCase):
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

    def _create_run(self) -> str:
        with get_session() as session:
            run = Run(
                email="apple@example.com",
                password="pass123",
                profile_id="prof-1",
                browser_provider="browser-default",
                card_provider="card-default",
                mail_provider="mail-default",
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run.id

    def _set_run_fields(self, run_id: str, **updates) -> None:
        with get_session() as session:
            run = session.get(Run, run_id)
            for key, value in updates.items():
                setattr(run, key, value)
            session.add(run)
            session.commit()

    def test_log_handler_includes_current_state(self):
        run_id = self._create_run()
        handler = LogBroadcastHandler()
        broadcaster = EventBroadcaster()

        LogBroadcastHandler.bind(run_id, broadcaster)
        try:
            set_current_task_state("VERIFY_EMAIL")
            record = logging.LogRecord(
                name="worker.test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="等待邮件中...",
                args=(),
                exc_info=None,
            )
            handler.emit(record)
        finally:
            LogBroadcastHandler.unbind()

        with get_session() as session:
            event = session.exec(select(RunEvent).order_by(RunEvent.timestamp.desc())).first()

        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "log")
        self.assertEqual(event.state, "VERIFY_EMAIL")
        self.assertEqual(event.payload["message"], "等待邮件中...")

    def test_emit_current_task_event_and_phase_update_use_bound_run(self):
        run_id = self._create_run()
        broadcaster = EventBroadcaster()

        LogBroadcastHandler.bind(run_id, broadcaster)
        try:
            set_current_task_state("AUTH")
            emit_current_task_event(
                "state_change",
                payload={"message": "进入密码页"},
            )
            update_current_task_run(phase="token_extraction")
        finally:
            LogBroadcastHandler.unbind()

        with get_session() as session:
            run = session.get(Run, run_id)
            event = session.exec(select(RunEvent).order_by(RunEvent.timestamp.desc())).first()

        self.assertEqual(run.phase, "token_extraction")
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "state_change")
        self.assertEqual(event.state, "AUTH")
        self.assertEqual(event.payload["message"], "进入密码页")

    def test_update_current_task_tokens_persists_to_run(self):
        """主流程提取 token 后通过此 helper 落库到 Run.openai_tokens。"""
        run_id = self._create_run()
        broadcaster = EventBroadcaster()

        LogBroadcastHandler.bind(run_id, broadcaster)
        try:
            update_current_task_tokens("ey-AT-main", "rt-main-xyz")
        finally:
            LogBroadcastHandler.unbind()

        with get_session() as session:
            run = session.get(Run, run_id)

        self.assertEqual(run.openai_tokens["access_token"], "ey-AT-main")
        self.assertEqual(run.openai_tokens["refresh_token"], "rt-main-xyz")
        self.assertTrue(run.openai_tokens["extracted_at"])  # ISO timestamp
        self.assertEqual(run.openai_tokens["id_token"], "")  # 占位

    def test_update_current_task_tokens_without_bound_run_is_noop(self):
        """没绑定 Worker 上下文（CLI 直跑场景）时静默跳过，不抛异常。"""
        # 不 bind() → 直接调
        try:
            update_current_task_tokens("a", "b")
        except Exception as exc:
            self.fail(f"应静默跳过，实际抛: {exc}")

    def test_resolve_runtime_config_uses_selected_mail_account(self):
        svc = ConfigService(dotenv_path="/tmp/__nonexistent__.env")
        svc.save_provider_config("browser", "browser-default", {"driver": "adspower", "ads_api": "http://ads.test"})
        svc.save_provider_config("card", "card-default", {"driver": "efuncard", "efuncard_token": "card-token"})
        svc.save_provider_config("mail", "mail-default", {"provider_name": "applemail", "session_mode": "credentialed"})
        account = svc.save_mail_account(
            label="Apple A",
            provider_name="applemail",
            email="apple@example.com",
            client_id="cid-1",
            refresh_token="rt-1",
            extra={"account_id": "acct-1"},
        )

        with get_session() as session:
            run = Run(
                email="apple@example.com",
                password="pass123",
                profile_id="prof-1",
                browser_provider="browser-default",
                card_provider="card-default",
                mail_provider="mail-default",
                mail_account_id=account.id,
            )
            session.add(run)
            session.commit()
            session.refresh(run)

        with patch("src.api.deps.get_config_service", return_value=svc):
            cfg = _resolve_runtime_config(run)

        self.assertEqual(cfg.email_provider_name, "applemail")
        self.assertEqual(cfg.mail_client_id, "cid-1")
        self.assertEqual(cfg.mail_refresh_token, "rt-1")
        self.assertIn("apple@example.com", cfg.known_mail_accounts_json)

    def test_resolve_runtime_config_rejects_mismatched_mail_account_email(self):
        svc = ConfigService(dotenv_path="/tmp/__nonexistent__.env")
        svc.save_provider_config("mail", "mail-default", {"provider_name": "applemail", "session_mode": "credentialed"})
        account = svc.save_mail_account(
            label="Apple A",
            provider_name="applemail",
            email="apple@example.com",
            client_id="cid-1",
            refresh_token="rt-1",
        )

        run = Run(
            email="other@example.com",
            password="pass123",
            profile_id="prof-1",
            mail_provider="mail-default",
            mail_account_id=account.id,
        )

        with patch("src.api.deps.get_config_service", return_value=svc):
            with self.assertRaisesRegex(RuntimeError, "任务邮箱"):
                _resolve_runtime_config(run)

    @patch("main.run_task")
    @patch("main._build_runtime_clients")
    @patch("src.api.worker._resolve_runtime_config")
    def test_execute_task_inner_fails_fast_when_mail_runtime_preflight_fails(
        self,
        mock_resolve_runtime_config,
        mock_build_runtime_clients,
        mock_run_task,
    ):
        run_id = self._create_run()
        broadcaster = EventBroadcaster()
        mail_api = unittest.mock.MagicMock()
        mail_api.ensure_runtime_ready.side_effect = RuntimeError("credentialed-sessions 缺失")
        mock_build_runtime_clients.return_value = (unittest.mock.MagicMock(), unittest.mock.MagicMock(), mail_api)
        mock_resolve_runtime_config.return_value = unittest.mock.MagicMock()

        _execute_task_inner(run_id, broadcaster)

        mock_run_task.assert_not_called()
        with get_session() as session:
            run = session.get(Run, run_id)
            events = list(session.exec(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.timestamp)).all())

        self.assertIsNotNone(run)
        self.assertEqual(run.status, "failed")
        self.assertIn("mail_runtime_preflight_failed", run.error_reason)
        self.assertTrue(
            any(
                event.event_type == "action"
                and event.payload.get("action_id") == "mail_runtime_preflight"
                and event.payload.get("result") == "failed"
                and "credentialed-sessions 缺失" in event.payload.get("error", "")
                for event in events
            )
        )

    @patch("main.run_task")
    @patch("main._build_runtime_clients")
    @patch("src.api.worker._resolve_runtime_config")
    def test_execute_task_inner_passes_resume_retry_mode_and_phase(
        self,
        mock_resolve_runtime_config,
        mock_build_runtime_clients,
        mock_run_task,
    ):
        run_id = self._create_run()
        self._set_run_fields(run_id, phase="payment", retry_mode="resume")
        broadcaster = EventBroadcaster()
        mail_api = unittest.mock.MagicMock()
        mail_api.ensure_runtime_ready.return_value = "credentialed"
        mock_build_runtime_clients.return_value = (
            unittest.mock.MagicMock(),
            unittest.mock.MagicMock(),
            mail_api,
        )
        mock_resolve_runtime_config.return_value = unittest.mock.MagicMock()

        _execute_task_inner(run_id, broadcaster)

        mock_run_task.assert_called_once()
        self.assertEqual(mock_run_task.call_args.kwargs["retry_mode"], "resume")
        self.assertEqual(mock_run_task.call_args.kwargs["start_phase"], "payment")

    @patch("main.run_task")
    @patch("main._build_runtime_clients")
    @patch("src.api.worker._resolve_runtime_config")
    def test_execute_task_inner_stops_when_task_is_cancelled_mid_run(
        self,
        mock_resolve_runtime_config,
        mock_build_runtime_clients,
        mock_run_task,
    ):
        run_id = self._create_run()
        broadcaster = EventBroadcaster()
        mail_api = unittest.mock.MagicMock()
        mail_api.ensure_runtime_ready.return_value = "credentialed"
        mock_build_runtime_clients.return_value = (
            unittest.mock.MagicMock(),
            unittest.mock.MagicMock(),
            mail_api,
        )
        mock_resolve_runtime_config.return_value = unittest.mock.MagicMock()

        def _cancel_during_run(**_kwargs):
            self._set_run_fields(run_id, status="cancelled")
            ensure_current_task_active("unit-test-mid-run")

        mock_run_task.side_effect = _cancel_during_run

        LogBroadcastHandler.bind(run_id, broadcaster)
        try:
            _execute_task_inner(run_id, broadcaster)
        finally:
            LogBroadcastHandler.unbind()

        with get_session() as session:
            run = session.get(Run, run_id)
            events = list(
                session.exec(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.timestamp)
                ).all()
            )

        self.assertEqual(run.status, "cancelled")
        self.assertTrue(
            any(
                event.event_type == "action"
                and event.payload.get("action_id") == "task_cancelled"
                for event in events
            )
        )


class TestWorkerConcurrency(unittest.TestCase):
    """验证 Worker 并发上限可配置、受范围约束。"""

    def setUp(self):
        import src.api.worker as worker_mod
        self._worker = worker_mod
        # 重置 worker 模块内部状态，保证每个用例独立
        with worker_mod._lock:
            if worker_mod._executor is not None:
                try:
                    worker_mod._executor.shutdown(wait=False)
                except Exception:
                    pass
            worker_mod._executor = None
            worker_mod._max_workers = None

    def tearDown(self):
        # 避免泄漏到其他测试
        with self._worker._lock:
            if self._worker._executor is not None:
                try:
                    self._worker._executor.shutdown(wait=False)
                except Exception:
                    pass
            self._worker._executor = None
            self._worker._max_workers = None

    def test_max_workers_default_is_two(self):
        """未设置 MAX_WORKERS 时默认 2。"""
        # 清空可能残留的环境变量
        saved = os.environ.pop("MAX_WORKERS", None)
        try:
            status = self._worker.get_worker_status()
            self.assertEqual(status["max_workers"], 2)
        finally:
            if saved is not None:
                os.environ["MAX_WORKERS"] = saved

    def test_max_workers_respects_env(self):
        """MAX_WORKERS=6 时 worker 层应读到 6。"""
        with patch.dict(os.environ, {"MAX_WORKERS": "6"}, clear=False):
            # 清缓存强制重新解析
            self._worker._max_workers = None
            status = self._worker.get_worker_status()
            self.assertEqual(status["max_workers"], 6)

    def test_max_workers_clamped_to_range(self):
        """超出 [1, 32] 时回退默认值 2。"""
        with patch.dict(os.environ, {"MAX_WORKERS": "999"}, clear=False):
            self._worker._max_workers = None
            self.assertEqual(self._worker.get_worker_status()["max_workers"], 2)

        with patch.dict(os.environ, {"MAX_WORKERS": "0"}, clear=False):
            self._worker._max_workers = None
            self.assertEqual(self._worker.get_worker_status()["max_workers"], 2)

    def test_override_max_workers_rebuilds_executor(self):
        """override_max_workers 立即生效并重建线程池。"""
        self._worker.override_max_workers(4)
        self.assertEqual(self._worker.get_worker_status()["max_workers"], 4)
        self.assertIsNotNone(self._worker._executor)

    def test_override_max_workers_ignores_out_of_range(self):
        """override_max_workers 超范围直接忽略且不改变状态。"""
        self._worker.override_max_workers(3)
        self._worker.override_max_workers(999)
        self.assertEqual(self._worker.get_worker_status()["max_workers"], 3)


class TestLogBroadcastSanitization(unittest.TestCase):
    """LogBroadcastHandler.emit 应对敏感字段做脱敏。"""

    def setUp(self):
        # 每个用例独立 fake broadcaster，避免跨用例串扰
        self.broadcaster = MagicMock()
        LogBroadcastHandler._local = __import__("threading").local()
        LogBroadcastHandler.bind("run-sanitize", self.broadcaster)

    def tearDown(self):
        LogBroadcastHandler.unbind()

    def _emit(self, message: str) -> str:
        handler = LogBroadcastHandler()
        record = logging.LogRecord(
            name="worker.sanitize.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        self.assertTrue(self.broadcaster.emit_sync.called)
        kwargs = self.broadcaster.emit_sync.call_args.kwargs
        return kwargs["payload"]["message"]

    def test_log_broadcast_sanitizes_bearer_token(self):
        out = self._emit("请求头: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature")
        self.assertIn("Bearer ***", out)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9.payload.signature", out)

    def test_log_broadcast_sanitizes_long_hex_token(self):
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
        out = self._emit(f"token={token} 结束")
        # 保留首尾 4 位便于调试，中间掩掉
        self.assertIn("a1b2***8f90", out)
        self.assertNotIn(token, out)

    def test_log_broadcast_sanitizes_password_query(self):
        out = self._emit("callback url: https://example.com/cb?password=hunter2&x=1")
        self.assertIn("password=***", out)
        self.assertNotIn("hunter2", out)

    def test_log_broadcast_sanitizes_pwd_and_pass_aliases(self):
        out_pwd = self._emit("conn: pwd=s3cret!")
        self.assertIn("pwd=***", out_pwd)
        self.assertNotIn("s3cret!", out_pwd)

        out_pass = self._emit("login pass=topsecret123")
        self.assertIn("pass=***", out_pass)
        self.assertNotIn("topsecret123", out_pass)

    def test_log_broadcast_sanitizes_card_number(self):
        out = self._emit("绑定卡号 4242424242424242 成功")
        self.assertIn("************4242", out)
        self.assertNotIn("4242424242424242", out)

    def test_log_broadcast_preserves_short_tokens(self):
        # 6 位短验证码不能被掩码（调试需要）
        out = self._emit("收到短信验证码 123456，已填入")
        self.assertIn("123456", out)

    def test_log_broadcast_does_not_mutate_record(self):
        """脱敏只作用于 payload 副本，不能改写原始 LogRecord。"""
        handler = LogBroadcastHandler()
        original = "Bearer eyJabcdef.ghijkl.mnopqr"
        record = logging.LogRecord(
            name="worker.sanitize.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=original,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        self.assertEqual(record.getMessage(), original)


class TestDecideFinalOutcome(unittest.TestCase):
    """worker._decide_final_outcome：注册 Run 的终态判定逻辑（方案 A）。

    核心约束：
      - final_state == "HOME" → success，无论 has_error 如何（修复 reached_HOME_but_error_logged 误判）
      - final_state != "HOME" + has_error → failed (error_logged_at_state=XXX)
      - final_state != "HOME" + no has_error → failed (silent_failure_at_state=XXX)
    """

    def test_home_without_error_is_success(self):
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("HOME", has_error=False)
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["error_reason"], "")
        self.assertFalse(outcome["has_warning"])
        self.assertEqual(outcome["message"], "自动化流程执行完成")
        self.assertEqual(outcome["i18n_key"], "task_events.orchestrator_complete")

    def test_home_with_error_is_still_success_with_warning(self):
        """到 HOME + 有 ERROR → 仍标 success，但 has_warning=True（修复历史误判 bug）。"""
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("HOME", has_error=True)
        self.assertEqual(outcome["status"], "success", "到 HOME 即视为成功，不应因 ERROR 翻盘")
        self.assertEqual(outcome["error_reason"], "")
        self.assertTrue(outcome["has_warning"])
        self.assertIn("warning", outcome["message"])

    def test_lowercase_home_normalized_to_success(self):
        """final_state 大小写不敏感（worker 已 upper 但纯函数应自防御）。"""
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("home", has_error=False)
        self.assertEqual(outcome["status"], "success")

    def test_intermediate_state_with_error_is_failed(self):
        """未到 HOME + 有 ERROR → failed with error_logged_at_state。"""
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("VERIFY_EMAIL", has_error=True)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_reason"], "error_logged_at_state=VERIFY_EMAIL")
        self.assertFalse(outcome["has_warning"])
        self.assertIn("VERIFY_EMAIL", outcome["message"])
        self.assertEqual(outcome["i18n_key"], "task_events.orchestrator_complete_with_error")

    def test_intermediate_state_without_error_is_silent_failure(self):
        """未到 HOME + 无 ERROR → failed with silent_failure_at_state。"""
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("ABOUT_YOU", has_error=False)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_reason"], "silent_failure_at_state=ABOUT_YOU")
        self.assertIn("ABOUT_YOU", outcome["message"])

    def test_empty_state_with_error_yields_unknown(self):
        """状态为空（极端：从未进入任何 state）→ failed, UNKNOWN 占位。"""
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("", has_error=True)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_reason"], "error_logged_at_state=UNKNOWN")

    def test_empty_state_without_error_yields_silent_failure_unknown(self):
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome("", has_error=False)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_reason"], "silent_failure_at_state=UNKNOWN")

    def test_none_state_is_treated_as_empty(self):
        """final_state=None（worker 入参防御）→ 等同空串。"""
        from src.api.worker import _decide_final_outcome
        outcome = _decide_final_outcome(None, has_error=False)  # type: ignore[arg-type]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error_reason"], "silent_failure_at_state=UNKNOWN")


class TestWorkerNoLongerOwnsExitIpCapture(unittest.TestCase):
    """worker._capture_and_persist_exit_ip 已被删除。

    职责拆分为：
    - DB 落库 → ``src/orchestration/preflight.write_run_exit_ip``（见 test_preflight.py）
    - 浏览器内抓 IP → ``main.run_task`` 内 ``fetch_exit_ip_from_page``（线上 E2E 验证）

    本测试仅留一道护栏：确保 worker 模块里没人再 import 该死代码。
    """

    def test_function_is_removed(self):
        from src.api import worker as worker_mod
        self.assertFalse(
            hasattr(worker_mod, "_capture_and_persist_exit_ip"),
            "_capture_and_persist_exit_ip 已废弃；IP 抓取改到 main.run_task 浏览器启动后做。"
            " 历史 bug：旧实现在浏览器启动前用 Python httpx 抓 ipinfo.io，"
            " 跟 AdsPower 注入的住宅代理是两条网络栈，永远抓不到正确 IP。",
        )

    def test_write_run_exit_ip_is_canonical_path(self):
        """write_run_exit_ip 是新版唯一公开的 IP 落库入口。"""
        from src.orchestration.preflight import write_run_exit_ip
        self.assertTrue(callable(write_run_exit_ip))
