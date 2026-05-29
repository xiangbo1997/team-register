# -*- coding: utf-8 -*-
"""
卡池服务 — 手动入池 + 手动触发预热 + 任务页选熟卡

设计立场：
  - 卡池 = `card_activations` 表的子集 + 卡池元信息字段
  - 每张卡有"成熟度" `warmup_count / target_warmup_count`，由运维手动按按钮触发
  - 任务页"从卡池选"时，只展示 `warmup_count >= target_warmup_count` 的成熟卡
  - 旧的"运行时"路径（`card_activation_service.get_or_create_activation`）仍然走，
    那是 X988 verify 一次性消耗的兜底缓存，与本服务不冲突

注意事项：
  - 后台预热用 `threading.Thread(daemon=True)` 异步跑，前端轮询状态
  - 任何后台异常都要 catch + 写到 `last_warmup_status='failed'`，前端可见
  - 重复点 "热这张卡" → 检查 `last_warmup_status == 'running'` 拒绝重入
  - 复用 `src/orchestration/warmup.py:execute_card_warmup`，**不重写**浏览器逻辑
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.db.engine import get_session
from src.db.models import CardActivation
from src.models import CardInfo

logger = logging.getLogger(__name__)


# ── 状态常量（与 DB 字段值对齐） ────────────────────────────
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

_VALID_PROVIDERS = ("efuncard", "nodecard", "x988card")


# ── 辅助：CardActivation → CardInfo（execute_card_warmup 需要 CardInfo 入参） ──
def card_activation_to_info(rec: CardActivation) -> CardInfo:
    """从 DB 行重建 CardInfo（只填 execute_card_warmup 实际用到的字段）。"""
    return CardInfo(
        card_number=rec.card_number,
        expiry_month=rec.expiry_month,
        expiry_year=rec.expiry_year,
        cvv=rec.cvv,
        last_four=(rec.card_number or "")[-4:],
        name_on_card=rec.name_on_card or "",
        status="active",
        created_at=(rec.activated_at.isoformat() if rec.activated_at else ""),
        billing_address=rec.billing_address or "",
        bin_country=rec.bin_country or "",
    )


# ── 入池：调卡商 verify 拿卡 + 落库 ─────────────────────────
def add_card(
    *,
    card_key: str,
    card_provider: str,
    target_warmup_count: int,
    card_api: Any,
) -> tuple[Optional[CardActivation], dict[str, str]]:
    """手动入池：调卡商 API 拿卡信息 + 创建 CardActivation。

    Args:
        card_key: 用户粘贴的 CDK
        card_provider: efuncard / nodecard / x988card
        target_warmup_count: 目标预热次数（>= 0）
        card_api: 卡商客户端实例（注入，便于测试 mock）

    Returns:
        (CardActivation, {})  — 成功
        (None, {"reason": ...}) — 重复 / 卡商失败 / 校验失败
    """
    if not card_key or not card_key.strip():
        return None, {"reason": "empty_card_key"}
    cdk = card_key.strip()
    provider = (card_provider or "").strip().lower()
    if provider not in _VALID_PROVIDERS:
        return None, {"reason": "invalid_provider", "detail": f"必须是 {_VALID_PROVIDERS} 之一"}
    if target_warmup_count < 0:
        return None, {"reason": "invalid_target", "detail": "target_warmup_count 不能为负"}

    # 1. 防重复
    with get_session() as session:
        existing = session.get(CardActivation, cdk)
        if existing is not None:
            return None, {"reason": "duplicate", "detail": f"卡密已在池中（status={existing.last_warmup_status}）"}

    # 2. 调卡商拿卡（这步可能耗时数秒，在请求线程里直接跑）
    try:
        card_info: Optional[CardInfo] = card_api.get_card(cdk)
        lookup_meta = dict(getattr(card_api, "_last_meta", {}) or {})
    except Exception as exc:
        logger.warning("入池调卡商失败 cdk=%s: %s", cdk[:8], exc)
        return None, {"reason": "card_api_error", "detail": str(exc)[:200]}

    if card_info is None:
        return None, {"reason": "card_not_found", "detail": "卡商返回空"}

    # 3. 落库（pending 状态）
    now = datetime.now(timezone.utc)
    with get_session() as session:
        # 二次防御：可能并发期间另一个请求已落库
        already = session.get(CardActivation, cdk)
        if already is not None:
            return None, {"reason": "duplicate_race", "detail": "并发竞态：已被另一请求落库"}

        # X988 底层 client 在 get_card() 后把 3DS 所需 sms_api/phone 放入 _last_meta；
        # 卡池入库也要保存，后续热卡 wait_for_3ds 才能复用。
        sms_api = str(lookup_meta.get("sms_api", "") or "") if provider == "x988card" else ""
        phone = str(lookup_meta.get("phone", "") or "") if provider == "x988card" else ""
        record = CardActivation(
            card_key=cdk,
            card_provider=provider,
            card_number=card_info.card_number,
            expiry_month=card_info.expiry_month,
            expiry_year=card_info.expiry_year,
            cvv=card_info.cvv,
            name_on_card=card_info.name_on_card or "",
            billing_address=card_info.billing_address or "",
            bin_country=card_info.bin_country or "",
            sms_api=sms_api,
            phone=phone,
            activated_at=now,
            target_warmup_count=int(target_warmup_count),
            warmup_count=0,
            last_warmup_status=STATUS_PENDING,
        )
        try:
            session.add(record)
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("卡池入池并发唯一约束命中 cdk=%s", cdk[:8])
            return None, {"reason": "duplicate_race", "detail": "卡密已在池中"}
        session.refresh(record)
        # expunge 让调用方拿到独立对象
        session.expunge(record)
        logger.info(
            "卡池入池成功 cdk=%s provider=%s last4=%s target=%d",
            cdk[:8], provider, card_info.last_four, target_warmup_count,
        )
        return record, {}


# ── 列出池中所有卡（带过滤） ────────────────────────────────
def list_pool(
    *,
    include_invalidated: bool = False,
    only_ready: bool = False,
    card_provider: Optional[str] = None,
) -> list[CardActivation]:
    """列出池中卡。

    Args:
        include_invalidated: 是否包含已作废
        only_ready: 仅列出 warmup_count >= target_warmup_count 的成熟卡
        card_provider: 按卡商过滤
    """
    with get_session() as session:
        stmt = select(CardActivation)
        if not include_invalidated:
            stmt = stmt.where(CardActivation.is_invalidated == False)  # noqa: E712
        if card_provider:
            stmt = stmt.where(CardActivation.card_provider == card_provider.strip().lower())
        rows = list(session.exec(stmt).all())
        if only_ready:
            rows = [r for r in rows if r.warmup_count >= r.target_warmup_count]
        # 排序：成熟度高 + 使用次数少优先（最适合下次绑卡）
        rows.sort(
            key=lambda r: (
                -(r.warmup_count - r.target_warmup_count),  # 超额最多在前
                r.use_count or 0,                           # 用过最少在前
                -(r.warmed_at.timestamp() if r.warmed_at else 0),  # 最近热过的在前
            )
        )
        # 把 row 拷出 session 域
        for r in rows:
            session.expunge(r)
        return rows


# ── 任务页"从卡池选"专用：返回最佳熟卡 ───────────────────
def select_for_task() -> Optional[CardActivation]:
    """挑选下一个最适合用于注册任务的成熟卡。

    Returns:
        CardActivation（已 expunge）或 None（池空 / 无成熟卡）
    """
    rows = list_pool(only_ready=True)
    return rows[0] if rows else None


# ── 查热卡进度 ─────────────────────────────────────────────
def get_warmup_status(card_key: str) -> Optional[dict[str, Any]]:
    """查指定卡的预热状态。"""
    with get_session() as session:
        rec = session.get(CardActivation, card_key)
        if rec is None:
            return None
        return {
            "card_key": rec.card_key,
            "warmup_count": int(rec.warmup_count or 0),
            "target_warmup_count": int(rec.target_warmup_count or 0),
            "last_warmup_status": rec.last_warmup_status or STATUS_PENDING,
            "last_warmup_reason": rec.last_warmup_reason or "",
            "warmed_at": rec.warmed_at.isoformat() if rec.warmed_at else "",
            "is_ready": (int(rec.warmup_count or 0) >= int(rec.target_warmup_count or 0)),
            "is_invalidated": bool(rec.is_invalidated),
        }


# ── 触发热卡（异步） ─────────────────────────────────────────
class _AlreadyRunningError(Exception):
    """已经有热卡线程在跑，拒绝重入。"""


def trigger_warmup(
    card_key: str,
    *,
    config: Any,
    card_api: Any,
    svc_for_warmup_pool: Any,
    proxy_url: str = "",
) -> dict[str, Any]:
    """异步触发一次预热。

    Args:
        card_key: 要热的卡 key
        config: AppConfig 实例
        card_api: 卡商客户端（execute_card_warmup 内部用）
        svc_for_warmup_pool: ConfigService 实例（select_warmup_account 用）
        proxy_url: 代理 URL（可空）

    Returns:
        {"status": "running", "warmup_count": ..., "target": ...}
        重入时抛 _AlreadyRunningError
    """
    with get_session() as session:
        rec = session.get(CardActivation, card_key)
        if rec is None:
            raise ValueError(f"card_key not in pool: {card_key[:8]}")
        if rec.is_invalidated:
            raise ValueError(f"card invalidated, cannot warmup: {card_key[:8]}")
        if rec.last_warmup_status == STATUS_RUNNING:
            raise _AlreadyRunningError(f"warmup already running for {card_key[:8]}")

        # 把状态置为 running，立即 commit（防并发重入）
        rec.last_warmup_status = STATUS_RUNNING
        rec.last_warmup_reason = ""
        session.add(rec)
        session.commit()
        session.refresh(rec)
        snapshot = {
            "card_key": rec.card_key,
            "warmup_count": int(rec.warmup_count or 0),
            "target_warmup_count": int(rec.target_warmup_count or 0),
        }
        # expunge 后启线程
        session.expunge(rec)
        rec_for_thread = rec

    # 起线程
    thread = threading.Thread(
        target=_do_warmup_one_round,
        args=(rec_for_thread, config, card_api, svc_for_warmup_pool, proxy_url),
        daemon=True,
        name=f"card-warmup-{card_key[:8]}",
    )
    thread.start()

    return {"status": STATUS_RUNNING, **snapshot}


def _do_warmup_one_round(
    rec_snapshot: CardActivation,
    config: Any,
    card_api: Any,
    svc_for_warmup_pool: Any,
    proxy_url: str,
) -> None:
    """后台线程：跑一轮 execute_card_warmup + 写结果。

    任何异常都被 catch + 写到 last_warmup_status='failed'，前端可见。
    """
    card_key = rec_snapshot.card_key
    success = False
    reason = ""

    try:
        # lazy import 避免循环依赖（execute_card_warmup 在 orchestration 层）
        from src.orchestration.warmup import execute_card_warmup

        card_info = card_activation_to_info(rec_snapshot)
        success = execute_card_warmup(
            config=config,
            card_info=card_info,
            card_api=card_api,
            card_key=card_key,
            svc=svc_for_warmup_pool,
            proxy_url=proxy_url,
        )
        if success:
            reason = "成功"
        else:
            detail = getattr(svc_for_warmup_pool, "last_card_warmup_reason", "")
            if not isinstance(detail, str):
                detail = ""
            reason = detail or "execute_card_warmup 返回 False（看 worker 日志）"
    except Exception as exc:
        logger.exception("热卡线程异常 cdk=%s", card_key[:8])
        success = False
        reason = f"{type(exc).__name__}: {str(exc)[:180]}"

    # 写结果回 DB
    now = datetime.now(timezone.utc)
    with get_session() as session:
        rec = session.get(CardActivation, card_key)
        if rec is None:
            logger.warning("热卡完成时 cdk=%s 已不在 DB（被 invalidate？）", card_key[:8])
            return
        rec.last_warmup_status = STATUS_SUCCESS if success else STATUS_FAILED
        rec.last_warmup_reason = reason[:200]
        if success:
            rec.warmup_count = int(rec.warmup_count or 0) + 1
            rec.warmed_at = now
        session.add(rec)
        session.commit()
        logger.info(
            "热卡完成 cdk=%s status=%s warmup_count=%d/%d",
            card_key[:8], rec.last_warmup_status, rec.warmup_count, rec.target_warmup_count,
        )


__all__ = [
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_FAILED",
    "card_activation_to_info",
    "add_card",
    "list_pool",
    "select_for_task",
    "get_warmup_status",
    "trigger_warmup",
]
