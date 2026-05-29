# -*- coding: utf-8 -*-
"""虚拟卡 Provider 实现子包。

新增卡商 = 在本目录新建 ``<kind>.py``，定义类并加 ``@register_provider`` 装饰器。
启动时 ``ProviderRegistry.discover()`` 会自动 import 完成注册。
"""
