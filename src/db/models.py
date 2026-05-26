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
from sqlalchemy import JSON, Boolean, Integer, String, Text

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
    # 卡 BIN 前 6 位，用于跨 Run 聚合统计 BIN 健康度（H2 风控审计）。
    # 非加密 —— BIN 不是敏感信息，且加密会破坏聚合查询能力。
    # server_default="" 保证旧库 ALTER TABLE 后 raw SQL INSERT 仍可省略此列。
    card_bin: str = Field(
        default="",
        sa_column=Column(String(8), nullable=False, server_default="", index=True),
    )
    # 弹性预热/养号调度字段
    next_action_at: Optional[datetime] = Field(default=None, index=True)  # 调度循环按 next_action_at <= now() 重入队
    warmup_pro_attempts: int = Field(default=0)  # 已对该卡完成的 Pro 账号代刷次数
    warmup_blocked_count: int = Field(default=0)  # 养号期间检测到 captcha/BLOCKED 累计次数
    # 本 Run 内 Stripe decline 重试计数（decline_retry_service 写入）。
    # 用途：① 控制台/日志可视化；② BIN 健康度查询时按 Run 聚合 decline 次数。
    decline_attempts: int = Field(default=0)
    # 注册时浏览器实际走的代理出口 IP（ChatGPT 服务端看到的 IP）。
    # 由 orchestrator preflight 调 ipinfo.io 抓取写入，失败留空；账号池列表展示用。
    # 非加密 —— IP 不是个人敏感信息，且后续聚合分析（同 IP 多号、IP 段位历史）需要明文。
    # IPv6 最长 45 字符（`0000:0000:0000:0000:0000:ffff:255.255.255.255`）。
    ip_address: str = Field(
        default="",
        sa_column=Column(String(45), nullable=False, server_default=""),
    )
    # 出口 IP 对应的国家 2 字母代码（ipinfo.io country 字段）；为空表示未抓到或未启用。
    ip_country: str = Field(
        default="",
        sa_column=Column(String(8), nullable=False, server_default=""),
    )
    config_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    # OpenAI session tokens（注册成功后 worker 写入，号池导出 cpa 格式时读取）：
    #   {"access_token": "ey...", "refresh_token": "...", "id_token": "",
    #    "extracted_at": "2026-05-12T03:21:00Z", "expires_at": ""}
    # server_default="{}" 保证旧库 ALTER 后 raw SQL INSERT 仍可省略此列。
    openai_tokens: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    error_reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    # 账号池段位（与 status 正交）：
    #   registered  — Phase 1+2 完成但未绑卡（普号池里等待绑卡）
    #   plus        — 已成功绑 ChatGPT Plus
    #   team        — 已成功绑 ChatGPT Team
    #   abandoned   — 绑卡失败且运维标记放弃
    account_tier: str = Field(
        default="registered",
        sa_column=Column(String(20), nullable=False, server_default="registered", index=True),
    )
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

    # 卡池预热元信息（手动入池 + 手动按钮触发预热模式）
    # target_warmup_count = 0 表示老数据 / 不需要预热（兼容历史 CardActivation 行）
    target_warmup_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    warmup_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    warmed_at: Optional[datetime] = Field(default=None)
    # last_warmup_status: pending(刚入池) / running(后台线程跑中) / success / failed
    last_warmup_status: str = Field(
        default="pending",
        sa_column=Column(String(20), nullable=False, server_default="pending", index=True),
    )
    last_warmup_reason: Optional[str] = Field(default=None, max_length=200)


# ── LinkTemplate ─────────────────────────────────
#
# 用户保存的"checkout 链接生成器表单"快照，用于号池页生成 hosted checkout 链接时复用。
# 不存可执行 JS，只是把 plan/seats/promo/country/currency/workspace 这些字段成组打包。
# CRUD 由 admin 管理；按 name 去重（unique）。


class LinkTemplate(SQLModel, table=True):
    """生成链接表单模板（号池"生成链接"弹窗里点"保存模板"产出的预填值）。"""

    __tablename__ = "link_templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 模板名：admin 用来在下拉里识别，例如"UK promo datroaiuk"
    name: str = Field(max_length=80, sa_column=Column(String(80), nullable=False, unique=True, index=True))
    plan: str = Field(default="team", max_length=20)          # team / plus / pro / pro_lite
    seat_quantity: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default="1"))
    promo_code: str = Field(default="", max_length=80)        # URL 优惠码（拼到 cancel_url）
    promo_campaign_id: str = Field(default="", max_length=80) # payload 级 promo（仅 Team 走）
    aimizy_country: str = Field(default="", max_length=8)
    aimizy_currency: str = Field(default="", max_length=8)
    workspace_name: str = Field(default="", max_length=120)
    return_mode: str = Field(default="long", max_length=10)   # long / app
    # 可选关联代理（proxies.id）：模板里指定后，生成链接时自动用这个代理出口
    # None = 用 .env 默认 PROXY。不做外键约束——代理删了模板里残留 id 自动失效（前端 fallback）
    proxy_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utc_now)

    # ── P4 模板化扩展字段（2026-05-25 schemas 包重构同步加入）────────────
    # 单点覆盖全局默认 schema 版本（None → 用 .env PLUS_SCHEMA_VERSION / 内置默认）
    # 例：v2 / v3，对应 src/payment_link/schemas/<plan>_<version>.py
    schema_version: Optional[str] = Field(default=None, max_length=20)
    # 生成 URL 后自动注入 ?locale=xxx（'en' / 'ja' / 'zh-CN'），None 不注入
    # 控制 Stripe hosted page 的渲染语言，对 OpenAI 后端 trial 校验无影响
    url_locale: Optional[str] = Field(default=None, max_length=10)
    # 浅合并到 OpenAI payload 的额外字段（运营 UI 低代码扩展逃生口）
    # 注意：dict 类型，会覆盖同名 payload 字段；不存任意可执行代码
    extra_payload_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # 标记预设模板：True 时在弹窗以快捷按钮形式呈现
    is_preset: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="0", index=True),
    )
    # 预设按钮排序（小→大）
    sort_order: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    # P6 暴露 checkout_ui_mode（hosted / custom）；None = 用 .env 全局默认或 'hosted'
    # 仅 Plus schema 真正生效；Team / Pro 接收但忽略（行为按各自 schema 内部规则）
    checkout_ui_mode: Optional[str] = Field(default=None, max_length=10)

    # ── promo 码 eligibility 验证状态（promo_eligibility 模块写入）────────
    # status 取值：eligible / exists / not_found / unknown / error
    # 含义：promo_code 在 last_eligibility_check_at 时刻调 ChatGPT eligibility API 的结果
    # 为空字符串表示从未验证过
    last_eligibility_status: str = Field(
        default="",
        sa_column=Column(String(20), nullable=False, server_default="", index=True),
    )
    last_eligibility_check_at: Optional[datetime] = Field(default=None, index=True)
    # 原始 metadata 响应（折扣金额/币种/时长等），便于 dashboard 展示
    last_eligibility_metadata: Optional[dict] = Field(default=None, sa_column=Column(JSON))


# Eligibility 状态常量（不入库，纯 Python 枚举字符串）
# 使用字符串常量而非 Enum，便于 SQL 直接对比 + JSON 序列化
class EligibilityStatus:
    ELIGIBLE = "eligible"      # 当前账号 + 当前代理可直接用
    EXISTS = "exists"          # 码存在但地区不匹配（user_not_eligible）
    NOT_FOUND = "not_found"    # 码不存在（invalid_code）或 token 过期
    UNKNOWN = "unknown"        # API 返回了未识别的 reason_code
    ERROR = "error"            # 网络/解析异常

    ALL = (ELIGIBLE, EXISTS, NOT_FOUND, UNKNOWN, ERROR)


# ── Proxy ────────────────────────────────────────
#
# 代理池：号池"生成 checkout 链接"按号选代理出口 IP。注册流程 / token 提取 /
# aimizy 中转**不读这张表**，继续用 .env 全局 PROXY。
# 代理 URL 用 EncryptedString 加密落盘（跟 MailAccount.refresh_token 同款）。


class Proxy(SQLModel, table=True):
    """代理池（仅用于"生成 checkout 链接"按号选 IP 出口）。"""

    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 代理标签：UI 下拉里给运维识别用，例如 "1024Proxy CA #1"
    label: str = Field(
        max_length=80,
        sa_column=Column(String(80), nullable=False, unique=True, index=True),
    )
    # 完整代理 URL（含用户名/密码）— 加密落盘
    url: str = Field(
        default="",
        sa_column=Column(EncryptedString(512), nullable=False, server_default=""),
    )
    # ISO 国家代码（CA/GB/SG 等）— 仅是 UI 提示，不和实际出口校验
    country: str = Field(default="", max_length=8, index=True)
    notes: str = Field(default="", max_length=200)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utc_now)
