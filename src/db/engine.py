# -*- coding: utf-8 -*-
"""
数据库引擎管理

默认 SQLite，通过 DATABASE_URL 环境变量可切换为 PostgreSQL。
"""

from __future__ import annotations

import os
import logging
from typing import Optional

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
    # app_setting_revisions 表由 SQLModel.metadata.create_all() 自动建（init_db 调用），
    # 这里无需手工 CREATE TABLE。


def _table_columns(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {str(item.get("name") or "") for item in inspector.get_columns(table_name)}


def _seed_runtime_defaults(engine: Engine) -> None:
    """为全新/升级后的控制台补齐默认 provider 与邮箱账号。"""
    from sqlmodel import select

    from src.config import load_config
    from src.db.models import AppSetting, MailAccount, ProviderConfig

    config = load_config()
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
