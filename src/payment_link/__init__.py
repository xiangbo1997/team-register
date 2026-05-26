# -*- coding: utf-8 -*-
"""src.payment_link 包入口。

向后兼容：保留所有原 src/payment_link.py 单文件的公开符号路径：
- `from src.payment_link import PaymentLinkGenerator`
- `@patch("src.payment_link.requests.post")` （测试 mock 路径）
- `from src.payment_link import logger` 等

包内分层：
- schemas/        每个 (plan, version) 一个文件，纯函数构造 payload
- aimizy.py       aimizy 中转客户端（仅 Team 工作）
- url_postprocess.py  响应解析 + locale 注入
- client.py       主客户端 PaymentLinkGenerator
"""
from __future__ import annotations

import logging

# 暴露 requests 模块，让测试 @patch("src.payment_link.requests.post") 仍能命中
# （curl_cffi 的 requests 不是 stdlib，behaves like requests 但接受 impersonate=）
from curl_cffi import requests  # noqa: F401  re-export for test patching

# 加载所有 schema 模块（触发 @register 副作用）
from .schemas import _load_all as _load_schemas

_load_schemas()

# 主类（外部入口）
from .client import PaymentLinkGenerator  # noqa: E402  re-export

# 包级别 logger（与原 module 同名，外部用 logging.getLogger("src.payment_link") 仍命中）
logger = logging.getLogger(__name__)


__all__ = [
    "PaymentLinkGenerator",
    "requests",
    "logger",
]
