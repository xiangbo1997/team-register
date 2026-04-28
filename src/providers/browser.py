# -*- coding: utf-8 -*-
"""
浏览器 Provider 抽象层

定义统一的浏览器连接接口，支持 AdsPower 及未来的 BitBrowser / Multilogin 等。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from src.models import ProxyInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserConnection:
    """浏览器连接信息"""
    ws_url: str
    proxy: Optional[ProxyInfo] = None


class BrowserProvider(ABC):
    """浏览器服务抽象基类"""

    @abstractmethod
    def connect(self, profile_id: str, proxy: Optional[ProxyInfo] = None) -> BrowserConnection:
        """
        启动浏览器并返回连接信息。

        Args:
            profile_id: 浏览器配置文件 ID
            proxy: 可选代理配置

        Returns:
            BrowserConnection 包含 WebSocket URL 和代理信息

        Raises:
            ConnectionError: 连接失败
        """

    @abstractmethod
    def preflight(self, target_url: str = "https://chatgpt.com/", proxy_url: str = "") -> None:
        """
        连通性预检查。

        Args:
            target_url: 目标站点 URL
            proxy_url: 可选代理 URL

        Raises:
            ConnectionError: 预检查失败
        """

    def disconnect(self, profile_id: str) -> None:
        """关闭浏览器（默认空实现，子类按需覆盖）。"""


class AdsPowerProvider(BrowserProvider):
    """AdsPower 反检测浏览器实现"""

    def __init__(self, api_url: str, api_key: str = "", ads_retries: int = 3) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._ads_retries = ads_retries

    def connect(self, profile_id: str, proxy: Optional[ProxyInfo] = None) -> BrowserConnection:
        from src.browser import get_browser_ws

        ws_url = get_browser_ws(
            ads_api=self._api_url,
            user_id=profile_id,
            api_key=self._api_key,
            proxy=proxy,
        )
        return BrowserConnection(ws_url=ws_url, proxy=proxy)

    def preflight(self, target_url: str = "https://chatgpt.com/", proxy_url: str = "") -> None:
        from src.browser import run_preflight_checks

        run_preflight_checks(
            ads_api=self._api_url,
            target_url=target_url,
            proxy_url=proxy_url,
            ads_retries=self._ads_retries,
        )
