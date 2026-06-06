# -*- coding: utf-8 -*-
"""运行证据、脱敏与产物落盘能力。"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from src.automation.models import Evidence

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SIX_DIGIT_CODE_RE = re.compile(r"\b\d{6}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d -]{6,}\d)\b")
_WS_URL_RE = re.compile(r"ws://[^\s\"']+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[a-z0-9._-]+")
_PROXY_CRED_RE = re.compile(r"://([^:/@\s]+):([^@/\s]+)@")

_SENSITIVE_QUERY_KEYS = {"token", "access_token", "refresh_token", "api_key", "key", "code", "session"}
_SENSITIVE_FIELD_NAMES = {
    "email",
    "phone",
    "phone_number",
    "verification_code",
    "code",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "set-cookie",
    "authorization",
    "ws_url",
    "api_key",
    "client_secret",
    "proxy",
    "proxy_url",
    "proxy_password",
    "card_number",
    "cvv",
    "expiry",
}


def sanitize_url(url: str) -> str:
    """移除 URL 中的敏感 query value。"""
    if not url:
        return ""

    parts = urlsplit(url)
    sanitized_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _SENSITIVE_QUERY_KEYS or any(marker in key.lower() for marker in ("token", "secret", "code", "key")):
            sanitized_query.append((key, "[REDACTED]"))
        else:
            sanitized_query.append((key, value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized_query), parts.fragment))


def _redact_text(value: str) -> str:
    """对文本执行模式化脱敏。"""
    redacted = sanitize_url(value)
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    redacted = _WS_URL_RE.sub("[REDACTED_WS_URL]", redacted)
    redacted = _BEARER_RE.sub(r"\1[REDACTED_TOKEN]", redacted)
    redacted = _PROXY_CRED_RE.sub("://[REDACTED_USER]:[REDACTED_PASS]@", redacted)
    redacted = _CARD_RE.sub("[REDACTED_CARD]", redacted)
    redacted = _SIX_DIGIT_CODE_RE.sub("[REDACTED_CODE]", redacted)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    return redacted


def _to_serializable(value: Any) -> Any:
    """将 dataclass / Enum 递归转成 JSON 友好结构。"""
    if is_dataclass(value):
        return {key: _to_serializable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _to_serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_serializable(item) for item in value]
    return value


def _is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SENSITIVE_FIELD_NAMES or any(marker in lowered for marker in ("token", "secret", "cookie", "proxy", "password"))


def redact_structure(value: Any, field_name: str = "") -> Any:
    """递归脱敏结构化对象。"""
    serializable = _to_serializable(value)

    if isinstance(serializable, dict):
        result: dict[str, Any] = {}
        for key, item in serializable.items():
            if _is_sensitive_field(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_structure(item, field_name=key)
        return result

    if isinstance(serializable, list):
        return [redact_structure(item, field_name=field_name) for item in serializable]

    if isinstance(serializable, str):
        if _is_sensitive_field(field_name):
            return "[REDACTED]"
        return _redact_text(serializable)

    return serializable


def build_llm_evidence_payload(evidence: Evidence) -> dict[str, Any]:
    """构造给 LLM 的上传副本。"""
    return redact_structure(evidence)


class ArtifactRecorder:
    """将运行证据稳定地写入文件系统。"""

    def __init__(self, base_dir: Path | str) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._step_counters: dict[str, int] = {}

    def start_run(self, account_id: str) -> str:
        safe_account = _redact_text(account_id).replace("/", "_")
        run_id = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{safe_account}-{uuid4().hex[:8]}"
        run_dir = self._base_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "account_id": safe_account}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._step_counters[run_id] = 0
        return run_id

    def record_step(
        self,
        *,
        run_id: str,
        evidence: Evidence,
        actions: list[dict[str, Any]] | list[Any],
        screenshot_path: str | None,
        trace_path: str | None = None,
        har_path: str | None = None,
    ) -> Path:
        run_dir = self._base_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        step_index = self._step_counters.get(run_id, 0) + 1
        self._step_counters[run_id] = step_index
        step_slug = (evidence.step_name or "step").lower().replace(" ", "_")
        step_dir = run_dir / f"{step_index:02d}_{step_slug}"
        step_dir.mkdir(parents=True, exist_ok=True)

        step_payload = redact_structure(evidence)
        (step_dir / "step.json").write_text(json.dumps(step_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (step_dir / "actionables.json").write_text(
            json.dumps(redact_structure(evidence.actionables), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (step_dir / "signals.json").write_text(
            json.dumps(redact_structure(evidence.signals), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._append_jsonl(run_dir / "evidence.jsonl", step_payload)
        self._append_jsonl(run_dir / "actions.jsonl", redact_structure(actions))

        self._copy_artifact(screenshot_path, step_dir / "screenshot.png")
        self._copy_artifact(trace_path, step_dir / "trace.zip")
        self._copy_artifact(har_path, step_dir / "network.har")
        return step_dir

    def record_handoff(self, *, run_id: str, payload: dict[str, Any]) -> Path:
        run_dir = self._base_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        handoff_path = run_dir / "handoff.json"
        handoff_path.write_text(json.dumps(redact_structure(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return handoff_path

    def record_triage(self, *, run_id: str, payload: dict[str, Any]) -> Path:
        """落盘卡住诊断结论。一次 run 可能多次卡住，故追加到 triage.jsonl。"""
        run_dir = self._base_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        triage_path = run_dir / "triage.jsonl"
        self._append_jsonl(triage_path, payload)
        return triage_path

    @staticmethod
    def _append_jsonl(path: Path, payload: Any) -> None:
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(redact_structure(payload), ensure_ascii=False))
            fp.write("\n")

    @staticmethod
    def _copy_artifact(source: str | None, destination: Path) -> None:
        if not source:
            return
        src = Path(source)
        if src.exists():
            shutil.copy2(src, destination)
