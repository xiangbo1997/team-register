# -*- coding: utf-8 -*-
"""1024Proxy 供应商适配器。

关键特性：
  - 白名单鉴权（auth_kind=ip_whitelist）：URL 不含凭据，运维提前在 1024 后台
    把服务器出口 IP 加白名单
  - Sticky IP 陷阱：1024 默认 time=10（10 分钟）窗口内同 URL 返回相同 IP；
    "每 50 条扫描换 IP" 想真生效必须每次请求生成新 {session}，让 1024 当新
    会话处理 —— 否则连续 50 次拉同一个 IP，轮换形同虚设
  - 国家码映射：项目用 ISO alpha-2（GB/US/JP），1024 部分国家用别的（UK 而非 GB）；
    country_map 字段处理映射
  - 响应：txt 单行 host:port 或 json 数组，按 provider.response_format 决定

URL 模板期望含占位符：{country} {num} {format} {session}
示例：
  https://white.1024proxy.com/white/api?region={country}&num={num}&time=10&format=1&type=txt&session={session}
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.proxy_clients.adapters.base import ProviderAdapter

logger = logging.getLogger(__name__)


class Proxy1024Adapter(ProviderAdapter):
    """1024Proxy（IP 白名单鉴权 + Sticky 绕过）。"""

    def build_request_url(
        self, provider: dict[str, Any], *, country: str, num: int = 1
    ) -> str:
        country_map = provider.get("country_map") or {}
        # 项目码 → 供应商码；找不到时回退到 default_country；再没有就 Rand
        effective_country = (country or "").strip().upper()
        mapped = (
            country_map.get(effective_country)
            or country_map.get("Default")
            or effective_country
            or provider.get("default_country")
            or "Rand"
        )
        # 强制每次新 session，绕过 1024 的 Sticky IP 缓存
        # 否则 time=10 窗口内同 URL 始终返回相同 IP，"轮换" 失效
        session = uuid.uuid4().hex[:12]
        try:
            return provider["api_url_template"].format(
                country=mapped,
                num=num,
                format=1,
                session=session,
            )
        except KeyError as exc:
            raise ValueError(
                f"1024 URL 模板缺少占位符 {exc}；"
                f"必须包含 {{country}} {{num}} {{format}} {{session}}"
            ) from exc

    def build_request_kwargs(self, provider: dict[str, Any]) -> dict[str, Any]:
        """1024 白名单鉴权不需要任何 header / auth。"""
        return {}
