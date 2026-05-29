# -*- coding: utf-8 -*-
"""X988 虚拟卡 provider（cards.779.chat / card.988.chat）。

特点：
- 一次 POST /api/exchange/verify 拿到全部卡信息
- 3DS 验证码通过 verify 响应里的 ``sms_api`` URL 自带拉取
- **verify 是一次性消耗的**，所以本 Provider 包了一层 mem + DB 双级缓存：
  首次 verify 成功后所有信息存 ``card_activations`` 表，下次同一 cdk
  直接拿缓存，不再触发 X988 verify。这是 X988CardProvider 与
  EfunCard / NodeCard 唯一的本质差异。
"""

from __future__ import annotations

import logging
from typing import Optional

from src.models import BillingInfo, CardInfo
from src.providers.base import FieldSpec, register_provider
from src.providers.card import CardProvider

logger = logging.getLogger(__name__)


@register_provider(
    provider_type="card",
    kind="x988card",
    display_name="X988 Card (一次性 verify + 缓存)",
    description=(
        "支持 cards.779.chat / card.988.chat，verify 一次性消耗，"
        "本 provider 自带 mem+DB 双缓存"
    ),
    schema=(
        FieldSpec(
            name="base_url",
            type="str",
            required=False,
            default="https://cards.779.chat",
            description="X988 API base URL",
        ),
        FieldSpec(
            name="request_timeout",
            type="int",
            required=False,
            default=15,
            description="HTTP 请求超时（秒）",
        ),
    ),
)
class X988CardProvider(CardProvider):
    """X988 虚拟卡实现。"""

    def __init__(
        self,
        base_url: str = "https://cards.779.chat",
        request_timeout: int = 15,
    ) -> None:
        from src.x988card import X988Card

        self._client = X988Card(base_url=base_url, request_timeout=request_timeout)
        # L1: 同一 provider 实例的内存缓存
        self._mem: dict[str, tuple[CardInfo, dict[str, str]]] = {}

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        cached = self._mem.get(card_key)
        if cached is not None:
            card, meta = cached
            self._client._last_meta = dict(meta)
            return card

        from src.services.card_activation_service import get_or_create_activation

        def _fetcher():
            card_info = self._client.get_card(card_key)
            return card_info, dict(self._client._last_meta or {})

        card, meta = get_or_create_activation(card_key, "x988card", fetcher=_fetcher)
        if card is not None:
            self._mem[card_key] = (card, meta)
            self._client._last_meta = dict(meta)
        return card

    def cancel_card(self, card_key: str) -> bool:
        return self._client.cancel_card(card_key)

    def get_billing(self, card_key: str) -> Optional[BillingInfo]:
        # X988 verify 已返回账单地址，但未提供交易流水接口；保持 None。
        return None

    @property
    def last_lookup_meta(self) -> dict:
        """透传底层 X988Card.last_lookup_meta，main.py 的 ExperienceStore 用这个查 verify 状态。"""
        return getattr(self._client, "last_lookup_meta", {}) or {}

    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        if not (self._client._last_meta or {}).get("sms_api"):
            from src.services.card_activation_service import get_sms_api
            sms_api = get_sms_api(card_key)
            if sms_api:
                self._client._last_meta = {"sms_api": sms_api, "phone": ""}
        return self._client.wait_for_3ds(card_key, timeout_sec=timeout_sec)
