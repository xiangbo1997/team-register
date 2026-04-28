# -*- coding: utf-8 -*-
"""
虚拟卡 Provider 抽象层

定义统一的虚拟卡接口，支持 Efuncard、NodeCard 及未来的其他卡商。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from src.models import BillingInfo, CardInfo

logger = logging.getLogger(__name__)


class CardProvider(ABC):
    """虚拟卡服务抽象基类"""

    @abstractmethod
    def get_card(self, card_key: str) -> Optional[CardInfo]:
        """
        获取可用的虚拟卡信息。

        优先复用已激活且未过期的卡，仅在必要时触发首次激活。

        Args:
            card_key: 卡密 / CDK

        Returns:
            成功返回 CardInfo，失败返回 None
        """

    @abstractmethod
    def cancel_card(self, card_key: str) -> bool:
        """
        销毁/注销卡片。

        Args:
            card_key: 卡密 / CDK

        Returns:
            是否成功
        """

    @abstractmethod
    def get_billing(self, card_key: str) -> Optional[BillingInfo]:
        """
        查询卡片账单和交易信息。

        Args:
            card_key: 卡密 / CDK

        Returns:
            BillingInfo 实例或 None
        """

    @abstractmethod
    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        """
        轮询等待 3DS 验证码。

        Args:
            card_key: 关联的卡密 / CDK
            timeout_sec: 最大等待秒数

        Returns:
            验证码字符串，超时返回 None
        """


class EfunCardProvider(CardProvider):
    """Efuncard 虚拟卡实现。

    Efuncard 的 query/redeem API 设计支持重复调用（已激活卡再 query 仍能拿信息），
    所以**不需要**像 X988 那样做"miss 才回源"的缓存。但仍然会把每次成功 get_card 的
    结果写一份到 ``card_activations`` 表，纯审计用途，让 ``/cards`` 页面可见
    "哪些卡密被用过"。
    """

    def __init__(self, token: str, base_url: str = "https://card.efuncard.com/api/external") -> None:
        from src.efuncard import EfunCard

        self._client = EfunCard(token=token, base_url=base_url)

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        card = self._client.get_card(card_key)
        if card is not None:
            # 审计写入：成功才记，失败/None 不污染表
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


class X988CardProvider(CardProvider):
    """X988 (cards.779.chat / card.988.chat) 虚拟卡实现。

    特点：
    - 一次 POST /api/exchange/verify 拿到全部卡信息
    - 3DS 验证码通过 verify 响应里的 ``sms_api`` URL 自带拉取
    - **verify 是一次性消耗的**，所以本 Provider 包了一层 mem + DB 双级缓存：
      首次 verify 成功后所有信息存 ``card_activations`` 表，下次同一 cdk
      直接拿缓存，不再触发 X988 verify。这是 X988CardProvider 与
      EfunCard / NodeCard 唯一的本质差异。
    """

    def __init__(
        self,
        base_url: str = "https://cards.779.chat",
        request_timeout: int = 15,
    ) -> None:
        from src.x988card import X988Card

        self._client = X988Card(base_url=base_url, request_timeout=request_timeout)
        # L1: 同一 provider 实例的内存缓存（同一任务内 N 次调用避免 N 次 DB 查询）
        # key=card_key, value=(CardInfo, meta_dict)
        self._mem: dict[str, tuple[CardInfo, dict[str, str]]] = {}

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        # L1: 内存命中
        cached = self._mem.get(card_key)
        if cached is not None:
            card, meta = cached
            # 关键：把 sms_api 同步回底层 client._last_meta，
            # 让 wait_for_3ds(x988card.py:178) 能直接拿到
            self._client._last_meta = dict(meta)
            return card

        # L2: DB 缓存 + miss 时回源 X988 verify
        from src.services.card_activation_service import get_or_create_activation

        def _fetcher():
            card_info = self._client.get_card(card_key)
            return card_info, dict(self._client._last_meta or {})

        card, meta = get_or_create_activation(card_key, "x988card", fetcher=_fetcher)
        if card is not None:
            self._mem[card_key] = (card, meta)
            # 即使是 DB 缓存命中（fetcher 没被调），也要回填 _last_meta 让 3DS 拿 sms_api
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
        # 防御：如果 caller 顺序错了（先 wait_for_3ds 后 get_card），
        # 或者 _last_meta 被某种异常清空，从 DB 直接补 sms_api
        if not (self._client._last_meta or {}).get("sms_api"):
            from src.services.card_activation_service import get_sms_api
            sms_api = get_sms_api(card_key)
            if sms_api:
                self._client._last_meta = {"sms_api": sms_api, "phone": ""}
        return self._client.wait_for_3ds(card_key, timeout_sec=timeout_sec)


class NodeCardProvider(CardProvider):
    """NodeCard 虚拟卡实现"""

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
        # NodeCard 暂未发现销卡接口
        logger.warning("NodeCard 暂不支持销卡操作")
        return False

    def get_billing(self, card_key: str) -> Optional[BillingInfo]:
        # 需要将 NodeCard 的交易列表适配到 BillingInfo
        from src.models import BillingInfo, Transaction

        txns = self._client.query_transactions(card_key)
        if not txns:
            return None

        # 构造 BillingInfo (NodeCard 响应中缺失部分字段，用默认值填充)
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
            card_id=0,  # NodeCard 暂无 card_id
            code=card_key,
            transactions=transactions,
            total_spent=sum(t.amount for t in transactions if t.amount < 0),
            remaining_balance=0.0,
        )

    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        return self._client.wait_for_3ds(card_key, timeout_sec=timeout_sec)
