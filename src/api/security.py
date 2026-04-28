# -*- coding: utf-8 -*-
"""认证、会话、CSRF 与 RBAC 依赖。"""

from __future__ import annotations

from secrets import compare_digest, token_urlsafe
from typing import Callable, Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, HTTPException, Request, status
from starlette.responses import RedirectResponse

from src.api.deps import get_auth_service
from src.db.models import User
from src.services.auth_service import AuthService

SESSION_USER_KEY = "console_user"
SESSION_COOKIE_NAME = "team_register_session"
CSRF_SESSION_KEY = "console_csrf_token"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ROLE_LEVELS = {"viewer": 1, "operator": 2, "admin": 3}


def role_allows(user_role: str, required_role: str) -> bool:
    return _ROLE_LEVELS.get((user_role or "").strip().lower(), 0) >= _ROLE_LEVELS.get(required_role, 999)


def serialize_user(user: User) -> dict[str, str]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
    }


def store_user_session(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = serialize_user(user)


def clear_user_session(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)
    request.session.pop(CSRF_SESSION_KEY, None)


def get_or_create_csrf_token(request: Request) -> str:
    if not hasattr(request, "session"):
        return ""
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    if not hasattr(request, "session"):
        return ""
    token = token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = token
    return token


def _load_user_from_session(request: Request, auth_service: AuthService) -> Optional[User]:
    if not hasattr(request, "session"):
        return None
    session_user = request.session.get(SESSION_USER_KEY)
    if not isinstance(session_user, dict):
        return None
    user_id = str(session_user.get("id", "") or "")
    if not user_id:
        return None
    user = auth_service.get_user_by_id(user_id)
    if not user:
        clear_user_session(request)
        return None
    return user


def get_current_user_from_request(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> Optional[User]:
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached
    user = _load_user_from_session(request, auth_service)
    request.state.current_user = user
    return user


def require_authenticated_user(
    request: Request,
    user: Optional[User] = Depends(get_current_user_from_request),
) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录控制台")
    return user


def require_role(required_role: str) -> Callable[..., User]:
    def _dependency(
        request: Request,
        user: User = Depends(require_authenticated_user),
    ) -> User:
        if not role_allows(user.role, required_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"当前操作需要 {required_role} 权限",
            )
        return user

    return _dependency


def _is_same_origin(request: Request, candidate: str) -> bool:
    if not candidate:
        return False
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    current = request.url
    return parsed.scheme == current.scheme and parsed.netloc == current.netloc


def validate_csrf(request: Request) -> None:
    method = request.method.upper()
    if method not in _UNSAFE_METHODS:
        return

    expected = get_or_create_csrf_token(request)
    provided = str(request.headers.get("X-CSRF-Token") or "").strip()
    if not provided or not expected or not compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败：token 无效")

    origin_or_referer = str(request.headers.get("origin") or request.headers.get("referer") or "").strip()
    if origin_or_referer and not _is_same_origin(request, origin_or_referer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败：来源不可信")


def require_csrf(request: Request) -> None:
    validate_csrf(request)


def redirect_to_login(request: Request) -> RedirectResponse:
    next_target = request.url.path
    if request.url.query:
        next_target = f"{next_target}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote(next_target, safe='/%?=&')}", status_code=status.HTTP_303_SEE_OTHER)


def require_page_user(request: Request, *, required_role: str = "viewer") -> User | RedirectResponse:
    user = getattr(request.state, "current_user", None)
    if user is None:
        return redirect_to_login(request)
    if not role_allows(user.role, required_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该页面")
    return user
