# -*- coding: utf-8 -*-
"""
数据库引擎管理

默认 SQLite，通过 DATABASE_URL 环境变量可切换为 PostgreSQL。
"""

from __future__ import annotations

import os
import logging
from typing import Any, Optional

from sqlalchemy import Engine, inspect, text
from sqlmodel import SQLModel, Session, create_engine

logger = logging.getLogger(__name__)

_DEFAULT_DB_URL = "sqlite:///team_register.db"

_engine: Optional[Engine] = None


def get_engine(database_url: Optional[str] = None) -> Engine:
    """
    获取数据库引擎单例。

    Args:
        database_url: 数据库连接串，为 None 时读取 DATABASE_URL 环境变量

    Returns:
        SQLAlchemy Engine 实例
    """
    global _engine
    if _engine is not None:
        return _engine

    url = database_url or os.getenv("DATABASE_URL", _DEFAULT_DB_URL)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_engine(url, echo=False, connect_args=connect_args)
    logger.info("数据库引擎初始化: %s", url.split("@")[-1] if "@" in url else url)
    return _engine


def init_db(database_url: Optional[str] = None) -> Engine:
    """
    初始化数据库：创建引擎并建表。

    Args:
        database_url: 数据库连接串

    Returns:
        已初始化的 Engine
    """
    engine = get_engine(database_url)
    SQLModel.metadata.create_all(engine)
    _run_schema_migrations(engine)
    _seed_runtime_defaults(engine)
    logger.info("数据库表创建完成。")
    return engine


def get_session() -> Session:
    """创建一个新的数据库会话。"""
    return Session(get_engine())


def _run_schema_migrations(engine: Engine) -> None:
    """对旧库执行轻量兼容迁移，仅补齐新增列 / 新增表。"""
    runs_columns = _table_columns(engine, "runs")
    runs_specs = {
        "browser_provider": "ALTER TABLE runs ADD COLUMN browser_provider VARCHAR(100) NOT NULL DEFAULT ''",
        "card_provider": "ALTER TABLE runs ADD COLUMN card_provider VARCHAR(100) NOT NULL DEFAULT ''",
        "mail_provider": "ALTER TABLE runs ADD COLUMN mail_provider VARCHAR(100) NOT NULL DEFAULT ''",
        "mail_account_id": "ALTER TABLE runs ADD COLUMN mail_account_id VARCHAR(32) NOT NULL DEFAULT ''",
        "retry_mode": "ALTER TABLE runs ADD COLUMN retry_mode VARCHAR(20) NOT NULL DEFAULT 'restart'",
        # 卡预热 / 养号 调度字段
        "next_action_at": "ALTER TABLE runs ADD COLUMN next_action_at DATETIME NULL",
        "warmup_pro_attempts": "ALTER TABLE runs ADD COLUMN warmup_pro_attempts INTEGER NOT NULL DEFAULT 0",
        "warmup_blocked_count": "ALTER TABLE runs ADD COLUMN warmup_blocked_count INTEGER NOT NULL DEFAULT 0",
        # H2 BIN 健康度跟踪：跨 Run 聚合统计同 BIN 失败率
        "card_bin": "ALTER TABLE runs ADD COLUMN card_bin VARCHAR(8) NOT NULL DEFAULT ''",
        # 普号池：注册成功但未绑卡 / 绑成功的 Plus/Team / 已放弃，与 status 正交
        "account_tier": "ALTER TABLE runs ADD COLUMN account_tier VARCHAR(20) NOT NULL DEFAULT 'registered'",
        # 本 Run 内 Stripe decline 重试计数（decline_retry_service 写入）
        "decline_attempts": "ALTER TABLE runs ADD COLUMN decline_attempts INTEGER NOT NULL DEFAULT 0",
        # OpenAI session tokens（号池 cpa 格式导出用；worker 在 token 提取后写入）
        "openai_tokens": "ALTER TABLE runs ADD COLUMN openai_tokens TEXT NOT NULL DEFAULT '{}'",
        # 注册时浏览器实际走的代理出口 IP + 国家（ipinfo.io 抓取，账号池列表展示）
        "ip_address": "ALTER TABLE runs ADD COLUMN ip_address VARCHAR(45) NOT NULL DEFAULT ''",
        "ip_country": "ALTER TABLE runs ADD COLUMN ip_country VARCHAR(8) NOT NULL DEFAULT ''",
        # 手机号注册模式（feat/mode-phone-registration 2026-05-27 引入）
        "phone_number": "ALTER TABLE runs ADD COLUMN phone_number VARCHAR(40) NOT NULL DEFAULT ''",
        "sms_order_id": "ALTER TABLE runs ADD COLUMN sms_order_id VARCHAR(64) NOT NULL DEFAULT ''",
        # Grok (x.ai) 注册（feat/grok-register 引入）：platform 区分 openai/grok，sso_token 存 Grok 产物
        "platform": "ALTER TABLE runs ADD COLUMN platform VARCHAR(20) NOT NULL DEFAULT 'openai'",
        "sso_token": "ALTER TABLE runs ADD COLUMN sso_token TEXT NOT NULL DEFAULT ''",
    }
    # mail_accounts.role 列（消除 Ambiguity #2）+ pro_warmup 号池调度字段
    mail_columns = _table_columns(engine, "mail_accounts")
    mail_specs = {
        "role": "ALTER TABLE mail_accounts ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'regular'",
        # 卡预热号池字段（仅 role=pro_warmup 用）
        "last_used_at": "ALTER TABLE mail_accounts ADD COLUMN last_used_at DATETIME NULL",
        "cooldown_until": "ALTER TABLE mail_accounts ADD COLUMN cooldown_until DATETIME NULL",
        "consecutive_failures": "ALTER TABLE mail_accounts ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0",
        "last_failure_reason": "ALTER TABLE mail_accounts ADD COLUMN last_failure_reason VARCHAR(200) NULL",
    }
    # 卡池：手动入池 + 手动触发预热的元信息
    card_columns = _table_columns(engine, "card_activations")
    card_specs = {
        "target_warmup_count": "ALTER TABLE card_activations ADD COLUMN target_warmup_count INTEGER NOT NULL DEFAULT 0",
        "warmup_count": "ALTER TABLE card_activations ADD COLUMN warmup_count INTEGER NOT NULL DEFAULT 0",
        "warmed_at": "ALTER TABLE card_activations ADD COLUMN warmed_at DATETIME NULL",
        "last_warmup_status": "ALTER TABLE card_activations ADD COLUMN last_warmup_status VARCHAR(20) NOT NULL DEFAULT 'pending'",
        "last_warmup_reason": "ALTER TABLE card_activations ADD COLUMN last_warmup_reason VARCHAR(200) NULL",
    }
    # 合成卡审计表：多国化新增 country 列（此前 _run_schema_migrations 完全没处理这张表）
    synth_audit_columns = _table_columns(engine, "synthetic_card_audits")
    synth_audit_specs = {
        "country": "ALTER TABLE synthetic_card_audits ADD COLUMN country VARCHAR(4) NOT NULL DEFAULT 'US'",
    }
    # 链接模板：关联代理 id（代理池功能新增）+ promo eligibility 验证状态
    link_template_columns = _table_columns(engine, "link_templates")
    link_template_specs = {
        "proxy_id": "ALTER TABLE link_templates ADD COLUMN proxy_id INTEGER NULL",
        # promo_eligibility 模块写入：last_eligibility_* 三字段
        "last_eligibility_status": "ALTER TABLE link_templates ADD COLUMN last_eligibility_status VARCHAR(20) NOT NULL DEFAULT ''",
        "last_eligibility_check_at": "ALTER TABLE link_templates ADD COLUMN last_eligibility_check_at DATETIME NULL",
        "last_eligibility_metadata": "ALTER TABLE link_templates ADD COLUMN last_eligibility_metadata JSON NULL",
        # P4 模板化扩展字段（2026-05-25）
        "schema_version": "ALTER TABLE link_templates ADD COLUMN schema_version VARCHAR(20) NULL",
        "url_locale": "ALTER TABLE link_templates ADD COLUMN url_locale VARCHAR(10) NULL",
        "extra_payload_json": "ALTER TABLE link_templates ADD COLUMN extra_payload_json JSON NULL",
        "is_preset": "ALTER TABLE link_templates ADD COLUMN is_preset BOOLEAN NOT NULL DEFAULT 0",
        "sort_order": "ALTER TABLE link_templates ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
        # P6 暴露 checkout_ui_mode（2026-05-25）
        "checkout_ui_mode": "ALTER TABLE link_templates ADD COLUMN checkout_ui_mode VARCHAR(10) NULL",
        # promo metadata 结构化字段（2026-05-29，由 promo_eligibility_service._extract_promo_fields 填写）
        # 抽取自 last_eligibility_metadata 的 ChatGPT /promotions/metadata 响应
        "promo_percent_off": "ALTER TABLE link_templates ADD COLUMN promo_percent_off INTEGER NULL",
        "promo_duration_months": "ALTER TABLE link_templates ADD COLUMN promo_duration_months INTEGER NULL",
        "promo_expires_at": "ALTER TABLE link_templates ADD COLUMN promo_expires_at DATETIME NULL",
        "promo_max_redemptions": "ALTER TABLE link_templates ADD COLUMN promo_max_redemptions INTEGER NULL",
        "promo_applicable_plans": "ALTER TABLE link_templates ADD COLUMN promo_applicable_plans VARCHAR(120) NOT NULL DEFAULT ''",
    }

    with engine.begin() as conn:
        if runs_columns:
            for column_name, ddl in runs_specs.items():
                if column_name in runs_columns:
                    continue
                conn.execute(text(ddl))
                logger.info("已为 runs 表补齐字段: %s", column_name)
        if mail_columns:
            for column_name, ddl in mail_specs.items():
                if column_name in mail_columns:
                    continue
                conn.execute(text(ddl))
                logger.info("已为 mail_accounts 表补齐字段: %s", column_name)
        if card_columns:
            for column_name, ddl in card_specs.items():
                if column_name in card_columns:
                    continue
                conn.execute(text(ddl))
                logger.info("已为 card_activations 表补齐字段: %s", column_name)
        if synth_audit_columns:
            for column_name, ddl in synth_audit_specs.items():
                if column_name in synth_audit_columns:
                    continue
                conn.execute(text(ddl))
                logger.info("已为 synthetic_card_audits 表补齐字段: %s", column_name)
        if link_template_columns:
            for column_name, ddl in link_template_specs.items():
                if column_name in link_template_columns:
                    continue
                conn.execute(text(ddl))
                logger.info("已为 link_templates 表补齐字段: %s", column_name)

        # 加宽既有列 synthetic_card_audits.address_state: 4→16
        # （JP 都道府县全名如 Kanagawa=8 超出原 VARCHAR(4)）。
        # SQLite 不强制 VARCHAR 长度可跳过；PostgreSQL 需 ALTER COLUMN TYPE。
        if synth_audit_columns and not engine.url.drivername.startswith("sqlite"):
            try:
                conn.execute(text(
                    "ALTER TABLE synthetic_card_audits "
                    "ALTER COLUMN address_state TYPE VARCHAR(16)"
                ))
                logger.info("已加宽 synthetic_card_audits.address_state → VARCHAR(16)")
            except Exception as exc:  # noqa: BLE001 — 已是 16 或不支持时静默跳过
                logger.debug("address_state 加宽跳过（可能已是 16）: %s", exc)
    # app_setting_revisions 表由 SQLModel.metadata.create_all() 自动建（init_db 调用），
    # 这里无需手工 CREATE TABLE。


def _table_columns(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {str(item.get("name") or "") for item in inspector.get_columns(table_name)}


_SECRET_PLACEHOLDER_HINTS = ("your-", "xxx", "placeholder", "todo", "changeme", "example")


def _looks_like_secret(value: Any) -> bool:
    """简单非空 + 非占位符校验，避免把 .env 模板里的占位串 seed 到 DB。"""
    text = str(value or "").strip()
    if len(text) < 6:
        return False
    lower = text.lower()
    return not any(hint in lower for hint in _SECRET_PLACEHOLDER_HINTS)


def _seed_runtime_defaults(engine: Engine) -> None:
    """为全新/升级后的控制台补齐默认 provider 与邮箱账号。"""
    from sqlmodel import select

    from src.config import load_config
    from src.db.models import AppSetting, MailAccount, ProviderConfig

    config = load_config()

    # mail provider 实例列表 — 保留 mail-default 向后兼容，
    # 按 OUTLOOK_ENABLED / CFWORKER_ENABLED 开关追加 mail-cfworker-default / mail-outlook-default
    # （feat/mail-provider-classes 2026-05-24 引入；与 src/providers/mail_cfworker.py + mail_outlook.py 的子类对齐）
    mail_defaults = [
        (
            "mail",
            str(config.default_mail_provider or "mail-default").strip() or "mail-default",
            {
                "provider_name": config.email_provider_name,
                "session_mode": "credentialed" if config.email_provider_name == "applemail" else "managed",
                "mailbox": "INBOX",
            },
        ),
    ]
    if getattr(config, "cfworker_enabled", False):
        mail_defaults.append((
            "mail",
            "mail-cfworker-default",
            {
                "provider_name": "cfworker",
                "session_mode": "managed",
                "config_name": str(getattr(config, "cfworker_config_name", "") or "mydomain-cfworker").strip()
                               or "mydomain-cfworker",
                "supported_domains": ["zhangxb.xyz", "cloudsentryai.com"],
                "mailbox": "INBOX",
            },
        ))
    if getattr(config, "outlook_enabled", False):
        mail_defaults.append((
            "mail",
            "mail-outlook-default",
            {
                "provider_name": "outlook_email_plus",
                "session_mode": "managed",
                "config_name": str(getattr(config, "outlook_config_name", "") or "outlook-pool-default").strip()
                               or "outlook-pool-default",
                "supported_domains": [
                    "outlook.com", "hotmail.com", "live.com", "msn.com",
                    "outlook.jp", "hotmail.co.uk",
                ],
                "mailbox": "INBOX",
            },
        ))

    default_providers = [
        (
            "browser",
            str(config.default_browser_provider or "browser-default").strip() or "browser-default",
            {
                "driver": "adspower",
                "ads_api": config.ads_api,
                "ads_api_key": config.ads_api_key,
                "proxy": config.proxy,
            },
        ),
        (
            "card",
            str(config.default_card_provider or "card-default").strip() or "card-default",
            {
                "driver": config.card_provider,
                "efuncard_token": config.efuncard_token,
                "nodecard_api_url": config.nodecard_api_url,
                "nodecard_merchant_id": config.nodecard_merchant_id,
                "nodecard_platform_id": config.nodecard_platform_id,
                "x988card_api_base": config.x988card_api_base,
                "x988card_request_timeout": config.x988card_request_timeout,
            },
        ),
        *mail_defaults,
    ]

    # SMS / CAPTCHA / LLM provider seed —— 仅当 .env 有真值时建立默认 active 实例，
    # 占位值/空串/明显垃圾不入库，避免运维拿到 PROVIDER_NOT_CONFIGURED 误以为是 bug。
    # 幂等：以 (type, name) 为键，已存在则跳过；运维改名/停用后启动不会再覆盖。
    if _looks_like_secret(config.sms_api_key):
        default_providers.append((
            "sms",
            str(config.default_sms_provider or "sms-default").strip() or "sms-default",
            {
                "api_key": config.sms_api_key,
                "country": str(getattr(config, "sms_country", "") or "0").strip() or "0",
            },
        ))
    if str(getattr(config, "captcha_solver_kind", "noop") or "noop").strip().lower() != "noop":
        default_providers.append((
            "captcha",
            str(config.default_captcha_provider or "captcha-default").strip() or "captcha-default",
            {
                "kind": str(config.captcha_solver_kind or "noop").strip().lower(),
                "user_token": str(getattr(config, "nocaptcha_user_token", "") or "").strip(),
                "timeout_ms": int(getattr(config, "captcha_solver_timeout_ms", 30000) or 30000),
                "budget_cap_usd": float(getattr(config, "captcha_solver_budget_cap_usd", 5.0) or 5.0),
            },
        ))
    if _looks_like_secret(config.llm_api_key) and config.llm_base_url and config.llm_model:
        default_providers.append((
            "llm",
            str(config.default_llm_provider or "llm-default").strip() or "llm-default",
            {
                "base_url": config.llm_base_url,
                "api_key": config.llm_api_key,
                "model": config.llm_model,
                "timeout_ms": int(getattr(config, "llm_timeout_ms", 30000) or 30000),
                "confidence_threshold": float(getattr(config, "llm_confidence_threshold", 0.6) or 0.6),
            },
        ))

    with Session(engine) as session:
        for provider_type, provider_name, payload in default_providers:
            existing = session.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_type == provider_type,
                    ProviderConfig.provider_name == provider_name,
                )
            ).first()
            if existing is None:
                session.add(
                    ProviderConfig(
                        provider_type=provider_type,
                        provider_name=provider_name,
                        config=payload,
                        is_active=True,
                    )
                )

        if config.default_mail_account_id:
            existing_setting = session.get(AppSetting, "default_mail_account_id")
            if existing_setting is None:
                session.add(AppSetting(key="default_mail_account_id", value=config.default_mail_account_id))

        known_accounts = config.parse_known_mail_accounts()
        for provider_name, accounts in known_accounts.items():
            for item in accounts:
                email = str(item.get("email") or "").strip().lower()
                if not email:
                    continue
                existing_account = session.exec(
                    select(MailAccount).where(
                        MailAccount.provider_name == provider_name,
                        MailAccount.email == email,
                    )
                ).first()
                if existing_account is not None:
                    continue
                credentials = dict(item.get("credentials") or {})
                client_id = str(credentials.get("client_id") or item.get("client_id") or "").strip()
                refresh_token = str(credentials.get("refresh_token") or item.get("refresh_token") or "").strip()
                if not client_id or not refresh_token:
                    continue
                extra = dict(item.get("extra") or {})
                account_id = str(item.get("account_id") or "").strip()
                if account_id:
                    extra.setdefault("account_id", account_id)
                preserve = item.get("preserve_existing_mail")
                if preserve is not None:
                    extra.setdefault("preserve_existing_mail", bool(preserve))
                session.add(
                    MailAccount(
                        label=str(item.get("label") or email),
                        provider_name=provider_name,
                        email=email,
                        client_id=client_id,
                        refresh_token=refresh_token,
                        extra=extra,
                        is_active=True,
                    )
                )

        session.commit()

    # P2 seed：把弹窗里硬编码的 4 个快捷预设迁到 DB（is_preset=true）
    # 运营加新预设零代码，刷新 UI 立即生效
    _seed_link_template_presets(engine)

    # 注册方式 × 供应商组合 seed（feat/registration-profile 2026-05-27）：
    # 把 default_*_provider 等散落字段折叠成 email-default / phone-default 两条预设
    # 幂等：service 内部按 name 跳过已存在记录，运维改过的组合不被覆盖
    _seed_registration_profiles()


def _seed_registration_profiles() -> None:
    """初始化 email-default / phone-default 两个默认注册组合。

    幂等：service 内部按 name 跳过，已存在的组合不动；只有首次启动 / 全新库才创建。
    与 _seed_runtime_defaults 的 ProviderConfig seed 串联：那边先建好默认
    browser-default / card-default / mail-* 等记录，这边再 seed 引用它们。
    """
    # 延迟 import，避免循环依赖（service 依赖 db.engine.get_session）
    from src.services.registration_profile_service import RegistrationProfileService

    svc = RegistrationProfileService()
    try:
        result = svc.seed_from_appconfig()
        if result["seeded"]:
            logger.info("RegistrationProfile 已 seed: %s", result["seeded"])
        if result["skipped_existing"]:
            logger.debug(
                "RegistrationProfile 已存在跳过: %s",
                result["skipped_existing"],
            )
    except Exception as exc:
        # seed 失败不阻塞应用启动；下次启动会重试
        logger.warning("RegistrationProfile seed 失败（不阻塞启动）: %s", exc)


def _seed_link_template_presets(engine: Engine) -> None:
    """初始化 4 个内置预设 LinkTemplate（美/澳/日 Plus + Team 试用）。

    幂等：按 name 查重，已存在的预设不覆盖（运营改过的设置保留）。
    新增其他预设：运营在 UI「存为模板」→ 勾选「设为预设」，无需改本函数。
    """
    from sqlmodel import Session, select

    from src.db.models import LinkTemplate

    presets = [
        {
            "name": "🇺🇸 美 Plus 试用",
            "plan": "plus",
            "aimizy_country": "US",
            "aimizy_currency": "USD",
            "seat_quantity": 1,
            "return_mode": "long",
            "is_preset": True,
            "sort_order": 10,
        },
        {
            "name": "🇦🇺 澳 Plus 试用",
            "plan": "plus",
            "aimizy_country": "AU",
            "aimizy_currency": "AUD",
            "seat_quantity": 1,
            "return_mode": "long",
            "is_preset": True,
            "sort_order": 20,
        },
        {
            "name": "🇯🇵 日 Plus 试用",
            "plan": "plus",
            "aimizy_country": "JP",
            "aimizy_currency": "JPY",
            "seat_quantity": 1,
            "return_mode": "long",
            "is_preset": True,
            "sort_order": 30,
        },
        {
            "name": "🎯 Team 试用",
            "plan": "team",
            "aimizy_country": "SG",
            "aimizy_currency": "SGD",
            "seat_quantity": 5,
            "workspace_name": "MyTeam",
            "return_mode": "long",
            "is_preset": True,
            "sort_order": 40,
        },
    ]

    with Session(engine) as session:
        for preset in presets:
            existing = session.exec(
                select(LinkTemplate).where(LinkTemplate.name == preset["name"])
            ).first()
            if existing is not None:
                continue  # 已存在不覆盖，让运营改过的设置保留
            session.add(LinkTemplate(**preset))
        session.commit()
