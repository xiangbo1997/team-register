# -*- coding: utf-8 -*-
"""
自进化经验的 DB 持久化调度（learned_workflows 表）。

与 [[experience]]（artifacts/*.jsonl）双写：状态机/兜底层每次「AI 决策成败」
都通过 ExperienceStore 的 sink 回调调到这里的 record_outcome，upsert 一条 DB 记录，
让控制台 /workflows 页可视化学到的工作流并支持人工启禁/删除。

设计立场：
- 这是 db 层，automation 层不直接 import 本模块（避免循环依赖），靠 sink 回调注入。
- 任何写入异常都向上层（ExperienceStore._record_outcome 的 try/except）冒泡后被吞掉，
  绝不阻断注册主流程 —— DB 镜像失败不影响 jsonl 经验闭环。
- 择优逻辑（find_best_action）与 ExperienceStore.find_action_id 同语义：
  rate=s/(s+f) >= 阈值 且 s>=1 且 is_enabled，取 rate 最高者。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import LearnedWorkflow

logger = logging.getLogger(__name__)

# 与 ExperienceStore._MIN_SUCCESS_RATE 保持一致。
_MIN_SUCCESS_RATE = 0.5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _signature_hash(signal_signature: dict[str, Any]) -> str:
    """对信号签名算稳定 hash（JSON 列不便直接 WHERE 相等，用 hash 列查重）。"""
    raw = json.dumps(signal_signature or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def record_outcome(
    *,
    state: str,
    location: str,
    signal_signature: dict[str, Any],
    action_id: str,
    source: str,
    success: bool,
    platform: str = "openai",
    step_name: str = "",
    last_action_meta: Optional[dict[str, Any]] = None,
) -> Optional[LearnedWorkflow]:
    """upsert 一条经验记录，成败计数 +1。

    逻辑键 = (platform, state, location, signature_hash, action_id)。
    返回 upsert 后的记录；异常时返回 None（由调用方/上层吞掉，不阻断主流程）。
    """
    sig_hash = _signature_hash(signal_signature)
    try:
        with get_session() as session:
            stmt = select(LearnedWorkflow).where(
                LearnedWorkflow.platform == platform,
                LearnedWorkflow.state == state,
                LearnedWorkflow.location == location,
                LearnedWorkflow.signature_hash == sig_hash,
                LearnedWorkflow.action_id == action_id,
            )
            row = session.exec(stmt).first()
            if row is None:
                row = LearnedWorkflow(
                    platform=platform,
                    state=state,
                    location=location,
                    signal_signature=dict(signal_signature or {}),
                    signature_hash=sig_hash,
                    action_id=action_id,
                    source=source,
                    success_count=1 if success else 0,
                    fail_count=0 if success else 1,
                    is_enabled=True,
                    last_action_meta=dict(last_action_meta or {}),
                    step_name=step_name,
                )
                session.add(row)
            else:
                if success:
                    row.success_count += 1
                else:
                    row.fail_count += 1
                row.source = source
                if last_action_meta:
                    row.last_action_meta = dict(last_action_meta)
                if step_name:
                    row.step_name = step_name
                row.updated_at = _utc_now()
                session.add(row)
            session.commit()
            session.refresh(row)
            return row
    except Exception as exc:  # pragma: no cover - 防御性，DB 不可用时静默
        logger.warning("learned_workflows upsert 失败（已忽略）: %s", exc)
        return None


def find_best_action(
    *,
    state: str,
    location: str,
    signal_signature: dict[str, Any],
    candidate_ids: set[str],
    platform: str = "openai",
) -> str:
    """DB 版择优：返回成功率最高且达标（is_enabled + rate>=阈值 + s>=1）的 action_id。"""
    sig_hash = _signature_hash(signal_signature)
    try:
        with get_session() as session:
            stmt = select(LearnedWorkflow).where(
                LearnedWorkflow.platform == platform,
                LearnedWorkflow.state == state,
                LearnedWorkflow.location == location,
                LearnedWorkflow.signature_hash == sig_hash,
                LearnedWorkflow.is_enabled == True,  # noqa: E712 - SQLModel 需要 == True
            )
            rows = list(session.exec(stmt).all())
    except Exception as exc:  # pragma: no cover
        logger.warning("learned_workflows 查询失败（已忽略）: %s", exc)
        return ""

    best_id = ""
    best_rate = -1.0
    for row in rows:
        if row.action_id not in candidate_ids:
            continue
        total = row.success_count + row.fail_count
        if row.success_count < 1 or total == 0:
            continue
        rate = row.success_count / total
        if rate < _MIN_SUCCESS_RATE:
            continue
        if rate > best_rate:
            best_rate = rate
            best_id = row.action_id
    return best_id


def list_workflows(
    *,
    platform: Optional[str] = None,
    state: Optional[str] = None,
    enabled: Optional[bool] = None,
    limit: int = 500,
) -> list[LearnedWorkflow]:
    """列出经验记录（控制台 /workflows 用），按 updated_at 倒序。"""
    try:
        with get_session() as session:
            stmt = select(LearnedWorkflow)
            if platform:
                stmt = stmt.where(LearnedWorkflow.platform == platform)
            if state:
                stmt = stmt.where(LearnedWorkflow.state == state)
            if enabled is not None:
                stmt = stmt.where(LearnedWorkflow.is_enabled == enabled)
            stmt = stmt.order_by(LearnedWorkflow.updated_at.desc()).limit(limit)
            return list(session.exec(stmt).all())
    except Exception as exc:  # pragma: no cover
        logger.warning("learned_workflows 列表查询失败: %s", exc)
        return []


def set_enabled(workflow_id: int, enabled: bool) -> bool:
    """启用/禁用一条经验。返回是否成功。"""
    try:
        with get_session() as session:
            row = session.get(LearnedWorkflow, workflow_id)
            if row is None:
                return False
            row.is_enabled = enabled
            row.updated_at = _utc_now()
            session.add(row)
            session.commit()
            return True
    except Exception as exc:  # pragma: no cover
        logger.warning("learned_workflows 启禁失败: %s", exc)
        return False


def delete_workflow(workflow_id: int) -> bool:
    """删除一条经验。返回是否成功。"""
    try:
        with get_session() as session:
            row = session.get(LearnedWorkflow, workflow_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True
    except Exception as exc:  # pragma: no cover
        logger.warning("learned_workflows 删除失败: %s", exc)
        return False


def stats_overview(*, platform: Optional[str] = None) -> dict[str, Any]:
    """全局成功率概览（控制台 /workflows/stats）。"""
    rows = list_workflows(platform=platform, limit=10000)
    total_success = sum(r.success_count for r in rows)
    total_fail = sum(r.fail_count for r in rows)
    grand = total_success + total_fail
    enabled = sum(1 for r in rows if r.is_enabled)
    return {
        "workflow_count": len(rows),
        "enabled_count": enabled,
        "total_success": total_success,
        "total_fail": total_fail,
        "overall_success_rate": (total_success / grand) if grand else 0.0,
    }
