# -*- coding: utf-8 -*-
"""
后台任务 Worker

使用线程池执行注册任务，通过 EventBroadcaster 广播进度到 SSE。
通过 LogBroadcastHandler 将 run_task 产生的日志自动转为实时事件。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.api.i18n import build_i18n_message_payload
from src.db.engine import get_session
from src.db.models import Run
from src.services.event_service import EventBroadcaster

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 日志脱敏：复刻 src/automation/artifacts.py 的精神，针对 SSE 出站做轻量防护
# ---------------------------------------------------------------------------
# 注意：不修改 LogRecord 本身，只对要广播出去的消息副本做脱敏，避免影响其他 handler。
_BEARER_TOKEN_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*")
_PASSWORD_QUERY_RE = re.compile(r"(?i)(password|pwd|pass)\s*=\s*[^\s&\"'<>]+")
# 说明：为避免把 "token=..." 整段当成 token（因为 "=" 属于 base64 padding），
# 中段只允许字母/数字/+//_/-，末尾可选 "=" 做 padding。边界检查只排除非 "=" 的 token 字符，
# 这样 "name=abc..." 里 "=" 就被当作分隔符，其后的值能独立匹配。
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/_-])([A-Za-z0-9+/_-]{32,}=*)(?![A-Za-z0-9+/_-])")
_CARD_NUMBER_RE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")


def _mask_long_token(match: "re.Match[str]") -> str:
    """长 token 保留首尾各 4 位便于排障，中间统一替换为 ***。"""
    token = match.group(1)
    if len(token) <= 8:
        return token
    return f"{token[:4]}***{token[-4:]}"


def _mask_card_number(match: "re.Match[str]") -> str:
    """卡号保留末尾 4 位，其余用 * 遮蔽。"""
    digits = match.group(1)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def _sanitize_log_message(message: str) -> str:
    """对即将广播的日志消息执行脱敏。"""
    if not message:
        return message
    sanitized = _BEARER_TOKEN_RE.sub(r"\1***", message)
    sanitized = _PASSWORD_QUERY_RE.sub(r"\1=***", sanitized)
    sanitized = _CARD_NUMBER_RE.sub(_mask_card_number, sanitized)
    sanitized = _LONG_TOKEN_RE.sub(_mask_long_token, sanitized)
    return sanitized


class LogBroadcastHandler(logging.Handler):
    """
    将日志消息自动广播为 SSE 事件的 logging Handler。

    通用设计：挂到根 logger 上，只在 Worker 线程执行期间生效。
    通过 thread-local 绑定的 run_id 和 broadcaster 实现多任务隔离。
    """

    _local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        run_id = getattr(self._local, "run_id", None)
        broadcaster = getattr(self._local, "broadcaster", None)
        current_state = getattr(self._local, "current_state", None)
        if not run_id or not broadcaster:
            return

        # 映射 log level 到事件类型
        if record.levelno >= logging.ERROR:
            self._local.has_error = True
            event_type = "error"
        elif record.levelno >= logging.WARNING:
            event_type = "warning"
        else:
            event_type = "log"

        try:
            # 先取消息副本再脱敏；不修改 LogRecord，避免干扰其他 handler。
            safe_message = _sanitize_log_message(record.getMessage())
            broadcaster.emit_sync(
                run_id,
                event_type,
                state=current_state,
                payload={
                    "message": safe_message,
                    "logger": record.name,
                    "level": record.levelname,
                },
            )
        except Exception:
            # 广播失败不能影响业务流程
            pass

    @classmethod
    def bind(cls, run_id: str, broadcaster: EventBroadcaster) -> None:
        """为当前线程绑定 run_id 和 broadcaster。"""
        cls._local.run_id = run_id
        cls._local.broadcaster = broadcaster
        cls._local.has_error = False
        cls._local.current_state = None
        cls._local.cancel_notified = False

    @classmethod
    def set_current_state(cls, state: str | None) -> None:
        """为当前线程绑定自动化状态，便于日志事件携带 state 字段。"""
        cls._local.current_state = state or None

    @classmethod
    def get_current_state(cls) -> str | None:
        return getattr(cls._local, "current_state", None)

    @classmethod
    def check_has_error(cls) -> bool:
        """检查当前线程执行期间是否捕获到 ERROR 级别日志。"""
        return getattr(cls._local, "has_error", False)

    @classmethod
    def unbind(cls) -> None:
        """解除当前线程的绑定。"""
        cls._local.run_id = None
        cls._local.broadcaster = None
        cls._local.current_state = None
        cls._local.cancel_notified = False


# 全局安装 LogBroadcastHandler（只添加一次）
_broadcast_handler: Optional[LogBroadcastHandler] = None


def _ensure_broadcast_handler() -> None:
    """确保 LogBroadcastHandler 已安装到根 logger。"""
    global _broadcast_handler
    if _broadcast_handler is not None:
        return
    _broadcast_handler = LogBroadcastHandler()
    _broadcast_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(_broadcast_handler)

# 最大并发任务数（Playwright 同步阻塞，不宜太多）
# 首次创建线程池时按 AppConfig.max_workers 决议，失败则回退到 _DEFAULT_MAX_WORKERS。
_DEFAULT_MAX_WORKERS = 2
_MAX_WORKERS_MIN = 1
_MAX_WORKERS_MAX = 32
_max_workers: Optional[int] = None

_executor: Optional[ThreadPoolExecutor] = None
_lock = threading.Lock()


def _resolve_max_workers() -> int:
    """从 AppConfig.max_workers 读取上限；异常时回退默认值。"""
    try:
        from src.config import load_config  # 延迟导入避免测试场景下的循环依赖
        value = int(getattr(load_config(), "max_workers", _DEFAULT_MAX_WORKERS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 AppConfig.max_workers 失败，回退默认值 %d: %s", _DEFAULT_MAX_WORKERS, exc)
        return _DEFAULT_MAX_WORKERS
    if value < _MAX_WORKERS_MIN or value > _MAX_WORKERS_MAX:
        logger.warning(
            "AppConfig.max_workers=%d 超出允许范围 [%d, %d]，回退默认值 %d",
            value,
            _MAX_WORKERS_MIN,
            _MAX_WORKERS_MAX,
            _DEFAULT_MAX_WORKERS,
        )
        return _DEFAULT_MAX_WORKERS
    return value


def _current_max_workers() -> int:
    """获取当前生效的 max_workers（若尚未初始化则按需解析一次）。"""
    global _max_workers
    if _max_workers is None:
        _max_workers = _resolve_max_workers()
    return _max_workers


def override_max_workers(n: int) -> None:
    """
    测试/排障辅助：强制覆盖 max_workers 并重建线程池。

    - 清空缓存值 → 重建 ThreadPoolExecutor；
    - 旧线程池会被 shutdown(wait=False)，在途任务继续跑完，新任务走新池；
    - 参数范围外会被忽略并记录 warning。
    """
    global _executor, _max_workers
    if n < _MAX_WORKERS_MIN or n > _MAX_WORKERS_MAX:
        logger.warning(
            "override_max_workers(%d) 超出允许范围 [%d, %d]，已忽略",
            n,
            _MAX_WORKERS_MIN,
            _MAX_WORKERS_MAX,
        )
        return
    with _lock:
        _max_workers = n
        if _executor is not None:
            try:
                _executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.debug("旧线程池关闭时忽略异常", exc_info=True)
        _executor = ThreadPoolExecutor(
            max_workers=n,
            thread_name_prefix="task-worker",
        )

# 跟踪正在运行的任务，防止重复提交
_running_tasks: set[str] = set()
_cancel_requests: set[str] = set()

_RETRY_MODES = {"resume", "restart"}
_RESUMABLE_PHASES = {"registration", "token_extraction", "payment"}


class TaskCancelledError(RuntimeError):
    """任务被用户取消。"""


def _mark_cancel_requested(run_id: str) -> None:
    with _lock:
        _cancel_requests.add(run_id)


def _clear_cancel_requested(run_id: str) -> None:
    with _lock:
        _cancel_requests.discard(run_id)


def _is_cancel_requested(run_id: str) -> bool:
    with _lock:
        if run_id in _cancel_requests:
            return True

    try:
        with get_session() as session:
            run = session.get(Run, run_id)
            return bool(run and run.status == "cancelled")
    except Exception as exc:
        logger.warning("检查任务 %s 取消状态失败，按未取消继续: %s", run_id, exc)
        return False


def _current_task_context() -> tuple[str | None, EventBroadcaster | None, str | None]:
    run_id = getattr(LogBroadcastHandler._local, "run_id", None)
    broadcaster = getattr(LogBroadcastHandler._local, "broadcaster", None)
    state = LogBroadcastHandler.get_current_state()
    return run_id, broadcaster, state


def _emit_cancelled_once(run_id: str, *, checkpoint: str = "") -> None:
    _, broadcaster, state = _current_task_context()
    if not run_id or not broadcaster:
        return
    if getattr(LogBroadcastHandler._local, "cancel_notified", False):
        return

    payload = build_i18n_message_payload(
        "检测到任务已取消，停止继续执行",
        "task_events.task_cancelled",
        action_id="task_cancelled",
        result="cancelled",
    )
    if checkpoint:
        payload["checkpoint"] = checkpoint
    try:
        broadcaster.emit_sync(run_id, "action", state=state, payload=payload)
    finally:
        LogBroadcastHandler._local.cancel_notified = True


def ensure_task_active(run_id: str, checkpoint: str = "") -> None:
    """在关键步骤前检查任务是否已被取消。"""
    if not run_id:
        return
    if not _is_cancel_requested(run_id):
        return
    _emit_cancelled_once(run_id, checkpoint=checkpoint)
    raise TaskCancelledError(f"task_cancelled: run_id={run_id} checkpoint={checkpoint or 'unknown'}")


def ensure_current_task_active(checkpoint: str = "") -> None:
    """检查当前 Worker 线程绑定任务是否已被取消。"""
    run_id, _broadcaster, _state = _current_task_context()
    if not run_id:
        return
    ensure_task_active(run_id, checkpoint=checkpoint)


def request_task_cancel(run_id: str, broadcaster: EventBroadcaster | None = None) -> None:
    """记录取消请求，并向当前订阅者广播取消动作。"""
    _mark_cancel_requested(run_id)
    if broadcaster is None:
        return
    try:
        broadcaster.emit_sync(
            run_id,
            "action",
            payload=build_i18n_message_payload(
                "已收到取消请求，正在尽快停止任务",
                "task_events.cancel_requested",
                action_id="cancel_task",
                result="requested",
            ),
        )
    except Exception:
        logger.debug("广播任务 %s 取消请求失败（忽略）。", run_id)


def _resolve_retry_mode(value: str | None) -> str:
    normalized = str(value or "restart").strip().lower()
    return normalized if normalized in _RETRY_MODES else "restart"


def _resolve_start_phase(run: Run) -> str:
    retry_mode = _resolve_retry_mode(getattr(run, "retry_mode", "restart"))
    if retry_mode != "resume":
        return "registration"
    phase = str(getattr(run, "phase", "") or "registration").strip().lower()
    return phase if phase in _RESUMABLE_PHASES else "registration"


def _get_executor() -> ThreadPoolExecutor:
    """懒初始化线程池：首次访问时按 AppConfig.max_workers 决议。"""
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_current_max_workers(),
                    thread_name_prefix="task-worker",
                )
    return _executor


def submit_task(run_id: str, broadcaster: EventBroadcaster) -> bool:
    """
    将任务提交到线程池执行。

    Args:
        run_id: Run 记录 ID
        broadcaster: 事件广播器实例

    Returns:
        True 表示成功提交，False 表示已在运行或提交失败
    """
    with _lock:
        if run_id in _running_tasks:
            logger.warning("任务 %s 已在运行中，跳过重复提交。", run_id)
            return False
        _running_tasks.add(run_id)

    future = _get_executor().submit(_execute_task, run_id, broadcaster)
    future.add_done_callback(lambda f: _on_task_done(f, run_id))
    logger.info("任务 %s 已提交到线程池。", run_id)
    return True


def _on_task_done(future, run_id: str) -> None:
    """任务完成回调：清理跟踪集合，记录未捕获异常。"""
    with _lock:
        _running_tasks.discard(run_id)
        _cancel_requests.discard(run_id)

    exc = future.exception()
    if exc is not None:
        if isinstance(exc, TaskCancelledError):
            logger.info("任务 %s 已取消并退出 Worker。", run_id)
            return
        logger.error("任务 %s Worker 线程未捕获异常: %s", run_id, exc)
        _update_run_status(run_id, "failed", error_reason=f"worker_exception: {exc}")


def _execute_task(run_id: str, broadcaster: EventBroadcaster) -> None:
    """
    Worker 线程入口：加载 Run → 构建依赖 → 执行 run_task。

    复用 main.py 已有的完整流程，不重新实现业务逻辑。
    通过 LogBroadcastHandler 将所有日志自动转为 SSE 事件推送到前端。
    """
    # 安装日志广播 handler 并绑定当前线程
    _ensure_broadcast_handler()
    LogBroadcastHandler.bind(run_id, broadcaster)

    try:
        _execute_task_inner(run_id, broadcaster)
    finally:
        LogBroadcastHandler.unbind()


def _resolve_runtime_config(run: Run):
    """把任务级 provider / account 选择折叠到运行时配置对象。"""
    from src.api.deps import get_config_service

    svc = get_config_service()
    config = svc.get_config()

    browser_profile = svc.resolve_provider_config(
        "browser",
        run.browser_provider,
        default_name=config.default_browser_provider,
    )
    if browser_profile is not None:
        browser_payload = dict(browser_profile.config or {})
        driver = str(browser_payload.get("driver") or "adspower").strip().lower()
        if driver != "adspower":
            raise RuntimeError(f"暂不支持的 browser provider.driver: {driver}")
        config.ads_api = str(browser_payload.get("ads_api") or browser_payload.get("api_url") or config.ads_api).strip() or config.ads_api
        config.ads_api_key = str(browser_payload.get("ads_api_key") or config.ads_api_key).strip()
        browser_proxy = str(browser_payload.get("proxy") or "").strip()
        if browser_proxy:
            config.proxy = browser_proxy

    card_profile = svc.resolve_provider_config(
        "card",
        run.card_provider,
        default_name=config.default_card_provider,
    )
    if card_profile is not None:
        card_payload = dict(card_profile.config or {})
        config.card_provider = str(card_payload.get("driver") or config.card_provider).strip().lower() or config.card_provider
        if "efuncard_token" in card_payload:
            config.efuncard_token = str(card_payload.get("efuncard_token") or "").strip()
        if "nodecard_api_url" in card_payload:
            config.nodecard_api_url = str(card_payload.get("nodecard_api_url") or "").strip() or config.nodecard_api_url
        if "nodecard_merchant_id" in card_payload:
            config.nodecard_merchant_id = int(card_payload.get("nodecard_merchant_id") or 0)
        if "nodecard_platform_id" in card_payload:
            config.nodecard_platform_id = int(card_payload.get("nodecard_platform_id") or 0)
        if "x988card_api_base" in card_payload:
            config.x988card_api_base = (
                str(card_payload.get("x988card_api_base") or "").strip().rstrip("/")
                or config.x988card_api_base
            )
        if "x988card_request_timeout" in card_payload:
            try:
                config.x988card_request_timeout = int(card_payload.get("x988card_request_timeout") or 15)
            except (TypeError, ValueError):
                pass

    mail_profile = svc.resolve_provider_config(
        "mail",
        run.mail_provider,
        default_name=config.default_mail_provider,
    )
    if mail_profile is not None:
        mail_payload = dict(mail_profile.config or {})
        config.email_provider_name = (
            str(mail_payload.get("provider_name") or config.email_provider_name).strip().lower() or config.email_provider_name
        )
        session_mode = str(mail_payload.get("session_mode") or "").strip().lower()
        if session_mode:
            setattr(config, "mail_session_mode_override", session_mode)

        # 取 mail-default 里 admin UI 配的 provider_config 名（如 mydomain-cfworker）
        config.mail_config_name = str(mail_payload.get("config_name") or "").strip()

        mail_proxy = str(mail_payload.get("proxy") or "").strip()
        if mail_proxy:
            config.proxy = mail_proxy

    selected_account_id = str(run.mail_account_id or config.default_mail_account_id).strip()
    if selected_account_id:
        account = svc.get_mail_account(selected_account_id)
        if account is None or not account.is_active:
            raise RuntimeError("任务指定的邮箱账号不存在或已停用")
        if account.provider_name != config.email_provider_name:
            raise RuntimeError("任务指定的邮箱账号与 mail provider 不匹配")
        # worker 侧再次兜底，避免历史任务/默认账号残留导致邮箱与凭据错绑。
        if str(account.email or "").strip().lower() != str(run.email or "").strip().lower():
            raise RuntimeError("任务邮箱与所选邮箱账号不一致，请确保一个邮箱对应一个 client_id/refresh_token")
        account_extra = dict(account.extra or {})
        known_account_payload = {
            "label": account.label,
            "email": account.email,
            "client_id": account.client_id,
            "refresh_token": account.refresh_token,
            "extra": account_extra,
        }
        if account_extra.get("account_id"):
            known_account_payload["account_id"] = str(account_extra.get("account_id") or "")
        if "preserve_existing_mail" in account_extra:
            known_account_payload["preserve_existing_mail"] = bool(account_extra.get("preserve_existing_mail"))
        config.known_mail_accounts_json = json.dumps(
            {config.email_provider_name: [known_account_payload]},
            ensure_ascii=False,
        )
        config.mail_client_id = account.client_id
        config.mail_refresh_token = account.refresh_token
    elif getattr(config, "mail_session_mode_override", "") == "managed":
        config.known_mail_accounts_json = ""
        config.mail_client_id = ""
        config.mail_refresh_token = ""

    return config


def _execute_task_inner(run_id: str, broadcaster: EventBroadcaster) -> None:
    """实际执行逻辑（独立函数方便 finally 清理）。"""
    # 1. 加载 Run 记录
    with get_session() as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error("任务 %s 不存在，跳过执行。", run_id)
            return
        email = run.email
        password = run.password
        profile_id = run.profile_id
        card_key = run.card_key
        browser_provider = run.browser_provider
        card_provider = run.card_provider
        mail_provider = run.mail_provider
        mail_account_id = run.mail_account_id
        retry_mode = _resolve_retry_mode(run.retry_mode)
        start_phase = _resolve_start_phase(run)

    if _is_cancel_requested(run_id):
        logger.info("任务 %s 在启动前已被取消，跳过执行。", run_id)
        _emit_cancelled_once(run_id, checkpoint="before_start")
        return

    # 2. 更新状态为 running
    _update_run_status(run_id, "running", phase=start_phase, error_reason=None)
    broadcaster.emit_sync(
        run_id, "orchestrator_start",
        payload=build_i18n_message_payload(
            "任务开始执行",
            "task_events.orchestrator_start",
            retry_mode=retry_mode,
            start_phase=start_phase,
        ),
    )
    if retry_mode == "resume":
        broadcaster.emit_sync(
            run_id,
            "action",
            payload=build_i18n_message_payload(
                f"重试模式：从 {start_phase} 阶段继续执行",
                "task_events.retry_mode_resume",
                params={"start_phase": start_phase},
                action_id="retry_mode",
                result="resume",
                retry_mode=retry_mode,
                start_phase=start_phase,
            ),
        )

    # 3. 加载配置并构建运行时依赖
    try:
        ensure_task_active(run_id, "before_runtime_config")
        run.browser_provider = browser_provider
        run.card_provider = card_provider
        run.mail_provider = mail_provider
        run.mail_account_id = mail_account_id
        run.retry_mode = retry_mode
        config = _resolve_runtime_config(run)

        from main import _build_runtime_clients, run_task
        card_api, sms_api, mail_api = _build_runtime_clients(config)

    except TaskCancelledError:
        _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
        return
    except Exception as exc:
        logger.error("任务 %s 初始化失败: %s", run_id, exc)
        _update_run_status(run_id, "failed", error_reason=f"init_failed: {exc}")
        return

    try:
        ensure_task_active(run_id, "before_mail_runtime_preflight")
        resolved_session_mode = mail_api.ensure_runtime_ready(email)
        broadcaster.emit_sync(
            run_id,
            "action",
            state="VERIFY_EMAIL",
            payload=build_i18n_message_payload(
                f"邮箱服务运行态预检通过（session_mode={resolved_session_mode}）",
                "task_events.mail_runtime_preflight_ok",
                params={"session_mode": resolved_session_mode},
                action_id="mail_runtime_preflight",
                result="ok",
            ),
        )

        # 邮箱留空 → 走自动分配模式（仅 managed providers 支持）
        if not email and resolved_session_mode == "managed":
            ensure_task_active(run_id, "before_mail_auto_allocate")
            allocated = mail_api._provider.create_session(
                provider=mail_api._provider_name,
                purpose="auto-allocate",
                session_mode="managed",
                config_name=mail_api._config_name,
            )
            email = allocated.email
            # 立刻释放预分配的 session（实际收码用时再创建新 session，避免占住）
            try:
                mail_api._provider.complete(allocated, result="success", reason="auto-allocate probe")
            except Exception:
                pass
            # 回写 run.email，让后续 OpenAI 注册流程使用这个邮箱
            with get_session() as s:
                run_to_update = s.get(Run, run_id)
                if run_to_update:
                    run_to_update.email = email
                    run_to_update.updated_at = datetime.now(timezone.utc)
                    s.add(run_to_update)
                    s.commit()
            logger.info("自动分配邮箱: %s", email)
            broadcaster.emit_sync(
                run_id,
                "action",
                state="VERIFY_EMAIL",
                payload=build_i18n_message_payload(
                    f"自动分配邮箱: {email}",
                    "task_events.mail_auto_allocated",
                    params={"email": email},
                    action_id="mail_auto_allocate",
                    result="ok",
                ),
            )
    except TaskCancelledError:
        _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
        return
    except Exception as exc:
        logger.error("任务 %s 邮箱服务运行态预检失败: %s", run_id, exc)
        broadcaster.emit_sync(
            run_id,
            "action",
            state="VERIFY_EMAIL",
            payload=build_i18n_message_payload(
                "邮箱服务运行态预检失败",
                "task_events.mail_runtime_preflight_failed",
                action_id="mail_runtime_preflight",
                result="failed",
                error=str(exc),
            ),
        )
        _update_run_status(run_id, "failed", error_reason=f"mail_runtime_preflight_failed: {exc}")
        return

    # 4. 执行主流程（日志会通过 LogBroadcastHandler 自动推送到 SSE）
    try:
        run_task(
            config=config,
            card_api=card_api,
            sms_api=sms_api,
            mail_api=mail_api,
            ads_id=profile_id,
            cdk=card_key,
            email=email,
            password=password,
            retry_mode=retry_mode,
            start_phase=start_phase,
        )

        if _is_cancel_requested(run_id):
            broadcaster.emit_sync(
                run_id,
                "orchestrator_complete",
                payload=build_i18n_message_payload(
                    "任务已取消，自动化已停止",
                    "task_events.orchestrator_cancelled",
                ),
            )
            _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
            return

        # 通过日志级别判断真实执行结果（run_task 失败时不抛异常，只记录 ERROR 日志）
        if LogBroadcastHandler.check_has_error():
            broadcaster.emit_sync(
                run_id, "orchestrator_complete",
                payload=build_i18n_message_payload(
                    "自动化流程执行完成，但检测到错误",
                    "task_events.orchestrator_complete_with_error",
                ),
            )
            _update_run_status(run_id, "failed", error_reason="execution_error_detected")
        else:
            broadcaster.emit_sync(
                run_id, "orchestrator_complete",
                payload=build_i18n_message_payload(
                    "自动化流程执行完成",
                    "task_events.orchestrator_complete",
                ),
            )
            _update_run_status(run_id, "success")

    except TaskCancelledError:
        logger.warning("任务 %s 执行过程中收到取消信号，停止后续步骤。", run_id)
        broadcaster.emit_sync(
            run_id,
            "orchestrator_complete",
            payload=build_i18n_message_payload(
                "任务已取消，自动化已停止",
                "task_events.orchestrator_cancelled",
            ),
        )
        _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
    except Exception as exc:
        logger.error("任务 %s 执行失败: %s", run_id, exc)
        _update_run_status(run_id, "failed", error_reason=str(exc))


def _update_run_status(
    run_id: str,
    status: str,
    *,
    phase: Optional[str] = None,
    error_reason: Optional[str] = None,
) -> None:
    """更新 Run 记录状态。"""
    try:
        with get_session() as session:
            run = session.get(Run, run_id)
            if run:
                run.status = status
                if phase:
                    run.phase = phase
                if error_reason:
                    run.error_reason = error_reason
                run.updated_at = datetime.now(timezone.utc)
                session.add(run)
                session.commit()
    except Exception as exc:
        logger.error("更新任务 %s 状态失败: %s", run_id, exc)


def emit_current_task_event(
    event_type: str,
    *,
    state: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """在当前 Worker 线程绑定的任务上下文中发事件。"""
    run_id = getattr(LogBroadcastHandler._local, "run_id", None)
    broadcaster = getattr(LogBroadcastHandler._local, "broadcaster", None)
    effective_state = state if state is not None else LogBroadcastHandler.get_current_state()
    if not run_id or not broadcaster:
        return
    broadcaster.emit_sync(run_id, event_type, state=effective_state, payload=payload or {})


def set_current_task_state(state: Optional[str]) -> None:
    """同步当前 Worker 线程的自动化状态。"""
    LogBroadcastHandler.set_current_state(state)


def update_current_task_run(
    *,
    status: Optional[str] = None,
    phase: Optional[str] = None,
    error_reason: Optional[str] = None,
) -> None:
    """在当前 Worker 线程绑定的任务上下文中更新 Run 记录。"""
    run_id = getattr(LogBroadcastHandler._local, "run_id", None)
    if not run_id:
        return
    if status is None and phase is None and error_reason is None:
        return
    _update_run_status(
        run_id,
        status or "running",
        phase=phase,
        error_reason=error_reason,
    )


def get_worker_status() -> dict[str, Any]:
    """获取 Worker 状态信息。"""
    return {
        "max_workers": _current_max_workers(),
        "running_tasks": list(_running_tasks),
        "running_count": len(_running_tasks),
    }


def shutdown_workers(wait: bool = True) -> None:
    """关闭线程池。"""
    global _executor, _max_workers
    if _executor is not None:
        _executor.shutdown(wait=wait)
        _executor = None
        logger.info("Worker 线程池已关闭。")
    # 清空缓存，下次启动按最新 AppConfig 重新决议。
    _max_workers = None
    stop_warmup_scheduler()


# ── 卡预热 / 养号 调度循环 ─────────────────────────────────
# 60s tick: 扫库找 phase IN ('warming_up_card', 'warming_up_account') AND
# next_action_at <= now() 的 Run，重新提交到 worker 线程池执行，状态机会按 phase 决定下一步。

_WARMUP_PHASES = ("warming_up_card", "warming_up_account")
_warmup_scheduler_thread: Optional[threading.Thread] = None
_warmup_scheduler_stop = threading.Event()
_warmup_scheduler_interval_sec = 60


def _warmup_scheduler_tick(broadcaster_factory: Optional[Callable[[], Any]] = None) -> int:
    """单次扫描：返回本轮重新入队的 run 数。

    - broadcaster_factory: 调用方注入的"如何拿到 broadcaster"工厂函数；
      生产由 start_warmup_scheduler 传入；测试可注入 mock。
    """
    from src.db.engine import get_session as _get_session
    from sqlmodel import select

    now = datetime.now(timezone.utc)
    requeued = 0
    try:
        with _get_session() as session:
            stmt = select(Run).where(
                Run.phase.in_(_WARMUP_PHASES),  # type: ignore[attr-defined]
                Run.status != "cancelled",
                Run.status != "abandoned",
                Run.next_action_at != None,  # noqa: E711
                Run.next_action_at <= now,
            )
            due_runs = list(session.exec(stmt))

        if not due_runs:
            return 0

        broadcaster = broadcaster_factory() if broadcaster_factory else None
        for run in due_runs:
            run_id = run.id
            try:
                with _lock:
                    if run_id in _running_tasks:
                        continue  # 上一个 tick 已重入，跳过
                if broadcaster is None:
                    logger.info("warmup scheduler: 无 broadcaster，仅记录 due run %s", run_id)
                    continue
                ok = submit_task(run_id, broadcaster)
                if ok:
                    requeued += 1
                    logger.info("warmup scheduler: 重新入队 run %s (phase=%s)", run_id, run.phase)
            except Exception as per_run_exc:
                # 单 run 提交失败不应阻断整个 tick — 否则一颗老鼠屎坏一锅汤：
                # 第一个 due run 的 submit_task 抛异常，后续所有 due run 都会被漏掉。
                logger.error(
                    "warmup scheduler: run %s 入队失败（已记录，继续处理后续 run）: %s",
                    run_id, per_run_exc,
                )
    except Exception as exc:
        logger.error("warmup scheduler tick 异常: %s", exc)
    return requeued


def _warmup_scheduler_loop(broadcaster_factory: Callable[[], Any]) -> None:
    """常驻循环：每 60s 跑一次 tick。"""
    logger.info("warmup scheduler 已启动 (interval=%ds)", _warmup_scheduler_interval_sec)
    while not _warmup_scheduler_stop.is_set():
        try:
            _warmup_scheduler_tick(broadcaster_factory)
        except Exception as exc:
            logger.error("warmup scheduler loop 异常: %s", exc)
        # 用 wait 而不是 sleep，便于优雅 stop
        if _warmup_scheduler_stop.wait(_warmup_scheduler_interval_sec):
            break
    logger.info("warmup scheduler 已停止")


def start_warmup_scheduler(broadcaster_factory: Callable[[], Any]) -> bool:
    """启动 warmup 调度循环（幂等，已启动则直接返回 True）。"""
    global _warmup_scheduler_thread
    with _lock:
        if _warmup_scheduler_thread is not None and _warmup_scheduler_thread.is_alive():
            return True
        _warmup_scheduler_stop.clear()
        _warmup_scheduler_thread = threading.Thread(
            target=_warmup_scheduler_loop,
            args=(broadcaster_factory,),
            name="warmup-scheduler",
            daemon=True,
        )
        _warmup_scheduler_thread.start()
    return True


def stop_warmup_scheduler(wait_sec: float = 3.0) -> None:
    """停止 warmup 调度循环（用于测试 / shutdown）。"""
    global _warmup_scheduler_thread
    _warmup_scheduler_stop.set()
    thread = _warmup_scheduler_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=wait_sec)
    _warmup_scheduler_thread = None
