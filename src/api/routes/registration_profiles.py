# -*- coding: utf-8 -*-
"""
RegistrationProfile API（注册方式 × 供应商组合）

GET    /api/registration-profiles                       — 列表（可按 kind 过滤）
GET    /api/registration-profiles/{name}                — 详情
POST   /api/registration-profiles                       — 创建
PUT    /api/registration-profiles/{name}                — 更新（不含 is_default）
POST   /api/registration-profiles/{name}/set-default    — 设为该 kind 默认
DELETE /api/registration-profiles/{name}                — 删除（is_default 禁止）
GET    /api/registration-profiles/{name}/revisions      — 改动历史

权限：admin only；写操作要 CSRF。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import get_registration_profile_service
from src.api.security import require_csrf, require_role
from src.db.models import User
from src.services.registration_profile_service import (
    DefaultProfileNotDeletableError,
    InvalidProviderBindingsError,
    InvalidRegistrationKindError,
    RegistrationProfileError,
    RegistrationProfileNameConflictError,
    RegistrationProfileNotFoundError,
    RegistrationProfileService,
    validate_bindings_against_provider_configs,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registration-profiles", tags=["registration-profiles"])


# ── Request schemas ────────────────────────────────


class CreateProfileRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    registration_kind: str = Field(..., description="email / phone")
    provider_bindings: dict[str, str] = Field(
        default_factory=dict,
        description="槽位名 → ProviderConfig.provider_name 映射",
    )
    description: Optional[str] = Field(default=None, max_length=200)
    is_default: bool = Field(default=False)
    is_active: bool = Field(default=True)
    validate_bindings: bool = Field(
        default=True,
        description="True 时校验 bindings 引用的 provider 是否存在且 active",
    )


class UpdateProfileRequest(BaseModel):
    registration_kind: Optional[str] = Field(default=None)
    provider_bindings: Optional[dict[str, str]] = Field(default=None)
    description: Optional[str] = Field(default=None, max_length=200)
    is_active: Optional[bool] = Field(default=None)
    validate_bindings: bool = Field(default=True)


# ── 响应序列化 ─────────────────────────────────────


def _serialize(profile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "registration_kind": profile.registration_kind,
        "provider_bindings": dict(profile.provider_bindings or {}),
        "description": profile.description or "",
        "is_default": bool(profile.is_default),
        "is_active": bool(profile.is_active),
        "created_at": profile.created_at.isoformat() if profile.created_at else "",
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else "",
    }


def _serialize_revision(rev) -> dict[str, Any]:
    return {
        "id": rev.id,
        "profile_name": rev.profile_name,
        "snapshot": dict(rev.snapshot or {}),
        "action_log_id": rev.action_log_id or "",
        "created_by": rev.created_by or "",
        "created_at": rev.created_at.isoformat() if rev.created_at else "",
    }


# ── 路由 ──────────────────────────────────────────


@router.get("")
def list_profiles(
    registration_kind: Optional[str] = Query(default=None),
    active_only: bool = Query(default=False),
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
):
    """列出所有组合。"""
    rows = svc.list_profiles(
        registration_kind=registration_kind,
        active_only=active_only,
    )
    return [_serialize(row) for row in rows]


@router.get("/{name}")
def get_profile(
    name: str,
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
):
    profile = svc.get_profile(name)
    if profile is None:
        raise HTTPException(status_code=404, detail="组合不存在")
    return _serialize(profile)


@router.post("")
def create_profile(
    body: CreateProfileRequest,
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
    _csrf: None = Depends(require_csrf),
):
    if body.validate_bindings:
        errors = validate_bindings_against_provider_configs(body.provider_bindings)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_bindings", "errors": errors},
            )
    try:
        profile = svc.create(
            name=body.name,
            registration_kind=body.registration_kind,
            provider_bindings=body.provider_bindings,
            description=body.description,
            is_default=body.is_default,
            is_active=body.is_active,
            actor=user.id,
        )
        return _serialize(profile)
    except RegistrationProfileNameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (InvalidRegistrationKindError, InvalidProviderBindingsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{name}")
def update_profile(
    name: str,
    body: UpdateProfileRequest,
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
    _csrf: None = Depends(require_csrf),
):
    if body.provider_bindings is not None and body.validate_bindings:
        errors = validate_bindings_against_provider_configs(body.provider_bindings)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_bindings", "errors": errors},
            )
    try:
        profile = svc.update(
            name,
            registration_kind=body.registration_kind,
            provider_bindings=body.provider_bindings,
            description=body.description,
            is_active=body.is_active,
            actor=user.id,
        )
        return _serialize(profile)
    except RegistrationProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (InvalidRegistrationKindError, InvalidProviderBindingsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{name}/set-default")
def set_default(
    name: str,
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
    _csrf: None = Depends(require_csrf),
):
    try:
        profile = svc.set_default(name, actor=user.id)
        return _serialize(profile)
    except RegistrationProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RegistrationProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{name}")
def delete_profile(
    name: str,
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
    _csrf: None = Depends(require_csrf),
):
    try:
        deleted = svc.delete(name, actor=user.id)
    except DefaultProfileNotDeletableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="组合不存在")
    return {"deleted": True, "name": name}


@router.get("/{name}/revisions")
def list_revisions(
    name: str,
    user: User = Depends(require_role("admin")),
    svc: RegistrationProfileService = Depends(get_registration_profile_service),
):
    """改动历史（按时间倒序）。"""
    revs = svc.list_revisions(name)
    return [_serialize_revision(rev) for rev in revs]
