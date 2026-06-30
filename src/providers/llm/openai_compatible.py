# -*- coding: utf-8 -*-
"""OpenAI 兼容的辅助决策大模型 Provider。

承载注册自动化里**辅助决策大模型**（``LLMDecisionProvider`` + ``TriageDecisionProvider``）
的连接配置。worker（``src/api/worker.py`` 的 ``_resolve_runtime_config``）按 schema 字段名
把本 provider 的 config 覆盖到 ``AppConfig.llm_*``：

    base_url                      → config.llm_base_url
    api_key                       → config.llm_api_key
    model                         → config.llm_model
    timeout_ms                    → config.llm_timeout_ms
    confidence_threshold          → config.llm_confidence_threshold
    vision_enabled                → config.llm_vision_enabled            ← 视觉决策开关
    vision_screenshot_stall_threshold → config.llm_screenshot_on_stall_threshold

``vision_enabled`` 是本次新增的核心字段：开启后，辅助决策大模型在流程**卡住时**能看
页面截图做多模态决策（前提是 model 支持图片输入，如 gpt-4o）；诊断器（triage）默认
复用同一套 LLM 配置，也随之获得视觉能力。
"""

from __future__ import annotations

from src.providers.base import FieldSpec, register_provider
from src.providers.llm import LlmProvider


@register_provider(
    provider_type="llm",
    kind="openai_compatible",
    display_name="OpenAI 兼容大模型",
    description=(
        "辅助决策大模型（注册卡住时帮状态机选动作 / 诊断卡点）。"
        "对接任意 OpenAI Chat Completions 兼容端点；开启视觉决策需 model 支持图片输入。"
    ),
    schema=(
        FieldSpec(
            "base_url",
            type="str",
            required=True,
            description="OpenAI 兼容端点（如 https://proxy.example.com/v1）",
        ),
        FieldSpec(
            "api_key",
            type="secret",
            required=True,
            description="API key（密文存储，留空不覆盖原值）",
        ),
        FieldSpec(
            "model",
            type="str",
            required=True,
            default="gpt-4o-mini",
            description="模型名；开启视觉决策时必须是支持图片输入的多模态模型（如 gpt-4o）",
        ),
        FieldSpec(
            "timeout_ms",
            type="int",
            required=False,
            default=8000,
            description="单次调用超时（毫秒）",
        ),
        FieldSpec(
            "confidence_threshold",
            type="str",
            required=False,
            default="0.6",
            description="决策置信度阈值（0-1）；低于此值视为不确定",
        ),
        FieldSpec(
            "vision_enabled",
            type="bool",
            required=False,
            default=False,
            choices=("true", "false"),
            description="视觉决策：卡住时给模型附页面截图做多模态判断（需 model 支持图片）",
        ),
        FieldSpec(
            "vision_screenshot_stall_threshold",
            type="int",
            required=False,
            default=3,
            description="视觉决策触发阈值：同一状态卡顿到第 N 步才附截图（省 token），默认 3",
        ),
    ),
)
class OpenAICompatibleLlmProvider(LlmProvider):
    """OpenAI 兼容大模型 provider（schema 载体 + 凭据自检）。"""

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout_ms: int = 8000,
        confidence_threshold: str = "0.6",
        vision_enabled: bool = False,
        vision_screenshot_stall_threshold: int = 3,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.timeout_ms = int(timeout_ms or 8000)
        self.vision_enabled = bool(vision_enabled)
        self.vision_screenshot_stall_threshold = int(vision_screenshot_stall_threshold or 3)
        try:
            self.confidence_threshold = float(confidence_threshold or 0.6)
        except (TypeError, ValueError):
            self.confidence_threshold = 0.6

    def test_connection(self) -> tuple[bool, str]:
        """发一个最小 chat/completions 请求验证凭据。网络不可达返回 (False, 原因)。"""
        if not (self.base_url and self.api_key and self.model):
            return False, "base_url / api_key / model 不完整"
        try:
            import requests

            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=max(self.timeout_ms / 1000, 3),
            )
            if resp.status_code == 200:
                return True, f"连接成功（model={self.model}）"
            return False, f"端点返回 HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:  # 网络/代理/SSL 异常不抛，转成自检失败
            return False, f"连接失败: {type(exc).__name__}: {exc}"
