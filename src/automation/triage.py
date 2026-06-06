# -*- coding: utf-8 -*-
"""
错误分诊器（Triage）

当状态机进入 BLOCKED/ERROR，或连续失败/UNKNOWN，交由 Vision LLM 看截图 + 日志，
返回"分诊结论 + 建议动作类别"，让编排层决定是换代理、换卡、调 captcha solver
还是人工接管。

与现有 ``LLMDecisionProvider`` 的区别：
  - ``LLMDecisionProvider``：在已知候选 Action 里选一个（action-level，文本-only）
  - ``TriageDecisionProvider``：诊断"为什么卡住了"（incident-level，多模态）

返回值故意设计为 **类别标签**，而不是具体操作指令 —— 编排层根据类别派发给
Infra/Fintech/Workflow 的既有恢复路径，LLM 不直接控制浏览器。
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from src.automation.artifacts import _redact_text, sanitize_url

logger = logging.getLogger(__name__)


# 分诊类别与建议恢复路径的映射。
#
# 两类分诊：
#   1. 风控类（外部原因，运维侧可处置）：ip_pollution / card_declined / captcha /
#      account_banned / rate_limited —— 编排层据此换代理/换卡/调 solver/放弃。
#   2. 工程类（代码原因，开发侧需整改）：selector_stale / handler_stuck / dom_drift ——
#      不是外部封控，而是 selector 失效 / handler 逻辑卡死 / 页面 DOM 漂移，
#      诊断结论是给开发者看的「该改哪段代码」建议，suggested_action 统一指向
#      review_code（落盘 + 前端高亮，等人工改代码，不自动恢复）。
TRIAGE_CATEGORIES = {
    # —— 风控类（外部原因） ——
    "ip_pollution": "rotate_proxy",       # IP 被标记，换住宅/机房 IP
    "card_declined": "reorder_card",      # 卡拒付，申请新卡
    "captcha": "solve_captcha",           # 要求验证码，调第三方 solver
    "ui_change": "refresh_selectors",     # DOM 变了，刷新候选动作（轻量 UI 调整）
    "account_banned": "abort_task",       # 账号被封，放弃重试
    "rate_limited": "backoff_retry",      # 限流，等待后重试
    # —— 工程类（代码原因，给开发者看的整改建议） ——
    "selector_stale": "review_code",      # 元素找不到/选择器过期，需改 selector
    "handler_stuck": "review_code",       # handler 反复点同一步无进展，逻辑卡死
    "dom_drift": "review_code",           # 页面结构漂移，状态机推断/候选动作需更新
    # —— 兜底 ——
    "unknown": "manual_handoff",          # 无法判断，交人工
}

# 工程类类别集合：这些 category 的诊断是给开发者的代码整改建议，
# 前端应高亮展示「建议怎么改」，而非派给编排层自动恢复。
ENGINEERING_CATEGORIES = frozenset({"selector_stale", "handler_stuck", "dom_drift"})


@dataclass(frozen=True)
class TriageDecision:
    """分诊结论。"""

    category: str                          # TRIAGE_CATEGORIES 的 key
    suggested_action: str                  # 对应的恢复路径
    confidence: float                      # 0.0-1.0
    rationale: str = ""                    # 简短解释（为什么卡住）
    evidence_summary: str = ""             # LLM 观察到的关键线索（卡在哪）
    fix_suggestion: str = ""               # 工程类卡点的整改建议（建议怎么改）
    raw: dict[str, Any] = field(default_factory=dict)   # 原始响应，便于审计

    @property
    def is_actionable(self) -> bool:
        """是否可以据此自动派单（置信度高 + 不是 unknown）。"""
        return self.confidence >= 0.6 and self.category != "unknown"

    @property
    def is_engineering(self) -> bool:
        """是否工程类卡点（代码 bug，需开发整改而非自动恢复）。"""
        return self.category in ENGINEERING_CATEGORIES


class VisionLLMClient:
    """
    多模态 LLM 客户端：OpenAI 兼容的 ``chat/completions`` + ``image_url`` payload。

    与 ``OpenAICompatibleLLMClient`` 并存，职责分离：
      - 该类只做分诊（vision-heavy，调用频率低）
      - 旧类继续做 action-level 决策（text-only，调用频率高）
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_ms: int = 15000,
    ) -> None:
        if not all([base_url, api_key, model]):
            raise ValueError("VisionLLMClient: base_url / api_key / model 均不能为空")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_sec = max(timeout_ms / 1000, 1)

    def request_triage(
        self,
        *,
        screenshot_bytes: Optional[bytes],
        page_url: str,
        recent_logs: list[str],
        signals: dict[str, Any],
        last_error: str = "",
        recent_actions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """
        发送分诊请求。返回 JSON dict，格式由 prompt 约定（见下方 system prompt）。

        截图为 None 时会退化为纯文本模式 —— 保证在无 Vision 能力的模型上也能用。
        recent_actions 是最近执行的动作序列（含每步重试/卡顿计数），用于判定 handler_stuck
        这类「反复点同一步无进展」的工程类卡点。
        """
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _build_user_text(
                    page_url=page_url,
                    recent_logs=recent_logs,
                    signals=signals,
                    last_error=last_error,
                    recent_actions=recent_actions or [],
                ),
            }
        ]
        if screenshot_bytes:
            b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )

        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }

        response = requests.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        content_text = data["choices"][0]["message"]["content"]
        return json.loads(content_text)


_SYSTEM_PROMPT = (
    "你是一个受限网页注册自动化的事故分诊器。用户会给你一张截图（可能缺省）、"
    "当前页面 URL、最近日志片段、页面信号，以及最近执行的动作序列（含每步重试/卡顿计数）。"
    "你的任务：判断流程为什么卡住，输出严格 JSON。\n"
    "\n"
    "category 必须从下面两组里选一个：\n"
    "  【风控类·外部原因】ip_pollution（IP被标记）/ card_declined（卡拒付）/ "
    "captcha（要求验证码）/ ui_change（轻量UI调整）/ account_banned（账号被封）/ "
    "rate_limited（被限流）\n"
    "  【工程类·代码原因】selector_stale（元素一直找不到/选择器过期，日志反复报 locator 超时）/ "
    "handler_stuck（动作序列反复点同一步、retry/stall 计数累加但页面 DOM 指纹不变=逻辑卡死）/ "
    "dom_drift（页面结构相比预期发生漂移，状态机推断或候选动作已不匹配真实 DOM）\n"
    "  【兜底】unknown（证据不足无法判断）\n"
    "\n"
    "输出字段：\n"
    '  "category": 上面列表里的一个值\n'
    '  "confidence": 0.0-1.0 浮点数\n'
    '  "rationale": 一句话中文解释「为什么卡住」（不要暴露邮箱/token/卡号）\n'
    '  "evidence_summary": 关键观察「卡在哪」（如：动作序列连续3次 submit_password 但URL不变）\n'
    '  "fix_suggestion": 仅当 category 是工程类时填，给开发者的「建议怎么改」'
    "（如：submit_password 的 selector 需补 input[name=...] fallback）；非工程类留空字符串\n"
    "\n"
    "硬约束：禁止猜测任何新的选择器、URL、脚本去执行；fix_suggestion 只是给人看的建议，"
    "不是要你生成可执行代码；禁止编造不在日志/信号/动作序列里的事实。"
    "若证据不足，category 必须为 unknown，confidence ≤ 0.4。"
)


def _summarize_actions(recent_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把动作序列压成低敏摘要，供 LLM 判定 handler_stuck（反复点同一步无进展）。

    入参是 ``runtime.last_actions`` 的元素，字段为
    ``{action_id, kind, description, result, error}``；这里只保留动作标识与结果，
    description 走脱敏（可能含页面文本），不透传任何 fill value（密码/验证码）。
    """
    summary: list[dict[str, Any]] = []
    for item in recent_actions[-10:]:
        if not isinstance(item, dict):
            continue
        summary.append(
            {
                "action_id": str(item.get("action_id") or ""),
                "kind": str(item.get("kind") or ""),
                "description": _redact_text(str(item.get("description") or "")),
                "result": str(item.get("result") or ""),
            }
        )
    return summary


def _build_user_text(
    *,
    page_url: str,
    recent_logs: list[str],
    signals: dict[str, Any],
    last_error: str,
    recent_actions: Optional[list[dict[str, Any]]] = None,
) -> str:
    """构造用户侧 text 段，做脱敏。"""
    safe_url = sanitize_url(page_url)
    safe_logs = [_redact_text(line) for line in recent_logs[-10:]]
    safe_error = _redact_text(last_error)

    # signals 可能包含布尔值 / 小整数，低敏感；直接 JSON dump
    signals_clean = {k: v for k, v in (signals or {}).items() if not isinstance(v, (dict, list))}

    return json.dumps(
        {
            "url": safe_url,
            "signals": signals_clean,
            "recent_logs": safe_logs,
            "last_error": safe_error,
            "recent_actions": _summarize_actions(recent_actions or []),
        },
        ensure_ascii=False,
    )


class TriageDecisionProvider:
    """对 VisionLLMClient 输出做校验 + 转成 TriageDecision。"""

    def __init__(
        self,
        *,
        client: Any,
        confidence_threshold: float = 0.6,
    ) -> None:
        self._client = client
        self._confidence_threshold = confidence_threshold

    def diagnose(
        self,
        *,
        screenshot_bytes: Optional[bytes],
        page_url: str,
        recent_logs: list[str],
        signals: dict[str, Any],
        last_error: str = "",
        recent_actions: Optional[list[dict[str, Any]]] = None,
    ) -> TriageDecision:
        try:
            raw = self._client.request_triage(
                screenshot_bytes=screenshot_bytes,
                page_url=page_url,
                recent_logs=recent_logs,
                signals=signals,
                last_error=last_error,
                recent_actions=recent_actions or [],
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("分诊 LLM 调用失败，降级为 unknown: %s", exc)
            return TriageDecision(
                category="unknown",
                suggested_action=TRIAGE_CATEGORIES["unknown"],
                confidence=0.0,
                rationale=f"LLM 不可用: {exc}",
            )

        category = str(raw.get("category", "")).strip().lower()
        if category not in TRIAGE_CATEGORIES:
            return TriageDecision(
                category="unknown",
                suggested_action=TRIAGE_CATEGORIES["unknown"],
                confidence=0.0,
                rationale=f"LLM 返回未知类别: {raw.get('category')!r}",
                raw=raw,
            )

        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        # 低置信 → 降级为 unknown（避免误派单）
        if confidence < self._confidence_threshold:
            return TriageDecision(
                category="unknown",
                suggested_action=TRIAGE_CATEGORIES["unknown"],
                confidence=confidence,
                rationale=str(raw.get("rationale", "低置信度")),
                evidence_summary=str(raw.get("evidence_summary", "")),
                raw=raw,
            )

        # fix_suggestion 仅对工程类卡点有意义（给开发者的整改建议）；
        # 风控类即便 LLM 误填也丢弃，避免前端把"换代理"当成代码建议展示。
        fix_suggestion = ""
        if category in ENGINEERING_CATEGORIES:
            fix_suggestion = _redact_text(str(raw.get("fix_suggestion", "")))

        return TriageDecision(
            category=category,
            suggested_action=TRIAGE_CATEGORIES[category],
            confidence=confidence,
            rationale=str(raw.get("rationale", "")),
            evidence_summary=str(raw.get("evidence_summary", "")),
            fix_suggestion=fix_suggestion,
            raw=raw,
        )


def _resolve_triage_endpoint(config: Any) -> tuple[str, str, str]:
    """解析诊断器要用的供应商端点（base_url / api_key / model）。

    复用策略：决策器（LLM_*）和诊断器（TRIAGE_*）是两类大模型角色，但默认共用同一个
    供应商。优先用显式配的 TRIAGE_*；任一字段缺失则整体回退到 LLM_*——这样运维只配一套
    LLM_* 凭据，``TRIAGE_ENABLED=true`` 一开两类角色都能跑。

    需要单独给诊断器换模型时（如决策用小模型、诊断用 vision 大模型），再显式填 TRIAGE_*。
    """
    t_base = str(getattr(config, "triage_base_url", "") or "")
    t_key = str(getattr(config, "triage_api_key", "") or "")
    t_model = str(getattr(config, "triage_model", "") or "")
    if t_base and t_key and t_model:
        return t_base, t_key, t_model

    # 回退复用 LLM_*
    l_base = str(getattr(config, "llm_base_url", "") or "")
    l_key = str(getattr(config, "llm_api_key", "") or "")
    l_model = str(getattr(config, "llm_model", "") or "")
    return t_base or l_base, t_key or l_key, t_model or l_model


def build_triage_provider(config: Any) -> Optional[TriageDecisionProvider]:
    """按配置构造卡住诊断器（事故分诊器）。

    与 ``_build_llm_provider`` 同构的工厂：默认关闭（``triage_enabled=False`` 返回 None），
    端点不完整时降级为 None（绝不抛异常），保证主流程零行为变更。

    端点解析见 ``_resolve_triage_endpoint``：默认复用 LLM_* 供应商，无需单独配 TRIAGE_*。

    返回 None 时，``runtime.triage_provider`` 为空，``_run_triage`` 直接 no-op。
    """
    if not getattr(config, "triage_enabled", False):
        return None

    base_url, api_key, model = _resolve_triage_endpoint(config)
    if not (base_url and api_key and model):
        logger.warning(
            "分诊器已启用但端点不完整（TRIAGE_* 和 LLM_* 均缺失 base_url/api_key/model），降级为关闭"
        )
        return None

    try:
        client = VisionLLMClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_ms=getattr(config, "triage_timeout_ms", 15000),
        )
    except ValueError as exc:
        logger.warning("分诊器构造失败，降级为关闭: %s", exc)
        return None

    return TriageDecisionProvider(
        client=client,
        confidence_threshold=getattr(config, "triage_confidence_threshold", 0.6),
    )


class _RingLogHandler(logging.Handler):
    """环形日志缓冲：把最近 N 行日志写进 runtime.recent_log_buffer，供分诊器读取。

    诊断只需「最近发生了什么」，故用定长 ring buffer（默认 50 行）防内存膨胀。
    脱敏在分诊器侧统一做（``_build_user_text`` 调 ``_redact_text``），这里只存原文。
    """

    def __init__(self, buffer: list[str], capacity: int = 50) -> None:
        super().__init__()
        self._buffer = buffer
        self._capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        self._buffer.append(line)
        if len(self._buffer) > self._capacity:
            del self._buffer[: len(self._buffer) - self._capacity]


def attach_log_buffer(runtime: Any, *, capacity: int = 50) -> None:
    """给 runtime 挂一个环形日志缓冲 handler（仅当分诊器已启用时才挂）。

    分诊器关闭时（``triage_provider is None``）不挂 handler，零开销零行为变更。
    handler 挂到 root logger，自动捕获所有模块日志。生命周期跟随进程，
    单次注册任务级别无需手动 detach（buffer 定长不泄漏）。
    """
    if getattr(runtime, "triage_provider", None) is None:
        return
    buffer = getattr(runtime, "recent_log_buffer", None)
    if buffer is None:
        return
    handler = _RingLogHandler(buffer, capacity=capacity)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
