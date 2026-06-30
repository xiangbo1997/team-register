# -*- coding: utf-8 -*-
"""SMSBower 接码 Provider（smsbower.page，SMS-Activate 协议兼容）。

SMSBower 走 SMS-Activate 兼容端点 ``/stubs/handler_api.php``，**取号主路径**
（getNumber / getStatus / getBalance）与响应字符串（ACCESS_NUMBER / STATUS_OK /
NO_NUMBERS / ACCESS_BALANCE）一致，因此子类化 SmsActivateProvider 即可复用申号、
国家链降级、test_connection 自检逻辑。

⚠️ 价格筛选限制（与 hero/grizzly 的关键差异）：
SMSBower 的价格查询用增强动作 ``getPricesV3``（参考 GuJumpgate
``phone-sms/providers/smsbower.js``：``DEFAULT_PRICES_ACTION = 'getPricesV3'``），
其返回结构与基础 ``getPrices`` 不同。父类 ``_query_current_price`` 硬编码
``action=getPrices``，在 SMSBower 上可能解析失败返回 None → 触发「该国家跳过」。

**因此使用 SMSBower 时建议把 ``max_price`` / ``min_price`` 留空**，仅靠国家链降级；
否则价格查询失败会导致候选国家被逐一跳过、最终无号可取。schema 仍保留价格字段以
与其他兼容族 provider 表单一致，但其有效性不作保证。

实现：仅覆写 BASE_URL / CURRENCY_LABEL，schema 复用 ``build_sms_activate_schema``。
"""

from __future__ import annotations

from src.providers.base import register_provider
from src.providers.sms.sms_activate import (
    SmsActivateProvider,
    build_sms_activate_schema,
)


@register_provider(
    provider_type="sms",
    kind="sms_bower",
    display_name="SMSBower 接码平台",
    description=(
        "对接 smsbower.page（SMS-Activate 协议兼容）；取号支持多国家降级，"
        "但价格查询走 getPricesV3 与基础协议不兼容，建议价格区间留空"
    ),
    schema=build_sms_activate_schema(
        api_key_desc="SMSBower API key（去 smsbower.page 后台获取）",
        price_currency="平台账户货币，默认 RUB；⚠️SMSBower 价格查询不兼容，建议留空",
    ),
)
class SmsBowerProvider(SmsActivateProvider):
    """SMSBower 适配器：取号协议兼容，价格查询不保证（见模块注释）。"""

    BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    CURRENCY_LABEL = "RUB"
