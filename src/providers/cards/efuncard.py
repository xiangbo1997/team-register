# -*- coding: utf-8 -*-
"""Efuncard 虚拟卡 provider。

Efuncard 的 query/redeem API 设计支持重复调用（已激活卡再 query 仍能拿信息），
所以**不需要**像 X988 那样做"miss 才回源"的缓存。但仍然会把每次成功 get_card 的
结果写一份到 ``card_activations`` 表，纯审计用途，让 ``/cards`` 页面可见
"哪些卡密被用过"。
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
    kind="efuncard",
    display_name="Efuncard",
    description="Efuncard 虚拟卡（API 防重，自动写 card_activations 审计表）",
    schema=(
        FieldSpec(
            name="token",
            type="secret",
            required=True,
            description="Efuncard 外部 API token",
            aliases=("efuncard_token",),
        ),
        FieldSpec(
            name="base_url",
            type="str",
            required=False,
            default="https://card.efuncard.com/api/external",
            description="Efuncard API base URL",
        ),
    ),
)
class EfunCardProvider(CardProvider):
    """Efuncard 虚拟卡实现。"""

    def __init__(
        self,
        token: str,
        base_url: str = "https://card.efuncard.com/api/external",
    ) -> None:
        from src.efuncard import EfunCard

        self._client = EfunCard(token=token, base_url=base_url)

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        card = self._client.get_card(card_key)
        if card is not None:
            try:
                from src.services.card_activation_service import record_lookup
                record_lookup(card_key, "efuncard", card)
            except Exception as exc:
                logger.warning("EfunCardProvider: record_lookup 失败（不影响主流程）: %s", exc)
        return card

    def cancel_card(self, card_key: str) -> bool:
        return self._client.cancel(card_key)

    def get_billing(self, card_key: str) -> Optional[BillingInfo]:
        return self._client.billing(card_key)

    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        return self._client.wait_for_3ds(card_key, timeout_sec=timeout_sec)
