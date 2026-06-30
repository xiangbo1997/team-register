# -*- coding: utf-8 -*-
"""通用 HTTP 代理供应商适配器。

覆盖大部分按 URL 拉 IP + 标准 HTTP 鉴权的供应商：
  - IPRoyal（auth_kind=basic_auth）
  - SmartProxy / Bright Data（部分接口走 api_key）
  - 自建代理池

auth_kind 支持：
  "api_key"    → credentials={"key": ..., "header_name": "X-API-Key"}（默认 header）
  "basic_auth" → credentials={"username": ..., "password": ...}
  "none"       → 无鉴权

URL 模板占位符：{country} {num} {format}（不含 session，因为大部分供应商
不像 1024 那样有 sticky 缓存陷阱）
"""

from __future__ import annotations

import logging
from typing import Any

from src.proxy_clients.adapters.base import ProviderAdapter

logger = logging.getLogger(__name__)


class GenericHttpAdapter(ProviderAdapter):
    """通用 HTTP 代理供应商。"""

    def build_request_url(
        self, provider: dict[str, Any], *, country: str, num: int = 1
    ) -> str:
        country_map = provider.get("country_map") or {}
        effective_country = (country or "").strip().upper()
        mapped = (
            country_map.get(effective_country)
            or country_map.get("Default")
            or effective_country
            or provider.get("default_country")
            or ""
        )
        # 允许 URL 模板不含某些占位符（dict-style format 不会因缺占位符报错）
        # 但仍兜底 {session} 给 ""，避免某些用户复制 1024 风格模板时 KeyError
        try:
            return provider["api_url_template"].format(
                country=mapped,
                num=num,
                format=1,
                session="",
            )
        except KeyError as exc:
            raise ValueError(
                f"URL 模板含未支持占位符 {exc}；"
                f"generic_http 支持: {{country}} {{num}} {{format}}"
            ) from exc

    def build_request_kwargs(self, provider: dict[str, Any]) -> dict[str, Any]:
        auth_kind = (provider.get("auth_kind") or "none").lower()
        creds = provider.get("credentials") or {}

        if auth_kind == "api_key":
            header_name = (creds.get("header_name") or "X-API-Key").strip()
            key_value = str(creds.get("key") or "")
            return {"headers": {header_name: key_value}}

        if auth_kind == "basic_auth":
            return {
                "auth": (
                    str(creds.get("username") or ""),
                    str(creds.get("password") or ""),
                )
            }

        # "none" 或 "ip_whitelist"（generic 允许配 ip_whitelist 但不会发任何 header）
        return {}
