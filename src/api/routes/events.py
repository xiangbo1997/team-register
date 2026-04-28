# -*- coding: utf-8 -*-
"""
SSE 事件流 API

GET /api/tasks/{id}/events — 实时事件推送
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from src.api.deps import get_event_broadcaster
from src.api.i18n import DEFAULT_LOCALE, localize_event_data
from src.api.security import require_authenticated_user
from src.db.models import User
from src.services.event_service import EventBroadcaster

router = APIRouter(prefix="/api/tasks", tags=["events"])


@router.get("/{task_id}/events")
async def stream_events(
    request: Request,
    task_id: str,
    user: User = Depends(require_authenticated_user),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """SSE 事件流，用于实时推送任务状态变化和日志。"""
    locale = getattr(request.state, "locale", DEFAULT_LOCALE)

    async def event_generator():
        try:
            async for event_data in broadcaster.subscribe(task_id):
                payload = json.dumps(localize_event_data(locale, event_data), ensure_ascii=False, default=str)
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
