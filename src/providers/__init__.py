# -*- coding: utf-8 -*-
"""Provider 抽象层（插拔式架构）。

定义浏览器、虚拟卡、邮箱等外部服务的统一接口。
新增 provider = 在 ``src/providers/{browsers,cards,mails}/`` 新建一个文件
+ 加 ``@register_provider`` 装饰器，**无需修改本文件或任何核心代码**。

公共导出：
- 抽象基类：BrowserProvider / CardProvider / MailProvider
- 注册装饰器：register_provider / FieldSpec / ProviderMeta
- 注册表：ProviderRegistry / get_registry
- 异常：ProviderNotRegisteredError / ProviderConfigInvalidError
"""

from src.providers.base import (
    FieldSpec,
    ProviderMeta,
    register_provider,
)
from src.providers.browser import BrowserConnection, BrowserProvider
from src.providers.card import CardProvider
from src.providers.mail import (
    HttpMailProvider,
    MailProvider,
    MailRuntimeIncompatibleError,
    MailServiceError,
    MailSession,
    MissingProviderConfigError,
    ProviderUpstreamError,
)
from src.providers.registry import (
    ProviderConfigInvalidError,
    ProviderNotRegisteredError,
    ProviderRegistry,
    get_registry,
)
from src.providers.sms import SmsProvider

# 启动时一次性发现所有 provider 子包内自注册类
# 注意：这里只 import 不实例化 —— 实例化要等 worker 拿到 config 才做
get_registry().discover()


__all__ = [
    # 抽象基类
    "BrowserConnection",
    "BrowserProvider",
    "CardProvider",
    "HttpMailProvider",
    "MailProvider",
    "MailSession",
    "SmsProvider",
    # 异常
    "MailServiceError",
    "MailRuntimeIncompatibleError",
    "MissingProviderConfigError",
    "ProviderConfigInvalidError",
    "ProviderNotRegisteredError",
    "ProviderUpstreamError",
    # 注册基础设施
    "FieldSpec",
    "ProviderMeta",
    "ProviderRegistry",
    "get_registry",
    "register_provider",
]
