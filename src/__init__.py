# -*- coding: utf-8 -*-
"""
team-register 核心模块包

提供 EfunCard、SMSManager、MailManager 等业务模块的统一导出。
"""

from src.config import AppConfig, load_config
from src.models import CardInfo, ProxyInfo, SMSOrder
from src.efuncard import EfunCard
from src.nodecard import NodeCard
from src.sms import SMSManager
from src.mail import MailManager
from src.browser import fetch_proxy, get_browser_ws
from src.utils import human_delay, setup_logger
from src.automation import (
    ArtifactRecorder,
    AutomationRuntime,
    AutomationState,
    LLMDecisionProvider,
    OpenAICompatibleLLMClient,
    RegistrationStateMachine,
)

__all__ = [
    "AppConfig",
    "load_config",
    "CardInfo",
    "ProxyInfo",
    "SMSOrder",
    "EfunCard",
    "NodeCard",
    "SMSManager",
    "MailManager",
    "fetch_proxy",
    "get_browser_ws",
    "human_delay",
    "setup_logger",
    "ArtifactRecorder",
    "AutomationRuntime",
    "AutomationState",
    "LLMDecisionProvider",
    "OpenAICompatibleLLMClient",
    "RegistrationStateMachine",
]
