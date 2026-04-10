# -*- coding: utf-8 -*-
"""自动化运行时的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AutomationState(str, Enum):
    ENTRY = "ENTRY"
    AUTH = "AUTH"
    VERIFY_EMAIL = "VERIFY_EMAIL"
    ABOUT_YOU = "ABOUT_YOU"
    HOME = "HOME"
    PHONE = "PHONE"
    PAYMENT = "PAYMENT"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class ActionKind(str, Enum):
    CLICK = "click"
    FILL = "fill"
    PRESS = "press"
    SELECT = "select"
    WAIT = "wait"
    REFRESH = "refresh"
    BACK = "back"
    NEW_TAB = "new_tab"
    CLOSE_TAB = "close_tab"
    CLEAR_TARGET_STORAGE = "clear_target_storage"
    RECONNECT_PROFILE = "reconnect_profile"


class DecisionKind(str, Enum):
    CHOOSE_ACTION = "choose_action"
    REQUEST_EVIDENCE = "request_evidence"
    ABORT = "abort"


@dataclass
class Actionable:
    """可供策略层选择的候选动作摘要。"""

    action_id: str
    kind: ActionKind
    locator_ref: str = ""
    role: str = ""
    name: str = ""
    visible: bool = True
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    """执行器实际消费的动作对象。"""

    action_id: str
    kind: ActionKind
    description: str
    locator_ref: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    expected_outcomes: list[AutomationState] = field(default_factory=list)
    risk_level: str = "low"


@dataclass
class Evidence:
    """状态识别、决策和审计共用的证据包。"""

    url: str
    title: str = ""
    timestamp: str = ""
    step_name: str = ""
    state_candidates: list[AutomationState] = field(default_factory=list)
    actionables: list[Actionable] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    last_actions: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_state(self) -> AutomationState:
        if self.state_candidates:
            return self.state_candidates[0]
        return AutomationState.UNKNOWN


@dataclass
class Decision:
    """规则/LLM 统一决策协议。"""

    kind: DecisionKind
    action_id: str = ""
    requested_evidence: list[str] = field(default_factory=list)
    confidence: float = 1.0
    reason_code: str = ""
    rationale: str = ""


@dataclass
class VerificationResult:
    """动作后的验证结果。"""

    ok: bool
    reason_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MachineResult:
    """状态机运行结果。"""

    success: bool
    final_state: AutomationState
    failure_reason: str = ""
    manual_handoff_used: bool = False
