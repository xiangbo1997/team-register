# -*- coding: utf-8 -*-
"""
上场前体检（Preflight）—— 集成层

- 身份一致性（四元素：卡 BIN 国家 / 代理 IP 国家 / SMS 国家 / 账单国家）
- 浏览器指纹评分（WebRTC / Canvas / UA / 时区）

把已有的独立模块（`src/fintech/coherence.py`, `src/infra/fingerprint.py`）串到
编排流程最前面，给 ``PhaseOrchestrator.execute()`` 提供"一开工就先体检"的能力。

设计目标：
1. ``warn`` 模式：只记事件，不阻断任务（默认） —— 用于积累数据、不影响已有流程
2. ``block`` 模式：发现问题立即 abort，避免浪费卡/IP/验证码
3. 所有依赖（card/proxy 对象、page 对象）都以参数注入，便于单元测试

配置读取顺序：
1. 优先 ``os.environ['PREFLIGHT_MODE']`` / ``os.environ['FINGERPRINT_MIN_SCORE']``
2. 若提供了 ``AppConfig``，从其读取同名字段（兼容后续 config 扩展）
3. 最终默认值在 ``_resolve_mode()`` / ``_resolve_min_score()`` 里兜底
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.fintech.coherence import CoherenceReport, validate_identity_coherence

logger = logging.getLogger(__name__)


_VALID_MODES = ("off", "warn", "block")


@dataclass(frozen=True)
class PreflightReport:
    """体检结果汇总。"""

    ok: bool
    mode: str  # "off" | "warn" | "block"
    coherence: Optional[CoherenceReport] = None
    fingerprint: Optional[Any] = None  # FingerprintReport or None
    issues: list[str] = field(default_factory=list)
    should_abort: bool = False


def _resolve_mode(config: Any) -> str:
    """取 mode：env > config.preflight_mode > 'warn'。"""
    raw = os.getenv("PREFLIGHT_MODE")
    if raw is None and config is not None:
        raw = getattr(config, "preflight_mode", None)
    mode = str(raw or "warn").strip().lower()
    return mode if mode in _VALID_MODES else "warn"


def _resolve_min_score(config: Any) -> int:
    """取指纹最低分：env > config.fingerprint_min_score > 80（集成期先保守）。"""
    raw = os.getenv("FINGERPRINT_MIN_SCORE")
    if raw is None and config is not None:
        raw = getattr(config, "fingerprint_min_score", None)
    try:
        return int(raw) if raw is not None else 80
    except (TypeError, ValueError):
        return 80


def _normalize_sms_code(config: Any) -> str:
    """AppConfig.sms_country 可能是 '6' 也可能是 'US'，原样交给 coherence 处理。"""
    return str(getattr(config, "sms_country", "") or "").strip()


def check_coherence(
    *,
    config: Any,
    card_bin_country: str,
    proxy_country: str,
) -> CoherenceReport:
    """纯数据一致性校验。不依赖 page，因此可以在 Playwright 启动前就跑。"""
    return validate_identity_coherence(
        card_bin_country=card_bin_country,
        proxy_country=proxy_country,
        sms_country_code=_normalize_sms_code(config),
        billing_country=str(getattr(config, "billing_country", "US") or "US"),
    )


def check_fingerprint(
    *,
    page: Any,
    config: Any,
    expected_country: str = "",
    scorer: Optional[Callable[..., Any]] = None,
) -> Optional[Any]:
    """
    调 ``src/infra/fingerprint.score_page_fingerprint``。

    ``scorer`` 注入给测试用；生产走真实实现。
    任何异常都降级为 None（体检失败不能把主流程拖崩）。
    """
    try:
        if scorer is None:
            from src.infra.fingerprint import score_page_fingerprint

            scorer = score_page_fingerprint
        min_score = _resolve_min_score(config)
        return scorer(page, expected_country=expected_country, min_score=min_score)
    except Exception as exc:  # pragma: no cover - 真实环境异常
        logger.warning("指纹评分失败，降级为 None: %s", exc)
        return None


def run_preflight(
    *,
    config: Any,
    card_bin_country: str = "",
    proxy_country: str = "",
    page: Any = None,
    emit_event: Optional[Callable[..., None]] = None,
    scorer: Optional[Callable[..., Any]] = None,
) -> PreflightReport:
    """
    完整体检：先一致性，再指纹（若有 page）。

    参数:
      emit_event: 可选回调，签名 ``(event_type, payload)``；用于把体检结果写到 SSE。
      scorer:     指纹评分注入，测试友好。

    返回:
      PreflightReport。若 mode=block 且发现阻断级问题，``should_abort=True``。
    """
    mode = _resolve_mode(config)
    issues: list[str] = []

    # Mode = off：完全跳过，任务从不会因体检失败
    if mode == "off":
        return PreflightReport(ok=True, mode=mode)

    # 1) 一致性
    coherence = check_coherence(
        config=config,
        card_bin_country=card_bin_country,
        proxy_country=proxy_country,
    )
    if not coherence.ok:
        issues.extend(coherence.mismatches)

    # 2) 指纹（仅当 page 存在时）
    fingerprint = None
    if page is not None:
        fingerprint = check_fingerprint(
            page=page,
            config=config,
            expected_country=coherence.proxy_country,  # 期望国家 = 代理国家
            scorer=scorer,
        )
        if fingerprint is not None:
            verdict = getattr(fingerprint, "verdict", "pass")
            if verdict == "fail":
                issues.append(f"fingerprint_fail: score={getattr(fingerprint, 'score', 0)}")
            elif verdict == "warn":
                issues.append(f"fingerprint_warn: score={getattr(fingerprint, 'score', 0)}")

    # 3) 汇总
    has_block_level = (
        (coherence and getattr(coherence, "severity", "") == "block")
        or (fingerprint is not None and getattr(fingerprint, "verdict", "pass") == "fail")
    )
    should_abort = bool(mode == "block" and has_block_level)

    # 4) 事件上报
    if emit_event is not None:
        try:
            emit_event(
                "preflight",
                {
                    "mode": mode,
                    "coherence_ok": coherence.ok if coherence else True,
                    "coherence_severity": getattr(coherence, "severity", ""),
                    "fingerprint_verdict": getattr(fingerprint, "verdict", "") if fingerprint else "",
                    "fingerprint_score": getattr(fingerprint, "score", 0) if fingerprint else None,
                    "issues": issues,
                    "will_abort": should_abort,
                },
            )
        except Exception:
            # 事件上报失败不能影响决策
            pass

    # 5) 日志提示
    if has_block_level:
        if mode == "block":
            logger.error("Preflight 检测到阻断级问题，任务将中止: %s", issues)
        else:
            logger.warning("Preflight 检测到阻断级问题（warn 模式，继续执行）: %s", issues)
    elif issues:
        logger.info("Preflight 记录警告项: %s", issues)
    else:
        logger.info("Preflight 通过")

    return PreflightReport(
        ok=(not has_block_level),
        mode=mode,
        coherence=coherence,
        fingerprint=fingerprint,
        issues=issues,
        should_abort=should_abort,
    )
