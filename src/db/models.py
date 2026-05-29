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
    # 手机号注册模式（registration_kind="phone"）专用：
    # phone_number = 任务请求时用户填入或由 worker._execute_task_inner 通过
    # SMSManager.get_number() 申领后回写的国际格式手机号（含国家码，如 +14155551212）。
    # sms_order_id = SMS-Activate 订单号，runtime handler 用它 get_code 轮询 OTP。
    # 非加密 —— 这两个字段需要参与「同号复用 / 跨任务统计」查询。
    phone_number: str = Field(
        default="",
        sa_column=Column(String(40), nullable=False, server_default=""),
    )
    sms_order_id: str = Field(
        default="",
        sa_column=Column(String(64), nullable=False, server_default=""),
    )
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

    # ── 从 last_eligibility_metadata 抽取的结构化字段（P4 引入）────────
    # 抽取由 promo_eligibility_service._extract_promo_fields() 完成；字段路径基于
    # scripts/probe_promo_metadata.py 探测的真实 ChatGPT /promotions/metadata 响应。
    # 这些字段让号池升级弹窗"快捷模板"下拉能显示折扣力度并排序，而不是只显示模板名。
    # 全部 Optional：码未验证 / metadata API 缺失对应字段时为 None。
    #
    # 折扣百分比（0-100）。例：25 表示 25% off。索引便于按力度排序。
    promo_percent_off: Optional[int] = Field(default=None, index=True)
    # 折扣月数。例：12 = 12 个月内享折扣。
    promo_duration_months: Optional[int] = Field(default=None)
    # 促销过期时间（UTC）。可做"快过期告警"用。
    promo_expires_at: Optional[datetime] = Field(default=None, index=True)
    # 最大兑换次数（None 表示无上限或 metadata 未返回）。
    promo_max_redemptions: Optional[int] = Field(default=None)
    # 适用计划 CSV，如 "plus,team"。空串 = 适用全部或 metadata 未返回。
    promo_applicable_plans: str = Field(default="", max_length=120)


# Eligibility 状态常量（不入库，纯 Python 枚举字符串）
# 使用字符串常量而非 Enum，便于 SQL 直接对比 + JSON 序列化
class EligibilityStatus:
    ELIGIBLE = "eligible"      # 当前账号 + 当前代理可直接用
    EXISTS = "exists"          # 码存在但地区不匹配（user_not_eligible）
    NOT_FOUND = "not_found"    # 码不存在（invalid_code）或 token 过期
    REDEEMED = "redeemed"      # 码已被兑换过（code_already_redeemed）
    UNKNOWN = "unknown"        # API 返回了未识别的 reason_code
    ERROR = "error"            # 网络/解析异常

    ALL = (ELIGIBLE, EXISTS, NOT_FOUND, REDEEMED, UNKNOWN, ERROR)


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


# ── ProxyProvider ────────────────────────────────
#
# 动态代理供应商：存"按需拉取 IP"的 API 端点配置（如 1024Proxy / IPRoyal）。
# 与 Proxy（静态死 IP）正交：Proxy 一条 = 一个固定 host:port；ProxyProvider 一条
# = 一个 API 套餐，运行时调 adapter 现拉一次性 IP（家庭住宅 IP 池）。
#
# 接入逻辑见：
#   src/proxy_clients/adapters/base.py    ProviderAdapter ABC + 模板方法
#   src/proxy_clients/adapters/registry.py  按 kind 分发
#   src/proxy_clients/dynamic_pool.py     "每 N 条换 IP" 状态机
#
# 主要消费方：src/services/code_discovery_service.py (promo 探索任务)
# 注册流程不读这张表（继续走 .env PROXY），与现有 fetch_proxy() 解耦。


class ProxyProvider(SQLModel, table=True):
    """动态代理供应商：存 API 端点配置 + 鉴权方式，运行时按需拉一次性 IP。"""

    __tablename__ = "proxy_providers"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 供应商标签（UI 下拉识别），如 "1024 美国住宅" / "IPRoyal 全球随机"
    label: str = Field(
        max_length=80,
        sa_column=Column(String(80), nullable=False, unique=True, index=True),
    )
    # 适配器分发键：决定调 ProviderAdapter 子类。
    # 本轮支持 "1024proxy" / "generic_http"；后续可扩 "iproyal" / "brightdata" 等。
    kind: str = Field(
        max_length=32,
        sa_column=Column(String(32), nullable=False),
    )
    # URL 模板（加密）。占位符由 adapter 渲染：{country} {num} {format} {session}
    # 1024 示例：
    #   https://white.1024proxy.com/white/api?region={country}&num={num}&time=10&format=1&type=txt&session={session}
    api_url_template: str = Field(
        default="",
        sa_column=Column(EncryptedString(512), nullable=False, server_default=""),
    )
    # 鉴权方式：决定 credentials 字段结构与 build_request_kwargs 行为。
    #   "ip_whitelist" → credentials={"whitelisted_ip": "47.251.25.143"}（仅展示，不发送）
    #   "api_key"      → credentials={"key": "xxx", "header_name": "X-API-Key"}
    #   "basic_auth"   → credentials={"username": "u", "password": "p"}
    #   "none"         → credentials={}
    auth_kind: str = Field(
        default="none",
        sa_column=Column(String(20), nullable=False, server_default="none"),
    )
    # 凭据 JSON（加密）。结构按 auth_kind 决定；UI 层负责脱敏返回。
    credentials: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # 响应格式：决定 parse_proxy_response 的解析路径。
    #   "txt_line"             → 每行 host:port
    #   "json_array_host_port" → [{"host":..., "port":...}, ...]
    response_format: str = Field(
        default="txt_line",
        sa_column=Column(String(32), nullable=False, server_default="txt_line"),
    )
    # 国家码映射：项目 ISO alpha-2（GB/US）→ 供应商专属（UK/US）。
    # 空 dict 表示直接透传；例 1024：{"GB": "UK", "Default": "Rand"}
    country_map: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # 轮换阈值：每扫 N 条 promo 换一次 IP（DynamicProxyPool 用）。50 是本计划拍板默认。
    rotation_per_n_requests: int = Field(
        default=50,
        sa_column=Column(Integer, nullable=False, server_default="50"),
    )
    # Sticky 窗口（秒）：供应商保持同 IP 的时长；用于 UI 提示和未来 sticky 检测。
    # 1024 推荐 600（10 分钟）。0 表示供应商每次请求都返回新 IP（无需 session 占位符）。
    sticky_seconds: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    # UI 默认值：建任务时该供应商默认拉哪个国家的 IP
    default_country: str = Field(default="Rand", max_length=8)
    notes: str = Field(default="", max_length=200)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utc_now)


# ── RegistrationProfile ──────────────────────────
#
# 注册方式 × 供应商组合：把散落在 AppConfig 的 12 个选择器字段
# （default_browser_provider / default_card_provider / default_mail_provider /
#  card_provider / email_provider_name / outlook_enabled / cfworker_enabled / ...）
# 收敛成"按注册方式（email/phone）预设一套 provider 组合"。
#
# 与 ProviderConfig 的关系：
#   ProviderConfig  = 单个外部平台的凭据/驱动参数（如 card-efuncard 的 token）
#   RegistrationProfile = 一组 ProviderConfig 名字的命名引用（如 email-default 引用
#     browser-default + mail-cfworker-default + card-default）
#
# provider_bindings JSON 结构：
#   { "browser": "browser-default",
#     "card":    "card-default",
#     "mail":    "mail-cfworker-default",
#     "sms":     "sms-activate",       # phone 模式才需要
#     ... }
# value 必须是 ProviderConfig.provider_name 现有值；resolve 时若指向不存在的 name 报错。


class RegistrationProfile(SQLModel, table=True):
    """注册方式预设：把一组 provider 命名引用打包成"组合"。"""

    __tablename__ = "registration_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 组合名：UI 用来识别（如 "email-default" / "phone-default"）。
    # unique 防止同名重复；index 加速 set_default / get_default 查询。
    name: str = Field(
        max_length=60,
        sa_column=Column(String(60), nullable=False, unique=True, index=True),
    )
    # 注册方式：email / phone。
    # 一对一约束的软实现：靠 (registration_kind, is_default=True) 仅允许 1 条
    # （由 service 层在 set_default 时保证；DB 层不加 partial unique 索引，
    # 一是 SQLite/PG 写法不同，二是留一对多扩展空间）。
    registration_kind: str = Field(max_length=20, index=True)
    # provider 槽位 → ProviderConfig.provider_name 映射。
    # 槽位约定：browser / card / mail / sms / captcha / llm（按需补充）。
    # 不强校验槽位名称，让未来加新 provider 类型零 schema 改动。
    provider_bindings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    description: Optional[str] = Field(default=None, max_length=200)
    # 该 kind 的默认组合标志；同一 kind 仅允许 1 条 is_default=True（service 层保证）。
    is_default: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="0", index=True),
    )
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ── RegistrationProfileRevision ──────────────────
#
# 与 ProviderConfigRevision / AppSettingRevision 同款：写前 snapshot 旧值，
# 串 action_log_id 让助手 commit 可回溯，给未来接入 AssistantService 留接口。


class RegistrationProfileRevision(SQLModel, table=True):
    """RegistrationProfile 写前快照，用于审计与回滚。"""

    __tablename__ = "registration_profile_revisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_name: str = Field(max_length=60, index=True)
    # snapshot 结构：{"exists": bool, "registration_kind": str,
    #               "provider_bindings": dict, "description": str|None,
    #               "is_default": bool, "is_active": bool}
    snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    action_log_id: Optional[str] = Field(default=None, max_length=32, index=True)
    created_by: Optional[str] = Field(default=None, max_length=32, index=True)
    created_at: datetime = Field(default_factory=_utc_now)


# ── SyntheticCardAudit ────────────────────────────
#
# 合成卡生成审计表（A2）：用于"哪个 BIN + 哪个 State 的组合通过率最高"分析。
# 合规：**只存 BIN 前 4 位 + 卡号后 4 位**，绝不存完整卡号 / CVV。
# 数据闭环：生成时插 pending → 用户绑卡后通过 feedback 端点回写 success/declined。


class SyntheticCardAudit(SQLModel, table=True):
    """合成卡生成与绑卡反馈审计记录（不存完整卡号）。"""

    __tablename__ = "synthetic_card_audits"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    bin_prefix: str = Field(max_length=8, index=True)           # 4147 / 4100 等
    last_four: str = Field(max_length=4)                        # 卡号末 4 位
    address_state: str = Field(default="", max_length=4, index=True)   # NY / CA / ...
    address_zip: str = Field(default="", max_length=10)
    first_name: str = Field(default="", max_length=60)
    last_name: str = Field(default="", max_length=60)
    # feedback_status: pending(刚生成) / success(绑卡通过) / declined(绑卡被拒)
    feedback_status: str = Field(
        default="pending",
        sa_column=Column(String(20), nullable=False, server_default="pending", index=True),
    )
    decline_code: Optional[str] = Field(default=None, max_length=60)
    feedback_note: Optional[str] = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=_utc_now, index=True)
    feedback_at: Optional[datetime] = Field(default=None)
    created_by: Optional[str] = Field(default=None, max_length=32, index=True)
