# -*- coding: utf-8 -*-
"""ProviderAdapter ABC + 模板方法 fetch_one。

每家供应商 SDK 差异（鉴权方式 / URL 拼接 / 响应解析）都封装在子类里。
模板方法 fetch_one 固定流程：build URL → GET → parse → ipapi 反查国家 → ProxyInfo。

子类只需实现两个抽象方法：
  build_request_url       — 渲染 api_url_template
  build_request_kwargs    — 按 auth_kind 构造 requests 额外参数

parse_response 默认走 src/proxy_clients/parsers.py 的共享实现，
子类如有特殊格式可以重写。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import requests

from src.models import ProxyInfo
from src.proxy_clients.parsers import parse_proxy_response

logger = logging.getLogger(__name__)

# 拉 IP 的超时（秒）。1024 等供应商通常 1-3 秒响应；超过 20 秒大概率配置错误。
_FETCH_TIMEOUT_SEC = 20


class ProviderAdapter(ABC):
    """供应商 SDK 抽象基类。"""

    @abstractmethod
    def build_request_url(
        self, provider: dict[str, Any], *, country: str, num: int = 1
    ) -> str:
        """渲染 api_url_template。

        子类负责：国家映射（country_map）、session 占位符（如 sticky 绕过）、
        num / format 等参数填充。

        Args:
            provider: ProxyProvider 全量 dict（含 api_url_template / country_map 等）
            country: 调用方期望的国家码（ISO alpha-2 大写）
            num: 一次拉几个 IP（本计划暂时只用 1）

        Returns:
            完整可 GET 的 URL
        """

    @abstractmethod
    def build_request_kwargs(self, provider: dict[str, Any]) -> dict[str, Any]:
        """按 auth_kind 构造 requests.get 的额外 kwargs。

        典型返回：{"headers": {...}} / {"auth": (user, pwd)} / {}
        """

    def parse_response(
        self, text: str, response_format: str
    ) -> list[tuple[str, str]]:
        """默认共享解析。特殊响应格式（如 XML）的子类可重写。"""
        return parse_proxy_response(text, response_format)

    def fetch_one(
        self, provider: dict[str, Any], *, country: str
    ) -> Optional[ProxyInfo]:
        """模板方法：拉一个真实代理 IP。

        流程：build URL → requests.get → parse_response → ipapi 反查国家
            → 包装为 ProxyInfo。失败统一返 None（不抛异常），由调用方按需重试。

        Args:
            provider: ProxyProvider 全量 dict（必须含 with_secrets=True 的字段）
            country: 期望国家；adapter 内部决定怎么映射到 URL

        Returns:
            ProxyInfo(host, port, country) 或 None
        """
        try:
            url = self.build_request_url(provider, country=country, num=1)
        except (KeyError, ValueError) as exc:
            logger.error(
                "build_request_url 失败 provider=%s country=%s err=%s",
                provider.get("label"), country, exc,
            )
            return None

        kwargs = self.build_request_kwargs(provider)
        try:
            resp = requests.get(url, timeout=_FETCH_TIMEOUT_SEC, **kwargs)
        except requests.RequestException as exc:
            logger.warning(
                "fetch_one HTTP 异常 provider=%s err=%s",
                provider.get("label"), exc,
            )
            return None

        # 200 才进解析；4xx/5xx 直接当失败（不打日志详情避免泄露 URL 凭据）
        if resp.status_code != 200:
            logger.warning(
                "fetch_one 非 200 provider=%s status=%s body[:200]=%r",
                provider.get("label"), resp.status_code, resp.text[:200],
            )
            return None

        try:
            hosts = self.parse_response(resp.text, provider["response_format"])
        except ValueError as exc:
            logger.error("parse_response 失败 provider=%s err=%s", provider.get("label"), exc)
            return None

        if not hosts:
            logger.warning(
                "fetch_one 响应解析为空 provider=%s body[:200]=%r",
                provider.get("label"), resp.text[:200],
            )
            return None

        host, port = hosts[0]
        # 反查真实出口国家（复用 src/browser.py 的 _lookup_proxy_country）
        # 失败时 country="", 由调用方按 country_map 判断
        from src.browser import _lookup_proxy_country
        actual_country = _lookup_proxy_country(host, port)

        return ProxyInfo(host=host, port=port, country=actual_country)
