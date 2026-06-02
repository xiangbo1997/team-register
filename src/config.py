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
    # SMS 端点 base URL（空=走 SMSManager 默认 sms-activate；HeroSMS 等兼容平台由 provider 配置注入）
    sms_base_url: str = ""
    # SMS driver 名（sms_activate / hero_sms / five_sim ...）；异构协议 provider（如 five_sim）
    # 据此走 registry.build 直接构造，而非 SMSManager 标量管道。空=默认 sms-activate。
    sms_driver: str = ""
    # 异构 SMS provider 的原始 config dict（仅 five_sim 等非 SMSManager 协议使用）；
    # 字符串协议族（sms_activate/hero_sms）保持空，走 sms_api_key/sms_country/sms_base_url 标量。
    sms_provider_config: dict = field(default_factory=dict)

    # 邮件服务配置（通过 HTTP API 对接 email-provider）
    email_provider_base_url: str = "http://127.0.0.1:8000"
    email_provider_api_key: str = ""
    email_provider_name: str = "applemail"
    # 远程 email-provider 的 provider_configs.name（admin UI 配的 cfworker/freemail 等保存配置名）
    # 留空=服务端从 request 的 extra 取参；填了=服务端从 DB 注入加密 extra（cfworker_api_url、admin_token 等）
    mail_config_name: str = ""
    known_mail_accounts_json: str = ""
    # 邮件 provider 子类开关（feat/mail-provider-classes 2026-05-24 引入）
    # 启用任一 → MailManager 按邮箱域名自动路由到对应子类，不再依赖 email_provider_name 字符串
    # 两个都启用时：按 can_handle() 域名匹配选；都不命中 → fallback 到 email_provider_name 旧路径
    outlook_enabled: bool = False
    outlook_config_name: str = "outlook-pool-default"
    cfworker_enabled: bool = False
    cfworker_config_name: str = "mydomain-cfworker"
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
    default_sms_provider: str = "sms-default"
    default_captcha_provider: str = "captcha-default"
    default_llm_provider: str = "llm-default"
    default_mail_account_id: str = ""

    # LLM 决策器配置
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_ms: int = 8000
    llm_confidence_threshold: float = 0.6
    llm_max_consecutive_uncertain: int = 2
    # 卡顿升级（P0/P1）：同一 state 连续 N 步 DOM 指纹不变时提前触发 LLM 决策。
    # 默认 2（保守，retry_count>=1 仍是主触发）；设很大值（如 99）可等价关闭卡顿触发。
    llm_stall_threshold: int = 2
    # 卡顿到 N 步且 llm_vision_enabled=True 时，给 LLM 附页面截图调多模态。默认 3。
    llm_screenshot_on_stall_threshold: int = 3
    # 多模态视觉决策开关（P1）：默认关。LLM_MODEL 可能非多模态，不能靠探测，必须显式开。
    llm_vision_enabled: bool = False
    # grok_assist 式枚举兜底开关（P2）：默认关。开启后作为状态机硬失败前的最后一层。
    assist_fallback_enabled: bool = False

    # 分诊器（Vision LLM）配置
    triage_enabled: bool = False
    triage_base_url: str = ""
    triage_api_key: str = ""
    triage_model: str = ""
    triage_timeout_ms: int = 15000
    triage_confidence_threshold: float = 0.6

    # Captcha solver 配置（自愈框架，详见 src/automation/captcha_solver.py）
    # captcha_solver_kind: noop / manual / nocaptcha
    #   - noop: 默认，不做任何事（零行为变更）
    #   - manual: 强制走人工接管，但事件日志能区分"故意降级"
    #   - nocaptcha: 调 nocaptcha.io 的 universal Turnstile 端点
    # captcha_solver_budget_cap_usd: 每日预算硬上限。超出后自动降级 manual，避免 VLM 服务费失控。
    # captcha_solver_timeout_ms: 单次求解的超时墙；超时即视为失败、回到 BLOCKED。
    captcha_solver_kind: str = "noop"
    nocaptcha_user_token: str = ""
    captcha_solver_budget_cap_usd: float = 5.0
    captcha_solver_timeout_ms: int = 30000

    # Sentinel PoW 配置（详见 src/automation/sentinel.py）
    # sentinel_strategy: noop / pure_python
    #   - noop: 默认，不注入 openai-sentinel-token header（零行为变更）
    #   - pure_python: 本地 FNV-1a brute-force PoW + curl_cffi 拿挑战
    # sentinel_sdk_version: 写进 token payload 的 OpenAI SDK 版本号 marker
    # sentinel_impersonate: curl_cffi 浏览器伪装版本，需与 payment_link / promo_eligibility 保持一致
    # sentinel_timeout_ms: 单次 token 生成的超时墙；超时即视为失败、返回空 token（调用方继续裸跑）
    sentinel_strategy: str = "noop"
    sentinel_sdk_version: str = "20260124ceb8"
    sentinel_impersonate: str = "chrome120"
    sentinel_timeout_ms: int = 10000

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
    # checkout payload schema 版本（P4 模板化 2026-05-25）
    # 模板里填 schema_version 字段会单点覆盖；不填走这里的全局默认
    # OpenAI API 变动时切版本：加 src/payment_link/schemas/<plan>_v<n>.py + 改 .env
    plus_schema_version: str = "v2"
    team_schema_version: str = "v1"
    pro_schema_version: str = "v1"
    pro_lite_schema_version: str = "v1"
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

    # 批量注册场景下，由 worker 从 Run.config_snapshot.identity 注入的真实身份字段。
    # main.py:fill_about_you 优先用这些（防风控聚类），缺省时 fallback 到原 lowercase 随机生成。
    # 这些字段不会出现在 .env，只在 worker._resolve_runtime_config 里动态注入。
    identity_first_name: str = ""
    identity_last_name: str = ""
    identity_email_local: str = ""
    identity_birthdate: str = ""

    # 三模式注册（feat/mode-phone-registration 2026-05-27 引入）。
    # registration_kind: "email" / "phone"，由 worker._resolve_runtime_config 从 Run.config_snapshot 注入；
    # automation/runtime.py 用它判断 PHONE state 是放行（phone 模式走 handler）还是 fail（email 模式默认行为）。
    # requested_phone: 经 SMS-Activate 申领或用户手填的完整手机号（含国家码），传给 PHONE handler 填入表单。
    # sms_order_id: 对应的 SMS-Activate 订单号，handler 用它轮询 OTP。
    registration_kind: str = "email"
    requested_phone: str = ""
    sms_order_id: str = ""

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
            "captcha": [
                ("nocaptcha_user_token", "NOCAPTCHA_USER_TOKEN"),
            ],
        }

        modules_to_check = required_modules or []
        for module_name in modules_to_check:
            if module_name == "llm" and not self.llm_enabled:
                continue
            if module_name == "triage" and not self.triage_enabled:
                continue
            # captcha 模块只在使用第三方 solver 时才校验 token
            if module_name == "captcha" and self.captcha_solver_kind != "nocaptcha":
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
        sms_base_url=os.getenv("SMS_BASE_URL", ""),
        email_provider_base_url=os.getenv("EMAIL_PROVIDER_BASE_URL", "http://127.0.0.1:8000"),
        email_provider_api_key=os.getenv("EMAIL_PROVIDER_API_KEY", ""),
        email_provider_name=os.getenv("EMAIL_PROVIDER_NAME", "applemail").strip().lower() or "applemail",
        mail_config_name=os.getenv("MAIL_CONFIG_NAME", "").strip(),
        known_mail_accounts_json=os.getenv("KNOWN_MAIL_ACCOUNTS_JSON", ""),
        outlook_enabled=(os.getenv("OUTLOOK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}),
        outlook_config_name=os.getenv("OUTLOOK_CONFIG_NAME", "outlook-pool-default").strip() or "outlook-pool-default",
        cfworker_enabled=(os.getenv("CFWORKER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}),
        cfworker_config_name=os.getenv("CFWORKER_CONFIG_NAME", "mydomain-cfworker").strip() or "mydomain-cfworker",
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
        default_sms_provider=os.getenv("DEFAULT_SMS_PROVIDER", "sms-default").strip() or "sms-default",
        default_captcha_provider=os.getenv("DEFAULT_CAPTCHA_PROVIDER", "captcha-default").strip() or "captcha-default",
        default_llm_provider=os.getenv("DEFAULT_LLM_PROVIDER", "llm-default").strip() or "llm-default",
        default_mail_account_id=os.getenv("DEFAULT_MAIL_ACCOUNT_ID", "").strip(),
        llm_enabled=_read_bool("LLM_ENABLED", False),
        llm_base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        llm_timeout_ms=_read_int("LLM_TIMEOUT_MS", 8000),
        llm_confidence_threshold=_read_float("LLM_CONFIDENCE_THRESHOLD", 0.6),
        llm_max_consecutive_uncertain=_read_int("LLM_MAX_CONSECUTIVE_UNCERTAIN", 2),
        llm_stall_threshold=_read_int("LLM_STALL_THRESHOLD", 2),
        llm_screenshot_on_stall_threshold=_read_int("LLM_SCREENSHOT_ON_STALL_THRESHOLD", 3),
        llm_vision_enabled=_read_bool("LLM_VISION_ENABLED", False),
        assist_fallback_enabled=_read_bool("ASSIST_FALLBACK_ENABLED", False),
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
        # checkout schema 版本（P4 模板化）
        plus_schema_version=os.getenv("PLUS_SCHEMA_VERSION", "v2").strip().lower() or "v2",
        team_schema_version=os.getenv("TEAM_SCHEMA_VERSION", "v1").strip().lower() or "v1",
        pro_schema_version=os.getenv("PRO_SCHEMA_VERSION", "v1").strip().lower() or "v1",
        pro_lite_schema_version=os.getenv("PRO_LITE_SCHEMA_VERSION", "v1").strip().lower() or "v1",
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
        captcha_solver_kind=os.getenv("CAPTCHA_SOLVER_KIND", "noop").strip().lower() or "noop",
        nocaptcha_user_token=os.getenv("NOCAPTCHA_USER_TOKEN", "").strip(),
        captcha_solver_budget_cap_usd=_read_float("CAPTCHA_SOLVER_BUDGET_CAP_USD", 5.0),
        captcha_solver_timeout_ms=_read_int("CAPTCHA_SOLVER_TIMEOUT_MS", 30000),
        sentinel_strategy=os.getenv("SENTINEL_STRATEGY", "noop").strip().lower() or "noop",
        sentinel_sdk_version=os.getenv("SENTINEL_SDK_VERSION", "20260124ceb8").strip() or "20260124ceb8",
        sentinel_impersonate=os.getenv("SENTINEL_IMPERSONATE", "chrome120").strip() or "chrome120",
        sentinel_timeout_ms=_read_int("SENTINEL_TIMEOUT_MS", 10000),
    )

    logger.debug("配置加载完成: ads_api=%s, sms_country=%s", config.ads_api, config.sms_country)

    # L4 fail-fast warning：当 mail provider 在白名单（cfworker / skymail）但
    # MAIL_CONFIG_NAME 为空时，发出明显警告。这是兜底层 — main.py 直接跑（不走
    # admin UI / 任务 API）的入口能在配置加载阶段就看到问题，而不是等任务执行时
    # 撞 422。详见 docs/architecture/mail-provider-contract.md
    _warn_if_mail_config_incomplete(config)

    # A 类 provider 字段 deprecation warning（feat/registration-profile 2026-05-27）：
    # 检测 .env 显式设置了 provider 凭据/选择器字段时打一次性 warning，
    # 引导运维改去 /providers + /registration-profiles 页面管理。
    _warn_deprecated_provider_fields()

    return config


_MAIL_PROVIDERS_REQUIRING_CONFIG_NAME = frozenset({"cfworker", "skymail", "outlook_email_plus"})


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


# A 类 provider 字段 deprecation：feat/registration-profile 2026-05-27 引入。
# 这些 .env 字段已迁移到 ProviderConfig + RegistrationProfile，但仍保留作兼容护栏；
# 显式设置时打一次性 warning，引导运维改去 admin UI。
# 同进程内只 warn 一次，避免日志噪音。
_DEPRECATED_ENV_VARS = (
    "DEFAULT_BROWSER_PROVIDER",
    "DEFAULT_CARD_PROVIDER",
    "DEFAULT_MAIL_PROVIDER",
    "DEFAULT_MAIL_ACCOUNT_ID",
    "CARD_PROVIDER",
    "EFUNCARD_TOKEN",
    "NODECARD_API_URL",
    "NODECARD_MERCHANT_ID",
    "NODECARD_PLATFORM_ID",
    "X988CARD_API_BASE",
    "X988CARD_REQUEST_TIMEOUT",
    "ADS_API",
    "ADS_API_KEY",
    "EMAIL_PROVIDER_BASE_URL",
    "EMAIL_PROVIDER_API_KEY",
    "EMAIL_PROVIDER_NAME",
    "MAIL_CONFIG_NAME",
    "OUTLOOK_CONFIG_NAME",
    "CFWORKER_CONFIG_NAME",
    "KNOWN_MAIL_ACCOUNTS_JSON",
    "SMS_API_KEY",
    "SMS_COUNTRY",
)

_deprecation_warned = False


def _warn_deprecated_provider_fields() -> None:
    """检测 .env 显式设置了 A 类 provider 字段，打一次性 deprecation warning。

    判断"显式设置"的依据：环境变量存在（os.environ 里有 key，不论值是否为默认）。
    一次性：同进程多次 load_config 只 warn 一次。
    """
    global _deprecation_warned
    if _deprecation_warned:
        return
    set_vars = [v for v in _DEPRECATED_ENV_VARS if v in os.environ]
    if not set_vars:
        _deprecation_warned = True
        return
    logger.warning(
        "检测到 .env 显式设置了 A 类 provider 字段（%d 个）: %s。"
        "这些字段已迁移到 admin UI 管理（凭据→/providers，组合→/registration-profiles），"
        ".env 中的值仅作老部署兼容护栏。建议：① 通过 admin UI 配同名 ProviderConfig 并核对，"
        "② 删除 .env 中的对应字段，③ 后续从 admin UI 维护。详见 /registration-profiles 页提示。",
        len(set_vars), set_vars,
    )
    _deprecation_warned = True
