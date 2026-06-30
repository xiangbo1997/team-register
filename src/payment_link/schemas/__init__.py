# -*- coding: utf-8 -*-
"""Checkout payload schema 注册表。

每个 (plan, version) 对应一个 builder 函数。运行时通过 get_schema() 路由。
新增 schema 时：
  1. 在本目录加 `<plan>_v<n>.py`
  2. 用 @register("<plan>", "v<n>") 装饰 build_payload 函数
  3. 在 `_load_all()` 里 import 该模块（触发装饰器副作用）

设计原则：每个 schema 是纯函数 —— 只构造 dict，不做 HTTP / 不读 DB。
"""
from __future__ import annotations

from typing import Callable, Dict, Tuple

_REGISTRY: Dict[Tuple[str, str], Callable] = {}


def register(plan: str, version: str):
    """装饰器：把 build_payload 函数登记到 (plan, version)。"""
    key = (plan.strip().lower(), version.strip().lower())

    def decorator(fn: Callable) -> Callable:
        if key in _REGISTRY:
            raise RuntimeError(f"schema 重复注册: plan={key[0]} version={key[1]}")
        _REGISTRY[key] = fn
        return fn

    return decorator


def get_schema(plan: str, version: str) -> Callable:
    """根据 plan + 版本号取出 builder。未注册抛 ValueError（不静默回退）。"""
    key = (plan.strip().lower(), version.strip().lower())
    if key not in _REGISTRY:
        available = sorted(f"{p}/{v}" for p, v in _REGISTRY.keys())
        raise ValueError(
            f"未注册的 checkout schema: plan={key[0]} version={key[1]}; "
            f"可用版本: {', '.join(available) or '（空）'}"
        )
    return _REGISTRY[key]


def list_registered() -> list[tuple[str, str]]:
    """返回 [(plan, version), ...]，按字母序排序。"""
    return sorted(_REGISTRY.keys())


def _load_all() -> None:
    """import 所有 schema 模块，触发 @register 副作用。

    在 src.payment_link.__init__ 里调用一次。
    """
    from . import _common  # noqa: F401  共享 helper（无 register）
    from . import plus_v2  # noqa: F401
    from . import team_v1  # noqa: F401
    from . import pro_v1   # noqa: F401
