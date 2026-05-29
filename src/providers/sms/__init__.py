# -*- coding: utf-8 -*-
"""短信接码 Provider 子包。

新增接码平台 = 在本目录新建 ``<kind>.py``，定义类并加 ``@register_provider`` 装饰器。
启动时 ``ProviderRegistry.discover()`` 会自动 import 完成注册。

SmsProvider ABC 故意做得比 MailProvider 薄 —— 短信只有「申号 + 拿验证码」两步，
不存在 session/lease 概念。``test_connection`` 是可选自检钩子，供 ``/api/providers/.../test``
端点调用确认凭据有效（如 SMS-Activate 的 getBalance）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.models import SMSOrder


class SmsProvider(ABC):
    """短信接码 Provider 抽象基类。"""

    @abstractmethod
    def get_number(self, service: str = "dr") -> Optional[SMSOrder]:
        """申领一个手机号。

        Args:
            service: 服务代码（如 'dr' = OpenAI/ChatGPT, 'go' = Google）

        Returns:
            成功返回 SMSOrder（含 order_id + phone_number），失败返回 None
        """

    @abstractmethod
    def get_code(self, order_id: str, max_retries: int = 30) -> Optional[str]:
        """轮询等待短信验证码。

        Args:
            order_id: 上一步 get_number 返回的订单 ID
            max_retries: 最大轮询次数

        Returns:
            验证码字符串，超时返回 None
        """

    def test_connection(self) -> dict:
        """连通性自检（可选）。

        Returns:
            ``{"ok": bool, "message": str, "balance": Optional[float]}``
            子类实现需保证不抛异常 —— 失败也走 ``ok=False`` 返回。

        Raises:
            NotImplementedError: 子类未实现时；test_provider 端点会优雅降级
        """
        raise NotImplementedError("provider 未实现 test_connection")


__all__ = ["SmsProvider"]
