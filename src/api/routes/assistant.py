# -*- coding: utf-8 -*-
"""单助手 API。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import get_assistant_service
from src.api.security import get_or_create_csrf_token, require_authenticated_user, require_csrf
from src.db.models import User
from src.services.assistant_service import AssistantService

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class ChatRequest(BaseModel):
    message: str = ""
    intent_mode: str = "auto"
    page_context: dict[str, Any] = Field(default_factory=dict)
    draft_action: Optional[dict[str, Any]] = None


class PreviewRequest(BaseModel):
    message: str = ""
    draft_action: dict[str, Any]


class CommitRequest(BaseModel):
    preview_id: str


@router.get("/bootstrap")
def bootstrap(
    request: Request,
    user: User = Depends(require_authenticated_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
):
    payload = assistant_service.get_bootstrap(user)
    payload["csrf_token"] = get_or_create_csrf_token(request)
    return payload


@router.post("/chat")
def chat(
    body: ChatRequest,
    _: None = Depends(require_csrf),
    user: User = Depends(require_authenticated_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
):
    return assistant_service.chat(
        user=user,
        message=body.message,
        intent_mode=body.intent_mode,
        page_context=body.page_context,
        draft_action=body.draft_action,
    )


@router.post("/preview")
@router.post("/actions/preview")
def preview(
    body: PreviewRequest,
    _: None = Depends(require_csrf),
    user: User = Depends(require_authenticated_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
):
    return assistant_service.preview_action(
        user=user,
        message=body.message,
        draft_action=body.draft_action,
    )


@router.post("/commit")
@router.post("/actions/commit")
def commit(
    body: CommitRequest,
    _: None = Depends(require_csrf),
    user: User = Depends(require_authenticated_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
):
    try:
        return assistant_service.commit_action(user=user, action_id=body.preview_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/actions/{action_id}")
def get_action(
    action_id: str,
    user: User = Depends(require_authenticated_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
):
    try:
        return assistant_service.get_action(user=user, action_id=action_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
