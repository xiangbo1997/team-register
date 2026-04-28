# -*- coding: utf-8 -*-
"""SSE 事件流路由测试。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.api.i18n import DEFAULT_LOCALE
from src.api.routes.events import stream_events


class _FakeBroadcaster:
    async def subscribe(self, task_id: str, *, max_queue: int = 100):
        yield {
            "id": 1,
            "run_id": task_id,
            "event_type": "state_change",
            "state": "AUTH",
            "payload": {"message": "进入密码页"},
            "timestamp": "2026-04-12T12:00:00+00:00",
        }


def _build_request(locale: str = DEFAULT_LOCALE) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(locale=locale))


class TestEventsRoute(unittest.TestCase):
    def test_stream_events_uses_default_message_frames(self):
        async def _collect():
            response = await stream_events(
                request=_build_request(),
                task_id="run-1",
                user=MagicMock(),
                broadcaster=_FakeBroadcaster(),
            )
            chunk = await response.body_iterator.__anext__()
            return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

        payload_text = asyncio.new_event_loop().run_until_complete(_collect())
        self.assertTrue(payload_text.startswith("data: "))
        self.assertNotIn("event:", payload_text)

        data = json.loads(payload_text.split("data: ", 1)[1].strip())
        self.assertEqual(data["event_type"], "state_change")
        self.assertEqual(data["state"], "AUTH")

