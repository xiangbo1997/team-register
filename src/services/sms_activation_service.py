# -*- coding: utf-8 -*-
"""
SMS 号码复用持久化调度

借鉴成熟项目 GuJumpgate（phone-sms/providers/hero-sms.js 的 maxUses=3 + reactivate）：
一个接码号最多收 N 次 OTP，多个 ChatGPT 账号可复用同一号码收码，省接码费。

设计立场（与 [[card_activation_service]] 对照）：
- 卡缓存是「同 cdk 命中同卡」（一对一，X988 verify 一次性消耗驱动）；
- 号码复用是「同 provider+country 找任意未用满的活号」（一对多，省钱驱动）。

并发模型：
- provider+country 级 in-process 锁（单进程 uvicorn，跨进程场景不存在）；
- 认领即原子 +use_count，避免两个 worker 用同一号导致 OTP 串扰。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import SmsActivation

logger = logging.getLogger(__name__)


# ── provider+country 级 in-process 锁 ────────────────────────────────
# 防止两个 worker 同时认领同一个活号（会导致两个账号收到对方的 OTP）。
_claim_locks: dict[str, threading.Lock] = {}
_claim_locks_guard = threading.Lock()


def _get_claim_lock(provider_name: str, country: str) -> threading.Lock:
    """按 provider+country 粒度返回锁（双层锁防 dict 写入竞态）。"""
    key = f"{provider_name}::{country}"
    with _claim_locks_guard:
        lock = _claim_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _claim_locks[key] = lock
        return lock


def _norm(value: str, default: str = "") -> str:
    return str(value or "").strip() or default


def record_allocation(
    order_id: str,
    *,
    provider_name: str,
    country: str,
    phone_number: str,
    service: str = "dr",
    max_uses: int = 3,
) -> Optional[SmsActivation]:
    """申号成功后落库一条复用记录（use_count=1，本次申号已用 1 次）。

    Args:
        order_id: 接码平台订单号（主键，跨 Run 唯一）
        provider_name: 接码 provider（hero_sms / sms_activate / ...）
        country: 国家代码
        phone_number: 申到的手机号
        service: 服务代码（dr=OpenAI）
        max_uses: 复用上限（GuJumpgate 默认 3）

    Returns:
        落库的 SmsActivation；order_id 已存在或异常时返回 None（不阻塞主流程）。
    """
    oid = _norm(order_id)
    if not oid:
        return None
    try:
        normalized_max = max(1, int(max_uses) if max_uses else 3)
    except (TypeError, ValueError):
        normalized_max = 3

    try:
        with get_session() as session:
            if session.get(SmsActivation, oid) is not None:
                # 已存在（理论上不应发生，order_id 唯一）：不覆盖
                return None
            record = SmsActivation(
                order_id=oid,
                provider_name=_norm(provider_name, "hero_sms").lower(),
                country=_norm(country),
                phone_number=_norm(phone_number),
                service=_norm(service, "dr"),
                use_count=1,
                max_uses=normalized_max,
                last_used_at=datetime.now(timezone.utc),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            logger.info(
                "SmsActivation 落库 order=%s provider=%s country=%s max_uses=%d",
                oid, record.provider_name, record.country, record.max_uses,
            )
            return record
    except Exception as exc:  # noqa: BLE001 — 复用是增强，落库失败不阻塞申号
        logger.warning("SmsActivation 落库失败 order=%s: %s", oid, exc)
        return None


def try_reuse_active_number(
    provider_name: str,
    country: str,
) -> Optional[SmsActivation]:
    """认领一个「同 provider + 同 country 且未用满」的活号供复用。

    认领是原子的：拿到锁后查到候选立即 +use_count + 更新 last_used_at 再返回，
    避免两个 worker 拿到同一号。无可复用号时返回 None（调用方走新申号）。

    Returns:
        已认领（use_count 已自增）的 SmsActivation；无可用号时 None。
    """
    prov = _norm(provider_name, "hero_sms").lower()
    ctry = _norm(country)
    if not prov:
        return None

    with _get_claim_lock(prov, ctry):
        try:
            with get_session() as session:
                stmt = (
                    select(SmsActivation)
                    .where(SmsActivation.provider_name == prov)
                    .where(SmsActivation.country == ctry)
                    .where(SmsActivation.is_invalidated == False)  # noqa: E712
                    .where(SmsActivation.use_count < SmsActivation.max_uses)
                    .order_by(SmsActivation.activated_at)  # 最早的先用完再换
                )
                rec = session.exec(stmt).first()
                if rec is None:
                    return None
                rec.use_count = int(rec.use_count or 0) + 1
                rec.last_used_at = datetime.now(timezone.utc)
                session.add(rec)
                session.commit()
                session.refresh(rec)
                logger.info(
                    "SmsActivation 复用命中 order=%s phone=%s use_count=%d/%d",
                    rec.order_id, rec.phone_number, rec.use_count, rec.max_uses,
                )
                return rec
        except Exception as exc:  # noqa: BLE001 — 复用查询失败回退新申号
            logger.warning(
                "SmsActivation 复用查询失败 provider=%s country=%s: %s",
                prov, ctry, exc,
            )
            return None


def invalidate(order_id: str, reason: str = "") -> bool:
    """作废一个活号（号被平台取消 / 收码失败等），不再复用。"""
    oid = _norm(order_id)
    if not oid:
        return False
    try:
        with get_session() as session:
            rec = session.get(SmsActivation, oid)
            if rec is None:
                return False
            rec.is_invalidated = True
            rec.invalidate_reason = _norm(reason)[:200] or "manual"
            session.add(rec)
            session.commit()
            logger.info("SmsActivation 作废 order=%s reason=%s", oid, rec.invalidate_reason)
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("SmsActivation 作废失败 order=%s: %s", oid, exc)
        return False


def is_phone_blacklisted(phone_number: str) -> bool:
    """按手机号查是否曾被作废（OpenAI 拒过 / 收不到码）。

    接码平台号循环回收，同一号可能在不同 order 下重复申到。一旦某号被 OpenAI
    拒绝（用过/被标记），后续再申到它也是收不到码，直接拉黑跳过省时间省钱。

    Returns:
        True 表示该号有 is_invalidated 记录 → 申号阶段应跳过、立即换号。
    """
    phone = _norm(phone_number)
    if not phone:
        return False
    try:
        with get_session() as session:
            stmt = (
                select(SmsActivation)
                .where(SmsActivation.phone_number == phone)
                .where(SmsActivation.is_invalidated == True)  # noqa: E712
            )
            return session.exec(stmt).first() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("SmsActivation 黑名单查询失败 phone=%s: %s", phone, exc)
        return False


def blacklist_phone(phone_number: str, provider_name: str, country: str, reason: str = "") -> None:
    """把一个手机号加入黑名单（即使没有对应 order 记录也落一条 invalidated）。

    用于：申到一个号但还没正式 record_allocation 就发现它被拒/收不到码时，
    用一条合成记录（order_id=blacklist:<phone>）把该号永久拉黑。
    """
    phone = _norm(phone_number)
    if not phone:
        return
    synthetic_oid = f"blacklist:{phone}"
    try:
        with get_session() as session:
            if session.get(SmsActivation, synthetic_oid) is not None:
                return
            rec = SmsActivation(
                order_id=synthetic_oid,
                provider_name=_norm(provider_name, "hero_sms").lower(),
                country=_norm(country),
                phone_number=phone,
                use_count=0,
                max_uses=0,
                is_invalidated=True,
                invalidate_reason=_norm(reason)[:200] or "blacklisted",
            )
            session.add(rec)
            session.commit()
            logger.info("SmsActivation 拉黑手机号 phone=%s reason=%s", phone, rec.invalidate_reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SmsActivation 拉黑手机号失败 phone=%s: %s", phone, exc)
