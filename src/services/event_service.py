# -*- coding: utf-8 -*-
"""
事件广播服务

将 RunEvent 写入数据库并广播给 SSE 订阅者。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from functools import partial
from typing import Any, AsyncIterator

from src.db.engine import get_session
from src.db.models import RunEvent

logger = logging.getLogger(__name__)


class EventBroadcaster:
    """
    事件广播器。

    - emit() 写入 DB 并通知所有订阅者
    - subscribe() 返回异步迭代器用于 SSE 推送
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(list)

    @staticmethod
    def _event_to_dict(event: RunEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "run_id": event.run_id,
            "event_type": event.event_type,
            "state": event.state,
            "payload": event.payload,
            "timestamp": event.timestamp.isoformat(),
        }

    @staticmethod
    def _enqueue_event(
        queue: asyncio.Queue,
        event_data: dict[str, Any],
        *,
        run_id: str,
        event_type: str,
    ) -> None:
        try:
            queue.put_nowait(event_data)
        except asyncio.QueueFull:
            logger.warning("事件队列已满，丢弃事件: run_id=%s type=%s", run_id, event_type)

    def _broadcast_to_subscribers(
        self,
        run_id: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        for loop, queue in list(self._subscribers.get(run_id, [])):
            try:
                loop.call_soon_threadsafe(
                    partial(
                        self._enqueue_event,
                        queue,
                        event_data,
                        run_id=run_id,
                        event_type=event_type,
                    )
                )
            except RuntimeError:
                logger.debug("事件循环已关闭，跳过推送: run_id=%s type=%s", run_id, event_type)

    async def emit(
        self,
        run_id: str,
        event_type: str,
        *,
        state: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        """
        记录事件到数据库并广播。

        Args:
            run_id: 关联的 Run ID
            event_type: 事件类型（state_change / action / error / screenshot / log）
            state: 当前自动化状态
            payload: 事件附加数据
        """
        event = RunEvent(
            run_id=run_id,
            event_type=event_type,
            state=state,
            payload=payload or {},
            timestamp=datetime.now(timezone.utc),
        )

        with get_session() as session:
            session.add(event)
            session.commit()
            session.refresh(event)

        event_data = self._event_to_dict(event)
        self._broadcast_to_subscribers(run_id, event_type, event_data)

        return event

    async def subscribe(self, run_id: str, *, max_queue: int = 100) -> AsyncIterator[dict[str, Any]]:
        """
        订阅指定 Run 的事件流。

        Yields:
            事件字典
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        subscriber = (asyncio.get_running_loop(), queue)
        self._subscribers[run_id].append(subscriber)
        try:
            while True:
                event_data = await queue.get()
                yield event_data
        finally:
            self._subscribers[run_id].remove(subscriber)
            if not self._subscribers[run_id]:
                del self._subscribers[run_id]

    def emit_sync(
        self,
        run_id: str,
        event_type: str,
        *,
        state: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        """同步版 emit，用于非异步上下文（Worker 进程）。"""
        event = RunEvent(
            run_id=run_id,
            event_type=event_type,
            state=state,
            payload=payload or {},
            timestamp=datetime.now(timezone.utc),
        )
        with get_session() as session:
            session.add(event)
            session.commit()
            session.refresh(event)
        event_data = self._event_to_dict(event)
        self._broadcast_to_subscribers(run_id, event_type, event_data)
        return event
