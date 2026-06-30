# -*- coding: utf-8 -*-
"""
配置服务

支持从数据库读写配置，实现热更新语义：
- 新任务立即使用最新配置
- 运行中任务在 Phase 边界重读配置
- Run 表保存 config_snapshot 确保可复现
"""

from __future__ import annotations

import logging
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, get_args, get_origin

from sqlmodel import Session, select

from src.automation.artifacts import redact_structure
from src.config import AppConfig, load_config
from src.db.engine import get_session
from src.db.models import AppSetting, MailAccount, ProviderConfig, ProviderConfigRevision

logger = logging.getLogger(__name__)


# MailAccount.role 合法取值（消除 Ambiguity #2）
VALID_MAIL_ACCOUNT_ROLES = frozenset({"regular", "pro_warmup"})

# 预热号池调度参数（用户已确认）
# 同一垫脚石账号被挑中后冷却 30 分钟，避免短时反复登录被 OpenAI 风控
WARMUP_COOLDOWN_MINUTES = 30
# 连续登录失败阈值，达到后自动 is_active=False 剔除该账号
WARMUP_MAX_CONSECUTIVE_FAILURES = 3


class MailAccountInDefaultUseError(Exception):
    """删除被 default_mail_account_id 引用的 MailAccount 时抛出（消除 Ambiguity #5）。"""

    def __init__(self, account_id: str) -> None:
        super().__init__(
            f"邮箱账号 {account_id} 当前被 default_mail_account_id 引用，"
            f"请先在 /config 改默认账号或留空再删除。"
        )
        self.account_id = account_id


class InvalidMailAccountRoleError(ValueError):
    """role 不在白名单时抛出。"""

    def __init__(self, role: str) -> None:
        super().__init__(
            f"role={role!r} 不合法，必须是 {sorted(VALID_MAIL_ACCOUNT_ROLES)} 之一"
        )
        self.role = role


class ConfigService:
    """
    配置管理服务。

    在 .env 加载的 AppConfig 基础上，叠加数据库中的动态覆盖，
    并提供 Provider 配置的 CRUD 操作。
    """

    def __init__(self, dotenv_path: Optional[str] = None) -> None:
        self._base_config = load_config(dotenv_path)
        self._overrides: dict[str, Any] = {}
        self._load_persisted_overrides()

    def get_config(self) -> AppConfig:
        """获取当前生效的配置（.env 基础 + 动态覆盖）。"""
        if not self._overrides:
            return self._base_config

        base = asdict(self._base_config)
        merged = {**base, **self._overrides}

        valid_fields = {f.name for f in fields(AppConfig)}
        filtered = {k: v for k, v in merged.items() if k in valid_fields}
        return AppConfig(**filtered)

    def update_config(self, updates: dict[str, Any], *, allowed_fields: Optional[set[str]] = None) -> AppConfig:
        """
        动态更新配置（内存覆盖层）。

        Args:
            updates: 要覆盖的字段字典

        Returns:
            更新后的 AppConfig
        """
        valid_fields = {f.name for f in fields(AppConfig)}
        applied = {}
        app_fields = {f.name: f for f in fields(AppConfig)}
        field_allowlist = allowed_fields or set(app_fields.keys())
        for key, value in updates.items():
            if key not in field_allowlist:
                logger.warning("忽略非白名单配置字段: %s", key)
                continue
            if key in valid_fields:
                coerced = self._coerce_value(value, app_fields[key].type)
                self._overrides[key] = coerced
                self._persist_override(key, coerced)
                applied[key] = coerced
            else:
                logger.warning("忽略未知配置字段: %s", key)

        if applied:
            logger.info("配置已更新: %s", list(applied.keys()))

        return self.get_config()

    def get_config_snapshot(self) -> dict[str, Any]:
        """生成配置快照（用于 Run.config_snapshot，脱敏处理）。"""
        config = self.get_config()
        snapshot = asdict(config)

        sensitive_keys = {
            "efuncard_token", "sms_api_key", "email_provider_api_key",
            "mail_refresh_token", "mail_client_id", "llm_api_key",
            "ads_api_key", "task_password", "proxy", "known_mail_accounts_json",
        }
        for key in sensitive_keys:
            if key in snapshot and snapshot[key]:
                value = str(snapshot[key])
                snapshot[key] = f"{value[:4]}****" if len(value) > 4 else "****"

        return snapshot

    def get_editable_config_snapshot(self) -> dict[str, Any]:
        """返回允许在线修改的配置字段快照。"""
        snapshot = self.get_config_snapshot()
        return {key: snapshot[key] for key in self.SAFE_UPDATE_FIELDS if key in snapshot}

    def reload_base(self, dotenv_path: Optional[str] = None) -> AppConfig:
        """重新从 .env 加载基础配置。"""
        self._base_config = load_config(dotenv_path)
        self._load_persisted_overrides()
        logger.info("基础配置已重新加载。")
        return self.get_config()

    def get_mail_accounts(
        self,
        *,
        provider_name: Optional[str] = None,
        active_only: bool = False,
    ) -> list[MailAccount]:
        """查询邮箱账号池。"""
        with get_session() as session:
            stmt = select(MailAccount)
            if provider_name:
                stmt = stmt.where(MailAccount.provider_name == provider_name)
            if active_only:
                stmt = stmt.where(MailAccount.is_active == True)  # noqa: E712
            stmt = stmt.order_by(MailAccount.updated_at.desc())  # type: ignore[arg-type]
            return list(session.exec(stmt).all())

    def get_mail_account(self, account_id: str) -> Optional[MailAccount]:
        with get_session() as session:
            return session.get(MailAccount, account_id)

    def save_mail_account(
        self,
        *,
        account_id: Optional[str] = None,
        label: str,
        provider_name: str,
        email: str,
        client_id: str,
        refresh_token: str,
        extra: Optional[dict[str, Any]] = None,
        is_active: bool = True,
        role: str = "regular",
    ) -> MailAccount:
        """创建或更新邮箱账号。

        role 取值见 ``VALID_MAIL_ACCOUNT_ROLES``：
          - regular: 注册流程用作 credentialed 邮箱
          - pro_warmup: 卡预热垫脚石账号（不在任务级邮箱下拉中显示）
        """
        normalized_provider = str(provider_name or "").strip().lower() or "applemail"
        normalized_email = str(email or "").strip().lower()
        normalized_role = str(role or "regular").strip().lower() or "regular"
        if normalized_role not in VALID_MAIL_ACCOUNT_ROLES:
            raise InvalidMailAccountRoleError(normalized_role)
        with get_session() as session:
            account = session.get(MailAccount, account_id) if account_id else None
            if account is None and account_id:
                account = None
            if account is None:
                account = MailAccount(
                    label=str(label or normalized_email).strip() or normalized_email,
                    provider_name=normalized_provider,
                    email=normalized_email,
                    client_id=str(client_id or "").strip(),
                    refresh_token=str(refresh_token or "").strip(),
                    extra=dict(extra or {}),
                    is_active=bool(is_active),
                    role=normalized_role,
                )
            else:
                # 检测 reactivate 场景（is_active 从 False → True）：
                # 必须清掉残留 consecutive_failures + cooldown_until，否则
                # select_warmup_account 会跳过这个账号 / 一次失败立刻又被自动 disable。
                was_inactive = not bool(account.is_active)
                will_be_active = bool(is_active)
                is_reactivate = was_inactive and will_be_active

                account.label = str(label or normalized_email).strip() or normalized_email
                account.provider_name = normalized_provider
                account.email = normalized_email
                new_client_id = str(client_id or "").strip()
                new_refresh_token = str(refresh_token or "").strip()
                if new_client_id:
                    account.client_id = new_client_id
                if new_refresh_token:
                    account.refresh_token = new_refresh_token
                # pro_warmup 编辑场景：UI 不会回显 password（redact 后看不到原值），
                # 用户只改 profile_id 不重输密码时，新 extra 不会带 password —— 此时保留旧密码。
                merged_extra = dict(extra or {})
                if normalized_role == "pro_warmup":
                    old_extra = dict(account.extra or {})
                    if "password" not in merged_extra and old_extra.get("password"):
                        merged_extra["password"] = old_extra["password"]
                account.extra = merged_extra
                account.is_active = will_be_active
                account.role = normalized_role

                if is_reactivate and normalized_role == "pro_warmup":
                    account.consecutive_failures = 0
                    account.cooldown_until = None
                    account.last_failure_reason = None
                    logger.info(
                        "预热账号 %s 被重新启用，调度状态已自动重置（fails=0 / cooldown=None）",
                        account.id,
                    )

                account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    def delete_mail_account(self, account_id: str) -> bool:
        """删除邮箱账号。

        若该账号被 ``default_mail_account_id`` 引用，抛 ``MailAccountInDefaultUseError``
        而非静默删除（消除 Ambiguity #5）。
        """
        # 校验是否为默认引用账号
        current_default = str(self.get_config().default_mail_account_id or "").strip()
        if current_default and current_default == str(account_id or "").strip():
            raise MailAccountInDefaultUseError(account_id)

        with get_session() as session:
            account = session.get(MailAccount, account_id)
            if account is None:
                return False
            session.delete(account)
            session.commit()
            return True

    def mark_mail_account_verified(self, account_id: str) -> Optional[MailAccount]:
        with get_session() as session:
            account = session.get(MailAccount, account_id)
            if account is None:
                return None
            account.last_verified_at = datetime.now(timezone.utc)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    # ── 卡预热号池调度 ─────────────────────────────

    def select_warmup_account(self) -> Optional[MailAccount]:
        """从 pro_warmup 号池挑一个最久没用、未在冷却期、未禁用的账号。

        选中的同时立即更新 ``last_used_at`` 和 ``cooldown_until`` 占位，避免
        多 worker 并发挑中同一个账号（SQLite ``BEGIN IMMEDIATE`` 隐式拿写锁）。

        Returns:
            选中的 MailAccount（已 expunge 出 session，调用方只读使用）；
            池为空 / 全部冷却中 / 全部被禁用 → None
        """
        now = datetime.now(timezone.utc)
        with get_session() as session:
            stmt = (
                select(MailAccount)
                .where(MailAccount.role == "pro_warmup")
                .where(MailAccount.is_active == True)  # noqa: E712
            )
            candidates = list(session.exec(stmt).all())

            # cooldown_until 为 NULL 视为可用（从未被用过）；否则要求已过期
            available = [
                acc for acc in candidates
                if acc.cooldown_until is None or _ensure_aware(acc.cooldown_until) <= now
            ]
            if not available:
                logger.info(
                    "号池中无可用 pro_warmup 账号: 总数=%d 全部冷却中或禁用",
                    len(candidates),
                )
                return None

            # 按 last_used_at 升序：从未用过的（NULL）最优先，再是最久没用的
            available.sort(key=lambda a: (
                _ensure_aware(a.last_used_at) if a.last_used_at is not None else datetime.min.replace(tzinfo=timezone.utc),
            ))
            chosen = available[0]

            # 立即占位，防止其他 worker 并发挑中
            chosen.last_used_at = now
            chosen.cooldown_until = now + timedelta(minutes=WARMUP_COOLDOWN_MINUTES)
            chosen.updated_at = now
            session.add(chosen)
            session.commit()
            session.refresh(chosen)
            session.expunge(chosen)
            logger.info(
                "已挑选预热账号 %s (%s)，冷却到 %s",
                chosen.id, chosen.email or chosen.label, chosen.cooldown_until,
            )
            return chosen

    def record_warmup_outcome(
        self,
        account_id: str,
        *,
        success: bool,
        reason: str = "",
        failure_class: str = "account_failure",
    ) -> Optional[MailAccount]:
        """登录尝试后写回结果。

        - success=True：清零 consecutive_failures + 清空 last_failure_reason
        - success=False, failure_class='account_failure'：
          累加 consecutive_failures；累计达 ``WARMUP_MAX_CONSECUTIVE_FAILURES``
          自动 ``is_active=False`` 并清 ``cooldown_until=None``（运维需手动恢复）
        - success=False, failure_class='external_failure'：
          只更新 last_failure_reason，**不累计 consecutive_failures**。
          这是为了避免远程邮件服务 5xx / AdsPower 启动失败这类外部依赖
          抖动把整个 pro_warmup 号池连带 disable。

        Args:
            account_id: MailAccount.id
            success: 这次登录/预热是否成功
            reason: 失败时的简要原因（截断到 200 字符），便于运维诊断
            failure_class: 失败归因，仅 success=False 时生效。
                'account_failure'（默认）= 账号自身原因（cookies 失效 / 风控），累计计数
                'external_failure'        = 外部依赖原因（邮件 5xx / AdsPower 抖动），不累计

        Returns:
            更新后的 MailAccount；账号不存在返回 None
        """
        with get_session() as session:
            account = session.get(MailAccount, account_id)
            if account is None:
                logger.warning("record_warmup_outcome: 账号 %s 不存在", account_id)
                return None
            now = datetime.now(timezone.utc)
            if success:
                account.consecutive_failures = 0
                account.last_failure_reason = None
            else:
                # 标准化 failure_class，未知值按 account_failure 兜底
                normalized_class = str(failure_class or "").strip().lower()
                if normalized_class not in ("account_failure", "external_failure"):
                    normalized_class = "account_failure"

                # 失败原因写入：external 时打 [external] 前缀，便于运维 grep
                reason_text = (str(reason or "")[:200]) or "unknown"
                if normalized_class == "external_failure":
                    account.last_failure_reason = f"[external] {reason_text}"[:200]
                    logger.info(
                        "预热账号 %s 外部依赖失败（不累计计数）: %s",
                        account.id, reason_text,
                    )
                else:
                    account.consecutive_failures = int(account.consecutive_failures or 0) + 1
                    account.last_failure_reason = reason_text
                    if account.consecutive_failures >= WARMUP_MAX_CONSECUTIVE_FAILURES:
                        account.is_active = False
                        # 清 cooldown_until：运维 reactivate 时立即可被 select 选中，
                        # 避免被旧 cooldown 锁住。
                        account.cooldown_until = None
                        logger.warning(
                            "预热账号 %s (%s) 连续失败 %d 次，已自动禁用并清 cooldown",
                            account.id, account.email or account.label, account.consecutive_failures,
                        )
            account.updated_at = now
            session.add(account)
            session.commit()
            session.refresh(account)
            session.expunge(account)
            return account

    def reset_warmup_account(self, account_id: str) -> Optional[MailAccount]:
        """重置预热账号的调度状态（清失败计数 + cooldown + 失败原因）。

        典型用途：运维通过 UI 把 ``is_active`` 从 False → True 重新启用账号时，
        必须配套调用本方法清掉旧的 ``consecutive_failures=3`` 与残留 ``cooldown_until``，
        否则 reactivate 后 select 仍会跳过该账号 / 一次失败立刻又被自动 disable。

        本方法**不动 ``is_active``**，只重置调度元数据。

        Returns:
            更新后的 MailAccount；账号不存在返回 None
        """
        with get_session() as session:
            account = session.get(MailAccount, account_id)
            if account is None:
                logger.warning("reset_warmup_account: 账号 %s 不存在", account_id)
                return None
            now = datetime.now(timezone.utc)
            account.consecutive_failures = 0
            account.cooldown_until = None
            account.last_failure_reason = None
            account.updated_at = now
            session.add(account)
            session.commit()
            session.refresh(account)
            session.expunge(account)
            logger.info("预热账号 %s 调度状态已重置", account.id)
            return account

    # ── Provider 配置 CRUD ─────────────────────────

    def get_provider_configs(
        self,
        provider_type: Optional[str] = None,
        *,
        active_only: bool = False,
    ) -> list[ProviderConfig]:
        """查询 Provider 配置列表。"""
        with get_session() as session:
            stmt = select(ProviderConfig)
            if provider_type:
                stmt = stmt.where(ProviderConfig.provider_type == provider_type)
            if active_only:
                stmt = stmt.where(ProviderConfig.is_active == True)  # noqa: E712
            return list(session.exec(stmt).all())

    def get_provider_config(self, provider_type: str, provider_name: str) -> Optional[ProviderConfig]:
        """查询单个 Provider 配置。"""
        with get_session() as session:
            stmt = select(ProviderConfig).where(
                ProviderConfig.provider_type == provider_type,
                ProviderConfig.provider_name == provider_name,
            )
            return session.exec(stmt).first()

    def resolve_provider_config(
        self,
        provider_type: str,
        provider_name: str = "",
        *,
        default_name: str = "",
        active_only: bool = True,
    ) -> Optional[ProviderConfig]:
        """按显式选择 → 默认配置 解析 Provider。"""
        candidates = [
            str(provider_name or "").strip(),
            str(default_name or "").strip(),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            config = self.get_provider_config(provider_type, candidate)
            if config is None:
                continue
            if active_only and not config.is_active:
                continue
            return config
        return None

    def resolve_runtime_provider_config(
        self,
        provider_type: str,
        *,
        profile_bindings: Optional[dict[str, str]] = None,
        default_name: str = "",
    ) -> dict[str, Any]:
        """运行时取 provider 的 config dict（消费侧 fallback 链入口）。

        优先级：profile_bindings[provider_type] → default_name → 返回空 dict。

        Returns:
            空 dict 表示"未找到 active provider"，消费侧应回落到 .env（AppConfig 字段）。
        """
        bound_name = ""
        if profile_bindings:
            bound_name = str(profile_bindings.get(provider_type) or "").strip()
        cfg = self.resolve_provider_config(
            provider_type,
            bound_name,
            default_name=default_name,
            active_only=True,
        )
        if cfg is None:
            return {}
        return dict(cfg.config or {})

    def get_active_provider(self, provider_type: str) -> Optional[ProviderConfig]:
        """取该类型下最新 active 的 ProviderConfig（按 updated_at desc 取第一条）。

        给 ``ProviderRegistry.build_active()`` 用 —— 切换 active 即切供应商，
        不再依赖 AppConfig 上的 ``card_provider`` / ``email_provider_name`` 字段。

        多条 active 时取最新更新的；返回 None 表示该类型当前无 active 配置，
        消费侧应回落到 AppConfig（向后兼容）或抛错。
        """
        with get_session() as session:
            stmt = (
                select(ProviderConfig)
                .where(
                    ProviderConfig.provider_type == provider_type,
                    ProviderConfig.is_active == True,  # noqa: E712
                )
                .order_by(ProviderConfig.updated_at.desc())  # type: ignore[arg-type]
            )
            return session.exec(stmt).first()

    def list_active_providers(self, provider_type: str) -> list[ProviderConfig]:
        """列出该类型下所有 active ProviderConfig（按 updated_at desc）。

        给 mail 这种「多 provider 同时启用按邮箱域名路由」的场景用 ——
        ``ProviderRegistry.build_all_active()`` 会逐条尝试构造，单条失败不影响其它。
        """
        with get_session() as session:
            stmt = (
                select(ProviderConfig)
                .where(
                    ProviderConfig.provider_type == provider_type,
                    ProviderConfig.is_active == True,  # noqa: E712
                )
                .order_by(ProviderConfig.updated_at.desc())  # type: ignore[arg-type]
            )
            return list(session.exec(stmt).all())

    def save_provider_config(
        self,
        provider_type: str,
        provider_name: str,
        config: dict[str, Any],
        is_active: bool = True,
        *,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> ProviderConfig:
        """创建或更新 Provider 配置。"""
        with get_session() as session:
            stmt = select(ProviderConfig).where(
                ProviderConfig.provider_type == provider_type,
                ProviderConfig.provider_name == provider_name,
            )
            existing = session.exec(stmt).first()
            existing_cfg = dict(existing.config or {}) if existing else {}
            merged_config = self._merge_redacted_fields(config, existing_cfg)

            if existing:
                self._create_provider_revision(
                    session=session,
                    provider_type=provider_type,
                    provider_name=provider_name,
                    snapshot={
                        "exists": True,
                        "config": existing.config,
                        "is_active": existing.is_active,
                    },
                    action_log_id=action_log_id,
                    actor=actor,
                )
                existing.config = merged_config
                existing.is_active = is_active
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
                session.commit()
                session.refresh(existing)
                logger.info("更新 Provider 配置: %s/%s", provider_type, provider_name)
                return existing

            sanitized_new = self._strip_redacted_literals(merged_config)
            new_config = ProviderConfig(
                provider_type=provider_type,
                provider_name=provider_name,
                config=sanitized_new,
                is_active=is_active,
            )
            self._create_provider_revision(
                session=session,
                provider_type=provider_type,
                provider_name=provider_name,
                snapshot={"exists": False, "config": {}, "is_active": False},
                action_log_id=action_log_id,
                actor=actor,
            )
            session.add(new_config)
            session.commit()
            session.refresh(new_config)
            logger.info("创建 Provider 配置: %s/%s", provider_type, provider_name)
            return new_config

    def delete_provider_config(
        self,
        provider_type: str,
        provider_name: str,
        *,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> bool:
        """删除 Provider 配置。"""
        with get_session() as session:
            stmt = select(ProviderConfig).where(
                ProviderConfig.provider_type == provider_type,
                ProviderConfig.provider_name == provider_name,
            )
            existing = session.exec(stmt).first()
            if existing:
                self._create_provider_revision(
                    session=session,
                    provider_type=provider_type,
                    provider_name=provider_name,
                    snapshot={
                        "exists": True,
                        "config": existing.config,
                        "is_active": existing.is_active,
                    },
                    action_log_id=action_log_id,
                    actor=actor,
                )
                session.delete(existing)
                session.commit()
                logger.info("删除 Provider 配置: %s/%s", provider_type, provider_name)
                return True
            return False

    def list_provider_revisions(self, provider_type: str, provider_name: str) -> list[ProviderConfigRevision]:
        with get_session() as session:
            stmt = (
                select(ProviderConfigRevision)
                .where(
                    ProviderConfigRevision.provider_type == provider_type,
                    ProviderConfigRevision.provider_name == provider_name,
                )
                .order_by(ProviderConfigRevision.created_at.desc())  # type: ignore[arg-type]
            )
            return list(session.exec(stmt).all())

    def rollback_provider_config(
        self,
        revision_id: int,
        *,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Optional[ProviderConfig]:
        """按 revision 快照恢复 Provider 配置。"""
        with get_session() as session:
            revision = session.get(ProviderConfigRevision, revision_id)
            if not revision:
                raise ValueError(f"未找到 Provider revision: {revision_id}")

            snapshot = revision.snapshot or {}
            exists = bool(snapshot.get("exists"))
            config = snapshot.get("config", {}) or {}
            is_active = bool(snapshot.get("is_active", False))

            stmt = select(ProviderConfig).where(
                ProviderConfig.provider_type == revision.provider_type,
                ProviderConfig.provider_name == revision.provider_name,
            )
            existing = session.exec(stmt).first()
            self._create_provider_revision(
                session=session,
                provider_type=revision.provider_type,
                provider_name=revision.provider_name,
                snapshot={
                    "exists": bool(existing),
                    "config": existing.config if existing else {},
                    "is_active": existing.is_active if existing else False,
                },
                action_log_id=action_log_id,
                actor=actor,
            )

            if not exists:
                if existing:
                    session.delete(existing)
                    session.commit()
                return None

            if existing:
                existing.config = config
                existing.is_active = is_active
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return existing

            restored = ProviderConfig(
                provider_type=revision.provider_type,
                provider_name=revision.provider_name,
                config=config,
                is_active=is_active,
            )
            session.add(restored)
            session.commit()
            session.refresh(restored)
            return restored

    def redact_provider_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return redact_structure(config)

    def redact_mail_account(self, account: MailAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "label": account.label,
            "provider_name": account.provider_name,
            "email": account.email,
            "client_id": self._mask_value(account.client_id),
            "refresh_token": self._mask_value(account.refresh_token),
            "extra": redact_structure(account.extra),
            "role": str(getattr(account, "role", "regular") or "regular"),
            "is_active": account.is_active,
            "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else "",
            # 卡预热号池调度字段（仅 role=pro_warmup 有意义；regular 全 NULL）
            "last_used_at": account.last_used_at.isoformat() if account.last_used_at else "",
            "cooldown_until": account.cooldown_until.isoformat() if account.cooldown_until else "",
            "consecutive_failures": int(account.consecutive_failures or 0),
            "last_failure_reason": account.last_failure_reason or "",
            "updated_at": account.updated_at.isoformat() if account.updated_at else "",
        }

    def _create_provider_revision(
        self,
        *,
        session: Session,
        provider_type: str,
        provider_name: str,
        snapshot: dict[str, Any],
        action_log_id: Optional[str],
        actor: Optional[str],
    ) -> ProviderConfigRevision:
        revision = ProviderConfigRevision(
            provider_type=provider_type,
            provider_name=provider_name,
            snapshot=snapshot,
            action_log_id=action_log_id,
            created_by=actor,
        )
        session.add(revision)
        session.flush()
        return revision

    def _coerce_value(self, value: Any, target_type: Any) -> Any:
        """把页面/助手提交的字符串值转换成 AppConfig 字段类型。"""
        origin = get_origin(target_type)
        if origin is not None:
            args = [item for item in get_args(target_type) if item is not type(None)]
            if args:
                target_type = args[0]

        if target_type is bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if target_type is int:
            if isinstance(value, int):
                return value
            return int(str(value).strip())
        if target_type is float:
            if isinstance(value, (int, float)):
                return float(value)
            return float(str(value).strip())
        if value is None:
            return ""
        return str(value)

    def _load_persisted_overrides(self) -> None:
        """从数据库回填配置覆盖；若表尚未创建则静默跳过。"""
        try:
            with get_session() as session:
                rows = list(session.exec(select(AppSetting)).all())
        except Exception as exc:
            logger.debug("读取持久化配置覆盖失败（通常发生在建表前）: %s", exc)
            return

        app_fields = {f.name: f for f in fields(AppConfig)}
        loaded: dict[str, Any] = {}
        for row in rows:
            if row.key not in app_fields:
                continue
            loaded[row.key] = self._coerce_value(row.value, app_fields[row.key].type)
        self._overrides = loaded

    def _persist_override(self, key: str, value: Any) -> None:
        """写 AppSetting 前先把旧值快照到 AppSettingRevision（消除 Ambiguity #6）。"""
        from src.db.models import AppSettingRevision  # local import 避免循环

        with get_session() as session:
            row = session.get(AppSetting, key)
            previous = row.value if row is not None else None
            # 旧值与新值相等不写 revision（避免无意义的审计噪音）
            if previous != value:
                rev = AppSettingRevision(
                    key=key,
                    previous_value=previous if previous is not None else "",
                    new_value=value,
                )
                session.add(rev)
            if row is None:
                row = AppSetting(key=key, value=value)
            else:
                row.value = value
                row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()

    def list_app_setting_revisions(self, key: Optional[str] = None, *, limit: int = 20) -> list:
        """查询配置改动历史（消除 Ambiguity #6 配套查询接口）。"""
        from src.db.models import AppSettingRevision

        with get_session() as session:
            stmt = select(AppSettingRevision)
            if key:
                stmt = stmt.where(AppSettingRevision.key == key)
            stmt = stmt.order_by(AppSettingRevision.id.desc())  # type: ignore[union-attr]
            stmt = stmt.limit(max(1, min(int(limit or 20), 200)))
            return list(session.exec(stmt).all())

    @staticmethod
    def _mask_value(value: Any) -> str:
        raw = str(value or "")
        if not raw:
            return ""
        return f"{raw[:4]}****" if len(raw) > 4 else "****"

    # 字符串中只要出现 "[REDACTED" 前缀（[REDACTED] / [REDACTED_TOKEN] / [REDACTED_USER]:[REDACTED_PASS] 等）
    # 就视为脱敏占位符——禁止把这种"看起来像真值的脱敏串"作为真值写回 DB。
    _REDACTED_TAG = "[REDACTED"

    @classmethod
    def _is_redacted_literal(cls, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        return cls._REDACTED_TAG in value

    @classmethod
    def _merge_redacted_fields(
        cls,
        incoming: dict[str, Any],
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        """合并 UI 提交的 config 与 DB 既有 config：

        如果 incoming[k] 是脱敏占位符（如 ``"[REDACTED]"``），
        则保留 existing[k] 真值，避免 UI 回显被原样回写覆盖。

        典型场景：admin 编辑 provider 时，UI 把 proxy/api_key 显示为 [REDACTED]，
        用户只想改其他字段然后点保存，结果原本的敏感字段被字面量 [REDACTED] 覆盖，
        下游（远程 email-provider 等）拿到字面量调用上游 → InvalidURL → 500。
        """
        merged = dict(incoming or {})
        for key, val in merged.items():
            if cls._is_redacted_literal(val) and key in existing:
                merged[key] = existing[key]
        return merged

    @classmethod
    def _strip_redacted_literals(cls, config: dict[str, Any]) -> dict[str, Any]:
        """新建 provider 路径下，若用户硬塞 [REDACTED] 也要直接剔除（DB 不允许存脱敏占位）。"""
        return {k: v for k, v in (config or {}).items() if not cls._is_redacted_literal(v)}

    # SAFE_UPDATE_FIELDS：允许通过 /api/config PUT 改的字段白名单。
    #
    # 已移除的 A 类 provider 字段（feat/registration-profile 2026-05-27 迁移）：
    #   ads_api / ads_api_key                                  → /providers (browser-default)
    #   email_provider_base_url / email_provider_api_key       → /providers (mail-default)
    #   default_browser_provider / default_card_provider /
    #   default_mail_provider / default_mail_account_id        → /registration-profiles
    #   card_provider / efuncard_token / nodecard_*            → /providers (card-*)
    #   sms_api_key / sms_country                              → /providers (未来 sms-*)
    #
    # 这些字段在 AppConfig 里保留定义（兼容护栏 + 老 .env 兜底），但 API 不再允许从
    # /config 页面更改 —— 强制走 /providers + /registration-profiles 的清晰职责分工。
    SAFE_UPDATE_FIELDS = {
        # 网络/代理（全局，可被 browser provider 覆盖）
        "proxy",
        # 全局默认 provider 指针（registration profile 未指定时使用）
        "default_sms_provider",
        "default_captcha_provider",
        "default_llm_provider",
        # LLM 兜底决策（未来 Stage 会迁到 llm provider）
        "llm_enabled",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_timeout_ms",
        "llm_confidence_threshold",
        "llm_max_consecutive_uncertain",
        # 支付/绑卡全局策略
        "enable_payment_flow",
        "enable_card_warmup",
        "warmup_account_pool",
        "warmup_max_retries",
        "payment_plan",
        "payment_link_only",
        "payment_link_return_mode",
        "aimizy_country",
        "aimizy_currency",
        "billing_country",
        "billing_line1",
        "billing_line2",
        "billing_city",
        "billing_state",
        "billing_postal_code",
        # 运行时阈值
        "run_artifacts_dir",
        "trace_on_failure",
        "clean_context_mode",
        "max_navigation_retries",
        "max_email_attempts",
        "max_profile_reconnects",
        "max_manual_handoffs",
    }


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite 取出来的 datetime 是 naive，比较前补 UTC 时区。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
