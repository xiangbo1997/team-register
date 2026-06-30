# -*- coding: utf-8 -*-
"""
虚拟卡缓存管理 API + 卡池管理

GET    /api/cards                     — 列表（默认隐藏 invalidated；?include_invalidated=true 全量）
GET    /api/cards/{card_key}          — 详情
POST   /api/cards/{card_key}/invalidate — 手动作废
POST   /api/cards                     — 入池（卡池新增功能）
POST   /api/cards/{card_key}/warmup   — 触发一轮热卡（异步）
GET    /api/cards/{card_key}/warmup-status — 查热卡状态
GET    /api/cards/ready               — 列出成熟卡（任务页下拉用）

所有响应严格脱敏：
  - card_number 只返回 last4 + bin（前 6 位）
  - cvv / sms_api 完全不返回
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.security import require_csrf, require_role
from src.config import load_config
from src.db.engine import get_session
from src.db.models import CardActivation, SyntheticCardAudit, User
from src.services.card_activation_service import (
    WARMUP_CARD_CACHE_MAX_AGE_DAYS,
    _ensure_aware,
    _is_card_still_valid,
    get_activation,
    invalidate as svc_invalidate,
    list_activations,
)
from src.services.card_pool_service import (
    _AlreadyRunningError,
    add_card,
    get_warmup_status,
    list_pool,
    trigger_warmup,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cards", tags=["cards"])


class InvalidateRequest(BaseModel):
    reason: str = "manual"


class AddCardRequest(BaseModel):
    card_key: str = Field(..., min_length=1, max_length=64)
    card_provider: str = Field(..., description="efuncard / nodecard / x988card")
    target_warmup_count: int = Field(default=0, ge=0, le=20)


def _build_card_api(card_provider: str):
    """根据 card_provider 名构造对应卡商客户端实例。

    用 lazy import 避免顶层循环依赖（card_pool_service → cards.py → card_pool_service）。
    """
    config = load_config()
    provider = (card_provider or "").strip().lower()
    if provider == "efuncard":
        from src.efuncard import EfunCard
        return EfunCard(token=config.efuncard_token)
    if provider == "nodecard":
        from src.nodecard import NodeCard
        return NodeCard(
            base_url=config.nodecard_api_url,
            merchant_dict_id=config.nodecard_merchant_id or None,
            platform_id=config.nodecard_platform_id or None,
        )
    if provider == "x988card":
        from src.x988card import X988Card
        return X988Card(
            base_url=config.x988card_api_base,
            request_timeout=config.x988card_request_timeout,
        )
    raise HTTPException(status_code=400, detail=f"未知 card_provider: {card_provider}")


_LONG_DIGIT_RE = re.compile(r"\b\d{12,19}\b")
_SENSITIVE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SENSITIVE_WORD_RE = re.compile(r"\b(token|proxy|sms_api|cvv|authorization|bearer)\b", re.IGNORECASE)


def _safe_error_detail(detail: object, fallback: str) -> str:
    """对外错误信息只保留业务可读文本，避免泄漏卡号/CVV/sms_api/token/proxy。"""
    text = str(detail or "").strip()
    if not text:
        return fallback
    text = _LONG_DIGIT_RE.sub("[REDACTED_CARD]", text)
    text = _SENSITIVE_URL_RE.sub("[REDACTED_URL]", text)
    if _SENSITIVE_WORD_RE.search(text) or "Traceback" in text or len(text) > 180:
        return fallback
    return text


def _redact_pool(rec: CardActivation) -> dict[str, Any]:
    """卡池视角的脱敏序列化（含 warmup 进度字段）。"""
    raw = rec.card_number or ""
    last4 = raw[-4:] if len(raw) >= 4 else ""
    bin_prefix = raw[:6] if len(raw) >= 6 else ""
    activated_aware = _ensure_aware(rec.activated_at) if rec.activated_at else None
    warmed_aware = _ensure_aware(rec.warmed_at) if rec.warmed_at else None
    return {
        "card_key": rec.card_key,
        "card_provider": rec.card_provider,
        "last4": last4,
        "bin_prefix": bin_prefix,
        "bin_country": rec.bin_country,
        "expiry_month": rec.expiry_month,
        "expiry_year": rec.expiry_year,
        "name_on_card": rec.name_on_card,
        "use_count": int(rec.use_count or 0),
        "is_invalidated": bool(rec.is_invalidated),
        "warmup_count": int(rec.warmup_count or 0),
        "target_warmup_count": int(rec.target_warmup_count or 0),
        "is_ready": int(rec.warmup_count or 0) >= int(rec.target_warmup_count or 0),
        "last_warmup_status": rec.last_warmup_status or "pending",
        "last_warmup_reason": rec.last_warmup_reason or "",
        "warmed_at": warmed_aware.isoformat() if warmed_aware else "",
        "activated_at": activated_aware.isoformat() if activated_aware else "",
    }


def _redact(rec: CardActivation) -> dict[str, Any]:
    """脱敏序列化：卡号只露 last4 + 前 6 位 BIN，CVV 和 sms_api 完全不露。"""
    raw = rec.card_number or ""
    last4 = raw[-4:] if len(raw) >= 4 else ""
    bin_prefix = raw[:6] if len(raw) >= 6 else ""

    # 卡片自身是否过期 + max_age 是否触发
    valid = _is_card_still_valid(rec)
    now = datetime.now(timezone.utc)
    activated_aware = _ensure_aware(rec.activated_at) if rec.activated_at else None
    age_days = (now - activated_aware).days if activated_aware else 0
    days_until_max_age = max(0, WARMUP_CARD_CACHE_MAX_AGE_DAYS - age_days) if WARMUP_CARD_CACHE_MAX_AGE_DAYS > 0 else None

    if rec.is_invalidated:
        status = "invalidated"
    elif not valid:
        status = "expired"
    else:
        status = "active"

    return {
        "card_key": rec.card_key,
        "card_provider": rec.card_provider,
        "last4": last4,
        "bin_prefix": bin_prefix,
        "expiry_month": rec.expiry_month,
        "expiry_year": rec.expiry_year,
        "name_on_card": rec.name_on_card,
        "billing_address": rec.billing_address,
        "bin_country": rec.bin_country,
        "phone": (rec.phone or "")[-4:] if rec.phone else "",  # 只露后 4 位
        "activated_at": activated_aware.isoformat() if activated_aware else "",
        "last_used_at": _ensure_aware(rec.last_used_at).isoformat() if rec.last_used_at else "",
        "use_count": int(rec.use_count or 0),
        "is_invalidated": bool(rec.is_invalidated),
        "invalidate_reason": rec.invalidate_reason or "",
        "status": status,
        "days_until_max_age": days_until_max_age,
    }


@router.get("")
def list_cards(
    include_invalidated: bool = Query(False),
    card_provider: Optional[str] = Query(None, description="按卡商过滤: x988card / efuncard / nodecard"),
    user: User = Depends(require_role("admin")),
):
    """列表查询（admin 角色）。可按 card_provider 过滤。"""
    records = list_activations(
        include_invalidated=include_invalidated,
        card_provider=card_provider,
    )
    return [_redact(r) for r in records]


# ── 卡池管理端点 ────────────────────────────────────────────


@router.get("/ready")
def list_ready_cards(
    user: User = Depends(require_role("admin")),
):
    """列出可用于任务的成熟卡（warmup_count >= target_warmup_count）。

    任务创建页"从卡池选"下拉用此端点。
    """
    rows = list_pool(only_ready=True)
    return [_redact_pool(r) for r in rows]


@router.get("/pool")
def list_pool_cards(
    include_invalidated: bool = Query(False),
    only_ready: bool = Query(False),
    card_provider: Optional[str] = Query(None),
    user: User = Depends(require_role("admin")),
):
    """卡池视角列表（含 warmup 进度字段）。"""
    rows = list_pool(
        include_invalidated=include_invalidated,
        only_ready=only_ready,
        card_provider=card_provider,
    )
    return [_redact_pool(r) for r in rows]


class SyntheticVisaRequest(BaseModel):
    """合成卡生成请求（多国 PayPal 友好 BIN，绕过 RESTRICTED_USER）。"""
    country: str = Field(
        default="US",
        description="发卡国 ISO alpha-2：US / GB / CA / SG / HK；决定 BIN 集与地址池",
    )
    bin_prefix: Optional[str] = Field(
        default=None,
        description="可选指定 BIN：必须属于所选 country 的合法 BIN 集；空则按 country 随机",
    )
    seed: Optional[str] = Field(
        default=None,
        description="可选确定性 seed：同 seed 永远生成同一张卡（用于审计追踪 / 同账号重试）",
    )
    # A3：可选注入持卡人姓名（对齐 OpenAI 账户姓名时通过率更高）
    first_name: Optional[str] = Field(
        default=None,
        min_length=1, max_length=60,
        description="可选：覆写持卡人 First name（用于对齐 OpenAI 账户姓名）",
    )
    last_name: Optional[str] = Field(
        default=None,
        min_length=1, max_length=60,
        description="可选：覆写持卡人 Last name（用于对齐 OpenAI 账户姓名）",
    )


class SyntheticVisaFeedbackRequest(BaseModel):
    """合成卡绑卡反馈：A2 数据闭环用。"""
    status: str = Field(..., description="success / declined")
    decline_code: Optional[str] = Field(default=None, max_length=60,
        description="declined 时的拒绝码，例如 do_not_honor / card_declined")
    note: Optional[str] = Field(default=None, max_length=200,
        description="可选备注（PayPal 错误文案 / 触发场景等）")


_NAME_SAFE_RE = re.compile(r"^[A-Za-z][A-Za-z\-' ]{0,59}$")


def _validate_name(value: Optional[str], field: str) -> Optional[str]:
    """姓名白名单校验：只允许英文字母 / 连字符 / 撇号 / 空格，首位必须是字母。"""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _NAME_SAFE_RE.match(cleaned):
        raise HTTPException(
            status_code=400,
            detail=f"{field} 只允许英文字母 / 连字符 / 撇号 / 空格，且首位是字母",
        )
    return cleaned


@router.post("/synthetic-visa")
def generate_synthetic_visa_card(
    body: SyntheticVisaRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """生成 PayPal 友好的合成卡完整套件（卡 + 地址 + 姓名 + 电话）。

    用于 PayPal guest checkout 绑卡场景（一次返回所有 PayPal 表单需要的字段）：
      - 卡：4147/4100 BIN + Luhn 合法，通过 PayPal 预校验
      - 持卡人姓名：默认走主注册流姓名池；可通过 first_name/last_name 注入对齐 OpenAI 账户
      - 账单地址：精选 24 个美国中产社区真实地址（ZIP+state+city 通过 USPS）+ 24h 冷却（A1）
      - 电话：区号跟 state 一致，避坑 555 测试号

    所有字段保证内部一致：
      - 卡持卡人姓名 = 账单姓名
      - 电话区号 = 账单地址 state 区号
      - ZIP + state + city 三向通过 PayPal AVS

    ⚠️ 注意：合成卡**不能真实扣款**，仅用于绑定 0 元试用场景。
    """
    from src.fintech.country_profiles import (
        SUPPORTED_COUNTRIES,
        bin_prefix_strings,
        normalize_country,
    )
    from src.fintech.synthetic_visa import generate_synthetic_visa_kit

    # 校验国家
    country = normalize_country(body.country)
    if country not in SUPPORTED_COUNTRIES:
        raise HTTPException(
            status_code=400,
            detail=f"country 必须是 {', '.join(SUPPORTED_COUNTRIES)} 之一，得到 {country!r}",
        )

    # 校验 bin_prefix：若指定，必须属于该国合法 BIN 集
    bin_tuple = None
    if body.bin_prefix:
        clean = body.bin_prefix.strip()
        allowed = bin_prefix_strings(country)
        if clean not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{country} 的 bin_prefix 必须是 {', '.join(allowed)} 之一，得到 {clean!r}",
            )
        bin_tuple = tuple(int(c) for c in clean)

    override_first = _validate_name(body.first_name, "first_name")
    override_last = _validate_name(body.last_name, "last_name")

    kit = generate_synthetic_visa_kit(
        country=country,
        bin_prefix=bin_tuple,
        seed=body.seed,
        override_first_name=override_first,
        override_last_name=override_last,
    )
    payload = kit.to_form_payload()

    # A2：落审计（不存完整卡号 / CVV）
    audit_id = ""
    try:
        with get_session() as session:
            audit = SyntheticCardAudit(
                bin_prefix=kit.card.bin_prefix,
                last_four=kit.card.last_four,
                country=kit.country,
                address_state=kit.address_state,
                address_zip=kit.address_zip,
                first_name=kit.first_name,
                last_name=kit.last_name,
                feedback_status="pending",
                created_by=user.username,
            )
            session.add(audit)
            session.commit()
            session.refresh(audit)
            audit_id = audit.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("synthetic_visa_kit 审计落表失败（不阻塞生成）: %s", exc)

    logger.info(
        "synthetic_visa_kit 已生成: country=%s bin=%s last4=%s name=%s source=%s audit=%s requester=%s",
        kit.country, kit.card.bin_prefix, kit.card.last_four, kit.full_name,
        kit.source, audit_id or "skip", user.username,
    )
    payload["note"] = "合成卡仅过 PayPal 预校验，不能真实扣款；用于 0 元试用场景"
    payload["audit_id"] = audit_id
    return payload


@router.post("/synthetic-visa/{audit_id}/feedback")
def feedback_synthetic_visa(
    audit_id: str,
    body: SyntheticVisaFeedbackRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """回写合成卡绑卡结果（A2 数据闭环）。

    前端用户在 /cards 面板点 "通过 / 失败" 按钮触发，
    或运维直接 curl 反馈。
    """
    status = (body.status or "").strip().lower()
    if status not in ("success", "declined"):
        raise HTTPException(status_code=400, detail="status 必须是 'success' 或 'declined'")

    with get_session() as session:
        audit = session.get(SyntheticCardAudit, audit_id)
        if audit is None:
            raise HTTPException(status_code=404, detail="审计记录不存在或已被清理")
        if audit.feedback_status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"该审计记录已反馈 ({audit.feedback_status})，不允许重复回写",
            )
        audit.feedback_status = status
        audit.decline_code = (body.decline_code or "").strip()[:60] or None
        audit.feedback_note = (body.note or "").strip()[:200] or None
        audit.feedback_at = datetime.now(timezone.utc)
        session.add(audit)
        session.commit()
        session.refresh(audit)

    return {
        "ok": True,
        "audit_id": audit.id,
        "status": audit.feedback_status,
        "feedback_at": audit.feedback_at.isoformat() if audit.feedback_at else "",
    }


@router.get("/synthetic-visa/stats")
def stats_synthetic_visa(
    user: User = Depends(require_role("admin")),
):
    """合成卡审计统计：按 BIN / State 聚合通过率（运维 / 调参用）。"""
    from sqlmodel import select

    with get_session() as session:
        rows = session.exec(select(SyntheticCardAudit)).all()

    total = len(rows)
    by_bin: dict[str, dict[str, int]] = {}
    by_state: dict[str, dict[str, int]] = {}
    by_country: dict[str, dict[str, int]] = {}
    pending = sum(1 for r in rows if r.feedback_status == "pending")
    success = sum(1 for r in rows if r.feedback_status == "success")
    declined = sum(1 for r in rows if r.feedback_status == "declined")

    for r in rows:
        b = by_bin.setdefault(r.bin_prefix or "?", {"total": 0, "success": 0, "declined": 0, "pending": 0})
        b["total"] += 1
        b[r.feedback_status] = b.get(r.feedback_status, 0) + 1
        s = by_state.setdefault(r.address_state or "?", {"total": 0, "success": 0, "declined": 0, "pending": 0})
        s["total"] += 1
        s[r.feedback_status] = s.get(r.feedback_status, 0) + 1
        c = by_country.setdefault(
            getattr(r, "country", "") or "?",
            {"total": 0, "success": 0, "declined": 0, "pending": 0},
        )
        c["total"] += 1
        c[r.feedback_status] = c.get(r.feedback_status, 0) + 1

    return {
        "total": total,
        "pending": pending,
        "success": success,
        "declined": declined,
        "by_bin": by_bin,
        "by_state": by_state,
        "by_country": by_country,
    }


@router.get("/{card_key}")
def get_card_detail(
    card_key: str,
    user: User = Depends(require_role("admin")),
):
    """单卡详情。"""
    rec = get_activation(card_key)
    if rec is None:
        raise HTTPException(status_code=404, detail="卡密不存在或未激活")
    return _redact(rec)


@router.post("/{card_key}/invalidate")
def invalidate_card(
    card_key: str,
    body: Optional[InvalidateRequest] = None,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """手动作废卡密缓存。作废后该 cdk 不会再被尝试 verify（X988 一次性消耗）。"""
    reason = (body.reason if body else "manual") or "manual"
    ok = svc_invalidate(card_key, reason=reason)
    if not ok:
        raise HTTPException(status_code=404, detail="卡密不存在")
    rec = get_activation(card_key)
    return _redact(rec) if rec else {"ok": True}


@router.post("")
def add_card_to_pool(
    body: AddCardRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """手动入池：调卡商 API 拉卡 + 创建 CardActivation 记录。"""
    try:
        card_api = _build_card_api(body.card_provider)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("入池构造卡商客户端失败")
        raise HTTPException(status_code=503, detail="卡商客户端初始化失败，请检查服务端配置") from exc

    rec, meta = add_card(
        card_key=body.card_key,
        card_provider=body.card_provider,
        target_warmup_count=body.target_warmup_count,
        card_api=card_api,
    )
    if rec is None:
        reason = meta.get("reason", "unknown")
        detail = meta.get("detail", "")
        if reason in ("duplicate", "duplicate_race"):
            raise HTTPException(status_code=409, detail=_safe_error_detail(detail, "卡密已在池中"))
        if reason in ("invalid_provider", "invalid_target", "empty_card_key"):
            raise HTTPException(status_code=422, detail=_safe_error_detail(detail, reason))
        if reason == "card_api_error":
            raise HTTPException(status_code=502, detail=_safe_error_detail(detail, "卡商接口错误"))
        if reason == "card_not_found":
            raise HTTPException(status_code=404, detail=_safe_error_detail(detail, "卡密无效或已被占用"))
        raise HTTPException(status_code=500, detail="入池失败，请查看服务端日志")
    return _redact_pool(rec)


@router.post("/{card_key}/warmup")
def trigger_card_warmup(
    card_key: str,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """触发一轮热卡（异步起后台线程，立即返回 running 状态）。"""
    # 先看卡是不是在池里（也校验下权限边界）
    rec = get_activation(card_key)
    if rec is None:
        raise HTTPException(status_code=404, detail="卡密不存在")

    # 构造 execute_card_warmup 需要的依赖
    config = load_config()
    try:
        card_api = _build_card_api(rec.card_provider)
    except Exception as exc:
        logger.exception("热卡构造卡商客户端失败")
        raise HTTPException(status_code=503, detail="卡商客户端初始化失败，请检查服务端配置") from exc

    # 用 ConfigService 提供 select_warmup_account / record_warmup_outcome（execute_card_warmup 需要）
    from src.services.config_service import ConfigService
    svc = ConfigService()

    try:
        result = trigger_warmup(
            card_key,
            config=config,
            card_api=card_api,
            svc_for_warmup_pool=svc,
            proxy_url=config.proxy or "",
        )
    except _AlreadyRunningError:
        raise HTTPException(status_code=409, detail="该卡已有热卡线程在跑")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/{card_key}/warmup-status")
def get_card_warmup_status(
    card_key: str,
    user: User = Depends(require_role("admin")),
):
    """查指定卡的热卡进度（前端轮询用）。"""
    st = get_warmup_status(card_key)
    if st is None:
        raise HTTPException(status_code=404, detail="卡密不在池中")
    return st
