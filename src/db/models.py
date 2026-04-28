# -*- coding: utf-8 -*-
"""
数据库模型定义

使用 SQLModel 同时满足 ORM 持久化和 Pydantic 数据校验需求。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Column, Field, SQLModel
from sqlalchemy import JSON, Integer, Text

from src.db.crypto import EncryptedString


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


# ── Run ──────────────────────────────────────────


class Run(SQLModel, table=True):
    """注册任务运行记录"""

    __tablename__ = "runs"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    email: str = Field(default="", max_length=255, index=True)
    password: str = Field(default="", sa_column=Column(EncryptedString(255), nullable=False, server_default=""))
    status: str = Field(default="pending", max_length=20, index=True)
    phase: str = Field(default="registration", max_length=40)
    retry_mode: str = Field(default="restart", max_length=20)
    profile_id: str = Field(default="", max_length=100)
    card_key: str = Field(default="", sa_column=Column(EncryptedString(255), nullable=False, server_default=""))
    browser_provider: str = Field(default="", max_length=100)
    card_provider: str = Field(default="", max_length=100)
    mail_provider: str = Field(default="", max_length=100)
    mail_account_id: str = Field(default="", max_length=32)
    is_card_warmed_up: bool = Field(default=False)
    warmup_account_id: str = Field(default="", max_length=100)
    # 弹性预热/养号调度字段
    next_action_at: Optional[datetime] = Field(default=None, index=True)  # 调度循环按 next_action_at <= now() 重入队
    warmup_pro_attempts: int = Field(default=0)  # 已对该卡完成的 Pro 账号代刷次数
    warmup_blocked_count: int = Field(default=0)  # 养号期间检测到 captcha/BLOCKED 累计次数
    config_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    error_reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ── RunEvent ─────────────────────────────────────


class RunEvent(SQLModel, table=True):
    """运行事件流水"""

    __tablename__ = "run_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(max_length=32, index=True)
    event_type: str = Field(max_length=40)
    state: Optional[str] = Field(default=None, max_length=40)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    timestamp: datetime = Field(default_factory=_utc_now)


# ── Checkpoint ───────────────────────────────────


class Checkpoint(SQLModel, table=True):
    """阶段检查点，用于断点续跑"""

    __tablename__ = "checkpoints"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(max_length=32, index=True)
    phase: str = Field(max_length=40)
    state: str = Field(default="", max_length=40)
    resumable_data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    created_at: datetime = Field(default_factory=_utc_now)


# ── ProviderConfig ───────────────────────────────


class ProviderConfig(SQLModel, table=True):
    """外部服务提供商配置"""

    __tablename__ = "provider_configs"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(max_length=20, index=True)
    provider_name: str = Field(max_length=60, index=True)
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    is_active: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=_utc_now)


class MailAccount(SQLModel, table=True):
    """凭据模式邮箱账号池。"""

    __tablename__ = "mail_accounts"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    label: str = Field(default="", max_length=120, index=True)
    provider_name: str = Field(default="applemail", max_length=60, index=True)
    email: str = Field(default="", max_length=255, index=True)
    client_id: str = Field(default="", sa_column=Column(EncryptedString(255), nullable=False, server_default=""))
    refresh_token: str = Field(default="", sa_column=Column(EncryptedString(), nullable=False, server_default=""))
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    # role 区分凭据账号用途：
    #   regular = 注册流程用作 credentialed 邮箱（任务级 mail_account_id 下拉只显示这种）
    #   pro_warmup = 卡预热垫脚石账号；不应被任务级邮箱选中
    role: str = Field(default="regular", max_length=20, index=True)
    is_active: bool = Field(default=True, index=True)
    last_verified_at: Optional[datetime] = Field(default=None)
    # 以下 4 个字段仅 role=pro_warmup 时有意义，由 ConfigService.select_warmup_account /
    # record_warmup_outcome 维护：
    #   last_used_at         上次被预热挑中的时间，用于按"最久没用"排序均衡分布
    #   cooldown_until       冷却到期时间（默认 last_used_at + 30 分钟）；select 会过滤未到期的
    #   consecutive_failures 连续登录失败次数；累计 ≥3 时自动 is_active=False
    #   last_failure_reason  最近一次失败原因，便于运维诊断
    last_used_at: Optional[datetime] = Field(default=None, index=True)
    cooldown_until: Optional[datetime] = Field(default=None, index=True)
    consecutive_failures: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    last_failure_reason: Optional[str] = Field(default=None, max_length=200)
    updated_at: datetime = Field(default_factory=_utc_now)


class AppSetting(SQLModel, table=True):
    """运行时持久化配置覆盖。"""

    __tablename__ = "app_settings"

    key: str = Field(primary_key=True, max_length=120)
    value: Any = Field(default="", sa_column=Column(JSON, nullable=False, server_default='""'))
    updated_at: datetime = Field(default_factory=_utc_now)


# ── User ─────────────────────────────────────────


class User(SQLModel, table=True):
    """控制台登录用户"""

    __tablename__ = "users"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    username: str = Field(default="", max_length=80, index=True, unique=True)
    password_hash: str = Field(default="", sa_column=Column(Text, nullable=False))
    role: str = Field(default="viewer", max_length=20, index=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ── AssistantActionLog ───────────────────────────


class AssistantActionLog(SQLModel, table=True):
    """助手问答 / 预览 / 审核 / 执行审计日志。"""

    __tablename__ = "assistant_action_logs"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    user_id: Optional[str] = Field(default=None, max_length=32, index=True)
    mode: str = Field(default="doc_qa", max_length=20, index=True)
    intent: str = Field(default="", max_length=80)
    action_type: str = Field(default="", max_length=80, index=True)
    status: str = Field(default="completed", max_length=40, index=True)
    title: str = Field(default="", max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    audit: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    parent_action_id: Optional[str] = Field(default=None, max_length=32, index=True)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ── ProviderConfigRevision ───────────────────────


class ProviderConfigRevision(SQLModel, table=True):
    """Provider 配置写前快照，用于审计与回滚。"""

    __tablename__ = "provider_config_revisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(max_length=20, index=True)
    provider_name: str = Field(max_length=60, index=True)
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    action_log_id: Optional[str] = Field(default=None, max_length=32, index=True)
    created_by: Optional[str] = Field(default=None, max_length=32, index=True)
    created_at: datetime = Field(default_factory=_utc_now)


# ── AppSettingRevision ───────────────────────────


class AppSettingRevision(SQLModel, table=True):
    """app_settings 写前快照，用于审计 / 回滚（消除 Ambiguity #6）。"""

    __tablename__ = "app_setting_revisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(max_length=120, index=True)
    previous_value: Any = Field(default="", sa_column=Column(JSON, nullable=False, server_default='""'))
    new_value: Any = Field(default="", sa_column=Column(JSON, nullable=False, server_default='""'))
    action_log_id: Optional[str] = Field(default=None, max_length=32, index=True)
    created_by: Optional[str] = Field(default=None, max_length=32, index=True)
    created_at: datetime = Field(default_factory=_utc_now)


class CardActivation(SQLModel, table=True):
    """虚拟卡激活信息持久化缓存。

    解决 X988 卡商 verify 接口一次性消耗的问题：
    第一次 verify 成功后把所有卡信息存这里，下次同一 cdk 直接拿缓存，
    避免重复调 verify 导致 'redeem code is invalid' 错误。

    主键 = card_key（一对一）。卡号/CVV/sms_api 用 EncryptedString 加密。
    """

    __tablename__ = "card_activations"

    card_key: str = Field(primary_key=True, max_length=64)
    card_provider: str = Field(default="x988card", max_length=40, index=True)

    # 卡片本身（敏感字段加密）
    card_number: str = Field(default="", sa_column=Column(EncryptedString(64), nullable=False, server_default=""))
    expiry_month: str = Field(default="", max_length=2)
    expiry_year: str = Field(default="", max_length=4)
    cvv: str = Field(default="", sa_column=Column(EncryptedString(16), nullable=False, server_default=""))
    name_on_card: str = Field(default="", max_length=120)
    billing_address: str = Field(default="", max_length=255)
    bin_country: str = Field(default="", max_length=8)

    # X988 特有动态资源（sms_api URL 也是敏感的）
    sms_api: str = Field(default="", sa_column=Column(EncryptedString(255), nullable=False, server_default=""))
    phone: str = Field(default="", max_length=40)

    # 调度元信息
    activated_at: datetime = Field(default_factory=_utc_now, index=True)
    last_used_at: Optional[datetime] = Field(default=None)
    use_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    is_invalidated: bool = Field(default=False, index=True)
    invalidate_reason: Optional[str] = Field(default=None, max_length=200)
