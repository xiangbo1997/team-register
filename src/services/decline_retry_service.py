# -*- coding: utf-8 -*-
"""
Stripe Decline 重试编排层

为什么需要：
- 同一张卡 + 同一 Checkout Session 在 ``submit_pro_and_capture_outcome`` 跑一次后
  若是 ``insufficient_funds`` / ``do_not_honor`` 这类**可重试** decline 码，
  通常立即换 IP / 换页时序就能恢复（不是 BIN 终态封禁），但当前实现只跑一次就放弃。
- 借鉴 gpt-pp-team ``CTF-pay/retry_house_decline.py`` 的"多轮 + 时间抖动"思路，
  函数级重试（不 subprocess respawn），与现有 ``_classify_decline()`` 集成。

设计约束：
- ``max_attempts`` 默认 2，保守不让一张卡反复触发 Stripe velocity rule
- 仅对**可重试** decline 码重试；终态码（fraudulent / expired_card / incorrect_cvc）不重试
- 失败回写 ``Run.decline_attempts`` 计数，运维可通过控制台聚合统计
- 联动 ``bin_health_service``：每次 decline 写入 BIN 失败信号，让健康度统计能感知

不在范围：
- 不重写 ``submit_pro_and_capture_outcome``（保持单次提交语义）
- 不做异步队列：retry 是同步的，挂在 Playwright page 生命周期上
- 不替运维做"BIN 自动禁用"决策（决策权留给运维或调度器，避免误伤）

参考：``docs/research/risk-control-insights.md`` §3 fallback 决策树
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.db.engine import get_session
from src.db.models import Run

logger = logging.getLogger(__name__)


# ── 可重试 vs 终态 ─────────────────────────────────

# 可重试 decline 码：通常是发卡行风控/余额/网络抖动，重试有可能成功
RETRYABLE_DECLINE_CODES: frozenset[str] = frozenset(
    {
        "insufficient_funds",
        "do_not_honor",
        "card_declined",  # 模糊码，给一次机会
    }
)

# 终态 decline 码：重试只会浪费资源
TERMINAL_DECLINE_CODES: frozenset[str] = frozenset(
    {
        "fraudulent",
        "expired_card",
        "incorrect_cvc",
        "stolen_card",
    }
)


@dataclass(frozen=True)
class RetryOutcome:
    """重试编排的最终输出。"""

    final_status: str  # "succeeded" | "declined" | "failed"
    final_decline_code: str  # 最后一次的 decline 码（可能是空串）
    attempts_made: int
    succeeded_on_attempt: Optional[int]  # 1-based；未成功则 None
    history: list[dict[str, Any]]  # 每次尝试的完整 outcome


# ── 主编排函数 ────────────────────────────────────


def retry_decline_with_jitter(
    *,
    submit_callable: Callable[[], dict[str, Any]],
    max_attempts: int = 2,
    base_delay_ms: int = 1500,
    run_id: Optional[str] = None,
    card_bin: Optional[str] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> RetryOutcome:
    """
    包装 ``submit_callable``（通常是 ``submit_pro_and_capture_outcome`` 的闭包），
    遇可重试 decline 码时按抖动延迟重试。

    Args:
        submit_callable: 无参 callable，每次返回 ``submit_pro_and_capture_outcome``
            那种结构 ``{"status", "decline_code", "rationale", "raw_signals"}``。
            **必须**幂等：调用方负责把 page 重置到可重新提交的状态（通常是
            重新进 checkout 页 / 刷新 / 清空表单）。
        max_attempts: 总尝试次数（含首次）。默认 2 = 首次失败后再试 1 次。
        base_delay_ms: 重试前的基础延迟。实际延迟 = base * attempt + jitter [-300,+300]ms。
        run_id: 用于在 DB 累计 ``decline_attempts``；缺省则不写库（便于单测）。
        card_bin: 用于联动 ``bin_health_service`` 标记 decline 信号；缺省则跳过。
        sleep_fn: 注入点，便于测试不真睡。

    Returns:
        RetryOutcome：含完整尝试历史与最终状态。

    设计要点：
    - **不抛异常**：所有内部错误降级为 ``final_status="failed"`` 并记录到 history.rationale。
    - **DB / BIN 联动失败静默**：写库出问题不应破坏 retry 主流程。
    - **递增延迟 + 抖动**：第 1 次重试等 ``base_delay_ms`` ± 300ms，
      第 2 次重试等 ``base_delay_ms * 2`` ± 300ms，依此类推。
      避免均匀间隔被 Stripe velocity 检测识别为脚本重放。
    """
    if max_attempts < 1:
        max_attempts = 1
    history: list[dict[str, Any]] = []
    succeeded_on: Optional[int] = None

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            delay_ms = base_delay_ms * (attempt - 1) + random.randint(-300, 300)
            delay_ms = max(100, delay_ms)
            sleep_fn(delay_ms / 1000.0)

        try:
            outcome = submit_callable() or {}
        except Exception as exc:
            logger.exception("decline retry attempt %d submit_callable raised", attempt)
            outcome = {
                "status": "failed",
                "decline_code": "",
                "rationale": f"submit_callable_exception: {type(exc).__name__}: {exc!s}"[:300],
                "raw_signals": {},
            }

        history.append(dict(outcome))
        status = str(outcome.get("status", "")).lower()
        decline_code = str(outcome.get("decline_code", "")).lower()

        if status == "succeeded":
            succeeded_on = attempt
            break

        # 终态：不重试
        if decline_code in TERMINAL_DECLINE_CODES:
            logger.info(
                "decline retry stop: terminal code=%s attempt=%d/%d",
                decline_code, attempt, max_attempts,
            )
            _record_decline_signal(run_id=run_id, card_bin=card_bin, attempt=attempt)
            break

        # 可重试：记一次 + 写信号 + 进入下一轮（如还有）
        if decline_code in RETRYABLE_DECLINE_CODES:
            logger.info(
                "decline retry: code=%s attempt=%d/%d (will %s)",
                decline_code, attempt, max_attempts,
                "retry" if attempt < max_attempts else "give up",
            )
            _record_decline_signal(run_id=run_id, card_bin=card_bin, attempt=attempt)
            continue

        # status=failed 或 decline_code 空（识别失败）：不重试，避免无限循环
        logger.info(
            "decline retry stop: ambiguous status=%s code=%s attempt=%d",
            status, decline_code or "<empty>", attempt,
        )
        break

    if succeeded_on:
        final_status = "succeeded"
        final_code = ""
    else:
        last = history[-1] if history else {}
        final_status = str(last.get("status", "failed")).lower() or "failed"
        final_code = str(last.get("decline_code", "")).lower()

    return RetryOutcome(
        final_status=final_status,
        final_decline_code=final_code,
        attempts_made=len(history),
        succeeded_on_attempt=succeeded_on,
        history=history,
    )


# ── 内部信号联动 ─────────────────────────────────


def _record_decline_signal(
    *,
    run_id: Optional[str],
    card_bin: Optional[str],
    attempt: int,
) -> None:
    """
    把单次 decline 写入：
    - ``runs.decline_attempts``（按 attempt 累加，不直接覆盖）
    - 不直接写 BIN health；BIN 维度的失败聚合由 ``query_bin_health`` 在 Run.status="failed"
      落地后基于聚合查询得出。这里只保证 Run 自身有计数。

    失败静默：DB 异常不打断主流程。
    """
    if not run_id:
        return
    try:
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is None:
                logger.debug("decline retry: run_id=%s not found, skip counter", run_id)
                return
            run.decline_attempts = (run.decline_attempts or 0) + 1
            session.add(run)
            session.commit()
    except Exception as exc:
        logger.warning("decline retry: failed to bump run.decline_attempts: %s", exc)


__all__ = [
    "RETRYABLE_DECLINE_CODES",
    "TERMINAL_DECLINE_CODES",
    "RetryOutcome",
    "retry_decline_with_jitter",
]
