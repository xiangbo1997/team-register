# -*- coding: utf-8 -*-
"""促销码扫描结果 ledger 服务（跨任务剪枝持久化）

code_discovery_service 每扫一条候选码就调 record_scan() 落库；下次发现任务启动时
调 load_dead_codes() 读新鲜期内的 NOT_FOUND 码，喂 promo_scoring.ScanHistory.dead_codes
做沉底/剪枝，避免每轮全量重扫几千个已知死码、白烧 token + Cloudflare 预算。

设计（与 [[sms_activation_service]] / [[card_activation_service]] 同口径）：
  - (country, code) 业务唯一；record_scan upsert（存在则更新 status+scanned_at）。
  - 不用 LinkTemplate 存死码（会污染模板下拉）；本表是纯审计/剪枝 ledger。
  - country+code 级 in-process 锁防同一码并发 upsert 出两行（单进程 uvicorn）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import ScannedCode

logger = logging.getLogger(__name__)


# ── country+code 级 in-process 锁（防并发 upsert 双写）────────────────
_upsert_locks: dict[str, threading.Lock] = {}
_upsert_locks_guard = threading.Lock()


def _get_upsert_lock(country: str, code: str) -> threading.Lock:
    key = f"{country.lower()}::{code.lower()}"
    with _upsert_locks_guard:
        lock = _upsert_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _upsert_locks[key] = lock
        return lock


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def record_scan(country: str, code: str, status: str) -> None:
    """落库/更新一条扫描结果（(country, code) 存在则更新 status + scanned_at）。

    Args:
        country: ISO 国家码（大写或任意大小写，内部统一小写存）
        code: 候选码
        status: 终态（not_found / exists / eligible / error 等 EligibilityStatus 值）

    失败仅记 warning 不抛——ledger 是优化项，不能阻断主扫描流程。
    """
    cc = (country or "").strip().lower()
    low = (code or "").strip().lower()
    st = (status or "").strip().lower()
    if not cc or not low:
        return

    lock = _get_upsert_lock(cc, low)
    try:
        with lock, get_session() as session:
            existing = session.exec(
                select(ScannedCode).where(
                    ScannedCode.country == cc,
                    ScannedCode.code == low,
                )
            ).first()
            now = _utc_now()
            if existing is None:
                session.add(
                    ScannedCode(country=cc, code=low, status=st, scanned_at=now)
                )
            else:
                existing.status = st
                existing.scanned_at = now
                session.add(existing)
            session.commit()
    except Exception as exc:  # noqa: BLE001 — ledger 失败不阻断扫描
        logger.warning("record_scan 失败 country=%s code=%s err=%s", cc, low, exc)


def load_dead_codes(country: str, *, fresh_within_days: int = 30) -> frozenset[str]:
    """读新鲜期内 status='not_found' 的码集合（小写），喂 ScanHistory.dead_codes。

    Args:
        country: ISO 国家码
        fresh_within_days: 新鲜窗口；超过这个天数的死码不再视为"死"
                           （OpenAI 可能重新上架旧码，过期死码值得重扫）

    Returns:
        小写 code 的 frozenset；异常时返回空集（降级为不剪枝，安全）。
    """
    cc = (country or "").strip().lower()
    if not cc:
        return frozenset()
    cutoff = _utc_now() - timedelta(days=max(0, int(fresh_within_days or 0)))
    try:
        with get_session() as session:
            rows = session.exec(
                select(ScannedCode.code).where(
                    ScannedCode.country == cc,
                    ScannedCode.status == "not_found",
                    ScannedCode.scanned_at >= cutoff,
                )
            ).all()
        return frozenset(str(c).lower() for c in rows if c)
    except Exception as exc:  # noqa: BLE001 — 读失败降级为空集（不剪枝）
        logger.warning("load_dead_codes 失败 country=%s err=%s", cc, exc)
        return frozenset()


__all__ = ["record_scan", "load_dead_codes"]
