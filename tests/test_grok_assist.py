# -*- coding: utf-8 -*-
"""Grok AI 辅助决策 + 自进化层测试（feat/grok-register）。

覆盖：证据采集、经验命中走经验、未命中走 LLM、LLM 成功固化、
LLM 未启用降级、无候选返回 False。mock page/llm/experience，不需真浏览器或 API。
"""

import unittest
from unittest.mock import MagicMock

from src.automation import grok_assist as ga
from src.automation.models import Action, ActionKind, Decision, DecisionKind


# 模拟 page.evaluate：按 JS 片段返回不同结果
def _make_page(nodes, *, click_ok=True, fill_ok=True, url="https://accounts.x.ai/sign-up"):
    page = MagicMock()
    page.url = url
    page.title.return_value = "Create Your Grok Account"

    def _evaluate(js, arg=None):
        if "querySelectorAll('button, a" in js and "out.push" in js:  # collect actionables
            return nodes
        if "targetIdx" in js:  # click by idx
            return click_ok
        if "arg.idx" in js or "arg.value" in js:  # fill by idx
            return fill_ok
        return None
    page.evaluate.side_effect = _evaluate
    return page


_SAMPLE_NODES = [
    {"idx": 0, "tag": "button", "text": "Sign up with email", "role": "", "type": "",
     "name": "", "testid": "", "autocomplete": "", "ariaLabel": "", "href": "", "hasSvg": True,
     "disabled": False, "isInput": False},
    {"idx": 1, "tag": "button", "text": "Sign up with Apple", "role": "", "type": "",
     "name": "", "testid": "", "autocomplete": "", "ariaLabel": "", "href": "", "hasSvg": True,
     "disabled": False, "isInput": False},
    {"idx": 2, "tag": "input", "text": "", "role": "", "type": "email",
     "name": "email", "testid": "", "autocomplete": "email", "ariaLabel": "", "href": "", "hasSvg": False,
     "disabled": False, "isInput": True},
]


class TestCollectEvidence(unittest.TestCase):
    def test_collect_builds_actionables_and_step_location(self):
        page = _make_page(_SAMPLE_NODES)
        evidence, raw = ga.collect_evidence(page, step="entry", signals={"has_cookie": True})
        self.assertEqual(len(evidence.actionables), 3)
        self.assertIn("#grok_step=entry", evidence.url)  # step 编码进 location
        self.assertEqual(evidence.step_name, "grok_entry")
        # button → CLICK，input → FILL
        kinds = {a.action_id: a.kind for a in evidence.actionables}
        self.assertEqual(kinds["node_0"], ActionKind.CLICK)
        self.assertEqual(kinds["node_2"], ActionKind.FILL)


class TestAssistedAction(unittest.TestCase):
    def test_no_candidates_returns_false(self):
        page = _make_page([])  # 空页面无候选
        ok = ga.assisted_action(page, step="entry", want_fill=False)
        self.assertFalse(ok)

    def test_experience_hit_executes_without_llm(self):
        page = _make_page(_SAMPLE_NODES, click_ok=True)
        exp = MagicMock()
        exp.find_action_id.return_value = "node_0"  # 经验命中 email 按钮
        llm = MagicMock()
        ok = ga.assisted_action(page, step="entry", want_fill=False, experience=exp, llm_provider=llm)
        self.assertTrue(ok)
        exp.find_action_id.assert_called_once()
        llm.decide.assert_not_called()  # 命中经验不调 LLM

    def test_llm_choose_executes_and_records(self):
        page = _make_page(_SAMPLE_NODES, click_ok=True)
        exp = MagicMock()
        exp.find_action_id.return_value = ""  # 经验未命中
        llm = MagicMock()
        llm.decide.return_value = Decision(
            kind=DecisionKind.CHOOSE_ACTION, action_id="node_0", confidence=0.9, rationale="email 按钮"
        )
        ok = ga.assisted_action(page, step="entry", want_fill=False, experience=exp, llm_provider=llm)
        self.assertTrue(ok)
        llm.decide.assert_called_once()
        exp.record_success.assert_called_once()  # 成功后固化

    def test_llm_disabled_degrades_to_false(self):
        page = _make_page(_SAMPLE_NODES)
        exp = MagicMock()
        exp.find_action_id.return_value = ""
        # 无 LLM provider → 经验未命中后无路可走
        ok = ga.assisted_action(page, step="entry", want_fill=False, experience=exp, llm_provider=None)
        self.assertFalse(ok)
        exp.record_success.assert_not_called()

    def test_llm_abort_no_record(self):
        page = _make_page(_SAMPLE_NODES)
        exp = MagicMock()
        exp.find_action_id.return_value = ""
        llm = MagicMock()
        llm.decide.return_value = Decision(kind=DecisionKind.ABORT, rationale="无法判断")
        ok = ga.assisted_action(page, step="entry", want_fill=False, experience=exp, llm_provider=llm)
        self.assertFalse(ok)
        exp.record_success.assert_not_called()

    def test_fill_candidate_filtering(self):
        # want_fill=True 只保留 input → LLM 候选里只有 node_2
        page = _make_page(_SAMPLE_NODES, fill_ok=True)
        exp = MagicMock()
        exp.find_action_id.return_value = ""
        captured = {}
        def _decide(*, evidence, candidates):
            captured["ids"] = [c.action_id for c in candidates]
            return Decision(kind=DecisionKind.CHOOSE_ACTION, action_id="node_2", confidence=0.9)
        llm = MagicMock()
        llm.decide.side_effect = _decide
        ok = ga.assisted_action(page, step="fill_email", want_fill=True, fill_value="x@y.com",
                                experience=exp, llm_provider=llm)
        self.assertTrue(ok)
        self.assertEqual(captured["ids"], ["node_2"])  # 只有 input 候选


if __name__ == "__main__":
    unittest.main()
