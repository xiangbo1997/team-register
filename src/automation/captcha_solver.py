# -*- coding: utf-8 -*-
"""
Turnstile / Captcha 自愈框架

设计目标：
- 在状态机进入 BLOCKED 之前，先给一次"自动求解"机会，命中即继续主流程
- 框架与具体 solver 解耦，未来要接 nocaptcha.io / yescaptcha.com / capsolver 时，
  只需新增一个 30 行的 SolverProvider 实现即可
- 当前默认 NoOpSolver（不做任何事），等价于"零行为变更"

⚠️ 重要结论（2026-06-04，3 个独立调研 agent + Cloudflare 官方文档交叉验证）：
  **「在别处求解 token 再注入回页面」对交互式 Turnstile 是死路，别再走这条路。**
  - Turnstile token 绑定「域名/sitekey + IP 信誉 + 浏览器遥测/TLS 指纹」，单次使用、
    300s 过期。solver 在自己的浏览器/IP 解出的 token，注入回 AdsPower 页面（不同 IP/指纹）
    后，服务端 siteverify 会以 domain/IP mismatch 拒绝。Cloudflare 官方原话："bots might
    complete challenges, but Cloudflare can detect bot-like signals and mark the token
    as invalid"（点了也白点）。
  - 被打到交互式（出现可勾选 ☐ 复选框）= Cloudflare 已判该会话高风险，此时点击只治标。
  - CDP 协议点击有 screenX/screenY<100 特征，被 Cloudflare 专门检测；Playwright/Selenium
    全中招，只有系统级点击（PyAutoGUI）或 Firefox 免疫。
  **正确的免费解（按 ROI）**：① 住宅/移动 IP + AdsPower 指纹一致性，让 Turnstile 回到
  被动模式自动变绿（根治）；② 偶发交互式用人工接管真人点击兜底（见 grok_runtime.py
  _wait_turnstile_manual_handoff）。本框架的 NoOpSolver 默认 + 不接开源 solver 是刻意决策。

使用方式：
    runtime = AutomationRuntime(..., captcha_solver=NoCaptchaSolver(api_key=...))

接入点：``runtime.py`` 状态机循环检测到 BLOCKED 时调用
``try_solve_captcha(runtime, evidence)``；返回 True 则 continue 主循环，
返回 False 则走原 BLOCKED 路径（triage → manual handoff）。

设计约束（与 triage.py 一致）：
- 失败静默：任何异常降级为 False，绝不打断主流程
- 事件落盘：每次尝试通过 ``emit_event`` 写入 SSE，便于事后分析
- 不阻塞主线程：solver 内部需要自己控制超时
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolveAttempt:
    """单次求解尝试的结果。"""

    success: bool
    provider_name: str  # 例如 "noop" / "nocaptcha" / "manual_fallback"
    duration_ms: int
    rationale: str = ""  # 失败原因或成功简述
    token: Optional[str] = None  # 求解出的 token（如 Turnstile 返回值）


class SolverProvider(ABC):
    """
    Captcha solver 的抽象接口。

    实现要点：
    - ``try_solve()`` 必须在 30s 内返回（建议 < 10s），超时即视为失败
    - 任何外部异常都要内部 catch，包装成 SolveAttempt(success=False)
    - ``provider_name`` 用于事件日志和监控聚合
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """识别用名称，例如 'nocaptcha' / 'yescaptcha'。"""

    @abstractmethod
    def try_solve(
        self,
        *,
        page_url: str,
        site_key: str = "",
        challenge_type: str = "turnstile",
    ) -> SolveAttempt:
        """
        尝试自动求解。

        参数：
          page_url: 当前页 URL，部分 solver 需要它做来源校验
          site_key: 从 DOM ``[data-sitekey]`` 提取的 widget id（可空）
          challenge_type: "turnstile" / "recaptcha_v2" / "hcaptcha"

        返回：SolveAttempt，``success=True`` 时 ``token`` 必须非空
        """


class NoOpSolver(SolverProvider):
    """
    默认 solver：什么都不做，直接返回失败。

    存在意义：让 ``runtime.captcha_solver`` 永远非 None，调用方不需要每次判空。
    在生产环境下可以注入这个，等同于"未启用自愈"。
    """

    @property
    def provider_name(self) -> str:
        return "noop"

    def try_solve(
        self,
        *,
        page_url: str,
        site_key: str = "",
        challenge_type: str = "turnstile",
    ) -> SolveAttempt:
        return SolveAttempt(
            success=False,
            provider_name=self.provider_name,
            duration_ms=0,
            rationale="noop solver always declines",
        )


class ManualFallbackSolver(SolverProvider):
    """
    手动降级 solver：等价 NoOpSolver，但更明确地记录"故意走人工"语义。

    使用场景：希望事件日志里清楚区分"未配置 solver"与"配置了但故意降级"。
    """

    @property
    def provider_name(self) -> str:
        return "manual_fallback"

    def try_solve(
        self,
        *,
        page_url: str,
        site_key: str = "",
        challenge_type: str = "turnstile",
    ) -> SolveAttempt:
        return SolveAttempt(
            success=False,
            provider_name=self.provider_name,
            duration_ms=0,
            rationale="manual fallback by design",
        )


class NoCaptchaSolver(SolverProvider):
    """
    nocaptcha.io 适配器：调用其 universal Cloudflare Turnstile 端点。

    端点契约（参考其官方文档；正式接入前必须实测）：
        POST https://api.nocaptcha.io/api/wanda/cloudflare/universal
        Header: User-Token: <token>
        Body  : {"href": "<page_url>", "sitekey": "<site_key>"}
        Resp  : {"status": 1, "data": {"token": "..."}} 成功
                {"status": 0, "msg": "..."}             失败

    设计要点：
    - 仅处理 Turnstile（OpenAI 主场景）；challenge_type != "turnstile" 直接降级失败
    - timeout_ms 上限保护：超时即视为失败，让主流程走 BLOCKED
    - 任何 HTTP / JSON 异常都内部 catch，包成 SolveAttempt(success=False)，绝不抛出
    - 不在 solver 内部做预算校验：预算控制由 ConfigService / ConfigSnapshot 在调用前
      把 captcha_solver_kind 切换成 manual / noop 来实现，保持本类纯粹

    依赖：
    - 复用项目已有的 requests（requirements.txt 已声明）
    """

    _DEFAULT_ENDPOINT = "https://api.nocaptcha.io/api/wanda/cloudflare/universal"

    def __init__(
        self,
        *,
        user_token: str,
        timeout_ms: int = 30000,
        endpoint: str = _DEFAULT_ENDPOINT,
    ) -> None:
        if not (user_token or "").strip():
            raise ValueError("NoCaptchaSolver requires non-empty user_token")
        self._user_token = user_token.strip()
        self._timeout_s = max(1.0, float(timeout_ms) / 1000.0)
        self._endpoint = endpoint

    @property
    def provider_name(self) -> str:
        return "nocaptcha"

    def try_solve(
        self,
        *,
        page_url: str,
        site_key: str = "",
        challenge_type: str = "turnstile",
    ) -> SolveAttempt:
        if challenge_type != "turnstile":
            return SolveAttempt(
                success=False,
                provider_name=self.provider_name,
                duration_ms=0,
                rationale=f"unsupported challenge_type: {challenge_type}",
            )
        if not (page_url or "").strip():
            return SolveAttempt(
                success=False,
                provider_name=self.provider_name,
                duration_ms=0,
                rationale="empty page_url",
            )

        started_ms = time.monotonic()
        try:
            import requests  # 延迟导入，避免在测试 mock 时引入额外依赖

            payload = {"href": page_url}
            if site_key:
                payload["sitekey"] = site_key

            resp = requests.post(
                self._endpoint,
                headers={"User-Token": self._user_token, "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout_s,
            )
            duration_ms = int((time.monotonic() - started_ms) * 1000)
            if resp.status_code != 200:
                return SolveAttempt(
                    success=False,
                    provider_name=self.provider_name,
                    duration_ms=duration_ms,
                    rationale=f"http_{resp.status_code}",
                )
            body = resp.json() if resp.content else {}
            status = body.get("status")
            if status != 1:
                return SolveAttempt(
                    success=False,
                    provider_name=self.provider_name,
                    duration_ms=duration_ms,
                    rationale=f"upstream_decline: {body.get('msg', 'no_msg')}",
                )
            token = ""
            data = body.get("data") or {}
            if isinstance(data, dict):
                token = str(data.get("token") or "").strip()
            if not token:
                return SolveAttempt(
                    success=False,
                    provider_name=self.provider_name,
                    duration_ms=duration_ms,
                    rationale="empty_token_in_success_response",
                )
            return SolveAttempt(
                success=True,
                provider_name=self.provider_name,
                duration_ms=duration_ms,
                token=token,
                rationale="ok",
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - started_ms) * 1000)
            return SolveAttempt(
                success=False,
                provider_name=self.provider_name,
                duration_ms=duration_ms,
                rationale=f"exception: {type(exc).__name__}: {exc!s}"[:200],
            )


def build_solver_from_config(config: Any) -> SolverProvider:
    """
    从 AppConfig 构造合适的 SolverProvider 实例。

    路由规则：
    - captcha_solver_kind == "nocaptcha" 且 nocaptcha_user_token 非空 → NoCaptchaSolver
    - captcha_solver_kind == "manual" → ManualFallbackSolver
    - 其他（含 "noop" 或配置错误）→ NoOpSolver

    设计：失败降级为 NoOpSolver，绝不抛异常，避免 worker 启动崩溃。
    """
    kind = str(getattr(config, "captcha_solver_kind", "noop") or "noop").strip().lower()
    if kind == "manual":
        return ManualFallbackSolver()
    if kind == "nocaptcha":
        token = str(getattr(config, "nocaptcha_user_token", "") or "").strip()
        if not token:
            logger.warning("captcha_solver_kind=nocaptcha but NOCAPTCHA_USER_TOKEN empty; degrading to noop")
            return NoOpSolver()
        timeout_ms = int(getattr(config, "captcha_solver_timeout_ms", 30000) or 30000)
        try:
            return NoCaptchaSolver(user_token=token, timeout_ms=timeout_ms)
        except Exception as exc:
            logger.warning("NoCaptchaSolver build failed: %s; degrading to noop", exc)
            return NoOpSolver()
    return NoOpSolver()


def _extract_site_key(page: Any) -> str:
    """从页面 DOM 提取 Turnstile / hCaptcha 的 sitekey。失败返回空串。"""
    if page is None:
        return ""
    try:
        # Playwright Locator 接口
        elem = page.locator("[data-sitekey]").first
        if elem and hasattr(elem, "get_attribute"):
            value = elem.get_attribute("data-sitekey", timeout=1000)
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass
    return ""


def _detect_challenge_type(signals: dict[str, Any]) -> str:
    """从 evidence.signals 推断挑战类型。当前仅识别 turnstile，未来可扩展。"""
    # 简单启发式：所有挑战默认按 turnstile 处理（OpenAI 主要场景）
    # 若未来接入更多 solver，可基于 signals['challenge_widget_src'] 等做精细判断
    return "turnstile"


def try_solve_captcha(runtime: Any, evidence: Any) -> bool:
    """
    主流程注入点：BLOCKED 前先尝试自动求解。

    返回：
      True  -> 求解成功，调用方应 continue 主循环
      False -> 求解失败或未配置 solver，调用方应走原 BLOCKED 路径

    约定：本函数永不抛出异常，任何错误降级为 False。
    """
    solver = getattr(runtime, "captcha_solver", None)
    if solver is None:
        return False

    page = getattr(runtime, "page", None)
    page_url = str(getattr(evidence, "url", "") or "")
    signals = dict(getattr(evidence, "signals", {}) or {})
    site_key = _extract_site_key(page)
    challenge_type = _detect_challenge_type(signals)

    try:
        attempt = solver.try_solve(
            page_url=page_url,
            site_key=site_key,
            challenge_type=challenge_type,
        )
    except Exception as exc:
        _emit_solver_event(
            runtime,
            success=False,
            provider_name=getattr(solver, "provider_name", "unknown"),
            duration_ms=0,
            rationale=f"solver_exception: {exc!s}",
        )
        return False

    _emit_solver_event(
        runtime,
        success=attempt.success,
        provider_name=attempt.provider_name,
        duration_ms=attempt.duration_ms,
        rationale=attempt.rationale,
    )
    return bool(attempt.success)


def _emit_solver_event(
    runtime: Any,
    *,
    success: bool,
    provider_name: str,
    duration_ms: int,
    rationale: str,
) -> None:
    """事件上报。失败静默。"""
    emitter = getattr(runtime, "emit_event", None)
    if not callable(emitter):
        return
    try:
        emitter(
            "captcha_solver",
            payload={
                "success": success,
                "provider": provider_name,
                "duration_ms": duration_ms,
                "rationale": rationale,
            },
        )
    except Exception:
        return


__all__ = [
    "SolveAttempt",
    "SolverProvider",
    "NoOpSolver",
    "ManualFallbackSolver",
    "NoCaptchaSolver",
    "build_solver_from_config",
    "try_solve_captcha",
]
