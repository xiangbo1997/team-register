# -*- coding: utf-8 -*-
"""
ExperienceStore 工厂：装配「jsonl 经验 + DB 镜像双写」。

为什么放在 orchestration 层：automation 层（experience.py）刻意不依赖 db 层（避免循环
依赖），DB 双写靠 sink 回调注入。本工厂是「允许同时 import automation + db 的编排层」，
把 learned_workflow_service.record_outcome 包成 ExperienceStore 需要的 sink 签名。

被 main.py（CLI/worker 路径）和 orchestrator.py（三阶段路径）共用，保证两条实例化
路径行为一致（全局一致性）。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from src.automation.experience import ExperienceStore

logger = logging.getLogger(__name__)


def _make_db_sink(platform: str):
    """构造写 learned_workflows 表的 sink。失败静默（不阻断主流程）。"""

    def _sink(
        *,
        state: str,
        location: str,
        signal_signature: dict[str, Any],
        action_id: str,
        source: str,
        success: bool,
        step_name: str = "",
        last_action_meta: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            from src.services import learned_workflow_service as lw

            lw.record_outcome(
                state=state,
                location=location,
                signal_signature=signal_signature,
                action_id=action_id,
                source=source,
                success=success,
                platform=platform,
                step_name=step_name,
                last_action_meta=last_action_meta,
            )
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("learned_workflows 双写失败（已忽略）: %s", exc)

    return _sink


def build_experience_store(config: Any, *, platform: str = "openai") -> ExperienceStore:
    """主经验库（experience-memory.jsonl）+ DB 双写。"""
    base = str(getattr(config, "run_artifacts_dir", "") or "artifacts").strip() or "artifacts"
    return ExperienceStore(
        os.path.join(base, "experience-memory.jsonl"),
        sink=_make_db_sink(platform),
    )


def build_assist_store(config: Any, *, platform: str = "openai") -> Optional[ExperienceStore]:
    """枚举兜底专用经验库（assist-memory.jsonl）+ DB 双写。

    仅当 config.assist_fallback_enabled 为真时返回实例，否则 None（兜底层降级不启用）。
    用独立 jsonl 避免与主状态机经验混淆（兜底层 state 恒 UNKNOWN + location 带 #fragment）。
    """
    if not bool(getattr(config, "assist_fallback_enabled", False)):
        return None
    base = str(getattr(config, "run_artifacts_dir", "") or "artifacts").strip() or "artifacts"
    return ExperienceStore(
        os.path.join(base, "assist-memory.jsonl"),
        sink=_make_db_sink(platform),
    )
