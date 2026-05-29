# -*- coding: utf-8 -*-
"""DynamicProxyPool — "每 N 条请求换一次 IP" 的轮换状态机。

主要消费方：src/services/code_discovery_service.py（promo 探索任务）

使用流程：
    provider = get_provider(provider_id, with_secrets=True)
    pool = DynamicProxyPool(provider=provider, country='US')
    for code in candidate_codes:
        proxy_url, country = pool.next_proxy_url()
        result = check_eligibility(..., proxy_url=proxy_url)
        if result.http_status == 403:
            pool.force_rotate(reason='403')

线程不安全：discover 任务本就单线程串行扫码；并发场景需要调用方加锁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from src.models import ProxyInfo

logger = logging.getLogger(__name__)


@dataclass
class DynamicProxyPool:
    """动态代理池：达到轮换阈值或被强制时换 IP。

    Attributes:
        provider: ProxyProvider 全量 dict（必须含 with_secrets=True 的字段）
        country: 期望国家码（cross_matrix 模式时不同国家用不同池实例）
    """

    provider: dict[str, Any]
    country: str = "Rand"

    _current_proxy_info: Optional[ProxyInfo] = field(default=None, init=False)
    _used_count: int = field(default=0, init=False)
    _total_rotations: int = field(default=0, init=False)
    _total_403_rotations: int = field(default=0, init=False)
    _last_rotation_failed: bool = field(default=False, init=False)

    def next_proxy_url(self) -> tuple[str, str]:
        """返回当前可用 (proxy_url, country_iso)。

        - 首次调用或达到 rotation_per_n_requests 阈值时自动轮换
        - 轮换失败时 (_current_proxy_info=None) 返回 ("", "")，调用方应判空降级

        Returns:
            (proxy_url, country_iso)；proxy_url 形如 "http://1.2.3.4:8080"
        """
        threshold = int(self.provider.get("rotation_per_n_requests") or 50)
        if self._current_proxy_info is None or self._used_count >= threshold:
            self._rotate(reason="threshold")
        self._used_count += 1
        if self._current_proxy_info is None:
            return "", ""
        url = (
            f"http://{self._current_proxy_info.host}:"
            f"{self._current_proxy_info.port}"
        )
        return url, self._current_proxy_info.country or ""

    def force_rotate(self, *, reason: str = "manual") -> None:
        """主动触发轮换。403 / 任意 error 后调用方应主动调，立即换 IP 不等阈值。

        Args:
            reason: "403" / "manual" / 任意诊断字符串；"403" 会计入 stats
        """
        if reason == "403":
            self._total_403_rotations += 1
        self._rotate(reason=reason)

    def stats(self) -> dict[str, Any]:
        """供 SSE 推送的可观测指标。"""
        return {
            "total_rotations": self._total_rotations,
            "rotations_by_403": self._total_403_rotations,
            "current_ip_used_count": self._used_count,
            "current_ip": (
                f"{self._current_proxy_info.host}:{self._current_proxy_info.port}"
                if self._current_proxy_info else ""
            ),
            "current_country": (
                self._current_proxy_info.country
                if self._current_proxy_info else ""
            ),
            "last_rotation_failed": self._last_rotation_failed,
        }

    def _rotate(self, *, reason: str) -> None:
        """内部轮换：调 adapter.fetch_one 拉新 IP。失败时保留旧 IP 继续用。"""
        # 延迟 import 避免循环依赖（adapters.base 可能间接引用本文件）
        from src.proxy_clients.adapters.registry import get_adapter

        try:
            adapter = get_adapter(self.provider["kind"])
        except ValueError as exc:
            logger.error(
                "DynamicProxyPool 找不到 adapter provider=%s err=%s",
                self.provider.get("label"), exc,
            )
            self._last_rotation_failed = True
            return

        new_info = adapter.fetch_one(self.provider, country=self.country)
        if new_info is None:
            # 不抛异常：拉失败时保留 _current_proxy_info（也可能是 None 首次失败）
            # 由调用方按 stats 判断是否要重试或降级直连
            logger.error(
                "DynamicProxyPool 轮换失败 provider=%s country=%s reason=%s（保留旧 IP）",
                self.provider.get("label"), self.country, reason,
            )
            self._last_rotation_failed = True
            return

        self._current_proxy_info = new_info
        self._used_count = 0
        self._total_rotations += 1
        self._last_rotation_failed = False
        logger.info(
            "DynamicProxyPool 已轮换 reason=%s ip=%s:%s country=%s "
            "(total=%d, by_403=%d)",
            reason, new_info.host, new_info.port, new_info.country or "?",
            self._total_rotations, self._total_403_rotations,
        )
