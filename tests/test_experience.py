# -*- coding: utf-8 -*-
"""ExperienceStore 成败计数 + 择优淘汰 + 老 jsonl 兼容测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from src.automation.experience import ExperienceStore
from src.automation.models import Action, ActionKind, AutomationState, Evidence


def _evidence(signals=None):
    return Evidence(
        url="https://auth.openai.com/log-in/password",
        step_name="auth",
        state_candidates=[AutomationState.AUTH],
        signals=signals or {"has_password_input": True},
    )


def _action(action_id="submit_password"):
    return Action(action_id=action_id, kind=ActionKind.FILL, description="填密码")


class TestExperienceCounting(unittest.TestCase):
    def test_success_then_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl")
            store.record_success(evidence=_evidence(), action=_action(), source="llm")
            hit = store.find_action_id(evidence=_evidence(), candidates=[_action()])
            self.assertEqual(hit, "submit_password")

    def test_low_success_rate_is_evicted(self):
        """成功 1 次失败 3 次 → rate=0.25 < 0.5 → 不再返回（淘汰）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl")
            store.record_success(evidence=_evidence(), action=_action(), source="llm")
            for _ in range(3):
                store.record_failure(evidence=_evidence(), action=_action(), source="experience")
            hit = store.find_action_id(evidence=_evidence(), candidates=[_action()])
            self.assertEqual(hit, "")

    def test_prefers_higher_success_rate(self):
        """两个候选都达标时返回成功率更高者。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl")
            # action_a：2 成 0 败 = 1.0
            store.record_success(evidence=_evidence(), action=_action("a"), source="llm")
            store.record_success(evidence=_evidence(), action=_action("a"), source="llm")
            # action_b：1 成 1 败 = 0.5
            store.record_success(evidence=_evidence(), action=_action("b"), source="llm")
            store.record_failure(evidence=_evidence(), action=_action("b"), source="llm")
            hit = store.find_action_id(
                evidence=_evidence(), candidates=[_action("a"), _action("b")]
            )
            self.assertEqual(hit, "a")

    def test_stats_reports_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl")
            store.record_success(evidence=_evidence(), action=_action(), source="llm")
            store.record_failure(evidence=_evidence(), action=_action(), source="llm")
            stats = store.stats(evidence=_evidence())
            self.assertEqual(len(stats), 1)
            self.assertEqual(stats[0]["success_count"], 1)
            self.assertEqual(stats[0]["fail_count"], 1)
            self.assertAlmostEqual(stats[0]["success_rate"], 0.5)

    def test_legacy_jsonl_without_outcome_is_success(self):
        """老记录无 outcome 字段 → 按 success 解析（向后兼容）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            # 手写一条老格式记录（无 outcome 键）
            legacy = {
                "entry_type": "action",
                "state": "AUTH",
                "location": "auth.openai.com/log-in/password",
                "signals": {k: False for k in ExperienceStore._signal_signature({})},
                "action_id": "submit_password",
                "source": "llm",
                "step_name": "auth",
            }
            # signals 要和 _evidence() 的签名一致才能命中
            legacy["signals"] = ExperienceStore._signal_signature({"has_password_input": True})
            path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

            store = ExperienceStore(path)
            hit = store.find_action_id(evidence=_evidence(), candidates=[_action()])
            self.assertEqual(hit, "submit_password")

    def test_sink_called_on_record(self):
        captured = []

        def _sink(**kwargs):
            captured.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl", sink=_sink)
            store.record_success(evidence=_evidence(), action=_action(), source="llm")
            store.record_failure(evidence=_evidence(), action=_action(), source="experience")
        self.assertEqual(len(captured), 2)
        self.assertTrue(captured[0]["success"])
        self.assertFalse(captured[1]["success"])
        self.assertEqual(captured[0]["action_id"], "submit_password")

    def test_sink_exception_does_not_break(self):
        def _bad_sink(**kwargs):
            raise RuntimeError("DB down")

        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl", sink=_bad_sink)
            # 不应抛异常
            store.record_success(evidence=_evidence(), action=_action(), source="llm")
            self.assertEqual(
                store.find_action_id(evidence=_evidence(), candidates=[_action()]),
                "submit_password",
            )

    def test_fragment_isolates_location(self):
        """带 #fragment 的 location 与不带的互相隔离（兜底层经验隔离基础）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "m.jsonl")
            ev_a = Evidence(
                url="https://chatgpt.com/#openai_about_you",
                state_candidates=[AutomationState.UNKNOWN],
                signals={},
            )
            ev_b = Evidence(
                url="https://chatgpt.com/#openai_phone",
                state_candidates=[AutomationState.UNKNOWN],
                signals={},
            )
            store.record_success(evidence=ev_a, action=_action("node_1"), source="llm")
            # ev_b 不同 fragment → 不命中 ev_a 的经验
            self.assertEqual(store.find_action_id(evidence=ev_b, candidates=[_action("node_1")]), "")
            self.assertEqual(store.find_action_id(evidence=ev_a, candidates=[_action("node_1")]), "node_1")


if __name__ == "__main__":
    unittest.main()
