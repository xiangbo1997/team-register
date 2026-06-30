# -*- coding: utf-8 -*-
"""SMS-Verification-Number 接码 Provider（sms-verification-number.com，SMS-Activate 协议兼容）。

走标准 SMS-Activate 兼容端点 ``/stubs/handler_api``（注意：无 ``.php`` 后缀，与
hero-sms 的 handler_api.php 略有差别，但动作与响应协议一致）。getNumber / getStatus /
getPrices / getBalance 动作与响应字符串（ACCESS_NUMBER / STATUS_OK / NO_NUMBERS /
ACCESS_BALANCE）全部一致，唯一差异是 base URL 与账户货币。

实现与 HeroSMS 同款：子类化 SmsActivateProvider，仅覆写 BASE_URL / CURRENCY_LABEL，
schema 复用 ``build_sms_activate_schema`` 工厂。

参考来源：GuJumpgate ``phone-sms/providers/sms-verification-number.js``
（DEFAULT_BASE_URL = https://sms-verification-number.com/stubs/handler_api）。
"""

from __future__ import annotations

from src.providers.base import register_provider
from src.providers.sms.sms_activate import (
    SmsActivateProvider,
    build_sms_activate_schema,
)


@register_provider(
    provider_type="sms",
    kind="sms_verification_number",
    display_name="SMS-Verification-Number 接码平台",
    description="对接 sms-verification-number.com（SMS-Activate 协议兼容），支持价格区间筛选 + 多国家降级",
    schema=build_sms_activate_schema(
        api_key_desc="SMS-Verification-Number API key（去 sms-verification-number.com 后台获取）",
        price_currency="平台账户货币，默认 USD",
    ),
)
class SmsVerificationNumberProvider(SmsActivateProvider):
    """SMS-Verification-Number 适配器：协议与 SMS-Activate 一致，仅端点与货币不同。"""

    # 注意端点是 handler_api（无 .php），与 hero-sms 的 handler_api.php 不同
    BASE_URL = "https://sms-verification-number.com/stubs/handler_api"
    CURRENCY_LABEL = "USD"
