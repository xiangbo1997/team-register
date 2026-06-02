# -*- coding: utf-8 -*-
"""
自进化经验（learned_workflows）管理 API

GET    /api/workflows           — 列表（platform/state/enabled 过滤），按成功率展示
GET    /api/workflows/stats     — 全局成功率概览
POST   /api/workflows/{id}/toggle — 启用/禁用（禁用后状态机 find 不再命中）
DELETE /api/workflows/{id}      — 删除一条经验

权限：全部 admin 角色；写操作 + CSRF。
脱敏：last_action_meta 可能含 fill 值，展示前用 artifacts.redact_structure 脱敏。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.security import require_csrf, require_role
from src.automation.artifacts import redact_structure
from src.db.models import LearnedWorkflow, User
from src.services import learned_workflow_service as lw

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class ToggleRequest(BaseModel):
    enabled: bool


def _serialize(row: LearnedWorkflow) -> dict[str, Any]:
    """脱敏序列化：last_action_meta 走 redact_structure（可能含 fill 值）。"""
    total = (row.success_count or 0) + (row.fail_count or 0)
    rate = (row.success_count / total) if total else 0.0
    try:
        safe_meta = redact_structure(dict(row.last_action_meta or {}))
        # last_action_meta 的 "value" 键存的是填进表单的真实值（可能是密码/验证码），
        # redact_structure 按字段名判定敏感不覆盖 "value"，这里显式遮掩。
        if isinstance(safe_meta, dict) and "value" in safe_meta:
            safe_meta["value"] = "[REDACTED]"
    except Exception:
        safe_meta = {}
    return {
        "id": row.id,
        "platform": row.platform,
        "state": row.state,
        "location": row.location,
        "action_id": row.action_id,
        "source": row.source,
        "success_count": int(row.success_count or 0),
        "fail_count": int(row.fail_count or 0),
        "success_rate": round(rate, 3),
        "is_enabled": bool(row.is_enabled),
        "is_low_rate": total > 0 and rate < 0.5,  # 前端高亮淘汰候选
        "signal_signature": dict(row.signal_signature or {}),
        "last_action_meta": safe_meta,
        "step_name": row.step_name,
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


@router.get("")
def list_workflows(
    platform: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    enabled: Optional[bool] = Query(None),
    user: User = Depends(require_role("admin")),
):
    """列表（admin）。按 platform/state/enabled 过滤，按 updated_at 倒序。"""
    rows = lw.list_workflows(platform=platform, state=state, enabled=enabled)
    return [_serialize(r) for r in rows]


@router.get("/stats")
def workflow_stats(
    platform: Optional[str] = Query(None),
    user: User = Depends(require_role("admin")),
):
    """全局成功率概览。"""
    return lw.stats_overview(platform=platform)


@router.post("/{workflow_id}/toggle")
def toggle_workflow(
    workflow_id: int,
    body: ToggleRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """启用/禁用一条经验。禁用后状态机 find_best_action 不再返回它。"""
    ok = lw.set_enabled(workflow_id, body.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="经验记录不存在")
    return {"ok": True, "id": workflow_id, "enabled": body.enabled}


@router.delete("/{workflow_id}")
def delete_workflow(
    workflow_id: int,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """删除一条经验。"""
    ok = lw.delete_workflow(workflow_id)
    if not ok:
        raise HTTPException(status_code=404, detail="经验记录不存在")
    return {"ok": True, "id": workflow_id}
