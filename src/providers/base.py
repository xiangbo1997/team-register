# -*- coding: utf-8 -*-
"""Provider 元数据与自注册装饰器。

设计目标：让"新增一个供应商"=新建一个文件 + 加 @register_provider 装饰器，
**零核心代码改动**即可在 UI/任务运行时立即可用。

关键类：
- ``FieldSpec``：单个配置字段的元数据，前端按此动态渲染表单
- ``ProviderMeta``：provider 类的全部元数据（类型/kind/schema 等）
- ``register_provider``：装饰器，import 时把类挂到 ProviderRegistry 单例

约束：
- ``provider_type`` 与 DB 表 ``provider_configs.provider_type`` 对齐
  （browser / card / mail / sms / captcha / llm）
- ``kind`` 与 ``provider_configs.provider_name`` 对齐（efuncard / cfworker / ...）
- schema 字段名 = provider 类 ``__init__`` 形参名，registry build() 会按 schema 过滤
  config 字典传给构造器
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldSpec:
    """单个 provider 配置字段的元数据。

    前端按此动态渲染表单；后端按此校验必填字段；registry.build() 按此过滤
    传给构造器的 kwargs。

    ``aliases`` 用于兼容 DB 里历史的字段名 —— 比如 EfunCardProvider 构造器
    收 ``token``，但 DB 旧数据里写的是 ``efuncard_token``，加 alias 后 registry
    会自动把 alias 映射到正式 ``name``。
    """

    name: str
    type: str = "str"  # 'str' | 'int' | 'bool' | 'secret'
    required: bool = False
    default: Any = None
    description: str = ""
    # 可选枚举值，前端渲染为 <select>
    choices: tuple[str, ...] = field(default_factory=tuple)
    # 历史/别名字段，registry 构造时会把 alias key 重命名为 name
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProviderMeta:
    """Provider 类的元数据。"""

    provider_type: str
    kind: str
    display_name: str
    schema: tuple[FieldSpec, ...] = field(default_factory=tuple)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化给前端用。"""
        return {
            "provider_type": self.provider_type,
            "kind": self.kind,
            "display_name": self.display_name,
            "description": self.description,
            "schema": [
                {
                    "name": f.name,
                    "type": f.type,
                    "required": f.required,
                    "default": f.default,
                    "description": f.description,
                    "choices": list(f.choices),
                }
                for f in self.schema
            ],
        }


def register_provider(
    *,
    provider_type: str,
    kind: str,
    display_name: str,
    schema: tuple[FieldSpec, ...] = (),
    description: str = "",
):
    """类装饰器：把 provider 类挂到 ProviderRegistry 单例。

    用法：
        @register_provider(
            provider_type="card",
            kind="x988card",
            display_name="X988 Card",
            schema=(FieldSpec("base_url", required=True), ...),
        )
        class X988CardProvider(CardProvider):
            def __init__(self, base_url, ...): ...
    """

    def deco(cls):
        meta = ProviderMeta(
            provider_type=provider_type,
            kind=kind,
            display_name=display_name,
            schema=tuple(schema),
            description=description,
        )
        cls.PROVIDER_META = meta
        # 延迟 import 避免循环依赖
        from src.providers.registry import ProviderRegistry

        ProviderRegistry.instance().register_class(cls)
        return cls

    return deco


# 类型提示用：所有被装饰过的 provider 类都有 PROVIDER_META 属性
class _HasProviderMeta:
    PROVIDER_META: ClassVar[ProviderMeta]
