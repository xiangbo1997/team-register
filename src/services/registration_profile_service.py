# -*- coding: utf-8 -*-
"""
RegistrationProfile 服务：注册方式 × 供应商组合的 CRUD + seed。

设计意图：把散落在 AppConfig 的 12 个 provider 选择器字段
（default_browser_provider / default_card_provider / default_mail_provider /
 card_provider / email_provider_name / outlook_enabled / cfworker_enabled / ...）
收敛成"按注册方式（email / phone）预设一套 provider 组合"。

本 Stage（1）只做组合的存取，不实现 resolve_providers（Stage 2 才需要——
那时会读现有 ProviderConfig 凭据装配出可运行的 provider 实例）。

约束：
  - 同一 registration_kind 仅允许 1 条 is_default=True（service 层保证）。
  - is_default 的组合不可删除（必须先把别的设为默认或换 kind）。
  - 槽位名（browser/card/mail/...）不强校验，留扩展空间；值必须非空字符串。

审计：所有写操作（create / update / delete / set_default）写前 snapshot
到 RegistrationProfileRevision，串 action_log_id / actor 字段；与
ProviderConfigRevision 同款，为未来接入 AssistantService.upsert_* 留通道。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from src.config import AppConfig, load_config
from src.db.engine import get_session
from src.db.models import (
    ProviderConfig,
    RegistrationProfile,
    RegistrationProfileRevision,
)

logger = logging.getLogger(__name__)


# 注册方式合法取值
VALID_REGISTRATION_KINDS = frozenset({"email", "phone", "grok"})

# seed 阶段使用的默认组合名（与现有 _seed_runtime_defaults 的 ProviderConfig 命名一致）
_DEFAULT_PROFILE_NAMES = {
    "email": "email-default",
    "phone": "phone-default",
    "grok": "grok-default",
}


class RegistrationProfileError(Exception):
    """RegistrationProfileService 通用业务异常基类。"""


class InvalidRegistrationKindError(RegistrationProfileError):
    """registration_kind 不在白名单。"""

    def __init__(self, kind: str) -> None:
        super().__init__(
            f"registration_kind={kind!r} 不合法，必须是 "
            f"{sorted(VALID_REGISTRATION_KINDS)} 之一"
        )
        self.kind = kind


class RegistrationProfileNotFoundError(RegistrationProfileError):
    """按 name 找不到组合。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"未找到 RegistrationProfile: {name}")
        self.name = name


class RegistrationProfileNameConflictError(RegistrationProfileError):
    """同名组合已存在。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"RegistrationProfile {name!r} 已存在")
        self.name = name


class DefaultProfileNotDeletableError(RegistrationProfileError):
    """is_default=True 的组合禁止删除。"""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"组合 {name!r} 当前是默认组合，禁止删除；"
            f"请先把同 kind 的其他组合设为默认。"
        )
        self.name = name


class InvalidProviderBindingsError(RegistrationProfileError):
    """provider_bindings 格式不合规（非 dict / 值非字符串 / 空字符串）。"""


class RegistrationProfileService:
    """RegistrationProfile CRUD + seed。

    本服务**不**实例化 provider；它只负责"组合 = 名字到 ProviderConfig.provider_name
    的映射"。Stage 2 的 worker 会拿到 bindings 后去查 ProviderConfig 拿凭据。
    """

    def __init__(self, base_config: Optional[AppConfig] = None) -> None:
        # base_config 主要给 seed 用；CRUD 操作不依赖 .env
        self._base_config = base_config

    # ── 查询 ──────────────────────────────────────────

    def list_profiles(
        self,
        *,
        registration_kind: Optional[str] = None,
        active_only: bool = False,
    ) -> list[RegistrationProfile]:
        """列出所有组合，可按 kind / is_active 过滤。"""
        with get_session() as session:
            stmt = select(RegistrationProfile)
            if registration_kind:
                stmt = stmt.where(
                    RegistrationProfile.registration_kind == registration_kind
                )
            if active_only:
                stmt = stmt.where(RegistrationProfile.is_active == True)  # noqa: E712
            stmt = stmt.order_by(
                RegistrationProfile.registration_kind,
                RegistrationProfile.is_default.desc(),  # type: ignore[arg-type]
                RegistrationProfile.name,
            )
            return list(session.exec(stmt).all())

    def get_profile(self, name: str) -> Optional[RegistrationProfile]:
        """按 name 取组合；不存在返回 None。"""
        with get_session() as session:
            stmt = select(RegistrationProfile).where(
                RegistrationProfile.name == name
            )
            return session.exec(stmt).first()

    def get_default(self, registration_kind: str) -> Optional[RegistrationProfile]:
        """取该 kind 的默认组合；无返回 None。"""
        normalized = self._normalize_kind(registration_kind)
        with get_session() as session:
            stmt = (
                select(RegistrationProfile)
                .where(RegistrationProfile.registration_kind == normalized)
                .where(RegistrationProfile.is_default == True)  # noqa: E712
                .where(RegistrationProfile.is_active == True)  # noqa: E712
            )
            return session.exec(stmt).first()

    # ── 写入 ──────────────────────────────────────────

    def create(
        self,
        *,
        name: str,
        registration_kind: str,
        provider_bindings: dict[str, str],
        description: Optional[str] = None,
        is_default: bool = False,
        is_active: bool = True,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> RegistrationProfile:
        """创建新组合。name 必须唯一；同 kind 仅允许 1 条 is_default=True。"""
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise RegistrationProfileError("name 不能为空")
        normalized_kind = self._normalize_kind(registration_kind)
        normalized_bindings = self._validate_bindings(provider_bindings)

        with get_session() as session:
            existing = session.exec(
                select(RegistrationProfile).where(
                    RegistrationProfile.name == normalized_name
                )
            ).first()
            if existing is not None:
                raise RegistrationProfileNameConflictError(normalized_name)

            now = datetime.now(timezone.utc)
            profile = RegistrationProfile(
                name=normalized_name,
                registration_kind=normalized_kind,
                provider_bindings=normalized_bindings,
                description=(description or None),
                is_default=bool(is_default),
                is_active=bool(is_active),
                created_at=now,
                updated_at=now,
            )

            self._create_revision(
                session=session,
                profile_name=normalized_name,
                snapshot={"exists": False},
                action_log_id=action_log_id,
                actor=actor,
            )

            # 若声明为默认，先把同 kind 其他记录的 is_default 清掉
            if is_default:
                self._clear_other_defaults(
                    session,
                    registration_kind=normalized_kind,
                    keep_name=normalized_name,
                )

            session.add(profile)
            session.commit()
            session.refresh(profile)
            logger.info(
                "创建 RegistrationProfile: %s (kind=%s, default=%s)",
                normalized_name, normalized_kind, is_default,
            )
            return profile

    def update(
        self,
        name: str,
        *,
        registration_kind: Optional[str] = None,
        provider_bindings: Optional[dict[str, str]] = None,
        description: Optional[str] = None,
        is_active: Optional[bool] = None,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> RegistrationProfile:
        """更新组合的非默认字段。is_default 由 set_default() 单独管理。"""
        normalized_name = str(name or "").strip()
        with get_session() as session:
            profile = session.exec(
                select(RegistrationProfile).where(
                    RegistrationProfile.name == normalized_name
                )
            ).first()
            if profile is None:
                raise RegistrationProfileNotFoundError(normalized_name)

            self._create_revision(
                session=session,
                profile_name=normalized_name,
                snapshot=self._snapshot_existing(profile),
                action_log_id=action_log_id,
                actor=actor,
            )

            if registration_kind is not None:
                profile.registration_kind = self._normalize_kind(registration_kind)
            if provider_bindings is not None:
                profile.provider_bindings = self._validate_bindings(provider_bindings)
            if description is not None:
                profile.description = description or None
            if is_active is not None:
                profile.is_active = bool(is_active)
            profile.updated_at = datetime.now(timezone.utc)

            session.add(profile)
            session.commit()
            session.refresh(profile)
            logger.info("更新 RegistrationProfile: %s", normalized_name)
            return profile

    def set_default(
        self,
        name: str,
        *,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> RegistrationProfile:
        """把指定组合设为该 kind 的默认，同 kind 其他记录的 is_default 自动清零。"""
        normalized_name = str(name or "").strip()
        with get_session() as session:
            profile = session.exec(
                select(RegistrationProfile).where(
                    RegistrationProfile.name == normalized_name
                )
            ).first()
            if profile is None:
                raise RegistrationProfileNotFoundError(normalized_name)
            if not profile.is_active:
                raise RegistrationProfileError(
                    f"组合 {normalized_name!r} 已停用，无法设为默认；请先启用。"
                )

            self._create_revision(
                session=session,
                profile_name=normalized_name,
                snapshot=self._snapshot_existing(profile),
                action_log_id=action_log_id,
                actor=actor,
            )

            self._clear_other_defaults(
                session,
                registration_kind=profile.registration_kind,
                keep_name=normalized_name,
            )
            profile.is_default = True
            profile.updated_at = datetime.now(timezone.utc)

            session.add(profile)
            session.commit()
            session.refresh(profile)
            logger.info(
                "设为默认 RegistrationProfile: %s (kind=%s)",
                normalized_name, profile.registration_kind,
            )
            return profile

    def delete(
        self,
        name: str,
        *,
        action_log_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> bool:
        """删除组合；is_default=True 的禁止删，必须先转交默认。"""
        normalized_name = str(name or "").strip()
        with get_session() as session:
            profile = session.exec(
                select(RegistrationProfile).where(
                    RegistrationProfile.name == normalized_name
                )
            ).first()
            if profile is None:
                return False
            if profile.is_default:
                raise DefaultProfileNotDeletableError(normalized_name)

            self._create_revision(
                session=session,
                profile_name=normalized_name,
                snapshot=self._snapshot_existing(profile),
                action_log_id=action_log_id,
                actor=actor,
            )
            session.delete(profile)
            session.commit()
            logger.info("删除 RegistrationProfile: %s", normalized_name)
            return True

    # ── Seed ──────────────────────────────────────────

    def seed_from_appconfig(self, *, force: bool = False) -> dict[str, list[str]]:
        """首次启动时根据当前 AppConfig 字段值 seed 默认组合。

        seed 内容：
          - email-default → registration_kind=email，默认组合包含 browser / card / mail
          - phone-default → registration_kind=phone，默认组合包含 browser / card / mail / sms

        provider 槽位的 value 取自 AppConfig.default_* 字段；为空时回退到
        seed_runtime_defaults 同款的命名约定（"browser-default" 等）。

        幂等：默认按 name 跳过已存在；force=True 时覆盖现有 provider_bindings。

        Returns:
            {"seeded": [...], "skipped_existing": [...], "updated": [...]}
        """
        config = self._base_config or load_config()
        plan = self._build_seed_plan(config)
        result: dict[str, list[str]] = {
            "seeded": [],
            "skipped_existing": [],
            "updated": [],
        }

        with get_session() as session:
            for spec in plan:
                existing = session.exec(
                    select(RegistrationProfile).where(
                        RegistrationProfile.name == spec["name"]
                    )
                ).first()

                if existing is None:
                    self._create_revision(
                        session=session,
                        profile_name=spec["name"],
                        snapshot={"exists": False},
                        action_log_id=None,
                        actor="seed",
                    )
                    session.add(RegistrationProfile(
                        name=spec["name"],
                        registration_kind=spec["registration_kind"],
                        provider_bindings=spec["provider_bindings"],
                        description=spec["description"],
                        is_default=True,
                        is_active=True,
                    ))
                    result["seeded"].append(spec["name"])
                    continue

                if not force:
                    result["skipped_existing"].append(spec["name"])
                    continue

                self._create_revision(
                    session=session,
                    profile_name=spec["name"],
                    snapshot=self._snapshot_existing(existing),
                    action_log_id=None,
                    actor="seed",
                )
                existing.registration_kind = spec["registration_kind"]
                existing.provider_bindings = spec["provider_bindings"]
                existing.description = spec["description"]
                existing.is_default = True
                existing.is_active = True
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
                result["updated"].append(spec["name"])

            session.commit()

        if result["seeded"]:
            logger.info("RegistrationProfile seeded: %s", result["seeded"])
        if result["updated"]:
            logger.info("RegistrationProfile updated (force=True): %s", result["updated"])
        return result

    # ── Revision 查询 ─────────────────────────────────

    def list_revisions(self, profile_name: str) -> list[RegistrationProfileRevision]:
        with get_session() as session:
            stmt = (
                select(RegistrationProfileRevision)
                .where(RegistrationProfileRevision.profile_name == profile_name)
                .order_by(RegistrationProfileRevision.created_at.desc())  # type: ignore[arg-type]
            )
            return list(session.exec(stmt).all())

    # ── 内部 helpers ──────────────────────────────────

    def _build_seed_plan(self, config: AppConfig) -> list[dict[str, Any]]:
        """根据 AppConfig 当前字段值，决定 email-default / phone-default 的 binding。

        provider name 优先级：
          1. AppConfig.default_<type>_provider（如运维已显式配过）
          2. _seed_runtime_defaults 同款的命名约定（browser-default / card-default /
             mail-default 等），保证与 ProviderConfig 表里 seed 进来的记录对齐
        """
        browser_name = (
            str(getattr(config, "default_browser_provider", "") or "").strip()
            or "browser-default"
        )
        card_name = (
            str(getattr(config, "default_card_provider", "") or "").strip()
            or "card-default"
        )
        # mail 的命名约定要照顾 cfworker / outlook 开关；与 _seed_runtime_defaults 对齐
        if getattr(config, "cfworker_enabled", False):
            mail_default_name = "mail-cfworker-default"
        elif getattr(config, "outlook_enabled", False):
            mail_default_name = "mail-outlook-default"
        else:
            mail_default_name = (
                str(getattr(config, "default_mail_provider", "") or "").strip()
                or "mail-default"
            )

        email_bindings = {
            "browser": browser_name,
            "card": card_name,
            "mail": mail_default_name,
        }
        phone_bindings = {
            "browser": browser_name,
            "card": card_name,
            "mail": mail_default_name,
            # sms provider 目前无 ProviderConfig 记录，预留 name；
            # Stage 3 接入 sms provider 时统一注册命名
            "sms": "sms-activate-default",
        }

        return [
            {
                "name": _DEFAULT_PROFILE_NAMES["email"],
                "registration_kind": "email",
                "provider_bindings": email_bindings,
                "description": "邮箱注册默认组合（由 seed_from_appconfig 自动生成）",
            },
            {
                "name": _DEFAULT_PROFILE_NAMES["phone"],
                "registration_kind": "phone",
                "provider_bindings": phone_bindings,
                "description": "手机号注册默认组合（由 seed_from_appconfig 自动生成）",
            },
        ]

    def _normalize_kind(self, kind: str) -> str:
        normalized = str(kind or "").strip().lower()
        if normalized not in VALID_REGISTRATION_KINDS:
            raise InvalidRegistrationKindError(normalized)
        return normalized

    def _validate_bindings(self, bindings: Any) -> dict[str, str]:
        """provider_bindings 必须是 {str: str}，值不能是空串。"""
        if not isinstance(bindings, dict):
            raise InvalidProviderBindingsError(
                "provider_bindings 必须是 dict[str, str]"
            )
        normalized: dict[str, str] = {}
        for slot, value in bindings.items():
            slot_str = str(slot or "").strip()
            value_str = str(value or "").strip()
            if not slot_str:
                raise InvalidProviderBindingsError(
                    "provider_bindings 槽位名不能为空"
                )
            if not value_str:
                raise InvalidProviderBindingsError(
                    f"provider_bindings 槽位 {slot_str!r} 的 provider 名不能为空"
                )
            normalized[slot_str] = value_str
        return normalized

    def _clear_other_defaults(
        self,
        session: Session,
        *,
        registration_kind: str,
        keep_name: str,
    ) -> None:
        """把同 kind 其他 is_default=True 的记录改为 False（确保一对一约束）。"""
        stmt = (
            select(RegistrationProfile)
            .where(RegistrationProfile.registration_kind == registration_kind)
            .where(RegistrationProfile.is_default == True)  # noqa: E712
            .where(RegistrationProfile.name != keep_name)
        )
        rows = list(session.exec(stmt).all())
        if not rows:
            return
        now = datetime.now(timezone.utc)
        for row in rows:
            row.is_default = False
            row.updated_at = now
            session.add(row)

    def _snapshot_existing(self, profile: RegistrationProfile) -> dict[str, Any]:
        return {
            "exists": True,
            "registration_kind": profile.registration_kind,
            "provider_bindings": dict(profile.provider_bindings or {}),
            "description": profile.description,
            "is_default": bool(profile.is_default),
            "is_active": bool(profile.is_active),
        }

    def _create_revision(
        self,
        *,
        session: Session,
        profile_name: str,
        snapshot: dict[str, Any],
        action_log_id: Optional[str],
        actor: Optional[str],
    ) -> RegistrationProfileRevision:
        revision = RegistrationProfileRevision(
            profile_name=profile_name,
            snapshot=snapshot,
            action_log_id=action_log_id,
            created_by=actor,
        )
        session.add(revision)
        session.flush()
        return revision


# ── 校验工具（给 API / worker 复用）──────────────────


def validate_bindings_against_provider_configs(
    bindings: dict[str, str],
    *,
    required_slots: Optional[set[str]] = None,
) -> list[str]:
    """校验 bindings 里引用的 provider_name 在 ProviderConfig 表里存在且 active。

    本函数不属于 service 类，因为 Stage 1 不强制要求引用合法（允许 seed 出
    "sms-activate-default" 这种暂未创建的占位名），但 Stage 2 worker 走真实
    任务时必须调用本函数避免运行时炸。

    Args:
        bindings: provider 槽位 → provider_name 映射
        required_slots: 必须存在且合法的槽位集合（如 {"browser", "mail"}）；
            None 时校验所有非空 binding

    Returns:
        错误信息列表；空表示全部合法
    """
    errors: list[str] = []
    targets = (
        {slot: bindings[slot] for slot in required_slots if slot in bindings}
        if required_slots is not None
        else dict(bindings)
    )
    if required_slots is not None:
        missing = sorted(required_slots - set(bindings.keys()))
        for slot in missing:
            errors.append(f"必填槽位缺失: {slot}")

    with get_session() as session:
        for slot, provider_name in targets.items():
            if not provider_name:
                errors.append(f"槽位 {slot!r} 未指定 provider name")
                continue
            # 槽位与 ProviderConfig.provider_type 的映射（命名约定不严格匹配，
            # 比如 mail-cfworker-default 的 provider_type 是 "mail"）：
            # 这里采用宽松校验——只要 provider_name 在表里且 active 即可，
            # 不强制 provider_type 与 slot 名相等。Stage 2 真正消费时再分槽位定型。
            existing = session.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_name == provider_name
                )
            ).first()
            if existing is None:
                errors.append(
                    f"槽位 {slot!r} 引用的 provider {provider_name!r} 不存在"
                )
            elif not existing.is_active:
                errors.append(
                    f"槽位 {slot!r} 引用的 provider {provider_name!r} 已停用"
                )
    return errors
