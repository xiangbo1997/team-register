# -*- coding: utf-8 -*-
"""浏览器 Provider 抽象基类。

实现类已拆到 ``src/providers/browsers/`` 子目录。本文件仅保留：
- ``BrowserConnection`` 数据类
- ``BrowserProvider`` ABC
- 兼容 re-export ``AdsPowerProvider``（避免外部 import 一次性崩）
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
    """浏览器连接信息。"""
    ws_url: str
    proxy: Optional[ProxyInfo] = None


class BrowserProvider(ABC):
    """浏览器服务抽象基类。"""

    @abstractmethod
    def connect(self, profile_id: str, proxy: Optional[ProxyInfo] = None) -> BrowserConnection:
        """启动浏览器并返回连接信息。"""

    @abstractmethod
    def preflight(self, target_url: str = "https://chatgpt.com/", proxy_url: str = "") -> None:
        """连通性预检查。"""

    def disconnect(self, profile_id: str) -> None:
        """关闭浏览器（默认空实现，子类按需覆盖）。"""


# ── 兼容 re-export ──────────────────────────────────
from src.providers.browsers.adspower import AdsPowerProvider  # noqa: E402,F401
