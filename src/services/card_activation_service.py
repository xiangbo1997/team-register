# -*- coding: utf-8 -*-
"""
虚拟卡激活信息持久化缓存

解决 X988 卡商 verify 接口一次性消耗的问题：
  - 第一次 verify 成功 → 缓存 CardInfo + sms_api 到 DB
  - 后续同一 cdk 调用直接命中缓存，跳过 X988
  - 卡本身过期或 max_age 到期 → 缓存失效（X988 不能重新 verify，只能换卡）
  - 运维 UI 可手动作废
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import CardActivation
from src.models import CardInfo

logger = logging.getLogger(__name__)


# 卡缓存 max_age 防御：即使卡本身 expiry_year/expiry_month 没到，
# 超过这个天数也强制失效，防御 X988 后端可能回收/重置卡片。
# 用户已确认 7 天；如确认 X988 不会"偷改"卡，可调高甚至设为 0=永不到期。
WARMUP_CARD_CACHE_MAX_AGE_DAYS = 7


# ── card_key 级 in-process lock ──────────────────────────────────────
# 解决 X988 verify 一次性消耗 + 并发竞态：两个 worker 同时 cache miss 时，
# 第二个 worker 应当等第一个完成后再读缓存（命中），而不是也调一次 fetcher
# 浪费 X988 verify 配额，更糟时还会触发 SQLite UNIQUE 冲突。
#
# 设计选择：
#   - in-process（threading.Lock），不是跨进程：当前 warmup 调度在单进程
#     uvicorn 里跑，跨进程并发场景不存在；如果未来扩多进程，需要换分布式锁
#     （Redis SETNX / DB 行级锁）。
#   - 按 card_key 粒度，不是全局锁：不同 cdk 之间不需要互斥，全局锁会拖慢吞吐。
#   - 锁 dict 永不清理：单卡量级（< 1000）下内存占用可忽略；如果 cdk 量级
#     爆炸再考虑 weakref 或 LRU。
_card_key_locks: dict[str, threading.Lock] = {}
_card_key_locks_guard = threading.Lock()


def _get_card_key_lock(card_key: str) -> threading.Lock:
    """按需创建并返回 card_key 对应的 Lock（双层锁防 dict 写入竞态）。"""
    with _card_key_locks_guard:
        lock = _card_key_locks.get(card_key)
        if lock is None:
            lock = threading.Lock()
            _card_key_locks[card_key] = lock
        return lock


# ────────────────────────────────────────────────────
# 内部工具
# ────────────────────────────────────────────────────


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite 取出来的 datetime 是 naive，比较前补 UTC 时区。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_card_still_valid(rec: CardActivation) -> bool:
    """两条独立校验都过才算 valid。

    1. 卡本身 expiry_year/expiry_month 还没到（year*12+month 整数比较）
    2. activated_at 到现在 < WARMUP_CARD_CACHE_MAX_AGE_DAYS
    """
    now = datetime.now(timezone.utc)

    # 卡本身过期
    try:
        card_yyyymm = int(rec.expiry_year) * 12 + int(rec.expiry_month)
        now_yyyymm = now.year * 12 + now.month
        if card_yyyymm < now_yyyymm:
            return False
    except (ValueError, TypeError):
        # 字段坏数据，保守失效
        return False

    # max_age 防御
    if WARMUP_CARD_CACHE_MAX_AGE_DAYS > 0:
        activated = _ensure_aware(rec.activated_at)
        if (now - activated).days >= WARMUP_CARD_CACHE_MAX_AGE_DAYS:
            return False

    return True


def _to_card_info(rec: CardActivation) -> CardInfo:
    """从 DB 记录还原 CardInfo（与 X988Card._parse_card 输出形态一致）。"""
    return CardInfo(
        card_number=rec.card_number,
        expiry_month=rec.expiry_month,
        expiry_year=rec.expiry_year,
        cvv=rec.cvv,
        name_on_card=rec.name_on_card,
        status="ACTIVE",
        billing_address=rec.billing_address,
        bin_country=rec.bin_country,
    )


def _to_meta(rec: CardActivation) -> dict[str, str]:
    """还原底层 X988Card._last_meta dict（wait_for_3ds 需要）。"""
    return {
        "key": rec.card_key,
        "sms_api": rec.sms_api,
        "phone": rec.phone,
        "activated_at": _ensure_aware(rec.activated_at).isoformat() if rec.activated_at else "",
    }


# ────────────────────────────────────────────────────
# 对外 API
# ────────────────────────────────────────────────────


def get_or_create_activation(
    card_key: str,
    card_provider: str,
    *,
    fetcher: Callable[[], tuple[Optional[CardInfo], dict[str, str]]],
) -> tuple[Optional[CardInfo], dict[str, str]]:
    """缓存 first，miss 时调 fetcher 并落库。

    Args:
        card_key: 卡密（X988 cdk）
        card_provider: 卡商标识（'x988card' / 'efuncard' / 'nodecard'）
        fetcher: 缓存 miss 时的回源函数，返回 (CardInfo or None, meta_dict)。
                 通常是 X988Card.get_card 的 closure。

    Returns:
        (CardInfo, meta) tuple；缓存命中或 fetcher 成功时 CardInfo 非空。
        失败时 CardInfo=None，meta 可能含 'reason' key 说明原因。
    """
    cdk_short = card_key[:8] if card_key else "<empty>"

    # 1. 抢 card_key 级 lock：第二个 worker 在这里等第一个完成 fetcher + commit，
    #    然后下一行 session.get 就能命中缓存 → 不会再调一次 fetcher 浪费 X988 verify。
    with _get_card_key_lock(card_key):
        # 1.1 查 DB 缓存（拿到 lock 后再查，避免拿锁前的 stale 读）
        with get_session() as session:
            existing = session.get(CardActivation, card_key)
            if existing is not None:
                if existing.is_invalidated:
                    logger.info(
                        "CardActivation 命中但已被作废 cdk=%s reason=%s",
                        cdk_short, existing.invalidate_reason or "<no reason>",
                    )
                    # is_invalidated=True 视为永久失效，不走 fetcher（因为 X988 不能重新 verify）
                    return None, {"reason": "card_invalidated"}

                if not _is_card_still_valid(existing):
                    logger.warning(
                        "CardActivation 命中但卡已过期 cdk=%s expiry=%s/%s",
                        cdk_short, existing.expiry_year, existing.expiry_month,
                    )
                    # 卡过期同样视为永久失效（X988 不能重新 verify）
                    return None, {"reason": "card_expired"}

                # 缓存命中 → 更新 last_used_at + use_count
                existing.last_used_at = datetime.now(timezone.utc)
                existing.use_count = int(existing.use_count or 0) + 1
                session.add(existing)
                session.commit()
                session.refresh(existing)
                logger.info(
                    "CardActivation 缓存命中 cdk=%s last4=%s use_count=%d",
                    cdk_short, (existing.card_number or "")[-4:], existing.use_count,
                )
                return _to_card_info(existing), _to_meta(existing)

        # 1.2 缓存 miss → 调 fetcher（底层 X988 verify）
        logger.info("CardActivation miss cdk=%s，回源 fetcher", cdk_short)
        card_info, meta = fetcher()
        if card_info is None:
            return None, dict(meta or {})

        # 1.3 落库（保留二次防御应对极少数跨进程并发场景）
        with get_session() as session:
            already = session.get(CardActivation, card_key)
            if already is not None and not already.is_invalidated:
                logger.info("CardActivation 并发竞态：另一 worker 已落库 cdk=%s", cdk_short)
                already.last_used_at = datetime.now(timezone.utc)
                already.use_count = int(already.use_count or 0) + 1
                session.add(already)
                session.commit()
                session.refresh(already)
                return _to_card_info(already), _to_meta(already)

            record = CardActivation(
                card_key=card_key,
                card_provider=str(card_provider or "x988card").strip().lower() or "x988card",
                card_number=card_info.card_number,
                expiry_month=card_info.expiry_month,
                expiry_year=card_info.expiry_year,
                cvv=card_info.cvv,
                name_on_card=card_info.name_on_card or "",
                billing_address=card_info.billing_address or "",
                bin_country=card_info.bin_country or "",
                sms_api=str(meta.get("sms_api", "") or ""),
                phone=str(meta.get("phone", "") or ""),
            )
            session.add(record)
            session.commit()
            logger.info(
                "CardActivation 已缓存 cdk=%s last4=%s sms_api=%s",
                cdk_short, card_info.card_number[-4:],
                "yes" if record.sms_api else "no",
            )
        return card_info, dict(meta or {})


def get_sms_api(card_key: str) -> str:
    """供 wait_for_3ds 防御性使用：直接拿 sms_api，不触发 fetcher。

    返回空串表示：缓存不存在 / 已作废 / 卡已过期。
    """
    with get_session() as session:
        rec = session.get(CardActivation, card_key)
        if rec is None or rec.is_invalidated or not _is_card_still_valid(rec):
            return ""
        return rec.sms_api or ""


def invalidate(card_key: str, reason: str = "manual") -> bool:
    """手动作废一张缓存卡（运维 UI 用）。

    返回 True 表示找到并作废了；False 表示卡密不存在。
    """
    with get_session() as session:
        rec = session.get(CardActivation, card_key)
        if rec is None:
            return False
        rec.is_invalidated = True
        rec.invalidate_reason = (str(reason or "")[:200]) or "manual"
        session.add(rec)
        session.commit()
        logger.info("CardActivation 已作废 cdk=%s reason=%s", card_key[:8], rec.invalidate_reason)
        return True


def record_lookup(
    card_key: str,
    card_provider: str,
    card_info: CardInfo,
    *,
    sms_api: str = "",
    phone: str = "",
) -> CardActivation:
    """记录一次 get_card 调用结果到 card_activations 表。

    与 ``get_or_create_activation`` 的语义差异：
      - get_or_create：缓存优先，只 X988 等"一次性 verify"卡商用
      - record_lookup：纯审计写入，efuncard / nodecard 这种"可重复查询"的卡商用，
        让 /cards 页能看到"哪些卡密被用过了"。每次调用 use_count + 1。

    重复调用同一 cdk 不会插重复记录 —— 走 upsert 语义（已存在则刷新 + use_count++）。
    """
    cdk_short = card_key[:8] if card_key else "<empty>"
    now = datetime.now(timezone.utc)
    with get_session() as session:
        existing = session.get(CardActivation, card_key)
        if existing is not None:
            # 同 cdk 重复调：只 use_count + last_used_at 累加，不覆盖历史字段
            existing.use_count = int(existing.use_count or 0) + 1
            existing.last_used_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            session.expunge(existing)
            logger.info(
                "CardActivation 重复使用 cdk=%s last4=%s use_count=%d provider=%s",
                cdk_short, (existing.card_number or "")[-4:], existing.use_count, existing.card_provider,
            )
            return existing

        record = CardActivation(
            card_key=card_key,
            card_provider=str(card_provider or "").strip().lower() or "unknown",
            card_number=card_info.card_number,
            expiry_month=card_info.expiry_month,
            expiry_year=card_info.expiry_year,
            cvv=card_info.cvv,
            name_on_card=card_info.name_on_card or "",
            billing_address=card_info.billing_address or "",
            bin_country=card_info.bin_country or "",
            sms_api=str(sms_api or ""),
            phone=str(phone or ""),
            last_used_at=now,
            use_count=1,  # 第一次记录就算一次使用
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        session.expunge(record)
        logger.info(
            "CardActivation 已记录（audit）cdk=%s last4=%s provider=%s",
            cdk_short, card_info.card_number[-4:], card_provider,
        )
        return record


def list_activations(
    *,
    include_invalidated: bool = False,
    card_provider: Optional[str] = None,
) -> list[CardActivation]:
    """列表查询（API 用）。按 last_used_at desc 排序，未用过的 NULL 排末尾。

    Args:
        include_invalidated: 是否含已作废
        card_provider: 按卡商过滤（'x988card' / 'efuncard' / 'nodecard'），None=全部
    """
    with get_session() as session:
        stmt = select(CardActivation)
        if not include_invalidated:
            stmt = stmt.where(CardActivation.is_invalidated == False)  # noqa: E712
        if card_provider:
            stmt = stmt.where(CardActivation.card_provider == str(card_provider).strip().lower())
        records = list(session.exec(stmt).all())
        # Python 排序：last_used_at desc，NULL 排末尾
        records.sort(
            key=lambda r: (r.last_used_at is None, -(_ensure_aware(r.last_used_at).timestamp() if r.last_used_at else 0)),
        )
        # expunge 让调用方安全跨 session 读取
        for r in records:
            session.expunge(r)
        return records


def get_activation(card_key: str) -> Optional[CardActivation]:
    """查单条，详情页用。"""
    with get_session() as session:
        rec = session.get(CardActivation, card_key)
        if rec is None:
            return None
        session.expunge(rec)
        return rec
