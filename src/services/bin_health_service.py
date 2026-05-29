# -*- coding: utf-8 -*-
"""
BIN 健康度服务（H2 风控审计）

为什么需要：
- Stripe 对低质卡 BIN 段（已知刷量段）有专门封禁
- 同一个 BIN 短期内被本项目大量复用，会触发 BIN-level 风控
- 一旦触发，**同一卡商的所有后续注册都会被拒**，影响成功率

本服务提供：
- ``record_run_bin(run_id, card_bin)``：注册时把 BIN 前 6 位写到 ``runs.card_bin``
- ``query_bin_health(card_bin, window_hours)``：查某 BIN 在过去 N 小时的成功/失败计数
- ``list_unhealthy_bins(window_hours, min_attempts, fail_rate_threshold)``：批量列出"该冷却"的 BIN

设计原则：
- 仅查询 + 写入，不做"自动禁用"决策（决策权交给运维或调度器，避免误伤）
- 失败静默：DB 异常降级为 None / 空列表，不打断主流程
- BIN 仅取前 6 位（IIN）：足以聚合，不暴露完整卡号
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from src.db.engine import get_session
from src.db.models import Run

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinHealthSnapshot:
    """单 BIN 在指定时间窗内的健康度快照。"""

    card_bin: str
    window_hours: int
    total_runs: int
    success_runs: int
    failed_runs: int
    pending_runs: int

    @property
    def fail_rate(self) -> float:
        """失败率（completed_runs 中的占比）。total=0 时返回 0.0。"""
        completed = self.success_runs + self.failed_runs
        return (self.failed_runs / completed) if completed > 0 else 0.0

    @property
    def is_unhealthy(self) -> bool:
        """启发式判断：>=3 次尝试且失败率 >= 60%。供调度器/运维参考。"""
        return (self.success_runs + self.failed_runs) >= 3 and self.fail_rate >= 0.6


def _normalize_bin(card_number_or_bin: str) -> str:
    """从完整卡号 / 已截取 BIN 提取前 6 位数字。失败返回空串。"""
    if not card_number_or_bin:
        return ""
    digits = "".join(ch for ch in str(card_number_or_bin) if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def record_run_bin(
    run_id: str,
    card_number_or_bin: str,
    *,
    session: Optional[Session] = None,
) -> bool:
    """
    把 BIN 前 6 位写到 ``runs.card_bin``。

    ``session`` 可注入用于测试；默认开新会话。
    返回：是否写入成功（DB 异常或 run 不存在均返回 False，主流程静默）。
    """
    bin_value = _normalize_bin(card_number_or_bin)
    if not bin_value or not run_id:
        return False
    own_session = session is None
    sess = session or get_session()
    try:
        run = sess.get(Run, run_id)
        if run is None:
            return False
        run.card_bin = bin_value
        run.updated_at = datetime.now(timezone.utc)
        sess.add(run)
        if own_session:
            sess.commit()
        else:
            sess.flush()
        return True
    except Exception as exc:
        logger.warning("record_run_bin 失败 (run_id=%s): %s", run_id, exc)
        if own_session:
            try:
                sess.rollback()
            except Exception:
                pass
        return False
    finally:
        if own_session:
            sess.close()


def query_bin_health(
    card_bin: str,
    *,
    window_hours: int = 24,
    session: Optional[Session] = None,
) -> Optional[BinHealthSnapshot]:
    """查指定 BIN 在过去 N 小时的健康度。BIN 为空或异常返回 None。"""
    bin_value = _normalize_bin(card_bin)
    if not bin_value:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))
    own_session = session is None
    sess = session or get_session()
    try:
        rows = sess.exec(
            select(Run).where(Run.card_bin == bin_value, Run.created_at >= cutoff)
        ).all()
        success = sum(1 for r in rows if r.status == "success")
        failed = sum(1 for r in rows if r.status == "failed")
        pending = sum(1 for r in rows if r.status not in ("success", "failed"))
        return BinHealthSnapshot(
            card_bin=bin_value,
            window_hours=window_hours,
            total_runs=len(rows),
            success_runs=success,
            failed_runs=failed,
            pending_runs=pending,
        )
    except Exception as exc:
        logger.warning("query_bin_health 失败 (bin=%s): %s", bin_value, exc)
        return None
    finally:
        if own_session:
            sess.close()


def list_unhealthy_bins(
    *,
    window_hours: int = 24,
    min_attempts: int = 3,
    fail_rate_threshold: float = 0.6,
    session: Optional[Session] = None,
) -> list[BinHealthSnapshot]:
    """
    批量列出过去 N 小时内"应被运维关注"的 BIN。

    判定：尝试数 >= min_attempts 且 (failed/(success+failed)) >= fail_rate_threshold
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))
    own_session = session is None
    sess = session or get_session()
    try:
        rows = sess.exec(
            select(Run).where(Run.card_bin != "", Run.created_at >= cutoff)
        ).all()
        bins: dict[str, dict[str, int]] = {}
        for r in rows:
            bucket = bins.setdefault(r.card_bin, {"success": 0, "failed": 0, "pending": 0, "total": 0})
            bucket["total"] += 1
            if r.status == "success":
                bucket["success"] += 1
            elif r.status == "failed":
                bucket["failed"] += 1
            else:
                bucket["pending"] += 1
        results: list[BinHealthSnapshot] = []
        for bin_value, counts in bins.items():
            completed = counts["success"] + counts["failed"]
            if completed < min_attempts:
                continue
            fail_rate = counts["failed"] / completed if completed > 0 else 0.0
            if fail_rate < fail_rate_threshold:
                continue
            results.append(
                BinHealthSnapshot(
                    card_bin=bin_value,
                    window_hours=window_hours,
                    total_runs=counts["total"],
                    success_runs=counts["success"],
                    failed_runs=counts["failed"],
                    pending_runs=counts["pending"],
                )
            )
        # 按失败率降序方便运维优先处理
        results.sort(key=lambda s: s.fail_rate, reverse=True)
        return results
    except Exception as exc:
        logger.warning("list_unhealthy_bins 失败: %s", exc)
        return []
    finally:
        if own_session:
            sess.close()


__all__ = [
    "BinHealthSnapshot",
    "record_run_bin",
    "query_bin_health",
    "list_unhealthy_bins",
]
