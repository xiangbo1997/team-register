# -*- coding: utf-8 -*-
"""learned_workflow_service 测试：upsert 计数 / 择优 / 启禁 / 删除。"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.services import learned_workflow_service as lw


def _build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


_SIG = {"has_password_input": True, "has_code_input": False}
_LOC = "auth.openai.com/log-in/password"


class LearnedWorkflowServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = _build_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def _record(self, action_id, success, source="llm"):
        return lw.record_outcome(
            state="AUTH",
            location=_LOC,
            signal_signature=_SIG,
            action_id=action_id,
            source=source,
            success=success,
        )

    def test_upsert_increments_counts(self):
        self._record("submit_password", True)
        self._record("submit_password", True)
        self._record("submit_password", False)
        rows = lw.list_workflows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].success_count, 2)
        self.assertEqual(rows[0].fail_count, 1)

    def test_find_best_action_respects_rate_threshold(self):
        # 2/3 = 0.67 >= 0.5 → 返回
        self._record("submit_password", True)
        self._record("submit_password", True)
        self._record("submit_password", False)
        best = lw.find_best_action(
            state="AUTH", location=_LOC, signal_signature=_SIG,
            candidate_ids={"submit_password", "wait_short"},
        )
        self.assertEqual(best, "submit_password")

    def test_find_best_action_evicts_low_rate(self):
        # 1/4 = 0.25 < 0.5 → 不返回
        self._record("bad", True)
        for _ in range(3):
            self._record("bad", False)
        best = lw.find_best_action(
            state="AUTH", location=_LOC, signal_signature=_SIG, candidate_ids={"bad"},
        )
        self.assertEqual(best, "")

    def test_disabled_workflow_not_returned(self):
        row = self._record("submit_password", True)
        lw.set_enabled(row.id, False)
        best = lw.find_best_action(
            state="AUTH", location=_LOC, signal_signature=_SIG, candidate_ids={"submit_password"},
        )
        self.assertEqual(best, "")

    def test_delete_workflow(self):
        row = self._record("submit_password", True)
        self.assertTrue(lw.delete_workflow(row.id))
        self.assertEqual(len(lw.list_workflows()), 0)
        self.assertFalse(lw.delete_workflow(row.id))  # 已删，再删返回 False

    def test_prefers_higher_rate_among_candidates(self):
        self._record("a", True)
        self._record("a", True)            # a: 1.0
        self._record("b", True)
        self._record("b", False)           # b: 0.5
        best = lw.find_best_action(
            state="AUTH", location=_LOC, signal_signature=_SIG, candidate_ids={"a", "b"},
        )
        self.assertEqual(best, "a")

    def test_stats_overview(self):
        self._record("a", True)
        self._record("b", False)
        ov = lw.stats_overview()
        self.assertEqual(ov["workflow_count"], 2)
        self.assertEqual(ov["total_success"], 1)
        self.assertEqual(ov["total_fail"], 1)
        self.assertAlmostEqual(ov["overall_success_rate"], 0.5)

    def test_different_signature_creates_separate_rows(self):
        self._record("submit_password", True)
        lw.record_outcome(
            state="AUTH", location=_LOC,
            signal_signature={"has_password_input": False, "has_code_input": True},
            action_id="submit_password", source="llm", success=True,
        )
        self.assertEqual(len(lw.list_workflows()), 2)


if __name__ == "__main__":
    unittest.main()
