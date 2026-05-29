# -*- coding: utf-8 -*-
"""ProviderAdapter 工厂注册表。

按 ProxyProvider.kind 字段分发到具体 adapter 子类。新增供应商：
  1. 在 src/proxy_clients/adapters/ 新建 <kind>.py 实现 ProviderAdapter
  2. 在本文件 _REGISTRY 注册
  3. 在 src/services/proxy_provider_service.py:_SUPPORTED_KINDS 和
     get_supported_kinds() 添加元信息

注意保持 _REGISTRY 与 proxy_provider_service._SUPPORTED_KINDS 一致；
两边都修不会有自动检查，靠 PR review。
"""

from __future__ import annotations

from src.proxy_clients.adapters.base import ProviderAdapter
from src.proxy_clients.adapters.generic_http import GenericHttpAdapter
from src.proxy_clients.adapters.proxy1024 import Proxy1024Adapter


_REGISTRY: dict[str, ProviderAdapter] = {
    "1024proxy": Proxy1024Adapter(),
    "generic_http": GenericHttpAdapter(),
}


def get_adapter(kind: str) -> ProviderAdapter:
    """按 kind 取 adapter；未注册的 kind 抛 ValueError。"""
    safe_kind = (kind or "").strip().lower()
    if safe_kind not in _REGISTRY:
        raise ValueError(
            f"未注册的 provider kind={kind!r}；"
            f"支持: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[safe_kind]


def list_registered_kinds() -> list[str]:
    """供运维/测试用，列出当前已注册的 kind。"""
    return sorted(_REGISTRY.keys())
