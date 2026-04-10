# -*- coding: utf-8 -*-
"""
配置管理模块

从环境变量加载配置，并进行必填项校验。
使用 dataclass 提供类型安全的配置访问。
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _read_bool(name: str, default: bool = False) -> bool:
    """从环境变量读取布尔值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int) -> int:
    """从环境变量读取整数，异常时回退默认值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法整数，回退默认值 %d", name, raw, default)
        return default


def _read_float(name: str, default: float) -> float:
    """从环境变量读取浮点数，异常时回退默认值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法浮点数，回退默认值 %.2f", name, raw, default)
        return default


@dataclass
class AppConfig:
    """应用配置 — 所有字段均从环境变量加载"""

    # AdsPower 配置
    ads_api: str = "http://local.adspower.net:50325"
    ads_api_key: str = ""

    # Efuncard 支付配置
    efuncard_token: str = ""

    # NodeCard 支付配置
    nodecard_api_url: str = "https://api.node-card.com"
    nodecard_merchant_id: int = 0
    nodecard_platform_id: int = 0
    card_provider: str = "efuncard"  # "efuncard" 或 "nodecard"

    # SMS-Activate 接码配置
    sms_api_key: str = ""
    sms_country: str = "6"

    # 小苹果邮件服务配置（MAIL_DOMAIN 为历史兼容字段，本地 provider 模式可选）
    mail_domain: str = ""
    mail_refresh_token: str = ""
    mail_client_id: str = ""

    # 测试/任务参数
    task_ads_id: str = ""
    task_cdk: str = ""
    task_email: str = ""
    task_password: str = ""

    # 网络配置
    proxy: str = ""

    # LLM 决策器配置
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_ms: int = 8000
    llm_confidence_threshold: float = 0.6
    llm_max_consecutive_uncertain: int = 2

    # 自动化运行时配置
    enable_payment_flow: bool = True
    payment_plan: str = "team"
    payment_link_only: bool = False
    payment_link_return_mode: str = "long"
    aimizy_country: str = "SG"
    aimizy_currency: str = "SGD"
    billing_country: str = "US"
    billing_line1: str = "350 5th Ave"
    billing_line2: str = ""
    billing_city: str = "New York"
    billing_state: str = "NY"
    billing_postal_code: str = "10118"
    run_artifacts_dir: str = "artifacts/runs"
    trace_on_failure: bool = True
    clean_context_mode: str = "reuse_and_clean"
    max_navigation_retries: int = 2
    max_email_attempts: int = 3
    max_profile_reconnects: int = 2
    max_manual_handoffs: int = 2

    def validate(self, required_modules: Optional[list[str]] = None) -> list[str]:
        """
        校验配置完整性，返回缺失字段列表。

        Args:
            required_modules: 需要校验的模块列表，可选值: 'efuncard', 'sms', 'mail', 'ads', 'task'
                             为 None 时只校验基础字段

        Returns:
            缺失字段名称列表，空列表表示校验通过
        """
        missing: list[str] = []

        # 按模块校验
        module_checks: dict[str, list[tuple[str, str]]] = {
            "efuncard": [("efuncard_token", "EFUNCARD_TOKEN")],
            "sms": [("sms_api_key", "SMS_API_KEY")],
            "mail": [
                ("mail_refresh_token", "MAIL_REFRESH_TOKEN"),
                ("mail_client_id", "MAIL_CLIENT_ID"),
            ],
            "ads": [("ads_api_key", "ADS_API_KEY")],
            "task": [
                ("task_ads_id", "TASK_ADS_ID"),
                ("task_cdk", "TASK_CDK"),
                ("task_email", "TASK_EMAIL"),
                ("task_password", "TASK_PASSWORD"),
            ],
            "llm": [
                ("llm_base_url", "LLM_BASE_URL"),
                ("llm_api_key", "LLM_API_KEY"),
                ("llm_model", "LLM_MODEL"),
            ],
        }

        modules_to_check = required_modules or []
        for module_name in modules_to_check:
            if module_name == "llm" and not self.llm_enabled:
                continue
            checks = module_checks.get(module_name, [])
            for attr_name, env_name in checks:
                if not getattr(self, attr_name, ""):
                    missing.append(env_name)

        return missing

    def build_billing_profile(self) -> dict[str, str]:
        """构造支付页账单资料字典，供 checkout 填表阶段复用。"""
        return {
            "country": str(self.billing_country or "US").strip().upper() or "US",
            "line1": str(self.billing_line1 or "").strip(),
            "line2": str(self.billing_line2 or "").strip(),
            "city": str(self.billing_city or "").strip(),
            "state": str(self.billing_state or "").strip(),
            "postal_code": str(self.billing_postal_code or "").strip(),
        }


def load_config(dotenv_path: Optional[str] = None) -> AppConfig:
    """
    从 .env 文件和环境变量加载配置。

    Args:
        dotenv_path: .env 文件路径，为 None 时自动查找

    Returns:
        填充完毕的 AppConfig 实例
    """
    load_dotenv(dotenv_path=dotenv_path)

    config = AppConfig(
        ads_api=os.getenv("ADS_API", "http://local.adspower.net:50325"),
        ads_api_key=os.getenv("ADS_API_KEY", ""),
        efuncard_token=os.getenv("EFUNCARD_TOKEN", ""),
        nodecard_api_url=os.getenv("NODECARD_API_URL", "https://api.node-card.com"),
        nodecard_merchant_id=_read_int("NODECARD_MERCHANT_ID", 0),
        nodecard_platform_id=_read_int("NODECARD_PLATFORM_ID", 0),
        card_provider=os.getenv("CARD_PROVIDER", "efuncard").strip().lower() or "efuncard",
        sms_api_key=os.getenv("SMS_API_KEY", ""),
        sms_country=os.getenv("SMS_COUNTRY", "6"),
        mail_domain=os.getenv("MAIL_DOMAIN", ""),
        mail_refresh_token=os.getenv("MAIL_REFRESH_TOKEN", ""),
        mail_client_id=os.getenv("MAIL_CLIENT_ID", ""),
        task_ads_id=os.getenv("TASK_ADS_ID", ""),
        task_cdk=os.getenv("TASK_CDK", ""),
        task_email=os.getenv("TASK_EMAIL", ""),
        task_password=os.getenv("TASK_PASSWORD", ""),
        proxy=os.getenv("PROXY", ""),
        llm_enabled=_read_bool("LLM_ENABLED", False),
        llm_base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        llm_timeout_ms=_read_int("LLM_TIMEOUT_MS", 8000),
        llm_confidence_threshold=_read_float("LLM_CONFIDENCE_THRESHOLD", 0.6),
        llm_max_consecutive_uncertain=_read_int("LLM_MAX_CONSECUTIVE_UNCERTAIN", 2),
        enable_payment_flow=_read_bool("ENABLE_PAYMENT_FLOW", True),
        payment_plan=os.getenv("PAYMENT_PLAN", "team").strip().lower() or "team",
        payment_link_only=_read_bool("PAYMENT_LINK_ONLY", False),
        payment_link_return_mode=os.getenv("PAYMENT_LINK_RETURN_MODE", "long").strip().lower() or "long",
        aimizy_country=os.getenv("AIMIZY_COUNTRY", "SG").strip().upper() or "SG",
        aimizy_currency=os.getenv("AIMIZY_CURRENCY", "SGD").strip().upper() or "SGD",
        billing_country=os.getenv("BILLING_COUNTRY", "US").strip().upper() or "US",
        billing_line1=os.getenv("BILLING_LINE1", "350 5th Ave").strip(),
        billing_line2=os.getenv("BILLING_LINE2", "").strip(),
        billing_city=os.getenv("BILLING_CITY", "New York").strip(),
        billing_state=os.getenv("BILLING_STATE", "NY").strip(),
        billing_postal_code=os.getenv("BILLING_POSTAL_CODE", "10118").strip(),
        run_artifacts_dir=os.getenv("RUN_ARTIFACTS_DIR", "artifacts/runs"),
        trace_on_failure=_read_bool("TRACE_ON_FAILURE", True),
        clean_context_mode=os.getenv("CLEAN_CONTEXT_MODE", "reuse_and_clean"),
        max_navigation_retries=_read_int("MAX_NAVIGATION_RETRIES", 2),
        max_email_attempts=_read_int("MAX_EMAIL_ATTEMPTS", 3),
        max_profile_reconnects=_read_int("MAX_PROFILE_RECONNECTS", 2),
        max_manual_handoffs=_read_int("MAX_MANUAL_HANDOFFS", 2),
    )

    logger.debug("配置加载完成: ads_api=%s, sms_country=%s", config.ads_api, config.sms_country)
    return config
