# -*- coding: utf-8 -*-
"""
Grok 注册「AI 辅助决策 + 自进化」层（feat/grok-register）

当 grok_runtime 的硬规则 selector 找不到目标元素（页面改版 / 新弹窗 / 多语言变体）时，
不再直接 raise，而是走这一层：

  采集证据(Evidence) → 查经验(ExperienceStore) → 命中则执行
                                              ↘ 未命中 + 有 LLM → LLMDecisionProvider 决策 → 执行
                                                                                      ↘ 成功 → record_success 固化

固化后，下次遇到同一页面（同 location + 同信号签名），ExperienceStore 直接命中，
不再调 LLM —— 这就是项目「遇到不能处理的问题 → AI 辅助 → 按新流程固化」的自进化闭环。

复用既有基建（不改 OpenAI runtime.py）：
- ``src.automation.models``：Evidence / Action / Actionable / Decision / DecisionKind / ActionKind
- ``src.automation.experience.ExperienceStore``：经验持久化（grok 用独立 jsonl 文件）
- ``src.automation.llm.LLMDecisionProvider``：受限 LLM 决策（与 OpenAI 同一个）

关键技巧：ExperienceStore 的 `_signal_signature` 写死了 OpenAI 信号键集（grok 信号进不去），
所以 grok 把「步骤名」编码进 ``evidence.url`` 的 location 维度（``...sign-up#grok_step=verify_email``），
让经验按 (location, state) 精确隔离，不依赖 signal 键集，也不串味 OpenAI 经验。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from src.automation.models import (
    Action,
    ActionKind,
    Actionable,
    AutomationState,
    Decision,
    DecisionKind,
    Evidence,
)

logger = logging.getLogger("grok_assist")


# ── 采集页面所有可交互元素摘要（语言无关）──
# 返回 [{idx, tag, text, role, type, name, testid, href, hasSvg, isInput, inputType}]
_JS_COLLECT_ACTIONABLES = r"""() => {
  const isVisible = (n) => {
    if (!n) return false;
    const s = getComputedStyle(n);
    if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    const r = n.getBoundingClientRect();
    return r.width>0 && r.height>0;
  };
  const out = [];
  let idx = 0;
  const nodes = document.querySelectorAll('button, a, [role="button"], input, select, textarea');
  for (const n of nodes) {
    if (!isVisible(n)) continue;
    const tag = n.tagName.toLowerCase();
    const isInput = (tag === 'input' || tag === 'textarea' || tag === 'select');
    out.push({
      idx: idx++,
      tag,
      text: ((n.innerText || n.textContent || n.value || '').replace(/\s+/g,' ').trim()).slice(0, 60),
      role: n.getAttribute('role') || '',
      type: (n.getAttribute('type') || '').toLowerCase(),
      name: n.getAttribute('name') || '',
      testid: n.getAttribute('data-testid') || '',
      autocomplete: (n.getAttribute('autocomplete') || '').toLowerCase(),
      ariaLabel: n.getAttribute('aria-label') || '',
      href: (n.getAttribute('href') || '').slice(0, 80),
      hasSvg: !!n.querySelector('svg'),
      disabled: !!n.disabled,
      isInput,
    });
    if (idx >= 60) break;
  }
  return out;
}"""

# 按 idx 执行点击（与采集同序遍历，保证 idx 稳定）
_JS_CLICK_BY_IDX = r"""(targetIdx) => {
  const isVisible = (n) => {
    if (!n) return false;
    const s = getComputedStyle(n);
    if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    const r = n.getBoundingClientRect();
    return r.width>0 && r.height>0;
  };
  let idx = 0;
  const nodes = document.querySelectorAll('button, a, [role="button"], input, select, textarea');
  for (const n of nodes) {
    if (!isVisible(n)) continue;
    if (idx === targetIdx) {
      n.scrollIntoView({ block: 'center' });
      n.focus(); n.click();
      return true;
    }
    idx++;
    if (idx >= 60) break;
  }
  return false;
}"""

# 按 idx 填值（React 受控）
_JS_FILL_BY_IDX = r"""(arg) => {
  const targetIdx = arg.idx, value = arg.value;
  const isVisible = (n) => {
    if (!n) return false;
    const s = getComputedStyle(n);
    if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
    const r = n.getBoundingClientRect();
    return r.width>0 && r.height>0;
  };
  let idx = 0;
  const nodes = document.querySelectorAll('button, a, [role="button"], input, select, textarea');
  for (const n of nodes) {
    if (!isVisible(n)) continue;
    if (idx === targetIdx) {
      n.focus(); n.click();
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      const tracker = n._valueTracker;
      if (tracker) tracker.setValue('');
      if (setter) { setter.call(n, ''); setter.call(n, value); } else { n.value = value; }
      n.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
      n.dispatchEvent(new Event('change', { bubbles: true }));
      n.blur();
      return true;
    }
    idx++;
    if (idx >= 60) break;
  }
  return false;
}"""


def collect_evidence(page: Any, *, step: str, signals: Optional[dict] = None) -> tuple[Evidence, list[dict]]:
    """采集当前页面证据 + 原始候选元素列表。

    step：grok 步骤名（entry/fill_email/verify_email/profile/sso），编码进 url location 隔离经验。
    返回 (Evidence, raw_nodes)；raw_nodes 用于执行时按 idx 回映射真实 DOM。
    """
    try:
        raw_nodes = page.evaluate(_JS_COLLECT_ACTIONABLES) or []
    except Exception as exc:
        logger.warning("grok 采集 actionables 失败: %s", exc)
        raw_nodes = []

    actionables: list[Actionable] = []
    for node in raw_nodes:
        is_input = bool(node.get("isInput"))
        kind = ActionKind.FILL if is_input else ActionKind.CLICK
        # 给每个元素一段人类/LLM 可读的 name，便于 LLM 判断
        descr_bits = [
            node.get("text") or "",
            node.get("ariaLabel") or "",
            node.get("testid") or "",
            node.get("name") or "",
            node.get("autocomplete") or "",
        ]
        name = " | ".join(b for b in descr_bits if b)[:80] or f"<{node.get('tag')}>"
        actionables.append(
            Actionable(
                action_id=f"node_{node.get('idx')}",
                kind=kind,
                locator_ref=f"idx:{node.get('idx')}",
                role=node.get("role") or node.get("tag") or "",
                name=name,
                visible=True,
                enabled=not node.get("disabled"),
                metadata={
                    "tag": node.get("tag"),
                    "type": node.get("type"),
                    "autocomplete": node.get("autocomplete"),
                    "has_svg": node.get("hasSvg"),
                    "href": node.get("href"),
                },
            )
        )

    try:
        url = str(page.url)
    except Exception:
        url = ""
    try:
        title = str(page.title())
    except Exception:
        title = ""

    # step 编码进 url location（绕开 OpenAI 写死的 signal 键集，让经验按步骤精确隔离）
    location_url = f"{url}#grok_step={step}"
    evidence = Evidence(
        url=location_url,
        title=title,
        step_name=f"grok_{step}",
        state_candidates=[AutomationState.UNKNOWN],
        actionables=actionables,
        signals=dict(signals or {}),
    )
    return evidence, raw_nodes


def _candidates_from_evidence(evidence: Evidence, *, want_fill: bool) -> list[Action]:
    """把 Actionable 转成 LLM/Experience 用的 Action 候选。

    want_fill=True 时只保留可填元素（input/textarea/select），否则只保留可点元素。
    """
    out: list[Action] = []
    for a in evidence.actionables:
        is_fill = a.kind == ActionKind.FILL
        if want_fill != is_fill:
            continue
        out.append(
            Action(
                action_id=a.action_id,
                kind=a.kind,
                description=f"{a.role}: {a.name}",
                locator_ref=a.locator_ref,
                params={"idx": int(a.locator_ref.split(":")[1])} if a.locator_ref.startswith("idx:") else {},
            )
        )
    return out


def assisted_action(
    page: Any,
    *,
    step: str,
    want_fill: bool,
    fill_value: str = "",
    experience: Any = None,
    llm_provider: Any = None,
    emit: Optional[Callable] = None,
    signals: Optional[dict] = None,
    verify: Optional[Callable[[], bool]] = None,
) -> bool:
    """硬规则失败后的 AI 辅助决策入口。

    流程：采集证据 → 查经验命中则执行 → 否则 LLM 决策 → 执行 → verify 通过则固化。

    Args:
        step: grok 步骤名（经验隔离用）。
        want_fill: True=要找填值元素，False=要找点击元素。
        fill_value: want_fill 时要填的值。
        experience: ExperienceStore 实例（None 则不查/不固化）。
        llm_provider: LLMDecisionProvider 实例（None 则跳过 LLM）。
        emit: 事件回调 emit(event_type, state, message, **extra)。
        verify: 执行后校验回调，返回 True 表示动作生效（用于决定是否固化）。

    Returns:
        True=成功执行了一个动作（且 verify 通过 / 无 verify）；False=无可用决策。
    """
    def _emit(msg: str, **extra: Any) -> None:
        if callable(emit):
            try:
                emit("action", None, msg, action_id="grok_assist", **extra)
            except Exception:
                pass

    evidence, _raw = collect_evidence(page, step=step, signals=signals)
    candidates = _candidates_from_evidence(evidence, want_fill=want_fill)
    if not candidates:
        _emit("AI 辅助：当前页面无可用候选元素", result="no_candidates", step=step)
        return False

    def _execute(action_id: str) -> bool:
        action = next((c for c in candidates if c.action_id == action_id), None)
        if action is None:
            return False
        idx = action.params.get("idx")
        if idx is None:
            return False
        try:
            if want_fill:
                ok = bool(page.evaluate(_JS_FILL_BY_IDX, {"idx": idx, "value": fill_value}))
            else:
                ok = bool(page.evaluate(_JS_CLICK_BY_IDX, idx))
        except Exception as exc:
            logger.warning("grok 辅助执行 idx=%s 失败: %s", idx, exc)
            return False
        if not ok:
            return False
        time.sleep(1.0)
        return verify() if callable(verify) else True

    # 1) 查经验
    if experience is not None:
        try:
            hit = experience.find_action_id(evidence=evidence, candidates=candidates)
        except Exception:
            hit = ""
        if hit:
            _emit(f"AI 辅助：命中经验直接执行 {hit}", result="experience_hit", step=step, action_id_chosen=hit)
            if _execute(hit):
                return True
            # 经验失效（页面变了）→ 继续走 LLM

    # 2) LLM 兜底
    if llm_provider is not None:
        try:
            decision: Decision = llm_provider.decide(evidence=evidence, candidates=candidates)
        except Exception as exc:
            logger.warning("grok LLM 决策异常: %s", exc)
            decision = Decision(kind=DecisionKind.ABORT, rationale=str(exc))
        if decision.kind == DecisionKind.CHOOSE_ACTION and decision.action_id:
            _emit(
                f"AI 辅助：LLM 决策选择 {decision.action_id}（{decision.rationale[:60]}）",
                result="llm_choose", step=step, action_id_chosen=decision.action_id,
                confidence=decision.confidence,
            )
            if _execute(decision.action_id):
                # 3) 固化
                if experience is not None:
                    action = next((c for c in candidates if c.action_id == decision.action_id), None)
                    if action is not None:
                        try:
                            experience.record_success(evidence=evidence, action=action, source="llm")
                            _emit("AI 辅助：已固化为经验（下次同页面直接复用）", result="recorded", step=step)
                        except Exception as exc:
                            logger.warning("grok 经验固化失败: %s", exc)
                return True
        else:
            _emit(f"AI 辅助：LLM 未给出可执行决策（{decision.kind.value}）", result="llm_no_action", step=step)

    _emit("AI 辅助：经验与 LLM 均无可用决策", result="exhausted", step=step)
    return False


def build_grok_experience(config: Any) -> Any:
    """构造 grok 专用 ExperienceStore（独立 jsonl，不与 OpenAI 经验混）。失败返回 None。"""
    try:
        import os
        from src.automation.experience import ExperienceStore
        base = str(getattr(config, "run_artifacts_dir", "") or "artifacts").strip() or "artifacts"
        path = os.path.join(base, "grok_experience.jsonl")
        return ExperienceStore(path)
    except Exception as exc:
        logger.warning("grok ExperienceStore 构造失败（降级无经验）: %s", exc)
        return None


__all__ = ["collect_evidence", "assisted_action", "build_grok_experience"]
