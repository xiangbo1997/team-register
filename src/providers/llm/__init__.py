# -*- coding: utf-8 -*-
"""LLM（辅助决策大模型）Provider 子包。

这一类 provider 承载注册自动化里**辅助决策大模型**的连接配置（base_url / api_key /
model / 视觉决策开关等），供 ``/providers`` 页面结构化渲染表单、worker 解析后覆盖到
``AppConfig.llm_*`` 字段。

与其他 provider 的区别：LLM provider **不做运行时行为**（不申号、不扣款）——真正的 LLM
调用在 ``src/automation/llm.py`` 的 ``OpenAICompatibleLLMClient`` 与
``src/automation/triage.py`` 的 ``VisionLLMClient``。本 provider 类只是 schema 载体 +
可选的 ``test_connection`` 自检钩子，让运维能在 admin UI 里像配 SMS/Card 一样配大模型。

新增 LLM 供应商 = 在本目录新建 ``<kind>.py`` 加 ``@register_provider`` 装饰器；
启动时 ``ProviderRegistry.discover()`` 自动 import 完成注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class LlmProvider(ABC):
    """LLM（辅助决策大模型）Provider 抽象基类。

    故意做得极薄：LLM provider 只承载连接配置，不抽象出运行时方法。子类按需实现
    ``test_connection`` 做凭据自检（供 ``/api/providers/.../test`` 调用）。
    """

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """凭据自检。返回 (是否可用, 说明文案)。

        实现可发一个最小 chat/completions 请求验证 base_url + api_key + model。
        网络/代理不可达时返回 (False, 原因) 而非抛异常。
        """
        raise NotImplementedError
