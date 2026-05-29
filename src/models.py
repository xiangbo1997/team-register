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
    last_four: str = ""
    name_on_card: str = ""
    status: str = ""
    created_at: str = ""
    auto_cancel_at: str = ""
    billing_address: str = ""
    card_prefix: str = ""
    validity_minutes: int = 0
    is_replace: bool = False
    bin_country: str = ""

    @classmethod
    def from_api_response(cls, data: dict) -> Optional["CardInfo"]:
        """从 Efuncard API 的原始 dict 构造实例，字段缺失时返回 None"""
        try:
            expiry_month = data.get("expiry_month", data.get("expiryMonth"))
            expiry_year = data.get("expiry_year", data.get("expiryYear"))
            billing_address = (
                data.get("billing_address")
                or data.get("billingAddress")
                or data.get("full_billing_address")
                or data.get("fullBillingAddress")
                or data.get("address")
                or ""
            )
            if not billing_address:
                # 兼容拆字段返回：addressLine1/city/state/postalCode/country
                line1 = data.get("addressLine1") or data.get("billingAddressLine1") or ""
                line2 = data.get("addressLine2") or data.get("billingAddressLine2") or ""
                city = data.get("city") or data.get("billingLocality") or ""
                state = data.get("state") or data.get("billingAdministrativeArea") or ""
                postal = data.get("postalCode") or data.get("zip") or data.get("billingPostalCode") or ""
                country = data.get("country") or data.get("billingCountry") or ""
                billing_address = ", ".join(
                    str(part).strip()
                    for part in (line1, line2, city, state, postal, country)
                    if str(part or "").strip()
                )
            return cls(
                card_number=str(data["cardNumber"]),
                expiry_month=str(expiry_month),
                expiry_year=str(expiry_year),
                cvv=str(data["cvv"]),
                last_four=str(data.get("lastFour", "") or ""),
                name_on_card=str(data.get("nameOnCard", "") or ""),
                status=str(data.get("status", "") or ""),
                created_at=str(data.get("createdAt", "") or ""),
                auto_cancel_at=str(data.get("autoCancelAt", "") or ""),
                billing_address=str(billing_address or ""),
                card_prefix=str(data.get("cardPrefix", "") or ""),
                validity_minutes=int(data.get("validityMinutes", 0)),
                is_replace=bool(data.get("isReplace", False)),
            )
        except (KeyError, TypeError):
            return None

    @property
    def expiry_display(self) -> str:
        """格式化为 MM/YY 格式，用于页面填写"""
        return f"{int(self.expiry_month):02d}/{self.expiry_year[-2:]}"


@dataclass(frozen=True)
class Transaction:
    """卡片交易记录"""
    id: str
    amount: float
    currency: str
    merchant: str
    status: str
    created_at: str


@dataclass(frozen=True)
class BillingInfo:
    """卡片账单信息"""
    card_id: int
    code: str
    transactions: list[Transaction]
    total_spent: float
    remaining_balance: float

    @classmethod
    def from_api_response(cls, data: dict) -> Optional["BillingInfo"]:
        """从 API 响应构造账单信息"""
        try:
            transactions = [
                Transaction(
                    id=str(t["id"]),
                    amount=float(t["amount"]),
                    currency=str(t["currency"]),
                    merchant=str(t["merchant"]),
                    status=str(t["status"]),
                    created_at=str(t["createdAt"]),
                )
                for t in data.get("transactions", [])
            ]
            return cls(
                card_id=int(data["cardId"]),
                code=str(data["code"]),
                transactions=transactions,
                total_spent=float(data["totalSpent"]),
                remaining_balance=float(data["remainingBalance"]),
            )
        except (KeyError, TypeError):
            return None


@dataclass(frozen=True)
class ProxyInfo:
    """代理服务器信息"""
    host: str
    port: str
    country: str = ""

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class SMSOrder:
    """SMS-Activate 接码订单"""
    order_id: str
    phone_number: str
