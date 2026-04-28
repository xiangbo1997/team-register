# -*- coding: utf-8 -*-
"""控制台认证 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.api.deps import get_auth_service
from src.api.security import (
    clear_user_session,
    get_current_user_from_request,
    get_or_create_csrf_token,
    require_csrf,
    rotate_csrf_token,
    store_user_session,
)
from src.db.models import User
from src.services.auth_service import AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    next: str = "/"


def _serialize_user(user: User) -> dict[str, str]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
    }


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    _: None = Depends(require_csrf),
    auth_service: AuthService = Depends(get_auth_service),
):
    user = auth_service.authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    clear_user_session(request)
    store_user_session(request, user)
    request.state.current_user = user
    return {
        "ok": True,
        "user": _serialize_user(user),
        "next": body.next or "/",
        "csrf_token": rotate_csrf_token(request),
    }


@router.post("/logout")
def logout(
    request: Request,
    _: None = Depends(require_csrf),
):
    clear_user_session(request)
    request.state.current_user = None
    return {"ok": True, "csrf_token": rotate_csrf_token(request)}


@router.get("/me")
def me(
    request: Request,
    user: User | None = Depends(get_current_user_from_request),
):
    csrf_token = get_or_create_csrf_token(request)
    if not user:
        return {"authenticated": False, "user": None, "csrf_token": csrf_token}
    return {"authenticated": True, "user": _serialize_user(user), "csrf_token": csrf_token}
