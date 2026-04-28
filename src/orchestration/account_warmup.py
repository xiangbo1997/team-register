# -*- coding: utf-8 -*-
"""
账号养号 v1（弹性 7 天窗口）

业务流程：
  1) 注册成功（HOME state + token 提取）后，如果 ENABLE_ACCOUNT_WARMUP=true，
     标 phase = "warming_up_account" + next_action_at = now + 24h
  2) 调度循环（worker.py 60s tick）按 next_action_at 触发本模块的 run_warmup_session
  3) run_warmup_session：登录账号 → 检测风控信号 → 发 N 条对话 → 关浏览器
  4) decide_next_warmup_action：根据 blocked_count 和已养天数动态决定下一步
     - blocked_count == 0 + 已养 ≥ min_days → 切到 "binding_team" 准备绑卡
     - blocked_count == 1 → 推 next_action +1 天，最多养 max_days
     - blocked_count >= warmup_blocked_threshold → 标 "abandoned"

设计要点：
  - 单次 session 只跑一次登录 + 3-10 条消息，不在同一进程内 sleep 24h
  - 信号检测：captcha 出现 / BLOCKED 状态 / phone re-verify prompt → blocked_count +1
  - 同 AdsPower profile + 同 IP，不切换设备指纹（agent 调研：一致性 > 切换）
  - 每天对话条数 random，模拟真实用户波动
  - 话题从配置文件载入（留空用内置默认），随机抽取避免脚本化模式
"""

from __future__ import annotations

import json
import logging
import pathlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# 内置默认话题：偏向自然 / 技术询问 / 写作辅助，覆盖典型 ChatGPT 用户场景
_DEFAULT_TOPICS: tuple[str, ...] = (
    "Help me write a friendly email declining a meeting request",
    "Explain quantum computing to a 10-year-old",
    "Suggest 3 healthy lunch ideas with chicken and rice",
    "Translate the phrase 'thank you for your patience' to Spanish, French, and Japanese",
    "What are the differences between TCP and UDP?",
    "Recommend a 30-minute beginner workout I can do at home",
    "Summarize the plot of Hamlet in 100 words",
    "Help me plan a 3-day trip to Tokyo on a budget",
    "What is the boiling point of water at sea level?",
    "Write a haiku about Monday mornings",
    "Compare React and Vue for a small project",
    "Give me 5 productivity tips for working from home",
    "What's the difference between machine learning and deep learning?",
    "Suggest a name for a new coffee shop",
    "Explain the concept of compound interest with an example",
)


@dataclass(frozen=True)
class WarmupSessionResult:
    """单次养号 session 结果。"""

    success: bool
    messages_sent: int = 0
    blocked_signals: list[str] = None  # type: ignore[assignment]
    error: str = ""

    def __post_init__(self) -> None:
        # frozen dataclass 兼容默认 list
        if self.blocked_signals is None:
            object.__setattr__(self, "blocked_signals", [])


@dataclass(frozen=True)
class NextActionDecision:
    """决策树输出：下一步做什么。"""

    next_phase: str  # "warming_up_account" | "binding_team" | "abandoned"
    next_action_at: Optional[datetime] = None
    reason: str = ""


def load_topics(path: str = "") -> list[str]:
    """从 JSON 文件加载话题列表。空路径或读取失败返回内置默认。"""
    if not path:
        return list(_DEFAULT_TOPICS)
    try:
        p = pathlib.Path(path)
        if not p.exists():
            logger.warning("话题文件不存在 (%s)，回退内置", path)
            return list(_DEFAULT_TOPICS)
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            logger.warning("话题文件格式异常 (%s)，回退内置", path)
            return list(_DEFAULT_TOPICS)
        return [str(item) for item in data if isinstance(item, str) and item.strip()]
    except Exception as exc:
        logger.warning("加载话题文件失败 (%s)，回退内置: %s", path, exc)
        return list(_DEFAULT_TOPICS)


def pick_messages(
    *,
    topics: list[str],
    min_count: int,
    max_count: int,
    rng: Optional[random.Random] = None,
) -> list[str]:
    """从话题池随机抽 N 条不重复消息。N 也是随机。"""
    rng = rng or random.Random()
    safe_min = max(1, int(min_count or 1))
    safe_max = max(safe_min, int(max_count or safe_min))
    n = rng.randint(safe_min, safe_max)
    n = min(n, len(topics))
    if n <= 0:
        return []
    return rng.sample(topics, n)


def detect_blocked_signals(
    *,
    page_signals: dict[str, Any],
    page_url: str = "",
) -> list[str]:
    """从单次养号 session 的页面信号字典检测风控指标。返回触发列表（可空）。"""
    triggered: list[str] = []
    sigs = dict(page_signals or {})

    if sigs.get("has_challenge_widget"):
        triggered.append("captcha_widget")
    if sigs.get("has_challenge_text"):
        triggered.append("captcha_text")
    if sigs.get("has_phone_input"):
        # 已经登录后的 home 页突然要求 phone 验证 → 风控
        triggered.append("phone_re_verify")
    if sigs.get("has_role_alert"):
        triggered.append("role_alert")
    if sigs.get("has_auth_error") or "auth/error" in str(page_url or ""):
        triggered.append("auth_error_url")
    return triggered


def run_warmup_session(
    *,
    run_id: str,
    page: Any,
    config: Any,
    rng: Optional[random.Random] = None,
    chat_send_fn: Optional[Callable[[Any, str], bool]] = None,
    page_signal_fn: Optional[Callable[[Any], dict[str, Any]]] = None,
    page_url_fn: Optional[Callable[[Any], str]] = None,
    emit_event: Optional[Callable[..., None]] = None,
) -> WarmupSessionResult:
    """单次养号 session：发 N 条消息 + 检测风控信号。

    所有外部依赖（chat_send / signal采集 / URL 读取 / event 上报）都通过参数注入，
    生产路径调用时传入真实 handler，单测注入 mock。
    """
    if chat_send_fn is None:
        from src.orchestration.handlers import send_chat_message
        chat_send_fn = lambda p, msg: send_chat_message(p, msg)  # noqa: E731
    if page_signal_fn is None:
        page_signal_fn = _collect_signals_from_page
    if page_url_fn is None:
        page_url_fn = lambda p: str(getattr(p, "url", "") or "")  # noqa: E731

    topics = load_topics(getattr(config, "warmup_conversation_topics_path", "") or "")
    messages = pick_messages(
        topics=topics,
        min_count=int(getattr(config, "warmup_messages_per_day_min", 3) or 3),
        max_count=int(getattr(config, "warmup_messages_per_day_max", 10) or 10),
        rng=rng,
    )

    sent = 0
    blocked: list[str] = []
    try:
        for msg in messages:
            if chat_send_fn(page, msg):
                sent += 1
                if emit_event:
                    try:
                        emit_event("warmup_message_sent", {"run_id": run_id, "preview": msg[:50]})
                    except Exception:
                        pass
            else:
                logger.warning("send_chat_message 失败，跳过本条")
        # 信号采集：会话结束时检测一次风控
        try:
            sigs = page_signal_fn(page) or {}
            url = page_url_fn(page) or ""
            blocked = detect_blocked_signals(page_signals=sigs, page_url=url)
        except Exception as exc:
            logger.warning("风控信号采集失败: %s", exc)
        return WarmupSessionResult(success=True, messages_sent=sent, blocked_signals=blocked)
    except Exception as exc:
        logger.error("run_warmup_session 异常: %s", exc)
        return WarmupSessionResult(
            success=False, messages_sent=sent, blocked_signals=blocked,
            error=f"{type(exc).__name__}: {exc}"
        )


def decide_next_warmup_action(
    *,
    config: Any,
    days_warmed: int,
    blocked_count: int,
    last_session_blocked: bool,
    now: Optional[datetime] = None,
) -> NextActionDecision:
    """弹性决策树：根据养号天数和风控信号决定下一步。

    Args:
      config: AppConfig（读 warmup_min_days / warmup_max_days / warmup_blocked_threshold）
      days_warmed: 已经养了多少天（含今天）
      blocked_count: 累计触发风控信号次数
      last_session_blocked: 本次 session 是否触发了任何风控信号
      now: 时间基准（测试可注入）
    """
    now = now or datetime.now(timezone.utc)
    min_days = int(getattr(config, "warmup_min_days", 3) or 3)
    max_days = int(getattr(config, "warmup_max_days", 7) or 7)
    blocked_threshold = int(getattr(config, "warmup_blocked_threshold", 2) or 2)

    # 红线 1：累计 blocked 超阈值 → 弃号
    if blocked_count >= blocked_threshold:
        return NextActionDecision(
            next_phase="abandoned",
            next_action_at=None,
            reason=f"blocked_count={blocked_count} >= threshold={blocked_threshold}，弃号",
        )

    # 红线 2：达到 max_days 上限 → 不论信号绿/黄都强制走绑卡（最后一搏）
    if days_warmed >= max_days:
        return NextActionDecision(
            next_phase="binding_team",
            next_action_at=now,  # 立即
            reason=f"达到 max_days={max_days}，强制绑卡",
        )

    # 信号绿（本次没触发）+ 已养够 min_days → 进入绑卡
    if not last_session_blocked and days_warmed >= min_days:
        return NextActionDecision(
            next_phase="binding_team",
            next_action_at=now,
            reason=f"信号绿且 days_warmed={days_warmed} >= min_days={min_days}，进入绑卡",
        )

    # 否则继续养 1 天（信号黄 → 也只推 1 天，不延长太多）
    return NextActionDecision(
        next_phase="warming_up_account",
        next_action_at=now + timedelta(hours=24),
        reason=(
            f"继续养号: days_warmed={days_warmed} < {min_days if not last_session_blocked else max_days}, "
            f"blocked_count={blocked_count}"
        ),
    )


# ── 默认信号采集（独立于 EvidenceCollector，避免依赖 AutomationRuntime）─────


_PHONE_INPUT_SELECTOR = 'input[name="phoneNumber"]'
_CHALLENGE_WIDGET_SELECTOR = (
    'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[title*="captcha"], '
    '[data-sitekey], input[name*="captcha"]'
)
_ROLE_ALERT_SELECTOR = '[role="alert"]'


def _safe_count(page: Any, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:
        return 0


def _safe_visible(page: Any, selector: str, timeout: int = 1000) -> bool:
    try:
        return bool(page.locator(selector).first.is_visible(timeout=timeout))
    except Exception:
        return False


def _safe_body_text(page: Any) -> str:
    try:
        return str(page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return ""


def _collect_signals_from_page(page: Any) -> dict[str, Any]:
    """直接从 page 上探测风控相关信号，不依赖 EvidenceCollector。"""
    body_text = _safe_body_text(page)
    return {
        "has_phone_input": _safe_count(page, _PHONE_INPUT_SELECTOR) > 0,
        "has_challenge_widget": _safe_count(page, _CHALLENGE_WIDGET_SELECTOR) > 0,
        "has_challenge_text": any(
            phrase in body_text for phrase in ("captcha", "challenge", "verify you are human")
        ),
        "has_role_alert": _safe_count(page, _ROLE_ALERT_SELECTOR) > 0,
        "has_auth_error": "auth/error" in str(getattr(page, "url", "") or ""),
    }
