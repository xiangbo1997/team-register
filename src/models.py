# -*- coding: utf-8 -*-
"""
数据模型定义

使用 dataclass 代替松散的 Dict[str, Any]，提供编译期类型检查支持。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CardInfo:
    """Efuncard 虚拟卡信息"""
    card_number: str
    expiry_month: str
    expiry_year: str
    cvv: str
    name_on_card: str = ""
    status: str = ""
    created_at: str = ""
    auto_cancel_at: str = ""
    billing_address: str = ""

    @classmethod
    def from_api_response(cls, data: dict) -> Optional["CardInfo"]:
        """从 Efuncard API 的原始 dict 构造实例，字段缺失时返回 None"""
        try:
            expiry_year = data.get("expiry_year", data.get("expiryYear"))
            return cls(
                card_number=str(data["cardNumber"]),
                expiry_month=str(data["expiryMonth"]),
                expiry_year=str(expiry_year),
                cvv=str(data["cvv"]),
                name_on_card=str(data.get("nameOnCard", "") or ""),
                status=str(data.get("status", "") or ""),
                created_at=str(data.get("createdAt", "") or ""),
                auto_cancel_at=str(data.get("autoCancelAt", "") or ""),
            )
        except (KeyError, TypeError):
            return None

    @property
    def expiry_display(self) -> str:
        """格式化为 MM/YY 格式，用于页面填写"""
        return f"{int(self.expiry_month):02d}/{self.expiry_year[-2:]}"


@dataclass(frozen=True)
class ProxyInfo:
    """代理服务器信息"""
    host: str
    port: str

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class SMSOrder:
    """SMS-Activate 接码订单"""
    order_id: str
    phone_number: str
