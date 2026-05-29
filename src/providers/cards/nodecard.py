# -*- coding: utf-8 -*-
"""NodeCard 虚拟卡 provider。"""

from __future__ import annotations

import logging
from typing import Optional

from src.models import BillingInfo, CardInfo, Transaction
from src.providers.base import FieldSpec, register_provider
from src.providers.card import CardProvider

logger = logging.getLogger(__name__)


@register_provider(
    provider_type="card",
    kind="nodecard",
    display_name="NodeCard",
    description="NodeCard 虚拟卡（无销卡接口，自动写 card_activations 审计表）",
    schema=(
        FieldSpec(
            name="base_url",
            type="str",
            required=False,
            default="https://api.node-card.com",
            description="NodeCard API base URL",
        ),
        FieldSpec(
            name="merchant_dict_id",
            type="int",
            required=False,
            description="NodeCard merchant dict id（可选）",
        ),
        FieldSpec(
            name="platform_id",
            type="int",
            required=False,
            description="NodeCard platform id（可选）",
        ),
    ),
)
class NodeCardProvider(CardProvider):
    """NodeCard 虚拟卡实现。"""

    def __init__(
        self,
        base_url: str = "https://api.node-card.com",
        merchant_dict_id: Optional[int] = None,
        platform_id: Optional[int] = None,
    ) -> None:
        from src.nodecard import NodeCard

        self._client = NodeCard(
            base_url=base_url,
            merchant_dict_id=merchant_dict_id,
            platform_id=platform_id,
        )

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        card = self._client.get_card(card_key)
        if card is not None:
            try:
                from src.services.card_activation_service import record_lookup
                record_lookup(card_key, "nodecard", card)
            except Exception as exc:
                logger.warning("NodeCardProvider: record_lookup 失败（不影响主流程）: %s", exc)
        return card

    def cancel_card(self, card_key: str) -> bool:
        logger.warning("NodeCard 暂不支持销卡操作")
        return False

    def get_billing(self, card_key: str) -> Optional[BillingInfo]:
        txns = self._client.query_transactions(card_key)
        if not txns:
            return None

        transactions = [
            Transaction(
                id=str(t.get("order_id", "")),
                amount=float(t.get("amount", 0)),
                currency=str(t.get("currency", "USD")),
                merchant=str(t.get("merchant_name", "")),
                status=str(t.get("status", "")),
                created_at=str(t.get("create_time", "")),
            )
            for t in txns
        ]
        return BillingInfo(
            card_id=0,
            code=card_key,
            transactions=transactions,
            total_spent=sum(t.amount for t in transactions if t.amount < 0),
            remaining_balance=0.0,
        )

    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        return self._client.wait_for_3ds(card_key, timeout_sec=timeout_sec)
