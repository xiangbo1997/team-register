# -*- coding: utf-8 -*-
"""自动化状态机、证据采集与 LLM 受限决策能力。"""

from src.automation.artifacts import ArtifactRecorder, build_llm_evidence_payload, sanitize_url
from src.automation.captcha_solver import (
    ManualFallbackSolver,
    NoOpSolver,
    SolveAttempt,
    SolverProvider,
    try_solve_captcha,
)
from src.automation.experience import ExperienceStore
from src.automation.llm import LLMDecisionProvider, OpenAICompatibleLLMClient
from src.automation.models import (
    Action,
    ActionKind,
    Actionable,
    AutomationState,
    Decision,
    DecisionKind,
    Evidence,
    MachineResult,
    VerificationResult,
)
from src.automation.runtime import (
    AutomationRuntime,
    EvidenceCollector,
    RegistrationStateMachine,
    RuleDecisionProvider,
    extract_session_tokens_with_http,
    infer_state,
)

__all__ = [
    "Action",
    "ActionKind",
    "Actionable",
    "ArtifactRecorder",
    "ExperienceStore",
    "AutomationRuntime",
    "AutomationState",
    "Decision",
    "DecisionKind",
    "Evidence",
    "EvidenceCollector",
    "LLMDecisionProvider",
    "MachineResult",
    "ManualFallbackSolver",
    "NoOpSolver",
    "OpenAICompatibleLLMClient",
    "RegistrationStateMachine",
    "RuleDecisionProvider",
    "SolveAttempt",
    "SolverProvider",
    "VerificationResult",
    "build_llm_evidence_payload",
    "extract_session_tokens_with_http",
    "infer_state",
    "sanitize_url",
    "try_solve_captcha",
]
