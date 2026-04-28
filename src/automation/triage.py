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


# 分诊类别与建议恢复路径的映射
TRIAGE_CATEGORIES = {
    "ip_pollution": "rotate_proxy",       # IP 被标记，换住宅/机房 IP
    "card_declined": "reorder_card",      # 卡拒付，申请新卡
    "captcha": "solve_captcha",           # 要求验证码，调第三方 solver
    "ui_change": "refresh_selectors",     # DOM 变了，刷新候选动作
    "account_banned": "abort_task",       # 账号被封，放弃重试
    "rate_limited": "backoff_retry",      # 限流，等待后重试
    "unknown": "manual_handoff",          # 无法判断，交人工
}


@dataclass(frozen=True)
class TriageDecision:
    """分诊结论。"""

    category: str                          # TRIAGE_CATEGORIES 的 key
    suggested_action: str                  # 对应的恢复路径
    confidence: float                      # 0.0-1.0
    rationale: str = ""                    # 简短解释
    evidence_summary: str = ""             # LLM 观察到的关键线索
    raw: dict[str, Any] = field(default_factory=dict)   # 原始响应，便于审计

    @property
    def is_actionable(self) -> bool:
        """是否可以据此自动派单（置信度高 + 不是 unknown）。"""
        return self.confidence >= 0.6 and self.category != "unknown"


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
    ) -> dict[str, Any]:
        """
        发送分诊请求。返回 JSON dict，格式由 prompt 约定（见下方 system prompt）。

        截图为 None 时会退化为纯文本模式 —— 保证在无 Vision 能力的模型上也能用。
        """
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _build_user_text(
                    page_url=page_url,
                    recent_logs=recent_logs,
                    signals=signals,
                    last_error=last_error,
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
    "当前页面 URL、最近日志片段和页面信号。你的任务：识别卡点根因，输出严格 JSON，"
    "字段包括：\n"
    '  "category": 必须从 [ip_pollution, card_declined, captcha, ui_change, '
    'account_banned, rate_limited, unknown] 里选一个\n'
    '  "confidence": 0.0-1.0 浮点数\n'
    '  "rationale": 一句话中文解释（不要暴露截图里的邮箱/token/卡号）\n'
    '  "evidence_summary": 关键观察（如"看到红字 Card declined"）\n'
    "禁止猜测任何新的选择器、URL、脚本；禁止编造不在日志/信号里的事实。"
    "若证据不足，category 必须为 unknown，confidence ≤ 0.4。"
)


def _build_user_text(
    *,
    page_url: str,
    recent_logs: list[str],
    signals: dict[str, Any],
    last_error: str,
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
    ) -> TriageDecision:
        try:
            raw = self._client.request_triage(
                screenshot_bytes=screenshot_bytes,
                page_url=page_url,
                recent_logs=recent_logs,
                signals=signals,
                last_error=last_error,
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

        return TriageDecision(
            category=category,
            suggested_action=TRIAGE_CATEGORIES[category],
            confidence=confidence,
            rationale=str(raw.get("rationale", "")),
            evidence_summary=str(raw.get("evidence_summary", "")),
            raw=raw,
        )
