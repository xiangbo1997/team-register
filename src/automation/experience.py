# -*- coding: utf-8 -*-
"""把 LLM 解决过的卡点沉淀为可复用经验。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from src.automation.models import Action, Evidence

_MATCH_SIGNAL_KEYS = (
    "has_onboarding_prompt",
    "has_about_name_input",
    "has_age_input",
    "has_date_input",
    "has_code_input",
    "has_phone_input",
    "has_challenge_text",
    "has_challenge_widget",
    "has_auth_error",
)


class ExperienceStore:
    """持久化记录“某类页面特征 -> 成功动作”。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._entries: list[dict[str, Any]] = []
        self._load()

    def find_action_id(self, *, evidence: Evidence, candidates: list[Action]) -> str:
        candidate_ids = {item.action_id for item in candidates}
        state = evidence.primary_state.value
        location = self._location_key(evidence.url)
        signal_signature = self._signal_signature(evidence.signals)

        for entry in reversed(self._entries):
            if entry.get("entry_type") not in (None, "action"):
                continue
            if entry.get("state") != state:
                continue
            if entry.get("location") != location:
                continue
            if entry.get("signals") != signal_signature:
                continue
            action_id = str(entry.get("action_id", ""))
            if action_id in candidate_ids:
                return action_id
        return ""

    def record_success(self, *, evidence: Evidence, action: Action, source: str) -> None:
        entry = {
            "entry_type": "action",
            "state": evidence.primary_state.value,
            "location": self._location_key(evidence.url),
            "signals": self._signal_signature(evidence.signals),
            "action_id": action.action_id,
            "source": source,
            "step_name": evidence.step_name,
        }
        self._append_entry(entry)

    def record_event(
        self,
        *,
        category: str,
        name: str,
        payload: dict[str, Any],
        location: str = "",
    ) -> None:
        entry = {
            "entry_type": "event",
            "category": category,
            "name": name,
            "location": self._location_key(location) if location else "",
            "payload": payload,
        }
        self._append_entry(entry)

    def latest_event(self, *, category: str, name: str, location: str = "") -> dict[str, Any] | None:
        location_key = self._location_key(location) if location else ""
        for entry in reversed(self._entries):
            if entry.get("entry_type") != "event":
                continue
            if entry.get("category") != category or entry.get("name") != name:
                continue
            if location_key and entry.get("location") != location_key:
                continue
            payload = entry.get("payload")
            if isinstance(payload, dict):
                return payload
        return None

    def _append_entry(self, entry: dict[str, Any]) -> None:
        if entry in self._entries:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._entries.append(entry)

    def _load(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._entries.append(payload)

    @staticmethod
    def _location_key(url: str) -> str:
        parsed = urlsplit(str(url or ""))
        path = parsed.path or "/"
        return f"{parsed.netloc}{path}"

    @staticmethod
    def _signal_signature(signals: dict[str, Any]) -> dict[str, bool]:
        return {key: bool(signals.get(key)) for key in _MATCH_SIGNAL_KEYS}
