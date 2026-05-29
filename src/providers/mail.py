# -*- coding: utf-8 -*-
"""
邮件 Provider 抽象层

定义统一的邮箱会话接口，通过 HTTP API 对接第三方邮件服务。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 60
_POLL_CODE_TIMEOUT_BUFFER_SECONDS = 25

# 5xx 上游瞬时失败重试参数
# 解决 CF Worker / OpenAI 等上游短窗故障导致 team-register 直接 fail-fast 的问题
#
# 历史回顾：
#   v1: (5s, 15s) 共 3 次 attempts，覆盖窗口 20s
#       问题：实测 cfworker 短窗故障可持续 30s+，20s 退避不够，导致 warmup 整链失败。
#   v2(当前): (5s, 15s, 30s, 60s) 共 5 次 attempts，覆盖窗口 110s
#       覆盖大部分 cfworker / 上游短窗故障；最后一次 attempt 失败才彻底 give up。
#
# 改动权衡：
#   - 拉长重试窗口 → 调用方阻塞时间最坏从 ~20s 拉到 ~110s。
#     OK 因为：(a) warmup 调用是后台任务，不卡前端；(b) external_failure 不累计计数，
#     即便最后失败也不会把号池连带 disable。
_RETRY_MAX_ATTEMPTS = 5  # 共尝试 5 次 = 首次 + 4 次重试
_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 30.0, 60.0)  # 4 次退避节奏，覆盖 110s 短窗故障
_RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})


@dataclass
class MailSession:
    """邮箱会话信息，对应远端 session 生命周期"""
    session_id: str
    lease_token: str
    email: str
    provider: str
    before_ids: list[str] = field(default_factory=list)
    provider_meta: dict[str, Any] = field(default_factory=dict)
    account_id: str = ""
    session_mode: str = "managed"
    state: str = "leased"
    expires_at: str = ""


class MailServiceError(RuntimeError):
    """邮件服务致命错误，调用方应直接失败而不是继续人工接管。"""


class MailRuntimeIncompatibleError(MailServiceError):
    """email-provider 运行态与当前 latest-only 客户端协议不兼容（5xx / 401 / 404）。

    语义：服务端运行态出问题，客户端应当**升级或重启服务端**。包括：
      - 5xx：服务端实现 / 数据 / 上游有问题
      - 401：API key 不匹配
      - 404：端点缺失（服务端版本太旧）
    """


class MissingProviderConfigError(MailServiceError):
    """422 — provider 必填配置字段缺失。客户端应**修 admin UI / .env 后重试**。

    服务端契约：响应 ``{"detail": {"code": "PROVIDER_NOT_CONFIGURED",
    "message": "...", "missing_fields": [...]}}``。

    本地 fail-fast 也用这个异常（managed mode 下 config_name 为空时 MailManager
    立即 raise，不发任何 HTTP 请求）。

    关键属性：
      - ``error_code``：服务端 detail.code，方便上层根据具体 code 决策
      - ``missing_fields``：服务端 detail.missing_fields，方便给运维明确提示
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        missing_fields: Optional[list[str]] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "PROVIDER_NOT_CONFIGURED")
        self.missing_fields = list(missing_fields or ())


class ProviderUpstreamError(MailServiceError):
    """424 — provider 上游 API（如 CF Worker / Stripe / Apple Mail backend）返回 4xx。

    语义：服务端调上游被拒（domain 错 / auth 错 / 余额不足等）。客户端应当
    **换 provider 或修参数**，重试同一 provider 大概率仍失败。

    服务端契约：响应 ``{"detail": {"code": "PROVIDER_UPSTREAM_ERROR",
    "message": "...", "upstream_status": <int>}}``。
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        upstream_status: int = 0,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "PROVIDER_UPSTREAM_ERROR")
        self.upstream_status = int(upstream_status or 0)


class MailProvider(ABC):
    """邮箱服务抽象基类"""

    @abstractmethod
    def ensure_runtime_ready(
        self,
        provider: str,
        *,
        session_mode: str = "",
    ) -> dict[str, Any]:
        """
        检查当前 email-provider 运行态是否满足所需 provider / session_mode。

        Returns:
            诊断信息（至少包含 health / provider_profile）

        Raises:
            MailRuntimeIncompatibleError: 当前运行态不是 latest-only 所需协议
        """

    @abstractmethod
    def create_session(
        self,
        provider: str,
        *,
        purpose: str = "generic",
        session_mode: str = "",
        proxy: str = "",
        extra: Optional[dict[str, Any]] = None,
        email: str = "",
        account_id: str = "",
        account_extra: Optional[dict[str, Any]] = None,
        existing_account: Optional[dict[str, Any]] = None,
        lease_seconds: int = 900,
    ) -> MailSession:
        """
        创建邮箱会话，分配或接管一个邮箱地址。

        Args:
            provider: 邮箱 provider 名称
            purpose: 业务目的
            session_mode: 会话模式（managed / credentialed / 空串兼容旧接口）
            proxy: 代理地址
            extra: provider 配置
            email: 已知邮箱（不传则新分配）
            account_id: 已知 provider 账号 ID
            account_extra: 账号附加信息
            existing_account: 新版 credentialed 模式传入的既有邮箱上下文
            lease_seconds: 租约有效期

        Returns:
            MailSession

        Raises:
            ConnectionError: 服务不可达
            ValueError: 参数错误或 provider 不支持
        """

    @abstractmethod
    def poll_code(
        self,
        session: MailSession,
        *,
        keyword: str = "",
        timeout_seconds: int = 120,
        code_pattern: Optional[str] = None,
        otp_sent_at: Optional[float] = None,
        exclude_codes: Optional[list[str]] = None,
    ) -> Optional[str]:
        """
        轮询验证码。

        Args:
            session: 活跃的邮箱会话
            keyword: 邮件筛选关键词
            timeout_seconds: 等待时长
            code_pattern: 自定义验证码正则
            otp_sent_at: OTP 触发时间戳
            exclude_codes: 排除的历史验证码

        Returns:
            验证码字符串，失败返回 None
        """

    @abstractmethod
    def complete(self, session: MailSession, *, result: str = "success", reason: str = "") -> None:
        """
        标记会话完成并释放资源。

        Args:
            session: 邮箱会话
            result: "success" 或 "failed"
            reason: 失败原因
        """


class HttpMailProvider(MailProvider):
    """通过 HTTP API 对接远端邮件服务"""

    def __init__(self, base_url: str = "https://email.feixingqi.shop", api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_prefix = f"{self._base_url}/api/mailbox-service"
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        """构建请求头，包含 API Key 认证"""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _endpoint_url(self, endpoint: str) -> str:
        return f"{self._api_prefix}/{endpoint.lstrip('/')}"

    @staticmethod
    def _normalize_session_mode(value: str) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _is_retryable_request_error(exc: requests.RequestException) -> bool:
        """判断 requests 异常是否属于上游瞬时故障可重试范畴。

        重试条件：
          - 5xx (上游服务暂时不可用，如 CF Worker / OpenAI 短窗故障)
          - ConnectionError / Timeout (网络抖动)
        不重试：
          - 4xx (客户端错误，重试无意义且会污染日志)
          - 其他业务级异常
        """
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code in _RETRYABLE_STATUS_CODES

    def _request_with_retry(
        self,
        request_fn: Callable[[], requests.Response],
        *,
        operation: str,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> requests.Response:
        """按 _RETRY_BACKOFF_SECONDS 节奏重试可恢复的 5xx / 网络错误。

        request_fn 必须是无副作用、可重入的 closure（每次重试重新发请求），
        且应自行调用 raise_for_status() 以便我们捕获 HTTPError。

        sleep_fn 默认在函数体内 lookup time.sleep（不在签名里固化默认值），
        这样测试用 mock.patch("time.sleep") 能正确生效。
        """
        # 在函数体内取值，避免默认值在 def 时即被绑定为原 time.sleep
        actual_sleep = sleep_fn if sleep_fn is not None else time.sleep
        last_exc: Optional[requests.RequestException] = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return request_fn()
            except requests.RequestException as exc:
                last_exc = exc
                if not self._is_retryable_request_error(exc):
                    raise
                if attempt >= _RETRY_MAX_ATTEMPTS - 1:
                    raise
                backoff = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                logger.warning(
                    "%s 上游瞬时失败 (status=%s, attempt=%d/%d)，%.1fs 后重试: %s",
                    operation,
                    status_code if status_code is not None else "network",
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                    backoff,
                    exc,
                )
                actual_sleep(backoff)
        # 理论不会到这（循环最后一次失败必 raise），但满足类型检查
        assert last_exc is not None
        raise last_exc

    def _load_json_dict(self, response: requests.Response, *, endpoint: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MailRuntimeIncompatibleError(
                f"读取 email-provider /{endpoint} 失败：响应不是合法 JSON，请确认 {self._base_url} 正在运行最新版本。"
            ) from exc
        if not isinstance(payload, dict):
            raise MailRuntimeIncompatibleError(
                f"读取 email-provider /{endpoint} 失败：响应结构异常，请确认 {self._base_url} 正在运行最新版本。"
            )
        return payload

    @staticmethod
    def _extract_error_payload(
        response: Optional[requests.Response],
    ) -> tuple[str, str, list[str], int]:
        """从服务端 4xx/5xx 响应体里提取 detail.code / message / missing_fields / upstream_status。

        服务端 contract（见 docs/architecture/mail-provider-contract.md）：
            { "detail": {
                "code": "<MACHINE_READABLE>",
                "message": "<人类可读>",
                "missing_fields": ["..."],     # 仅 PROVIDER_NOT_CONFIGURED
                "upstream_status": <int>       # 仅 PROVIDER_UPSTREAM_ERROR
            }}

        不符合 contract 的响应体（旧服务端 / 网络层错误）容忍：返回空串 / 空列表，
        让上层兜底成 MailRuntimeIncompatibleError。
        """
        if response is None:
            return "", "", [], 0
        try:
            payload = response.json()
        except (ValueError, AttributeError):
            return "", "", [], 0
        if not isinstance(payload, dict):
            return "", "", [], 0
        detail = payload.get("detail")
        if not isinstance(detail, dict):
            return "", "", [], 0
        code = str(detail.get("code") or "").strip()
        message = str(detail.get("message") or "").strip()
        missing_raw = detail.get("missing_fields") or []
        missing_fields = [str(f) for f in missing_raw if f] if isinstance(missing_raw, list) else []
        try:
            upstream_status = int(detail.get("upstream_status") or 0)
        except (TypeError, ValueError):
            upstream_status = 0
        return code, message, missing_fields, upstream_status

    def _raise_runtime_request_error(
        self,
        *,
        exc: requests.RequestException,
        endpoint: str,
        operation: str,
    ) -> None:
        """根据 status code + 响应体 contract 把 HTTP 错误精准分类成业务异常。

        分类规则（与服务端 contract 对齐）：
          - 401 → MailRuntimeIncompatibleError（鉴权问题，运行态视为不兼容）
          - 404 → MailRuntimeIncompatibleError（端点缺失，服务端太旧）
          - 422 → MissingProviderConfigError（必填配置缺失，客户端应修 admin UI）
          - 424 → ProviderUpstreamError（上游 API 4xx，客户端应换 provider 或修参数）
          - 5xx → MailRuntimeIncompatibleError（服务端运行态异常，客户端应升级/重启服务端）
          - 其他 → MailRuntimeIncompatibleError（兜底）

        4xx 不会触发 ``_request_with_retry`` 重试（``_is_retryable_request_error``
        只认 5xx + ConnectionError + Timeout）；客户端立即 fail-fast，不做无效阻塞。
        """
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        code, server_message, missing_fields, upstream_status = self._extract_error_payload(response)

        if status_code == 401:
            raise MailRuntimeIncompatibleError(
                f"{operation}失败：email-provider 鉴权失败(401)，请检查 EMAIL_PROVIDER_API_KEY 是否与 {self._base_url} 当前运行实例匹配。"
            ) from exc
        if status_code == 404:
            raise MailRuntimeIncompatibleError(
                f"{operation}失败：当前 email-provider 缺少 /{endpoint}，说明 {self._base_url} 仍是旧运行态、错环境，或尚未重启到最新版本。"
            ) from exc
        if status_code == 422:
            human_message = (
                server_message
                or f"{operation}失败：必填 provider 配置字段缺失"
                + (f"（{', '.join(missing_fields)}）" if missing_fields else "")
                + "，请修 admin UI / .env 后重试。"
            )
            raise MissingProviderConfigError(
                human_message,
                error_code=code or "PROVIDER_NOT_CONFIGURED",
                missing_fields=missing_fields,
            ) from exc
        if status_code == 424:
            human_message = (
                server_message
                or f"{operation}失败：mail provider 上游 API 返回 4xx"
                + (f"（upstream_status={upstream_status}）" if upstream_status else "")
                + "，请换 provider 或修参数。"
            )
            raise ProviderUpstreamError(
                human_message,
                error_code=code or "PROVIDER_UPSTREAM_ERROR",
                upstream_status=upstream_status,
            ) from exc
        if status_code is not None and status_code >= 500:
            raise MailRuntimeIncompatibleError(
                f"{operation}失败：email-provider 返回 {status_code}，当前运行态可能异常；请先确认 {self._base_url} 已重启到最新版本后重试。"
            ) from exc
        # 400 是上游 provider（CF Worker / Stripe / Apple Mail backend）的客户端语义错误，
        # 不是 email-provider 本身的运行态问题。归为 ProviderUpstreamError 让 triage
        # 可以走"换邮箱/换 provider"的可恢复路径，而不是终止整个自动化任务。
        # 远端契约升级后应改为返回 424 + 结构化 detail；当前临时兜底识别 raw body。
        if status_code == 400:
            raw_body = ""
            try:
                raw_body = response.text if response is not None else ""
            except Exception:
                raw_body = ""
            human_message = (
                server_message
                or raw_body
                or f"{operation}失败：上游 provider 返回 400 客户端错误"
            )
            raise ProviderUpstreamError(
                f"{operation}失败：{human_message}",
                error_code=code or "PROVIDER_UPSTREAM_4XX",
                upstream_status=400,
            ) from exc
        raise MailRuntimeIncompatibleError(
            f"{operation}失败：无法确认 {self._base_url} 的 email-provider 运行态，原始错误: {exc}"
        ) from exc

    def ensure_runtime_ready(
        self,
        provider: str,
        *,
        session_mode: str = "",
    ) -> dict[str, Any]:
        provider_name = str(provider or "").strip().lower()
        normalized_mode = self._normalize_session_mode(session_mode)

        def _do_health() -> requests.Response:
            r = requests.get(
                self._endpoint_url("health"),
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r

        try:
            health_resp = self._request_with_retry(_do_health, operation="邮箱服务 health 检查")
        except requests.RequestException as exc:
            self._raise_runtime_request_error(
                exc=exc,
                endpoint="health",
                operation="邮箱服务 health 检查",
            )
        health_payload = self._load_json_dict(health_resp, endpoint="health")
        if health_payload.get("ok") is False:
            raise MailRuntimeIncompatibleError(
                f"邮箱服务 health 检查失败：{self._base_url} 返回 ok=false，请先确认运行态与配置。"
            )

        def _do_providers() -> requests.Response:
            r = requests.get(
                self._endpoint_url("providers"),
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r

        try:
            providers_resp = self._request_with_retry(_do_providers, operation="邮箱服务 provider 能力检查")
        except requests.RequestException as exc:
            self._raise_runtime_request_error(
                exc=exc,
                endpoint="providers",
                operation="邮箱服务 provider 能力检查",
            )
        providers_payload = self._load_json_dict(providers_resp, endpoint="providers")
        providers = providers_payload.get("providers")
        if not isinstance(providers, list):
            raise MailRuntimeIncompatibleError(
                "邮箱服务 provider 能力检查失败：/providers 响应缺少 providers 列表，请确认当前 email-provider 已升级到最新版本。"
            )

        provider_profile = next(
            (
                item for item in providers
                if isinstance(item, dict) and str(item.get("name") or "").strip().lower() == provider_name
            ),
            None,
        )
        if provider_profile is None:
            raise MailRuntimeIncompatibleError(
                f"邮箱服务 provider 能力检查失败：当前运行态未声明 provider={provider_name}，请检查是否连到了错环境。"
            )

        supported_modes_raw = provider_profile.get("supported_session_modes")
        if not isinstance(supported_modes_raw, list):
            raise MailRuntimeIncompatibleError(
                f"邮箱服务 provider 能力检查失败：provider={provider_name} 缺少 supported_session_modes，当前运行态过旧。"
            )
        supported_modes = {
            self._normalize_session_mode(item)
            for item in supported_modes_raw
            if self._normalize_session_mode(item)
        }
        if normalized_mode and normalized_mode not in supported_modes:
            raise MailRuntimeIncompatibleError(
                f"邮箱服务 provider 能力检查失败：provider={provider_name} 不支持 session_mode={normalized_mode}；"
                f"当前 8000 运行态可能过旧、错环境，或尚未重启到最新版本。"
            )

        return {
            "health": health_payload,
            "provider_profile": provider_profile,
            "supported_session_modes": sorted(supported_modes),
        }

    def list_provider_domains(
        self,
        provider: str,
        *,
        config_id: Optional[int] = None,
        config_name: str = "",
    ) -> dict[str, Any]:
        """查询远端 provider 当前启用的 mailbox 域名列表。

        用途：批量注册时，本地客户端需要把 identity_email_local 拼成完整 email
        （例如 ``william.harrison82@gitee.shop``）传给 ``create_session(email=...)``，
        让远端走 base_mailbox.py 已有的"模式 B"路径生成有意义的邮箱（替代默认 tmpXXXXXX）。

        返回 schema（与远端 GET /managed-providers/{provider}/domains 对齐）：
            {
                "provider": str,
                "default_domain": str,
                "enabled_domains": list[str],
            }

        失败语义：
        - 4xx / 404：远端尚未部署该端点 → 返回空字典 ``{"enabled_domains": []}``
          调用方据此降级回原 tmpXXXXXX 路径，不影响主流程。
        - 5xx / 网络抖动：复用 ``_request_with_retry`` 的指数退避；最终失败仍降级。
        """
        provider_name = str(provider or "").strip().lower()
        if not provider_name:
            return {"provider": "", "default_domain": "", "enabled_domains": []}

        endpoint_path = f"managed-providers/{provider_name}/domains"
        params: dict[str, Any] = {}
        if config_id is not None:
            params["config_id"] = int(config_id)
        if str(config_name or "").strip():
            params["config_name"] = str(config_name).strip()

        def _do_get() -> requests.Response:
            r = requests.get(
                self._endpoint_url(endpoint_path),
                headers=self._headers(),
                params=params or None,
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r

        try:
            resp = self._request_with_retry(_do_get, operation=f"查询 provider 启用域名 /{endpoint_path}")
        except requests.RequestException:
            # 端点未部署 / 网络异常 → 静默降级。调用方拿到空列表会回到原 tmpXXXXXX 路径。
            return {"provider": provider_name, "default_domain": "", "enabled_domains": []}

        payload = self._load_json_dict(resp, endpoint=endpoint_path)
        raw_domains = payload.get("enabled_domains") or []
        if not isinstance(raw_domains, list):
            raw_domains = []
        domains: list[str] = []
        seen: set[str] = set()
        for item in raw_domains:
            value = str(item or "").strip().lower()
            if value.startswith("@"):
                value = value[1:]
            if value and value not in seen:
                seen.add(value)
                domains.append(value)
        return {
            "provider": str(payload.get("provider") or provider_name),
            "default_domain": str(payload.get("default_domain") or "").strip().lower(),
            "enabled_domains": domains,
        }

    def create_session(
        self,
        provider: str,
        *,
        purpose: str = "generic",
        session_mode: str = "",
        proxy: str = "",
        extra: Optional[dict[str, Any]] = None,
        email: str = "",
        account_id: str = "",
        account_extra: Optional[dict[str, Any]] = None,
        existing_account: Optional[dict[str, Any]] = None,
        lease_seconds: int = 900,
        config_name: str = "",
    ) -> MailSession:
        normalized_mode = self._normalize_session_mode(session_mode)
        endpoint = "sessions"
        if normalized_mode == "managed":
            endpoint = "managed-sessions"
        elif normalized_mode == "credentialed":
            endpoint = "credentialed-sessions"
            self.ensure_runtime_ready(provider, session_mode=normalized_mode)

        payload: dict[str, Any] = {
            "provider": provider,
            "purpose": purpose,
            "lease_seconds": lease_seconds,
        }
        if config_name:
            payload["config_name"] = config_name
        if normalized_mode:
            payload["session_mode"] = normalized_mode
        if proxy:
            payload["proxy"] = proxy
        if extra:
            payload["extra"] = extra
        if existing_account:
            payload["existing_account"] = existing_account
        elif normalized_mode == "credentialed" and email:
            payload["existing_account"] = {
                "email": email,
                "account_id": account_id,
                "extra": account_extra or {},
            }
        else:
            if email:
                payload["email"] = email
            if account_id:
                payload["account_id"] = account_id
            if account_extra:
                payload["account_extra"] = account_extra

        def _do_create() -> requests.Response:
            r = requests.post(
                self._endpoint_url(endpoint),
                json=payload,
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r

        try:
            resp = self._request_with_retry(_do_create, operation=f"创建邮箱会话 /{endpoint}")
            data = resp.json()
        except requests.RequestException as exc:
            # 不论 managed / credentialed，都走统一的 status + contract 分类。
            # 之前 managed 分支直接 raise ConnectionError 把 4xx contract 信息全吃掉，
            # 导致上层无法区分"参数错"和"网络抖动"，违反架构契约。
            self._raise_runtime_request_error(
                exc=exc,
                endpoint=endpoint,
                operation=f"创建{'credentialed ' if normalized_mode == 'credentialed' else ''}邮箱会话",
            )

        return MailSession(
            session_id=data["session_id"],
            lease_token=data["lease_token"],
            email=data.get("email", ""),
            provider=data.get("provider", provider),
            before_ids=data.get("before_ids", []),
            provider_meta=data.get("provider_meta", {}),
            account_id=data.get("account_id", ""),
            session_mode=data.get("session_mode", normalized_mode or "managed"),
            state=data.get("state", "leased"),
            expires_at=data.get("expires_at", ""),
        )

    def poll_code(
        self,
        session: MailSession,
        *,
        keyword: str = "",
        timeout_seconds: int = 120,
        code_pattern: Optional[str] = None,
        otp_sent_at: Optional[float] = None,
        exclude_codes: Optional[list[str]] = None,
    ) -> Optional[str]:
        payload: dict[str, Any] = {
            "lease_token": session.lease_token,
            "timeout_seconds": timeout_seconds,
            "before_ids": session.before_ids,
        }
        if keyword:
            payload["keyword"] = keyword
        if code_pattern is not None:
            payload["code_pattern"] = code_pattern
        if otp_sent_at is not None:
            payload["otp_sent_at"] = otp_sent_at
        if exclude_codes:
            payload["exclude_codes"] = exclude_codes

        def _do_poll() -> requests.Response:
            r = requests.post(
                self._endpoint_url(f"sessions/{session.session_id}/poll-code"),
                json=payload,
                headers=self._headers(),
                timeout=max(timeout_seconds + _POLL_CODE_TIMEOUT_BUFFER_SECONDS, _REQUEST_TIMEOUT),
            )
            r.raise_for_status()
            return r

        try:
            resp = self._request_with_retry(_do_poll, operation="轮询验证码")
            data = resp.json()
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 404:
                raise MailRuntimeIncompatibleError(
                    f"轮询验证码失败：当前 email-provider 缺少 /sessions/{{id}}/poll-code，请确认 {self._base_url} 已重启到最新版本。"
                ) from exc
            if status_code is not None and status_code >= 500:
                raise MailServiceError(
                    f"轮询验证码失败：email-provider 返回 {status_code}，请确认 {self._base_url} 当前运行态是否健康。"
                ) from exc
            raise MailServiceError(f"轮询验证码失败: {exc}") from exc

        if data.get("status") == "ready":
            code = data.get("code", "")
            if code:
                logger.info("成功获取验证码: %s", code)
                return code

        status = str(data.get("status", "") or "").strip().lower()
        error_code = data.get("error_code", "")
        message = data.get("message", "")
        if error_code:
            if str(error_code).strip() not in {"POLL_TIMEOUT", "CODE_TIMEOUT"}:
                raise MailServiceError(message or f"轮询验证码失败 [{error_code}]")
            logger.warning("轮询验证码失败 [%s]: %s", error_code, message)
        elif status == "failed":
            raise MailServiceError(message or "邮件服务返回失败状态")
        else:
            logger.warning("轮询验证码未就绪: %s", message)

        return None

    def complete(self, session: MailSession, *, result: str = "success", reason: str = "") -> None:
        payload: dict[str, Any] = {
            "lease_token": session.lease_token,
            "result": result,
        }
        if reason:
            payload["reason"] = reason

        try:
            resp = requests.post(
                self._endpoint_url(f"sessions/{session.session_id}/complete"),
                json=payload,
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            logger.info("邮箱会话 %s 已标记为 %s", session.session_id, result)
        except requests.RequestException as exc:
            logger.error("完成邮箱会话失败: %s", exc)
