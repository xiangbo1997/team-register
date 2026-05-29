# -*- coding: utf-8 -*-
"""虚拟卡 Provider 抽象基类。

实现类已拆到 ``src/providers/cards/`` 子目录，每个文件用 ``@register_provider``
装饰器自注册到 ``ProviderRegistry``。本文件仅保留：
- ``CardProvider`` ABC（所有卡 provider 的基类）
- 兼容性 re-export（旧代码 ``from src.providers.card import EfunCardProvider``
  仍然可用，避免一次性大爆炸）
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from src.models import BillingInfo, CardInfo

logger = logging.getLogger(__name__)


class CardProvider(ABC):
    """虚拟卡服务抽象基类。"""

    @abstractmethod
    def get_card(self, card_key: str) -> Optional[CardInfo]:
        """获取可用的虚拟卡信息。优先复用已激活且未过期的卡。"""

    @abstractmethod
    def cancel_card(self, card_key: str) -> bool:
        """销毁/注销卡片。"""

    @abstractmethod
    def get_billing(self, card_key: str) -> Optional[BillingInfo]:
        """查询卡片账单和交易信息。"""

    @abstractmethod
    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        """轮询等待 3DS 验证码。"""


# ── 兼容 re-export（旧 import 路径继续可用） ────────────
# 子目录加载顺序由 pkgutil 决定，这里显式 import 触发装饰器并暴露符号
from src.providers.cards.efuncard import EfunCardProvider  # noqa: E402,F401
from src.providers.cards.nodecard import NodeCardProvider  # noqa: E402,F401
from src.providers.cards.x988card import X988CardProvider  # noqa: E402,F401
