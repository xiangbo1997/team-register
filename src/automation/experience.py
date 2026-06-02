# -*- coding: utf-8 -*-
"""把 LLM 解决过的卡点沉淀为可复用经验。

自进化闭环（P0 升级后）：
  - LLM/经验决策成功 -> record_success（outcome=success），失败 -> record_failure（outcome=fail）
  - find_action_id 改为「带统计择优」：聚合同 (state, location, signals) 各 action_id 的
    成败计数，算成功率 rate=s/(s+f)，只返回 rate>=阈值 且 s>=1 中 rate 最高者。
    rate 过低 = 已被淘汰（不再返回），避免坏经验永久复用。
  - jsonl 是 append-only，淘汰靠「聚合时不返回」而非物理删除，天然兼容老记录
    （老记录无 outcome 字段，按 success 解析）。
  - 可选 sink 回调：把每条成败 outcome 镜像写到 DB（learned_workflows 表），
    automation 层不直接依赖 db 层，由 main.py / orchestrator.py 构造时注入。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Optional

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

# 择优阈值：成功率低于此值的 action 视为已淘汰，find_action_id 不再返回。
_MIN_SUCCESS_RATE = 0.5


class ExperienceStore:
    """持久化记录“某类页面特征 -> 成功动作”，并按成败统计择优/淘汰。"""

    def __init__(self, path: str | Path, *, sink: Optional[Callable[..., Any]] = None) -> None:
        """
        Args:
            path: jsonl 文件路径。
            sink: 可选回调，签名 sink(*, state, location, signal_signature, action_id,
                  source, success, step_name)。用于把成败镜像写到 DB（双写）。
                  任何异常都被吞掉，绝不阻断主流程。
        """
        self._path = Path(path)
        self._sink = sink
        self._entries: list[dict[str, Any]] = []
        self._load()

    def find_action_id(self, *, evidence: Evidence, candidates: list[Action]) -> str:
        """在候选中找历史成功率最高且达标的 action_id；无则返回 ""。"""
        candidate_ids = {item.action_id for item in candidates}
        state = evidence.primary_state.value
        location = self._location_key(evidence.url)
        signal_signature = self._signal_signature(evidence.signals)

        # 聚合同 (state, location, signals) 下每个 action_id 的成败计数。
        counters: dict[str, dict[str, int]] = {}
        for entry in self._entries:
            if entry.get("entry_type") not in (None, "action"):
                continue
            if entry.get("state") != state:
                continue
            if entry.get("location") != location:
                continue
            if entry.get("signals") != signal_signature:
                continue
            action_id = str(entry.get("action_id", ""))
            if not action_id:
                continue
            # 老记录无 outcome 字段，按 success 解析（向后兼容）。
            outcome = str(entry.get("outcome", "success")).lower()
            bucket = counters.setdefault(action_id, {"success": 0, "fail": 0})
            if outcome == "fail":
                bucket["fail"] += 1
            else:
                bucket["success"] += 1

        best_id = ""
        best_rate = -1.0
        for action_id, bucket in counters.items():
            if action_id not in candidate_ids:
                continue
            success = bucket["success"]
            fail = bucket["fail"]
            total = success + fail
            if success < 1 or total == 0:
                continue
            rate = success / total
            if rate < _MIN_SUCCESS_RATE:
                continue  # 已淘汰
            if rate > best_rate:
                best_rate = rate
                best_id = action_id
        return best_id

    def stats(self, *, evidence: Evidence) -> list[dict[str, Any]]:
        """返回同 (state, location, signals) 下每个 action_id 的成败统计（控制台/可观测用）。"""
        state = evidence.primary_state.value
        location = self._location_key(evidence.url)
        signal_signature = self._signal_signature(evidence.signals)

        counters: dict[str, dict[str, int]] = {}
        for entry in self._entries:
            if entry.get("entry_type") not in (None, "action"):
                continue
            if entry.get("state") != state or entry.get("location") != location:
                continue
            if entry.get("signals") != signal_signature:
                continue
            action_id = str(entry.get("action_id", ""))
            if not action_id:
                continue
            outcome = str(entry.get("outcome", "success")).lower()
            bucket = counters.setdefault(action_id, {"success": 0, "fail": 0})
            bucket["fail" if outcome == "fail" else "success"] += 1

        result: list[dict[str, Any]] = []
        for action_id, bucket in counters.items():
            success = bucket["success"]
            fail = bucket["fail"]
            total = success + fail
            result.append(
                {
                    "action_id": action_id,
                    "success_count": success,
                    "fail_count": fail,
                    "success_rate": (success / total) if total else 0.0,
                }
            )
        return result

    def record_success(self, *, evidence: Evidence, action: Action, source: str) -> None:
        self._record_outcome(evidence=evidence, action=action, source=source, success=True)

    def record_failure(self, *, evidence: Evidence, action: Action, source: str) -> None:
        self._record_outcome(evidence=evidence, action=action, source=source, success=False)

    def _record_outcome(self, *, evidence: Evidence, action: Action, source: str, success: bool) -> None:
        state = evidence.primary_state.value
        location = self._location_key(evidence.url)
        signal_signature = self._signal_signature(evidence.signals)
        entry = {
            "entry_type": "action",
            "state": state,
            "location": location,
            "signals": signal_signature,
            "action_id": action.action_id,
            "source": source,
            "outcome": "success" if success else "fail",
            "step_name": evidence.step_name,
        }
        self._append_entry(entry)

        # 双写到 DB（若注入了 sink）。失败静默，绝不阻断主流程。
        if self._sink is not None:
            try:
                self._sink(
                    state=state,
                    location=location,
                    signal_signature=signal_signature,
                    action_id=action.action_id,
                    source=source,
                    success=success,
                    step_name=evidence.step_name,
                    last_action_meta=dict(action.params or {}),
                )
            except Exception:
                pass

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
        # 保留 fragment（grok / OpenAI 兜底层用 #step= 编码步骤隔离经验）。
        location = f"{parsed.netloc}{path}"
        if parsed.fragment:
            location = f"{location}#{parsed.fragment}"
        return location

    @staticmethod
    def _signal_signature(signals: dict[str, Any]) -> dict[str, bool]:
        return {key: bool(signals.get(key)) for key in _MATCH_SIGNAL_KEYS}
