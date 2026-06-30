# -*- coding: utf-8 -*-
"""AdsPower 反检测浏览器 provider。"""

from __future__ import annotations

import logging
from typing import Optional

from src.models import ProxyInfo
from src.providers.base import FieldSpec, register_provider
from src.providers.browser import BrowserConnection, BrowserProvider

logger = logging.getLogger(__name__)


@register_provider(
    provider_type="browser",
    kind="adspower",
    display_name="AdsPower",
    description="AdsPower 反检测浏览器（CDP 接入）",
    schema=(
        FieldSpec(
            name="api_url",
            type="str",
            required=True,
            default="http://local.adspower.net:50325",
            description="AdsPower local API base URL",
        ),
        FieldSpec(
            name="api_key",
            type="secret",
            required=False,
            description="AdsPower API key（v2 API 必填）",
        ),
        FieldSpec(
            name="ads_retries",
            type="int",
            required=False,
            default=3,
            description="启动失败重试次数",
        ),
    ),
)
class AdsPowerProvider(BrowserProvider):
    """AdsPower 反检测浏览器实现。"""

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
