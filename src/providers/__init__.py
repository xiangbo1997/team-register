# -*- coding: utf-8 -*-
"""
Provider 抽象层

定义浏览器、虚拟卡、邮箱等外部服务的统一接口，
支持通过 ProviderRegistry 即插即用地切换实现。
"""

from src.providers.browser import BrowserConnection, BrowserProvider
from src.providers.card import CardProvider
from src.providers.mail import MailProvider, MailSession
from src.providers.registry import ProviderRegistry

__all__ = [
    "BrowserConnection",
    "BrowserProvider",
    "CardProvider",
    "MailProvider",
    "MailSession",
    "ProviderRegistry",
]
