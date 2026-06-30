# -*- coding: utf-8 -*-
"""GrizzlySMS 接码 Provider（grizzlysms.com，SMS-Activate 协议兼容）。

GrizzlySMS 走标准 SMS-Activate 兼容端点 ``/stubs/handler_api.php``，getNumber /
getStatus / getPrices / getBalance 动作与响应字符串（ACCESS_NUMBER / STATUS_OK /
NO_NUMBERS / ACCESS_BALANCE）全部一致，唯一差异是 base URL 与账户货币。

实现与 HeroSMS 同款：子类化 SmsActivateProvider，仅覆写 BASE_URL / CURRENCY_LABEL，
schema 复用 ``build_sms_activate_schema`` 工厂。价格降级 / 国家链 / operator 互斥 /
test_connection 自检逻辑全部继承自父类。

参考来源：GuJumpgate ``phone-sms/providers/grizzlysms.js``（DEFAULT_BASE_URL =
https://api.grizzlysms.com/stubs/handler_api.php）。
"""

from __future__ import annotations

from src.providers.base import register_provider
from src.providers.sms.sms_activate import (
    SmsActivateProvider,
    build_sms_activate_schema,
)


@register_provider(
    provider_type="sms",
    kind="grizzly_sms",
    display_name="GrizzlySMS 接码平台",
    description="对接 grizzlysms.com（SMS-Activate 协议兼容），支持价格区间筛选 + 多国家降级",
    schema=build_sms_activate_schema(
        api_key_desc="GrizzlySMS API key（去 grizzlysms.com 后台获取）",
        price_currency="平台账户货币，GrizzlySMS 默认 RUB",
    ),
)
class GrizzlySmsProvider(SmsActivateProvider):
    """GrizzlySMS 适配器：协议与 SMS-Activate 一致，仅端点与货币不同。"""

    BASE_URL = "https://api.grizzlysms.com/stubs/handler_api.php"
    CURRENCY_LABEL = "RUB"
