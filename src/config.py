# -*- coding: utf-8 -*-
"""
配置管理模块

从环境变量加载配置，并进行必填项校验。
使用 dataclass 提供类型安全的配置访问。
"""

import os
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

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


def _read_int_in_range(
    name: str,
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    """读取整数并限定在 [min_value, max_value] 范围；超出范围回退默认值并告警。"""
    value = _read_int(name, default)
    if value < min_value or value > max_value:
        logger.warning(
            "环境变量 %s=%d 超出允许范围 [%d, %d]，回退默认值 %d",
            name,
            value,
            min_value,
            max_value,
            default,
        )
        return default
    return value


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

    # X988Card (cards.779.chat / card.988.chat) 配置
    x988card_api_base: str = "https://cards.779.chat"
    x988card_request_timeout: int = 15

    card_provider: str = "efuncard"  # "efuncard" / "nodecard" / "x988card"

    # SMS-Activate 接码配置
    sms_api_key: str = ""
    sms_country: str = "6"

    # 邮件服务配置（通过 HTTP API 对接 email-provider）
    email_provider_base_url: str = "http://127.0.0.1:8000"
    email_provider_api_key: str = ""
    email_provider_name: str = "applemail"
    # 远程 email-provider 的 provider_configs.name（admin UI 配的 cfworker/freemail 等保存配置名）
    # 留空=服务端从 request 的 extra 取参；填了=服务端从 DB 注入加密 extra（cfworker_api_url、admin_token 等）
    mail_config_name: str = ""
    known_mail_accounts_json: str = ""
    # 以下为历史兼容字段，本地 provider 模式已废弃
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

    # 控制台默认 provider / 账号选择
    default_browser_provider: str = "browser-default"
    default_card_provider: str = "card-default"
    default_mail_provider: str = "mail-default"
    default_mail_account_id: str = ""

    # LLM 决策器配置
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_ms: int = 8000
    llm_confidence_threshold: float = 0.6
    llm_max_consecutive_uncertain: int = 2

    # 分诊器（Vision LLM）配置
    triage_enabled: bool = False
    triage_base_url: str = ""
    triage_api_key: str = ""
    triage_model: str = ""
    triage_timeout_ms: int = 15000
    triage_confidence_threshold: float = 0.6

    # 上场前体检（Preflight）配置
    preflight_mode: str = "warn"           # off / warn / block
    fingerprint_min_score: int = 80

    # 自动化运行时配置
    enable_payment_flow: bool = True
    enable_card_warmup: bool = False
    warmup_account_pool: str = ""
    warmup_max_retries: int = 2

    # 卡预热（Pro 账号代刷模式）配置
    # 工作流：用 mail_accounts 中标记 role=pro_warmup 的账号登录 OpenAI，
    # 用待绑卡触发 Pro Plan $200/$100 订阅 → 卡余额不足 → 真实 insufficient_funds decline →
    # Stripe 给该卡积累 "活跃且发卡行配合" 信号 → 回到新注册账号绑 Team trial $1
    warmup_pro_attempt_amounts: str = "200,100"  # 逗号分隔金额序列；按顺序每次推迟 24h 执行一笔
    warmup_pro_interval_hours: int = 24  # 单卡两次 Pro attempt 必须间隔（防 Stripe velocity rule）

    # 账号养号配置
    enable_account_warmup: bool = False
    warmup_min_days: int = 3
    warmup_max_days: int = 7
    warmup_messages_per_day_min: int = 3
    warmup_messages_per_day_max: int = 10
    warmup_conversation_topics_path: str = ""  # 话题列表 JSON 文件路径，留空用内置默认
    warmup_blocked_threshold: int = 2  # 触发 captcha/BLOCKED 累计 N 次后标记 abandoned
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

    # 后台任务并发上限（范围 1-32，超出范围回退到默认值 2）
    max_workers: int = 2

    # 人类行为模拟开关：启用后使用贝塞尔鼠标轨迹 + 非匀速打字，降低自动化指纹；
    # 排障时可设为 false 退回 Playwright 原生 click/fill 以加快响应。
    humanize_enabled: bool = True

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
                ("email_provider_api_key", "EMAIL_PROVIDER_API_KEY"),
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
            "triage": [
                ("triage_base_url", "TRIAGE_BASE_URL"),
                ("triage_api_key", "TRIAGE_API_KEY"),
                ("triage_model", "TRIAGE_MODEL"),
            ],
        }

        modules_to_check = required_modules or []
        for module_name in modules_to_check:
            if module_name == "llm" and not self.llm_enabled:
                continue
            if module_name == "triage" and not self.triage_enabled:
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

    def parse_known_mail_accounts(self) -> dict[str, list[dict[str, Any]]]:
        """解析多 provider 已知邮箱配置。"""
        raw = str(self.known_mail_accounts_json or "").strip()
        if not raw:
            return {}

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("KNOWN_MAIL_ACCOUNTS_JSON 不是合法 JSON，已忽略。")
            return {}

        if not isinstance(payload, dict):
            logger.warning("KNOWN_MAIL_ACCOUNTS_JSON 顶层必须是对象，已忽略。")
            return {}

        normalized: dict[str, list[dict[str, Any]]] = {}
        for provider_name, accounts in payload.items():
            provider_key = str(provider_name or "").strip().lower()
            if not provider_key:
                continue

            if isinstance(accounts, dict):
                candidates = [accounts]
            elif isinstance(accounts, list):
                candidates = accounts
            else:
                logger.warning("KNOWN_MAIL_ACCOUNTS_JSON[%s] 必须是对象或数组，已忽略。", provider_key)
                continue

            normalized_accounts: list[dict[str, Any]] = []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                account = dict(item)
                email = str(account.get("email") or "").strip().lower()
                if email:
                    account["email"] = email
                normalized_accounts.append(account)

            if normalized_accounts:
                normalized[provider_key] = normalized_accounts

        return normalized


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
        x988card_api_base=os.getenv("X988CARD_API_BASE", "https://cards.779.chat").rstrip("/"),
        x988card_request_timeout=_read_int("X988CARD_REQUEST_TIMEOUT", 15),
        card_provider=os.getenv("CARD_PROVIDER", "efuncard").strip().lower() or "efuncard",
        sms_api_key=os.getenv("SMS_API_KEY", ""),
        sms_country=os.getenv("SMS_COUNTRY", "6"),
        email_provider_base_url=os.getenv("EMAIL_PROVIDER_BASE_URL", "http://127.0.0.1:8000"),
        email_provider_api_key=os.getenv("EMAIL_PROVIDER_API_KEY", ""),
        email_provider_name=os.getenv("EMAIL_PROVIDER_NAME", "applemail").strip().lower() or "applemail",
        mail_config_name=os.getenv("MAIL_CONFIG_NAME", "").strip(),
        known_mail_accounts_json=os.getenv("KNOWN_MAIL_ACCOUNTS_JSON", ""),
        mail_domain=os.getenv("MAIL_DOMAIN", ""),
        mail_refresh_token=os.getenv("MAIL_REFRESH_TOKEN", ""),
        mail_client_id=os.getenv("MAIL_CLIENT_ID", ""),
        task_ads_id=os.getenv("TASK_ADS_ID", ""),
        task_cdk=os.getenv("TASK_CDK", ""),
        task_email=os.getenv("TASK_EMAIL", ""),
        task_password=os.getenv("TASK_PASSWORD", ""),
        proxy=os.getenv("PROXY", ""),
        default_browser_provider=os.getenv("DEFAULT_BROWSER_PROVIDER", "browser-default").strip() or "browser-default",
        default_card_provider=os.getenv("DEFAULT_CARD_PROVIDER", "card-default").strip() or "card-default",
        default_mail_provider=os.getenv("DEFAULT_MAIL_PROVIDER", "mail-default").strip() or "mail-default",
        default_mail_account_id=os.getenv("DEFAULT_MAIL_ACCOUNT_ID", "").strip(),
        llm_enabled=_read_bool("LLM_ENABLED", False),
        llm_base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        llm_timeout_ms=_read_int("LLM_TIMEOUT_MS", 8000),
        llm_confidence_threshold=_read_float("LLM_CONFIDENCE_THRESHOLD", 0.6),
        llm_max_consecutive_uncertain=_read_int("LLM_MAX_CONSECUTIVE_UNCERTAIN", 2),
        triage_enabled=_read_bool("TRIAGE_ENABLED", False),
        triage_base_url=os.getenv("TRIAGE_BASE_URL", "").rstrip("/"),
        triage_api_key=os.getenv("TRIAGE_API_KEY", ""),
        triage_model=os.getenv("TRIAGE_MODEL", ""),
        triage_timeout_ms=_read_int("TRIAGE_TIMEOUT_MS", 15000),
        triage_confidence_threshold=_read_float("TRIAGE_CONFIDENCE_THRESHOLD", 0.6),
        preflight_mode=(os.getenv("PREFLIGHT_MODE", "warn").strip().lower() or "warn"),
        fingerprint_min_score=_read_int("FINGERPRINT_MIN_SCORE", 80),
        enable_payment_flow=_read_bool("ENABLE_PAYMENT_FLOW", True),
        enable_card_warmup=_read_bool("ENABLE_CARD_WARMUP", False),
        warmup_account_pool=os.getenv("WARMUP_ACCOUNT_POOL", ""),
        warmup_max_retries=_read_int("WARMUP_MAX_RETRIES", 2),
        warmup_pro_attempt_amounts=os.getenv("WARMUP_PRO_ATTEMPT_AMOUNTS", "200,100").strip() or "200,100",
        warmup_pro_interval_hours=_read_int("WARMUP_PRO_INTERVAL_HOURS", 24),
        enable_account_warmup=_read_bool("ENABLE_ACCOUNT_WARMUP", False),
        warmup_min_days=_read_int("WARMUP_MIN_DAYS", 3),
        warmup_max_days=_read_int("WARMUP_MAX_DAYS", 7),
        warmup_messages_per_day_min=_read_int("WARMUP_MESSAGES_PER_DAY_MIN", 3),
        warmup_messages_per_day_max=_read_int("WARMUP_MESSAGES_PER_DAY_MAX", 10),
        warmup_conversation_topics_path=os.getenv("WARMUP_CONVERSATION_TOPICS_PATH", "").strip(),
        warmup_blocked_threshold=_read_int("WARMUP_BLOCKED_THRESHOLD", 2),
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
        max_workers=_read_int_in_range("MAX_WORKERS", 2, min_value=1, max_value=32),
        humanize_enabled=_read_bool("HUMANIZE_ENABLED", True),
    )

    logger.debug("配置加载完成: ads_api=%s, sms_country=%s", config.ads_api, config.sms_country)

    # L4 fail-fast warning：当 mail provider 在白名单（cfworker / skymail）但
    # MAIL_CONFIG_NAME 为空时，发出明显警告。这是兜底层 — main.py 直接跑（不走
    # admin UI / 任务 API）的入口能在配置加载阶段就看到问题，而不是等任务执行时
    # 撞 422。详见 docs/architecture/mail-provider-contract.md
    _warn_if_mail_config_incomplete(config)

    return config


_MAIL_PROVIDERS_REQUIRING_CONFIG_NAME = frozenset({"cfworker", "skymail"})


def _warn_if_mail_config_incomplete(config: AppConfig) -> None:
    """L4 fail-fast warning — managed-only provider 必须有 MAIL_CONFIG_NAME。

    不 raise（避免阻塞向后兼容场景：admin 通过 UI 配 ProviderConfig 而不靠 .env），
    只 logger.warning，让运维显式看到风险。
    """
    if config.email_provider_name not in _MAIL_PROVIDERS_REQUIRING_CONFIG_NAME:
        return
    if (config.mail_config_name or "").strip():
        return
    logger.warning(
        "MAIL_CONFIG_NAME 未配置但 EMAIL_PROVIDER_NAME=%s 在 managed 白名单内。"
        "main.py 直接运行可能会撞 PROVIDER_NOT_CONFIGURED 422；"
        "请在 .env 设置 MAIL_CONFIG_NAME（如 mydomain-cfworker）"
        "或通过 admin UI /providers 配置 mail-default。",
        config.email_provider_name,
    )
