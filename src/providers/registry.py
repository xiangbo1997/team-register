# -*- coding: utf-8 -*-
"""
Provider 注册表

集中管理所有 Provider 实例，支持按名称查找和热插拔。
"""

from __future__ import annotations

import logging
from typing import Optional

from src.providers.browser import BrowserProvider
from src.providers.card import CardProvider
from src.providers.mail import MailProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    Provider 统一注册表。

    按类别（browser / card / mail）注册和查找 Provider 实例，
    支持运行时动态注册新实现。
    """

    def __init__(self) -> None:
        self._browsers: dict[str, BrowserProvider] = {}
        self._cards: dict[str, CardProvider] = {}
        self._mails: dict[str, MailProvider] = {}

    # ── 注册 ──────────────────────────────────────────

    def register_browser(self, name: str, provider: BrowserProvider) -> None:
        self._browsers[name] = provider
        logger.info("注册浏览器 Provider: %s", name)

    def register_card(self, name: str, provider: CardProvider) -> None:
        self._cards[name] = provider
        logger.info("注册虚拟卡 Provider: %s", name)

    def register_mail(self, name: str, provider: MailProvider) -> None:
        self._mails[name] = provider
        logger.info("注册邮件 Provider: %s", name)

    # ── 查找 ──────────────────────────────────────────

    def get_browser(self, name: str) -> Optional[BrowserProvider]:
        return self._browsers.get(name)

    def get_card(self, name: str) -> Optional[CardProvider]:
        return self._cards.get(name)

    def get_mail(self, name: str) -> Optional[MailProvider]:
        return self._mails.get(name)

    # ── 列举 ──────────────────────────────────────────

    def list_browsers(self) -> list[str]:
        return list(self._browsers.keys())

    def list_cards(self) -> list[str]:
        return list(self._cards.keys())

    def list_mails(self) -> list[str]:
        return list(self._mails.keys())
