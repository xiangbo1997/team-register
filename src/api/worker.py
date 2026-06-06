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


def _coerce_bool(value: Any) -> bool:
    """把 provider config 里的 bool 字段统一成 Python bool。

    前端 <select> 传的是字符串 'true'/'false'，DB 里也可能存成字符串或真 bool，
    这里统一解析：真 bool 直接用；字符串按 'true'/'1'/'yes'/'on' 判真（大小写不敏感）。
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


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
    """把任务级 provider / account 选择折叠到运行时配置对象。

    优先级（高 → 低）：
      1. Run 字段（run.browser_provider 等）—— 控制台/旧 API 直接指定
      2. config_snapshot.provider_overrides[<slot>] —— 新 API 任务级覆盖
      3. RegistrationProfile.provider_bindings[<slot>] —— 注册方式预设组合
      4. AppConfig.default_*_provider —— 全局兜底（兼容护栏，未来标 deprecated）

    优先级 1/4 是老路径；新增 2/3 在中间插入，老 task 行为完全不变。
    """
    from src.api.deps import get_config_service, get_registration_profile_service

    svc = get_config_service()
    config = svc.get_config()

    # ── 解析 RegistrationProfile：拿到当前任务用的 bindings + overrides ──
    snapshot = dict(run.config_snapshot or {})
    profile_name = str(snapshot.get("registration_profile_name") or "").strip()
    registration_kind = str(snapshot.get("registration_kind") or "email").strip().lower() or "email"
    raw_overrides = snapshot.get("provider_overrides") or {}
    overrides = {
        str(k).strip(): str(v).strip()
        for k, v in raw_overrides.items()
        if str(k).strip() and str(v).strip()
    } if isinstance(raw_overrides, dict) else {}

    profile_bindings: dict[str, str] = {}
    if profile_name or registration_kind in ("email", "phone", "grok"):
        try:
            profile_svc = get_registration_profile_service()
            profile = None
            if profile_name:
                profile = profile_svc.get_profile(profile_name)
                if profile is None:
                    logger.warning(
                        "registration_profile_name=%r 不存在，回退到 kind=%s 的默认组合",
                        profile_name, registration_kind,
                    )
            if profile is None:
                profile = profile_svc.get_default(registration_kind)
            if profile is not None:
                profile_bindings = dict(profile.provider_bindings or {})
        except Exception as exc:
            # 不阻塞任务：profile 解析失败仅记 warning，老 fallback 接管
            logger.warning("RegistrationProfile 解析失败（不阻塞，走 AppConfig 默认）: %s", exc)

    def _pick_binding(slot: str) -> str:
        """按优先级 2 → 3 取槽位 provider name；都没命中返回 ''。"""
        if slot in overrides:
            return overrides[slot]
        return profile_bindings.get(slot, "")

    # browser provider：Run 字段优先；其次 overrides / profile bindings；最后 AppConfig
    browser_default = _pick_binding("browser") or config.default_browser_provider
    browser_profile = svc.resolve_provider_config(
        "browser",
        run.browser_provider,
        default_name=browser_default,
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

    card_default = _pick_binding("card") or config.default_card_provider
    card_profile = svc.resolve_provider_config(
        "card",
        run.card_provider,
        default_name=card_default,
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

    mail_default = _pick_binding("mail") or config.default_mail_provider
    mail_profile = svc.resolve_provider_config(
        "mail",
        run.mail_provider,
        default_name=mail_default,
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

    # SMS provider：profile_bindings.sms → default_sms_provider；查到则覆盖 config.sms_*
    # 未命中（DB 无对应 active 行）则保留 .env 加载的 sms_api_key/sms_country，向后兼容。
    sms_default = _pick_binding("sms") or config.default_sms_provider
    sms_profile = svc.resolve_provider_config("sms", "", default_name=sms_default)
    if sms_profile is not None:
        sms_payload = dict(sms_profile.config or {})
        if "api_key" in sms_payload:
            config.sms_api_key = str(sms_payload.get("api_key") or "").strip()
        if "country" in sms_payload:
            config.sms_country = str(sms_payload.get("country") or "0").strip() or "0"
        # SMS driver 分两条路：
        #   ① 字符串协议族（sms_activate / hero_sms / grizzly_sms ... 均继承 SmsActivateProvider）
        #      → 拍扁成标量，main.py 走 SMSManager（base_url 由注册表通用解析，新增兼容族零改动）。
        #   ② 异构协议（five_sim 等直接继承 SmsProvider、自带 JSON 实现）
        #      → 保留整个 config dict + driver 名，main.py 走 registry.build 直接构造 provider。
        #
        # 字段兼容：历史 provider config 可能只写了 provider_name 而漏掉 driver
        # （线上 2026-05-30 实测：hero-sms 配置缺 driver 字段导致 base_url 不被注入，
        # HeroSMS api_key 被打到 sms-activate.org 端点引发 SSL EOF）。
        # 这里同时读两者，优先 driver；都没有则保持空（走默认 sms-activate 兼容旧行为）。
        sms_driver = str(
            sms_payload.get("driver") or sms_payload.get("provider_name") or ""
        ).strip()
        sms_base_url = str(sms_payload.get("base_url") or "").strip()
        config.sms_driver = sms_driver
        if sms_driver and sms_driver != "sms_activate":
            from src.providers import get_registry
            from src.providers.sms.sms_activate import SmsActivateProvider

            reg = get_registry()
            reg.discover()
            sms_cls = reg.get_class("sms", sms_driver)
            is_compat_family = isinstance(sms_cls, type) and issubclass(sms_cls, SmsActivateProvider)
            if sms_cls is not None and not is_compat_family:
                # 异构 provider：透传原始 config（去掉控制字段），main.py 据此 registry.build。
                config.sms_provider_config = {
                    k: v for k, v in sms_payload.items() if k not in ("driver",)
                }
            elif sms_base_url:
                config.sms_base_url = sms_base_url
            else:
                # 兼容族：注册表拿 BASE_URL；仅当覆写了端点（非默认 sms-activate）才注入。
                driver_base_url = str(getattr(sms_cls, "BASE_URL", "") or "").strip()
                if driver_base_url and driver_base_url != SmsActivateProvider.BASE_URL:
                    config.sms_base_url = driver_base_url
        elif sms_base_url:
            config.sms_base_url = sms_base_url

    # LLM provider：同模式
    llm_default = _pick_binding("llm") or config.default_llm_provider
    llm_profile = svc.resolve_provider_config("llm", "", default_name=llm_default)
    if llm_profile is not None:
        llm_payload = dict(llm_profile.config or {})
        if "base_url" in llm_payload:
            config.llm_base_url = str(llm_payload.get("base_url") or "").rstrip("/")
        if "api_key" in llm_payload:
            config.llm_api_key = str(llm_payload.get("api_key") or "").strip()
        if "model" in llm_payload:
            config.llm_model = str(llm_payload.get("model") or "").strip()
        if "timeout_ms" in llm_payload:
            try:
                config.llm_timeout_ms = int(llm_payload.get("timeout_ms") or 30000)
            except (TypeError, ValueError):
                pass
        if "confidence_threshold" in llm_payload:
            try:
                config.llm_confidence_threshold = float(llm_payload.get("confidence_threshold") or 0.6)
            except (TypeError, ValueError):
                pass
        # 视觉决策开关（卡住时给辅助决策大模型附页面截图做多模态判断）。
        # 诊断器（triage）默认复用同一套 LLM 配置，也随之获得视觉能力。
        if "vision_enabled" in llm_payload:
            config.llm_vision_enabled = _coerce_bool(llm_payload.get("vision_enabled"))
        if "vision_screenshot_stall_threshold" in llm_payload:
            try:
                config.llm_screenshot_on_stall_threshold = int(
                    llm_payload.get("vision_screenshot_stall_threshold") or 3
                )
            except (TypeError, ValueError):
                pass

    # Captcha provider：同模式
    captcha_default = _pick_binding("captcha") or config.default_captcha_provider
    captcha_profile = svc.resolve_provider_config("captcha", "", default_name=captcha_default)
    if captcha_profile is not None:
        captcha_payload = dict(captcha_profile.config or {})
        if "kind" in captcha_payload:
            config.captcha_solver_kind = str(captcha_payload.get("kind") or "noop").strip().lower() or "noop"
        if "user_token" in captcha_payload:
            config.nocaptcha_user_token = str(captcha_payload.get("user_token") or "").strip()
        if "timeout_ms" in captcha_payload:
            try:
                config.captcha_solver_timeout_ms = int(captcha_payload.get("timeout_ms") or 30000)
            except (TypeError, ValueError):
                pass
        if "budget_cap_usd" in captcha_payload:
            try:
                config.captcha_solver_budget_cap_usd = float(captcha_payload.get("budget_cap_usd") or 5.0)
            except (TypeError, ValueError):
                pass

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

    # task_mode 路由：
    #   "register_only" → 关掉 enable_payment_flow 让 main.run_task 跳过 Phase 3，
    #                     注册成功 + 提取 token 后 phase 停在 token_extraction，进普号池
    #   其他           → full 模式，按 .env / config 默认走完整流程
    # 注：snapshot 已在函数开头读取，这里直接复用
    task_mode = str(snapshot.get("task_mode") or "full").strip().lower()
    if task_mode == "register_only":
        config.enable_payment_flow = False
        logger.info(
            "task_mode=register_only：已强制关闭 enable_payment_flow，注册成功后跳过支付绑卡进入普号池"
        )

    # 真实身份注入（批量注册防风控用）：
    #   batch_register_service 创建 Run 时把 identity 落到 config_snapshot；
    #   这里取出来挂到 config，main.py:fill_about_you 优先用这些字段填表
    identity = snapshot.get("identity") or {}
    if isinstance(identity, dict):
        config.identity_first_name = str(identity.get("first_name") or "").strip()
        config.identity_last_name = str(identity.get("last_name") or "").strip()
        config.identity_email_local = str(identity.get("email_local") or "").strip()
        config.identity_birthdate = str(identity.get("birthdate") or "").strip()
        if config.identity_first_name:
            logger.info(
                "已注入真实身份: %s %s (email_local=%s, dob=%s)",
                config.identity_first_name, config.identity_last_name,
                config.identity_email_local, config.identity_birthdate,
            )

    # 三模式注册：把 registration_kind / requested_phone / sms_country 注入 config，
    # automation/runtime.py PHONE state 用 registration_kind 判定放行 vs fail。
    # registration_kind 已在函数开头读取，这里直接挂到 config
    config.registration_kind = registration_kind
    # 注册平台（feat/grok-register）：grok → worker 分叉到 grok_runtime；openai → main.run_task
    config.platform = str(snapshot.get("platform") or getattr(run, "platform", "") or "openai").strip().lower() or "openai"
    config.requested_phone = str(snapshot.get("requested_phone") or run.phone_number or "").strip()
    config.sms_order_id = str(run.sms_order_id or "").strip()
    # 国家优先级（2026-06-02 反转）：**SMS provider 配置优先**（line 533-534 已注入
    # hero-sms 的 country，如美国 187），表单 snapshot.sms_country 仅在 provider 未配时兜底。
    # 原因：实战中任务表单的 sms_country 一直传脏值印尼 6（默认/浏览器记忆），覆盖了运维
    # 在 /providers 精心配的干净国家（美国）→ 连续多个印尼脏号被 OpenAI 判「已注册」。
    # 运维在 /providers 配什么国家就用什么，表单不再能搞砸号源。
    provider_country = str(getattr(config, "sms_country", "") or "").strip()
    form_country = str(snapshot.get("sms_country") or "").strip()
    if provider_country and provider_country not in ("", "0"):
        # provider 已配国家 → 以它为准，忽略表单
        logger.info("sms_country 以 SMS provider 配置为准: %s（忽略表单值 %r）", provider_country, form_country)
    elif form_country:
        config.sms_country = form_country
        logger.info("sms_country provider 未配，回退表单值: %s", form_country)

    return config


# 模块级 domain 列表缓存：避免每个任务都打一次 GET /managed-providers/<name>/domains。
# key = (provider_name, config_name)；value = (timestamp, list[str])。
# TTL 设 300 秒，平衡运维改动 domain 列表的反应速度 vs 请求频次。
_DOMAIN_CACHE_TTL_S = 300.0
_domain_cache: dict[tuple[str, str], tuple[float, list[str], str]] = {}
_domain_cache_lock = threading.Lock()


def _resolve_requested_email_from_identity(
    config,
    mail_api,
    run_id: str,
    broadcaster: EventBroadcaster,
) -> str:
    """根据 batch 注入的 identity 拼出完整 email，让远端走"模式 B"路径生成有意义邮箱。

    流程：
    1. 拿 ``config.identity_email_local``（如 ``william.harrison82``），无则返回空串
    2. 通过 ``mail_api._provider.list_provider_domains`` 查可用 domain 列表（带 TTL 缓存）
    3. 随机选一个 domain（多 domain 轮换，分散注册指纹）
    4. 拼出 ``william.harrison82@gitee.shop`` 返回给调用方
    5. 任何步骤失败均返回空串，调用方降级回原 ``email=""`` 路径（远端默认 tmpXXXXXX）

    返回空串语义：调用方应直接传 ``email=""``，让远端自己生成。
    """
    import time

    email_local = (getattr(config, "identity_email_local", "") or "").strip().lower()
    if not email_local or "@" in email_local:
        # 没有注入身份（CLI 直跑 / 老批次）或 email_local 已是完整邮箱：跳过
        return ""

    provider_name = str(getattr(mail_api, "_provider_name", "") or "").strip().lower()
    config_name = str(getattr(mail_api, "_config_name", "") or "").strip()
    cache_key = (provider_name, config_name)

    # L1 缓存命中
    now = time.monotonic()
    with _domain_cache_lock:
        cached = _domain_cache.get(cache_key)
        if cached is not None:
            ts, domains, default_domain = cached
            if (now - ts) <= _DOMAIN_CACHE_TTL_S:
                return _pick_email_from_domains(email_local, domains, default_domain)

    # 远端拉取
    try:
        result = mail_api._provider.list_provider_domains(
            provider=provider_name,
            config_name=config_name or "",
        )
    except Exception as exc:
        logger.warning("查询 provider 域名失败，降级走默认 tmpXXXXXX: %s", exc)
        return ""

    domains = result.get("enabled_domains") or []
    default_domain = str(result.get("default_domain") or "").strip().lower()
    if not domains and not default_domain:
        # 远端尚未部署该端点 / 该 provider 没配 domain：降级
        logger.info(
            "邮箱拼接降级：远端未返回 enabled_domains（provider=%s）→ 走默认 tmpXXXXXX",
            provider_name,
        )
        return ""

    # 写缓存
    with _domain_cache_lock:
        _domain_cache[cache_key] = (now, list(domains), default_domain)

    return _pick_email_from_domains(email_local, domains, default_domain)


def _pick_email_from_domains(email_local: str, domains: list[str], default_domain: str) -> str:
    """在已知 domain 列表里随机挑一个拼成完整 email；列表空时回退 default_domain。"""
    import random

    if domains:
        domain = random.choice(domains)
    elif default_domain:
        domain = default_domain
    else:
        return ""
    full_email = f"{email_local}@{domain}".lower()
    logger.info("使用注入身份拼出真名邮箱: %s（替代默认 tmpXXXXXX）", full_email)
    return full_email


def _allocate_or_reuse_phone(config, sms_api):
    """申领手机号：先尝试复用活号（省钱），无可复用号再新申。

    复用条件：DB 里有「同 provider + 同 country 且 use_count < max_uses」的活号。
    命中后调 sms_api.request_additional_sms(order_id) 让平台准备重新发码，
    返回一个 SMSOrder 形态对象（带 order_id / phone_number），下游 get_code 拿新 OTP。

    Returns:
        (SMSOrder | None, reused: bool)。reused=True 表示复用旧号，False 表示新申。
    """
    from src.models import SMSOrder
    from src.services import sms_activation_service as sms_reuse

    provider_name = str(getattr(config, "sms_driver", "") or "hero_sms").strip().lower() or "hero_sms"
    country = str(getattr(config, "sms_country", "") or "").strip()
    # 服务码留空：让每个异构 provider 用**自己配置的默认**服务码。
    # sms-activate 族（hero/grizzly/...）配置 service="dr"，5sim 配置 product="openai"——
    # 两者服务码体系不同，worker 不能硬编码某一族的协议码强行覆盖（曾硬编码 "dr"
    # 导致 5sim 收到 "dr" 报 400 "no product"）。空串触发 provider 内部回落到自身默认。
    service = ""

    # 1. 尝试复用：仅当 provider 支持 request_additional_sms 时才走复用路径
    if hasattr(sms_api, "request_additional_sms"):
        reuse_rec = sms_reuse.try_reuse_active_number(provider_name, country)
        if reuse_rec is not None:
            ok = False
            try:
                ok = bool(sms_api.request_additional_sms(reuse_rec.order_id))
            except Exception as exc:  # noqa: BLE001 — 复用失败回退新申
                logger.warning("号码复用 request_additional_sms 异常 order=%s: %s", reuse_rec.order_id, exc)
            if ok:
                logger.info(
                    "号码复用成功 order=%s phone=%s（已用 %d/%d）",
                    reuse_rec.order_id, reuse_rec.phone_number,
                    reuse_rec.use_count, reuse_rec.max_uses,
                )
                return SMSOrder(order_id=reuse_rec.order_id, phone_number=reuse_rec.phone_number), True
            # 复用请求被平台拒绝 → 作废该活号，回退新申
            sms_reuse.invalidate(reuse_rec.order_id, reason="request_additional_sms_rejected")

    # 2. 新申号（多次尝试：申到黑名单号 → 取消 + 重申，跳过用过/被拒的号）。
    #    回应"好多号被用过收不到码"：申到的号先查黑名单，命中立即换号，不浪费一整轮注册。
    max_allocate_attempts = 5
    for attempt in range(1, max_allocate_attempts + 1):
        order = sms_api.get_number(service=service)
        if order is None or not order.phone_number:
            logger.warning("新申号失败/无货（尝试 %d/%d）", attempt, max_allocate_attempts)
            return None, False

        # 黑名单检查：该号之前被 OpenAI 拒过/收不到码 → 取消 + 换号
        if sms_reuse.is_phone_blacklisted(order.phone_number):
            logger.info(
                "申到黑名单号 %s（order=%s，之前被拒/无码），取消并换号（尝试 %d/%d）",
                order.phone_number, order.order_id, attempt, max_allocate_attempts,
            )
            try:
                if hasattr(sms_api, "cancel_number"):
                    sms_api.cancel_number(order.order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("取消黑名单号异常（忽略）: %s", exc)
            continue

        # 干净号：落库供后续复用（失败不阻塞）
        sms_reuse.record_allocation(
            order.order_id,
            provider_name=provider_name,
            country=country,
            phone_number=order.phone_number,
            service=service,
        )
        logger.info("新申号成功 phone=%s order=%s（尝试 %d）", order.phone_number, order.order_id, attempt)
        return order, False

    logger.error("连续 %d 次申到的都是黑名单号，放弃（建议换国家/价格或补充号池）", max_allocate_attempts)
    return None, False


def _blacklist_phone_on_failure(config, reason=""):
    """phone 模式注册失败时拉黑当前号（OpenAI 拒号/收不到码），下次申号跳过。

    仅在 phone 模式且号已申到时触发；同时取消激活止损。失败不阻塞。
    """
    try:
        kind = str(getattr(config, "registration_kind", "") or "").strip().lower()
        if kind != "phone":
            return
        phone = str(getattr(config, "requested_phone", "") or "").strip()
        order_id = str(getattr(config, "sms_order_id", "") or "").strip()
        if not phone:
            return
        provider_name = str(getattr(config, "sms_driver", "") or "hero_sms").strip().lower() or "hero_sms"
        country = str(getattr(config, "sms_country", "") or "").strip()
        from src.services import sms_activation_service as sms_reuse
        if order_id:
            sms_reuse.invalidate(order_id, reason=reason or "register_failed")
        sms_reuse.blacklist_phone(phone, provider_name, country, reason=reason or "register_failed")
        logger.info("phone 注册失败，已拉黑号 %s（order=%s）防止复用", phone, order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉黑失败号异常（忽略）: %s", exc)


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

    # 密码兜底：发起任务时密码可选，留空则生成强密码（保证四类字符，过 x.ai/OpenAI 强度校验）。
    # 回写到 run，便于 export_success / accounts.csv 留存真实密码。
    if not (password or "").strip():
        from src.automation.grok_runtime import _gen_password
        password = _gen_password()
        try:
            _persist_run_password(run_id, password)
        except Exception as exc:
            logger.warning("回写自动生成密码失败（不阻塞）: %s", exc)
        logger.info("任务 %s 未提供密码，已自动生成强密码", run_id[:12])

    # Grok (x.ai) 注册分叉（feat/grok-register）：platform=grok 完全走独立路径，
    # 跳过后面所有 OpenAI 逻辑（SMS 申领 / identity 合成 / OpenAI mail preflight / run_task）。
    # grok 只需 mail（真实收码）+ browser + captcha，终态以 sso token 判定。
    platform = str(getattr(config, "platform", "openai") or "openai").strip().lower()
    if platform == "grok":
        _execute_grok_inner(run_id, broadcaster, config, mail_api, profile_id, email, password)
        return

    # 手机号模式 + 空号 → 通过 SMS-Activate 静默申领手机号，回写 Run 与 config（在 mail preflight 之前）。
    # 任何失败立即标 failed —— 手机号是 Mode B 的核心，没号没法继续。
    registration_kind = str(getattr(config, "registration_kind", "email") or "email").strip().lower()
    if registration_kind == "phone" and not (getattr(config, "requested_phone", "") or "").strip():
        try:
            ensure_task_active(run_id, "before_sms_auto_allocate")
            order, reused = _allocate_or_reuse_phone(config, sms_api)
            if order is None or not order.phone_number:
                raise RuntimeError("SMS-Activate 获取手机号失败 (api 返回空)")
            config.requested_phone = order.phone_number
            config.sms_order_id = order.order_id
            with get_session() as s:
                run_to_update = s.get(Run, run_id)
                if run_to_update:
                    run_to_update.phone_number = order.phone_number
                    run_to_update.sms_order_id = order.order_id
                    run_to_update.updated_at = datetime.now(timezone.utc)
                    s.add(run_to_update)
                    s.commit()
            # 用 PREFLIGHT_PHONE 状态推进进度条（PHONE 留给真正进入手机验证步骤）。
            broadcaster.emit_sync(
                run_id,
                "state_change",
                state="PREFLIGHT_PHONE",
                payload=build_i18n_message_payload(
                    f"{'复用' if reused else '已自动分配'}手机号: {order.phone_number}（order={order.order_id}）",
                    "task_events.phone_auto_allocated",
                    params={"phone": order.phone_number, "order_id": order.order_id, "reused": reused},
                    action_id="phone_auto_allocate",
                    result="ok",
                ),
            )
            logger.info(
                "Mode B %s手机号: %s (order=%s)",
                "复用" if reused else "自动分配", order.phone_number, order.order_id,
            )
        except TaskCancelledError:
            _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
            return
        except Exception as exc:
            logger.error("任务 %s SMS 手机号申领失败: %s", run_id, exc)
            broadcaster.emit_sync(
                run_id,
                "action",
                state="PHONE",
                payload=build_i18n_message_payload(
                    "SMS 手机号申领失败",
                    "task_events.phone_auto_allocate_failed",
                    action_id="phone_auto_allocate",
                    result="failed",
                    error=str(exc),
                ),
            )
            _update_run_status(run_id, "failed", error_reason=f"phone_auto_allocate_failed: {exc}")
            return

    # 单任务邮箱模式 + 空邮箱 + cfworker：用 generate_identity() 合成 first/last/email_local，
    # 让下面 _resolve_requested_email_from_identity 能拼出 first.last82@<domain> 而不是 tmpXXXXXX。
    # batch 路径已有 identity 注入，这里只补单任务漏掉的链路。
    if (
        registration_kind == "email"
        and not email
        and not (getattr(config, "identity_email_local", "") or "").strip()
        and str(getattr(config, "email_provider_name", "") or "").strip().lower() == "cfworker"
    ):
        try:
            from src.services.identity_generator import generate_identity
            identity = generate_identity()
            config.identity_first_name = identity.first_name
            config.identity_last_name = identity.last_name
            config.identity_email_local = identity.email_local
            config.identity_birthdate = identity.birthdate
            logger.info(
                "Mode A cfworker 单任务静默生成身份: %s %s (email_local=%s, dob=%s)",
                identity.first_name, identity.last_name, identity.email_local, identity.birthdate,
            )
        except Exception as exc:
            # 身份生成失败不应阻塞 —— 退回 server 端默认 tmpXXXXXX
            logger.warning("Mode A cfworker identity 合成失败（回退 tmpXXX）: %s", exc)

    # Phone 模式跳过 mail preflight：
    # 设计上 phone 任务仍保留 mail provider 兜底（OpenAI 可能在手机验证后追加邮箱验证），
    # 但启动时不主动 probe email-provider —— 用户诉求 "手机号注册不需要被邮件卡住"。
    # 真到 runtime AutomationState.VERIFY_EMAIL 时才会调 mail_api 拿验证码；
    # 那时即使 email-provider 500，手机号已经申到、浏览器已经跑起来，损失最小。
    _skip_mail_preflight = registration_kind == "phone"
    if _skip_mail_preflight:
        logger.info(
            "任务 %s 是 phone 模式，跳过 mail runtime preflight（VERIFY_EMAIL 时按需拉取）",
            run_id,
        )
        broadcaster.emit_sync(
            run_id,
            "state_change",
            state="PREFLIGHT_MAIL",
            payload=build_i18n_message_payload(
                "phone 模式跳过邮箱预检（按需拉取）",
                "task_events.mail_runtime_preflight_skipped_phone",
                action_id="mail_runtime_preflight",
                result="skipped",
            ),
        )

    if not _skip_mail_preflight:
        try:
            ensure_task_active(run_id, "before_mail_runtime_preflight")
            resolved_session_mode = mail_api.ensure_runtime_ready(email)
            # 推进到 PREFLIGHT_MAIL（VERIFY_EMAIL 留给真正进入邮箱验证状态机步骤时）。
            broadcaster.emit_sync(
                run_id,
                "state_change",
                state="PREFLIGHT_MAIL",
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
                # 如果有 identity_email_local（来自 batch_register_service 的 identity_generator），
                # 拼出完整 email 通过 HTTP body.email 传给 server，走 server 已有的"模式 B"代码路径
                # （/software/email-provider/core/base_mailbox.py:1294 _CFWorkerProvider.get_email
                # 中 `if requested_email and "@" in requested_email` 分支）。
                # domain 来源：先调 server GET /managed-providers/<name>/domains 拿可用列表，随机挑一个。
                # 没有 identity_email_local 时（CLI 直跑 / 老批次）保持空 email，server 走默认 tmpXXXXXX。
                requested_email = _resolve_requested_email_from_identity(config, mail_api, run_id, broadcaster)
                allocated = mail_api._provider.create_session(
                    provider=mail_api._provider_name,
                    purpose="auto-allocate",
                    session_mode="managed",
                    config_name=mail_api._config_name,
                    email=requested_email,
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
                    "state_change",
                    state="PREFLIGHT_MAIL",
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

    # 3.5 邮箱预检完成 → 即将启动浏览器。推进进度条到 PREFLIGHT_BROWSER。
    # 真实出口 IP 抓取已移至 main.run_task 浏览器启动后（PREFLIGHT_IP），
    # 因为只有浏览器内才能拿到 AdsPower 注入的住宅代理实际出口。
    broadcaster.emit_sync(
        run_id,
        "state_change",
        state="PREFLIGHT_BROWSER",
        payload=build_i18n_message_payload(
            "准备启动浏览器...",
            "task_events.preflight_browser_start",
        ),
    )

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
            db_run_id=run_id,
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

        final_state = (LogBroadcastHandler.get_current_state() or "").upper()
        has_error = LogBroadcastHandler.check_has_error()
        outcome = _decide_final_outcome(final_state, has_error)

        broadcaster.emit_sync(
            run_id,
            "orchestrator_complete",
            payload=build_i18n_message_payload(
                outcome["message"],
                outcome["i18n_key"],
                has_warning=outcome["has_warning"],
            ),
        )
        if outcome["status"] == "success":
            # 额外 token 校验：HOME 但 openai_tokens 为空 = 误判（典型场景：未登录主页
            # 被识别为 HOME 后 run_task 直接返回，token 提取根本没执行）。
            # 防止 phone 模式 + has_unauth_chrome 路径漏判时仍标 success。
            if not _has_extracted_tokens(run_id):
                logger.warning(
                    "Run %s 终态 HOME 但 openai_tokens 为空，标 failed 防误报。",
                    run_id[:12],
                )
                _update_run_status(
                    run_id,
                    "failed",
                    error_reason=f"reached_HOME_but_no_tokens_extracted (final_state={final_state})",
                )
            else:
                _update_run_status(run_id, "success", phase=final_state.lower())
        else:
            # 若卡住诊断器给出了高置信结论，把卡点类别拼进 error_reason，
            # 便于 /accounts 按卡点类型聚合（如 selector_stale / ip_pollution）。
            error_reason = outcome["error_reason"]
            triage_category = _latest_triage_category(run_id)
            if triage_category:
                error_reason = f"{error_reason} [triage={triage_category}]"
            _update_run_status(run_id, "failed", error_reason=error_reason)
            # phone 模式注册失败且号已申到 → 大概率 OpenAI 拒号/收不到码，拉黑该号，
            # 下次申号自动跳过（回应"好多号被用过收不到码"）。
            _blacklist_phone_on_failure(config, reason=f"register_failed:{outcome.get('error_reason','')}")

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


def _execute_grok_inner(
    run_id: str,
    broadcaster: EventBroadcaster,
    config: Any,
    mail_api: Any,
    profile_id: str,
    email: str,
    password: str,
) -> None:
    """Grok (x.ai) 注册执行（feat/grok-register）。

    与 OpenAI 路径完全隔离：不申 SMS、不构建/使用 card_api、不调 main.run_task。
    复用 mail_api（真实收码）+ AdsPower（grok_runtime 内部连）+ captcha_solver。
    成功判定 = 拿到非空 sso token；写 Run.sso_token + status=success + account_tier=registered。
    """
    from src.automation.grok_runtime import run_grok_task, GrokRegistrationError

    # grok_runtime 的 emit 回调 → 适配到 broadcaster.emit_sync。
    # grok payload 已是 {"message": ..., **extra}，直接透传给 SSE。
    def _emit(event_type: str, state: Optional[str], payload: dict[str, Any]) -> None:
        try:
            if state:
                broadcaster.emit_sync(run_id, event_type, state=state, payload=payload)
            else:
                broadcaster.emit_sync(run_id, event_type, payload=payload)
        except Exception:
            pass

    # mail preflight（grok 必须真实收码；失败立即 fail）。
    # 邮箱留空 + managed provider（cfworker/outlook）→ 自动分配邮箱并回写（复用 OpenAI 同款逻辑）。
    try:
        ensure_task_active(run_id, "grok_before_mail_preflight")
        resolved_session_mode = mail_api.ensure_runtime_ready(email)
        if not email and str(resolved_session_mode or "").strip().lower() == "managed":
            ensure_task_active(run_id, "grok_before_mail_auto_allocate")
            # 与 GPT 路径一致：空邮箱 + cfworker → 先 generate_identity() 合成 first.last，
            # 否则 _resolve_requested_email_from_identity 拿不到 email_local 会返回空，
            # 导致 create_session(email="") 让 server 走出错分支 → 500（这是 grok 500 的真因）。
            if (
                not (getattr(config, "identity_email_local", "") or "").strip()
                and str(getattr(config, "email_provider_name", "") or "").strip().lower() == "cfworker"
            ):
                try:
                    from src.services.identity_generator import generate_identity
                    identity = generate_identity()
                    config.identity_first_name = identity.first_name
                    config.identity_last_name = identity.last_name
                    config.identity_email_local = identity.email_local
                    config.identity_birthdate = identity.birthdate
                    logger.info(
                        "Grok cfworker 单任务合成身份: %s %s (email_local=%s)",
                        identity.first_name, identity.last_name, identity.email_local,
                    )
                except Exception as exc:
                    logger.warning("Grok identity 合成失败（回退 tmpXXX）: %s", exc)
            requested_email = _resolve_requested_email_from_identity(config, mail_api, run_id, broadcaster)
            allocated = mail_api._provider.create_session(
                provider=mail_api._provider_name,
                purpose="grok-auto-allocate",
                session_mode="managed",
                config_name=mail_api._config_name,
                email=requested_email,
            )
            email = allocated.email
            # 立即释放预分配 session（收码时再建新 session）
            try:
                mail_api._provider.complete(allocated, result="success", reason="grok auto-allocate probe")
            except Exception:
                pass
            # 回写 run.email，供后续 grok 注册 + 号池展示用
            with get_session() as s:
                run_to_update = s.get(Run, run_id)
                if run_to_update:
                    run_to_update.email = email
                    run_to_update.updated_at = datetime.now(timezone.utc)
                    s.add(run_to_update)
                    s.commit()
            logger.info("Grok 自动分配邮箱: %s", email)
            broadcaster.emit_sync(
                run_id, "state_change", state="PREFLIGHT_MAIL",
                payload={"message": f"自动分配邮箱: {email}"},
            )
    except TaskCancelledError:
        _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
        return
    except Exception as exc:
        logger.error("Grok 任务 %s 邮箱预检失败: %s", run_id, exc)
        _update_run_status(run_id, "failed", error_reason=f"grok_mail_preflight_failed: {exc}")
        return

    broadcaster.emit_sync(
        run_id, "state_change", state="PREFLIGHT_BROWSER",
        payload={"message": "准备启动浏览器（Grok）..."},
    )

    try:
        ensure_task_active(run_id, "grok_before_run")
        sso = run_grok_task(
            config=config,
            mail_api=mail_api,
            ads_id=profile_id,
            email=email,
            password=password,
            db_run_id=run_id,
            emit=_emit,
        )
    except TaskCancelledError:
        logger.warning("Grok 任务 %s 执行中被取消。", run_id)
        _update_run_status(run_id, "cancelled", error_reason="cancelled_by_user")
        return
    except GrokRegistrationError as exc:
        logger.error("Grok 任务 %s 注册失败: %s", run_id, exc)
        broadcaster.emit_sync(
            run_id, "orchestrator_complete",
            payload={"message": f"Grok 注册失败：{exc}"},
        )
        _update_run_status(run_id, "failed", error_reason=f"grok_register_failed: {exc}")
        return
    except Exception as exc:
        logger.error("Grok 任务 %s 异常: %s", run_id, exc)
        _update_run_status(run_id, "failed", error_reason=f"grok_unexpected: {exc}")
        return

    # 成功：写 sso_token + 终态。grok 不复用 _decide_final_outcome（OpenAI HOME/token 专用）。
    if not sso:
        _update_run_status(run_id, "failed", error_reason="grok_no_sso")
        return
    try:
        with get_session() as session:
            run = session.get(Run, run_id)
            if run:
                run.sso_token = sso
                run.account_tier = "registered"
                run.updated_at = datetime.now(timezone.utc)
                session.add(run)
                session.commit()
    except Exception as exc:
        logger.error("Grok 任务 %s 写 sso_token 失败: %s", run_id, exc)
    broadcaster.emit_sync(
        run_id, "orchestrator_complete",
        payload={"message": f"Grok 注册成功（sso {len(sso)} 字符）"},
    )
    _update_run_status(run_id, "success", phase="grok_done")


# Run 终态判定的"成功 sentinel"集合。
# HOME 是 main.run_task 主流程的唯一终点（register_only 在 token 提取后 / full 在支付收尾后），
# 状态机能推到 HOME = 注册 + 邮箱验证 + token 提取（+ 支付，full 模式）都完成了。
_SUCCESS_TERMINAL_STATES: frozenset[str] = frozenset({"HOME"})


def _latest_triage_category(run_id: str) -> str:
    """捞最近一条卡住诊断事件的 category，拼进 failed 终态的 error_reason。

    诊断事件由 ``_run_triage`` 通过 ``emit_event("triage", ...)`` 写入 RunEvent；
    这里读最近一条非 unknown 的诊断，让 /accounts 列表能按卡点类型聚合统计。
    任何异常返回空串（诊断只是增益信息，绝不影响终态判定本身）。
    """
    try:
        from sqlmodel import select

        from src.db.models import RunEvent

        with get_session() as session:
            rows = session.exec(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.event_type == "triage")
                .order_by(RunEvent.timestamp.desc())
            ).all()
            for event in rows:
                category = str((event.payload or {}).get("category") or "").strip()
                if category and category != "unknown":
                    return category
    except Exception:
        return ""
    return ""


def _has_extracted_tokens(run_id: str) -> bool:
    """检查 Run.openai_tokens 是否含有非空 access_token。

    用于 success 终态守门：HOME 但 token 为空 = 注册流程没真正完成
    （典型场景：未登录主页被误判 HOME 后 run_task 直接静默返回，token 提取根本没跑）。
    DB 异常一律返回 True（保守，避免本可成功的 run 被误标 failed）。
    """
    try:
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return True
            tokens = run.openai_tokens or {}
            return bool(str(tokens.get("access_token") or "").strip())
    except Exception:
        return True


def _decide_final_outcome(final_state: str, has_error: bool) -> dict[str, Any]:
    """根据 run_task 收尾后的 final_state + has_error 决定 Run 终态。

    设计立场（方案 A）：
      到 HOME 即视为成功，**不再因为 has_error 翻盘**。
      历史 bug：旧逻辑要求 ``final_state == HOME AND not has_error`` 才算成功，
      但 HOME 后期常有非致命 ERROR（邮箱 session 清理超时 / 浏览器关闭 / IP 抓取失败），
      流程已跑完却被误标 failed。HOME 是状态机唯一终点 sentinel，能到这步可以信任。
      has_error 仍透出到 payload.has_warning，前端可显示"成功但有 warning"。

    Args:
        final_state: 最终状态字符串（已 upper），如 "HOME" / "ENTRY" / ""
        has_error: 本次 Worker 执行期间是否记录到 ERROR 级别日志

    Returns:
        dict:
            status: "success" / "failed"
            error_reason: 失败原因（仅 failed 时有意义）
            message: 给 SSE 推送的人类可读描述
            i18n_key: 给 build_i18n_message_payload 的键
            has_warning: 成功但有非致命 ERROR 时为 True
    """
    normalized = (final_state or "").upper()
    if normalized in _SUCCESS_TERMINAL_STATES:
        return {
            "status": "success",
            "error_reason": "",
            "message": (
                "自动化流程执行完成（含非致命 warning）"
                if has_error
                else "自动化流程执行完成"
            ),
            "i18n_key": "task_events.orchestrator_complete",
            "has_warning": has_error,
        }

    # 未到 HOME = 流程没跑完 = failed
    if has_error:
        reason = f"error_logged_at_state={normalized or 'UNKNOWN'}"
    else:
        # 没 ERROR 但状态没到终点 = run_task 静默 return（典型场景：吞异常 / 中途退出）
        reason = f"silent_failure_at_state={normalized or 'UNKNOWN'}"

    return {
        "status": "failed",
        "error_reason": reason,
        "message": f"自动化流程未达成功状态：{reason}",
        "i18n_key": "task_events.orchestrator_complete_with_error",
        "has_warning": False,
    }


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


def _persist_run_password(run_id: str, password: str) -> None:
    """把自动生成的密码回写到 Run（password 是 EncryptedString，写入自动加密）。"""
    with get_session() as session:
        run = session.get(Run, run_id)
        if run:
            run.password = password
            run.updated_at = datetime.now(timezone.utc)
            session.add(run)
            session.commit()


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


def update_current_task_tokens(
    access_token: str,
    refresh_token: str,
) -> None:
    """把当前 Worker 任务的 OpenAI session tokens 落库到 Run.openai_tokens。

    号池"生成 checkout 链接"和 cpa 格式导出依赖此字段。
    main.py:run_task 和 orchestrator.run 两条路径都要调一次。失败不抛。
    """
    run_id = getattr(LogBroadcastHandler._local, "run_id", None)
    if not run_id:
        return
    try:
        from src.db.engine import get_session
        from src.db.models import Run

        with get_session() as session:
            db_run = session.get(Run, run_id)
            if db_run is None:
                return
            db_run.openai_tokens = {
                "access_token": access_token or "",
                "refresh_token": refresh_token or "",
                "id_token": "",
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": "",
            }
            db_run.updated_at = datetime.now(timezone.utc)
            session.add(db_run)
            session.commit()
    except Exception as exc:
        logger.warning("token 写库失败 run=%s: %s", run_id[:12], exc)


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
