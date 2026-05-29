# -*- coding: utf-8 -*-
"""
链接模板服务

用户在号池"生成链接"弹窗里点"保存为模板"产出的预填值 CRUD。
模板按 name 去重（DB 层 unique 约束）。简单 CRUD，无业务规则。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.db.engine import get_session
from src.db.models import LinkTemplate

logger = logging.getLogger(__name__)


_VALID_PLANS = ("team", "plus", "pro", "pro_lite")
_VALID_RETURN_MODES = ("long", "app")


def _template_to_dict(t: LinkTemplate) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "plan": t.plan,
        "seat_quantity": int(t.seat_quantity or 0),
        "promo_code": t.promo_code or "",
        "promo_campaign_id": t.promo_campaign_id or "",
        "aimizy_country": t.aimizy_country or "",
        "aimizy_currency": t.aimizy_currency or "",
        "workspace_name": t.workspace_name or "",
        "return_mode": t.return_mode or "long",
        "proxy_id": t.proxy_id,  # 可选关联代理；None = 用 .env 默认
        "created_at": t.created_at.isoformat() if t.created_at else "",
        # promo_eligibility 模块写入的验证状态（dashboard 展示用）
        "last_eligibility_status": t.last_eligibility_status or "",
        "last_eligibility_check_at": (
            t.last_eligibility_check_at.isoformat() if t.last_eligibility_check_at else ""
        ),
        "last_eligibility_metadata": t.last_eligibility_metadata or None,
        # P2 模板化扩展字段（2026-05-25）
        "schema_version": t.schema_version or "",
        "url_locale": t.url_locale or "",
        "extra_payload_json": t.extra_payload_json or None,
        "is_preset": bool(t.is_preset),
        "sort_order": int(t.sort_order or 0),
        # P6 checkout_ui_mode（2026-05-25）
        "checkout_ui_mode": t.checkout_ui_mode or "",
        # promo metadata 结构化字段（P4 抽取 / 升级弹窗下拉显示力度用）
        "promo_percent_off": t.promo_percent_off,            # int | None
        "promo_duration_months": t.promo_duration_months,    # int | None
        "promo_expires_at": (
            t.promo_expires_at.isoformat() if t.promo_expires_at else ""
        ),
        "promo_max_redemptions": t.promo_max_redemptions,    # int | None
        "promo_applicable_plans": t.promo_applicable_plans or "",
    }


def list_templates() -> list[dict[str, Any]]:
    """列出所有模板。

    排序策略（P6 引入）：promo_percent_off DESC（NULLS LAST，让有力度的码排前）
    其次 created_at DESC。这样号池升级弹窗下拉里折扣最大的模板会自动排在最上。
    """
    with get_session() as session:
        # SQLAlchemy 的 nullslast 在 SQLite 上支持有限，用表达式手写更稳：
        # ORDER BY (promo_percent_off IS NULL), promo_percent_off DESC, created_at DESC
        # 第一列布尔判断把 NULL 推到后面（True>False）
        from sqlalchemy import case
        null_last = case((LinkTemplate.promo_percent_off.is_(None), 1), else_=0)
        rows = list(session.exec(
            select(LinkTemplate).order_by(
                null_last,
                LinkTemplate.promo_percent_off.desc(),  # type: ignore
                LinkTemplate.created_at.desc(),  # type: ignore
            )
        ).all())
        return [_template_to_dict(t) for t in rows]


def list_presets() -> list[dict[str, Any]]:
    """列出所有「预设」模板（is_preset=True），按 sort_order 升序。

    用于号池弹窗的快捷按钮渲染（取代硬编码的 4 个 applyPreset）。
    """
    with get_session() as session:
        rows = list(session.exec(
            select(LinkTemplate)
            .where(LinkTemplate.is_preset == True)  # noqa: E712  SQLAlchemy 表达式不能用 is_
            .order_by(LinkTemplate.sort_order, LinkTemplate.id)  # type: ignore
        ).all())
        return [_template_to_dict(t) for t in rows]


def get_template(template_id: int) -> Optional[dict[str, Any]]:
    """按 id 取单条；不存在返回 None。"""
    with get_session() as session:
        t = session.get(LinkTemplate, template_id)
        return _template_to_dict(t) if t else None


def create_template(
    *,
    name: str,
    plan: str = "team",
    seat_quantity: int = 1,
    promo_code: str = "",
    promo_campaign_id: str = "",
    aimizy_country: str = "",
    aimizy_currency: str = "",
    workspace_name: str = "",
    return_mode: str = "long",
    proxy_id: Optional[int] = None,
    # P2 模板化扩展字段
    schema_version: Optional[str] = None,
    url_locale: Optional[str] = None,
    extra_payload_json: Optional[dict] = None,
    is_preset: bool = False,
    sort_order: int = 0,
    # P6 暴露 checkout_ui_mode
    checkout_ui_mode: Optional[str] = None,
) -> dict[str, Any]:
    """创建模板。name 必填且全局唯一；plan/return_mode 走白名单校验。

    proxy_id 可选；若指定会校验代理存在（避免悬空引用）。
    """
    safe_name = (name or "").strip()
    if not safe_name:
        raise ValueError("模板名不能为空")
    if len(safe_name) > 80:
        raise ValueError("模板名过长（>80）")

    plan_lower = (plan or "team").strip().lower()
    if plan_lower not in _VALID_PLANS:
        raise ValueError(f"plan 必须是 {_VALID_PLANS} 之一，得到 {plan!r}")

    mode = (return_mode or "long").strip().lower()
    if mode not in _VALID_RETURN_MODES:
        raise ValueError(f"return_mode 必须是 {_VALID_RETURN_MODES} 之一，得到 {return_mode!r}")

    # proxy_id 存在性校验（容许 None；指定时必须真实存在）
    safe_proxy_id: Optional[int] = None
    if proxy_id is not None:
        from src.db.models import Proxy

        with get_session() as session:
            proxy_row = session.get(Proxy, int(proxy_id))
            if proxy_row is None:
                raise ValueError(f"代理不存在: id={proxy_id}")
            safe_proxy_id = int(proxy_id)

    with get_session() as session:
        # 预先按 name 判重，给出友好错误（DB unique 兜底）
        existing = session.exec(
            select(LinkTemplate).where(LinkTemplate.name == safe_name)
        ).first()
        if existing is not None:
            raise ValueError(f"模板名已存在: {safe_name}")

        t = LinkTemplate(
            name=safe_name,
            plan=plan_lower,
            seat_quantity=max(1, int(seat_quantity or 1)),
            promo_code=(promo_code or "").strip()[:80],
            promo_campaign_id=(promo_campaign_id or "").strip()[:80],
            aimizy_country=(aimizy_country or "").strip()[:8],
            aimizy_currency=(aimizy_currency or "").strip()[:8],
            workspace_name=(workspace_name or "").strip()[:120],
            return_mode=mode,
            proxy_id=safe_proxy_id,
            # P2 模板化扩展字段（None / 空值表示走全局默认或不启用）
            schema_version=(schema_version or "").strip()[:20] or None,
            url_locale=(url_locale or "").strip()[:10] or None,
            extra_payload_json=extra_payload_json if isinstance(extra_payload_json, dict) else None,
            is_preset=bool(is_preset),
            sort_order=int(sort_order or 0),
            # P6 checkout_ui_mode：仅接受 hosted / custom；其他值规整为 None（走默认）
            checkout_ui_mode=(
                checkout_ui_mode.strip().lower()
                if checkout_ui_mode and checkout_ui_mode.strip().lower() in ("hosted", "custom")
                else None
            ),
        )
        session.add(t)
        try:
            session.commit()
        except IntegrityError as exc:  # 并发情况下 DB unique 兜底
            session.rollback()
            raise ValueError(f"模板名已存在: {safe_name}") from exc
        session.refresh(t)
        logger.info("创建模板 id=%s name=%s plan=%s", t.id, t.name, t.plan)
        return _template_to_dict(t)


def delete_template(template_id: int) -> bool:
    """删除模板。返回 True 表示删了一条，False 表示不存在。"""
    with get_session() as session:
        t = session.get(LinkTemplate, template_id)
        if t is None:
            return False
        session.delete(t)
        session.commit()
        logger.info("删除模板 id=%s", template_id)
        return True


__all__ = [
    "list_templates",
    "list_presets",
    "get_template",
    "create_template",
    "delete_template",
]
