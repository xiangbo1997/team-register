# -*- coding: utf-8 -*-
"""HeroSMS 接码 Provider（hero-sms.com，SMS-Activate 协议兼容）。

HeroSMS 官方文档声明与 SMS-Activate 完全兼容（「将主机从 api.sms-activate.ae
替换为 hero-sms.com」），请求/响应字符串（ACCESS_NUMBER / STATUS_OK / NO_NUMBERS /
ACCESS_BALANCE）、国家码、服务码体系全部一致，唯一差异是 base URL。

因此本类直接子类化 SmsActivateProvider，复用其全部价格降级 / 国家链 / operator
互斥 / test_connection 自检逻辑，只覆写：
- ``BASE_URL``：指向 hero-sms.com 的兼容端点（注意是 handler_api.php，不是 steward.php）
- ``CURRENCY_LABEL``：HeroSMS 默认货币 USD（影响余额自检文案）

schema 用 ``build_sms_activate_schema`` 工厂构造（兼容族字段一致，仅 api_key 文案
与货币单位不同），避免与 sms_activate 重复维护 9 个 FieldSpec。

注意：``@register_provider`` 是类装饰器，不随继承传递，子类必须挂自己的装饰器
（kind=hero_sms）才会注册到 ProviderRegistry。父类的 (sms, sms_activate) 注册不受影响。
"""

from __future__ import annotations

from src.providers.base import register_provider
from src.providers.sms.sms_activate import (
    SmsActivateProvider,
    build_sms_activate_schema,
)


@register_provider(
    provider_type="sms",
    kind="hero_sms",
    display_name="HeroSMS 接码平台",
    description="对接 hero-sms.com（SMS-Activate 协议兼容），支持价格区间筛选 + 多国家降级",
    schema=build_sms_activate_schema(
        api_key_desc="HeroSMS API key（去 hero-sms.com 后台获取）",
        price_currency="平台账户货币，HeroSMS 默认 USD",
    ),
)
class HeroSmsProvider(SmsActivateProvider):
    """HeroSMS 适配器：协议与 SMS-Activate 一致，仅端点与货币不同。"""

    # SMS-Activate 兼容端点（注意 handler_api.php，与 sms-activate 的 steward.php 不同）
    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    CURRENCY_LABEL = "USD"
