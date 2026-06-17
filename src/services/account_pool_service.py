# -*- coding: utf-8 -*-
"""
普号池 / Plus 号池 / Team 号池服务

设计立场：
  - 三池 = 同一张 Run 表的三个 account_tier 状态：registered / plus / team / abandoned
  - 注册成功（Phase 1+2 完成）的号默认 account_tier='registered' → 进普号池
  - 运维从普号池里挑号 → 用熟卡生成 Team/Plus checkout 链接 → 手动完成绑卡
  - 绑卡成功 → 调 promote(run_id, "plus" 或 "team") → 号晋级
  - 绑卡失败 → 调 abandon(run_id, reason) → 号被标记放弃
  - access_token 来源：Run.config_snapshot 里如果有就用；否则从 accounts.csv 兜底（main.py:export_success
    把 token 写进 csv）。这是当前架构的妥协，后续可以让 worker 把 token 持久化到 Run 上

注意事项：
  - 本服务不跑浏览器自动化（那是 orchestrator 的事），只负责数据流和事件
  - generate_bind_link 复用 src/payment_link.py:PaymentLinkGenerator
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import CardActivation, MailAccount, Run, RunEvent

logger = logging.getLogger(__name__)


# 段位常量（与 DB 字段值对齐）
TIER_REGISTERED = "registered"
TIER_PLUS = "plus"
TIER_TEAM = "team"
TIER_ABANDONED = "abandoned"

_VALID_TIERS = (TIER_REGISTERED, TIER_PLUS, TIER_TEAM, TIER_ABANDONED)
_PROMOTABLE_TARGETS = (TIER_PLUS, TIER_TEAM)


# 注册平台常量（与 Run.platform 字段值对齐）：openai = GPT / ChatGPT，grok = x.ai Grok
PLATFORM_OPENAI = "openai"
PLATFORM_GROK = "grok"
_VALID_PLATFORMS = (PLATFORM_OPENAI, PLATFORM_GROK)


def _normalize_platform(platform: Optional[str]) -> Optional[str]:
    """规整 platform 过滤值。

    None / "" / "all" → None（不过滤，向后兼容）；合法值原样返回；非法值抛 ValueError。
    """
    if platform is None:
        return None
    p = str(platform).strip().lower()
    if p in ("", "all"):
        return None
    if p not in _VALID_PLATFORMS:
        raise ValueError(f"invalid platform: {platform!r}")
    return p


def _redact_email(email: str) -> str:
    """脱敏邮箱：前 3 后 4，本地名段够长则显示部分中间字符。"""
    s = str(email or "")
    if "@" not in s:
        return s
    local, _, domain = s.partition("@")
    if len(local) <= 4:
        masked = local[:1] + "***"
    else:
        masked = local[:3] + "***"
    return f"{masked}@{domain}"


def _extract_register_name(run: Run) -> str:
    """注册 ChatGPT 时实际填写的姓名（"About you" 表单），存在 config_snapshot.identity
    JSON 里而非独立 DB 列；历史 Run 无 identity 时返回空串。

    列表页（_account_to_dict）与导出（_serialize_full_json_entries）共用此逻辑，
    避免两处提取规则漂移。
    """
    snapshot = run.config_snapshot or {}
    identity = snapshot.get("identity") or {}
    if not isinstance(identity, dict):
        return ""
    register_name = str(identity.get("full_name") or "").strip()
    if not register_name:
        # full_name 缺失时用 first + last 兜底拼接
        fn = str(identity.get("first_name") or "").strip()
        ln = str(identity.get("last_name") or "").strip()
        register_name = (fn + " " + ln).strip()
    return register_name


def _account_to_dict(run: Run) -> dict[str, Any]:
    """脱敏序列化（不暴露 password / token）。"""
    register_name = _extract_register_name(run)
    return {
        "run_id": run.id,
        "email": run.email,
        "email_redacted": _redact_email(run.email),
        "register_name": register_name,
        "profile_id": run.profile_id,
        "browser_provider": run.browser_provider,
        "card_provider": run.card_provider,
        "mail_provider": run.mail_provider,
        "status": run.status,
        "phase": run.phase,
        "account_tier": run.account_tier,
        # 注册平台（openai=GPT / grok），供前端区分两类账号；legacy 行回退 openai
        "platform": run.platform or PLATFORM_OPENAI,
        "card_key": run.card_key,
        "card_bin": run.card_bin,
        "is_card_warmed_up": bool(run.is_card_warmed_up),
        "ip_address": run.ip_address or "",
        "ip_country": run.ip_country or "",
        # 运维自定义标签（账号池分类标记）；旧行 / 空值统一回退空列表，前端无需判 null
        "tags": list(run.tags or []),
        "error_reason": run.error_reason,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "updated_at": run.updated_at.isoformat() if run.updated_at else "",
    }


def list_pool(
    *,
    tier: str = TIER_REGISTERED,
    platform: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出指定段位的号。

    默认只展示 status=success 的号 —— 失败的号不算"账号"，不该入池。
    platform 可选过滤 openai / grok（None / "all" = 全平台，向后兼容）。
    """
    if tier not in _VALID_TIERS:
        raise ValueError(f"invalid tier: {tier}")
    platform_filter = _normalize_platform(platform)
    with get_session() as session:
        stmt = (
            select(Run)
            .where(Run.account_tier == tier)
            .where(Run.status == "success")
        )
        if platform_filter is not None:
            stmt = stmt.where(Run.platform == platform_filter)
        stmt = stmt.order_by(Run.created_at.desc()).limit(max(1, min(limit, 200)))  # type: ignore
        rows = list(session.exec(stmt).all())
        dicts = [_account_to_dict(r) for r in rows]

    # 合并每行的「最后一次 PIX 开通/核验」状态（列表状态列展示用）
    status_map = get_pix_status_map([d["run_id"] for d in dicts])
    for d in dicts:
        d["pix_status"] = status_map.get(d["run_id"])  # 无记录则为 None → 前端渲染「未开通」
    return dicts


def get_account_detail(run_id: str) -> Optional[dict[str, Any]]:
    """单号详情。返回 None 表示未找到。"""
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        d = _account_to_dict(run)
        # 详情多透出一个 access_token 是否可用的标志（不暴露 token 本身）
        token = _resolve_access_token(run)
        d["has_access_token"] = bool(token)
        return d


def promote(run_id: str, target_tier: str) -> dict[str, Any]:
    """绑卡成功后晋级到 plus / team。

    返回更新后的账号字典。Run 不存在或 tier 非法 → 抛 ValueError。
    """
    if target_tier not in _PROMOTABLE_TARGETS:
        raise ValueError(f"target_tier 必须是 {_PROMOTABLE_TARGETS} 之一，得到 {target_tier!r}")
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        old_tier = run.account_tier
        run.account_tier = target_tier
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        # 留痕
        session.add(RunEvent(
            run_id=run_id,
            event_type="account_promoted",
            state="payment",
            payload={
                "from_tier": old_tier,
                "to_tier": target_tier,
                "actor": "account_pool",
            },
        ))
        session.commit()
        session.refresh(run)
        logger.info("账号晋级 run_id=%s %s → %s", run_id[:12], old_tier, target_tier)
        return _account_to_dict(run)


def abandon(run_id: str, reason: str) -> dict[str, Any]:
    """绑卡失败 / 放弃这个号。"""
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        old_tier = run.account_tier
        run.account_tier = TIER_ABANDONED
        run.error_reason = (str(reason or "")[:500]) or "abandoned"
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        session.add(RunEvent(
            run_id=run_id,
            event_type="account_abandoned",
            state="payment",
            payload={
                "from_tier": old_tier,
                "reason": run.error_reason,
                "actor": "account_pool",
            },
        ))
        session.commit()
        session.refresh(run)
        logger.info("账号放弃 run_id=%s reason=%s", run_id[:12], run.error_reason)
        return _account_to_dict(run)


# ── 标签：运维自定义分类标记 ─────────────────────────────

# 单标签最长 24 字符、单号最多 20 个标签 —— 防止运维误粘大段文本撑爆 JSON 列。
_MAX_TAG_LEN = 24
_MAX_TAGS_PER_RUN = 20


def _normalize_tags(tags: list[str]) -> list[str]:
    """清洗标签列表：去首尾空白、去空串、去重保序、限长限量。

    去重大小写敏感（"UK" 与 "uk" 视为不同标签，由运维自行约束），
    仅按字符串完全相等去重。
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in tags or []:
        tag = str(raw or "").strip()[:_MAX_TAG_LEN]
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) >= _MAX_TAGS_PER_RUN:
            break
    return cleaned


def set_tags(run_id: str, tags: list[str]) -> dict[str, Any]:
    """全量覆盖某个号的标签列表（前端编辑器一次提交完整列表）。

    Run 不存在 → 抛 ValueError。返回更新后的账号字典（含 tags）。
    """
    cleaned = _normalize_tags(tags)
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        run.tags = cleaned
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        session.refresh(run)
        logger.info("账号标签更新 run_id=%s tags=%s", run_id[:12], cleaned)
        return _account_to_dict(run)


def list_all_tags() -> list[str]:
    """汇总账号池里出现过的全部标签（去重 + 按字典序），供前端快捷筛选 chip 渲染。

    只统计 status=success 的号（与 list_pool 口径一致），跨全部 tier / platform。
    """
    with get_session() as session:
        rows = session.exec(
            select(Run.tags).where(Run.status == "success")  # type: ignore
        ).all()
    bag: set[str] = set()
    for row in rows:
        for tag in (row or []):
            t = str(tag or "").strip()
            if t:
                bag.add(t)
    return sorted(bag)


# ── 一键绑卡：生成 checkout link（运维手动点击完成绑卡） ─────


def _resolve_access_token(run: Run) -> str:
    """从 Run.openai_tokens / Run.config_snapshot / accounts.csv 拿 access_token。

    优先级：
      1. Run.openai_tokens["access_token"]（新路径：worker 通过 update_current_task_tokens 写入）
      2. Run.config_snapshot["access_token"]（历史路径：早期 worker 写法）
      3. accounts.csv 按 email 反查（CLI 直跑 / main.py:export_success 写的旧路径）
      4. 都没有 → 返回空串
    """
    tokens = dict(run.openai_tokens or {})
    token = str(tokens.get("access_token") or "").strip()
    if token:
        return token

    snapshot = dict(run.config_snapshot or {})
    token = str(snapshot.get("access_token") or "").strip()
    if token:
        return token

    # 兜底：从 accounts.csv 反查
    csv_path = Path("accounts.csv")
    if not csv_path.exists():
        return ""
    target = (run.email or "").strip().lower()
    if not target:
        return ""
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                email = (row.get("email") or "").strip().lower()
                if email == target:
                    return str(row.get("access_token") or "").strip()
    except Exception as exc:
        logger.warning("读 accounts.csv 失败 (run=%s): %s", run.id[:12], exc)
    return ""


def get_access_token(run_id: str) -> dict[str, str]:
    """取单个号的明文 access_token（供 /accounts 行内"复制 token"按钮用）。

    复用 _resolve_access_token 的三级兜底（openai_tokens → config_snapshot →
    accounts.csv）。admin 操作：导出本就能拿全部 token，单个复制不增加权限面。

    Returns:
        {"email": ..., "access_token": ...}

    Raises:
        ValueError: run 不存在 / 该号无可用 access_token（需先刷新 token）。
    """
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError("账号不存在")
        token = _resolve_access_token(run)
        if not token:
            raise ValueError("该号 access_token 不可用，请先刷新 token 后重试")
        return {"email": run.email or "", "access_token": token}


_VALID_LINK_PLANS = ("team", "plus", "pro", "pro_lite")
_VALID_RETURN_MODES = ("long", "app")


def _humanize_link_error(raw: str) -> str:
    """把 PaymentLinkGenerator 返回的原始错误映射成 UI 可读的中文提示。

    client 层已对连接超时（curl(28) 等）做过中文化；这里再覆盖 401/403 等
    业务错误，让 /accounts 弹窗的 toast 不再直接甩英文 curl/HTTP 报文给运维。
    无法识别的错误原样返回（截断防过长），保留排障信息。
    """
    low = (raw or "").lower()
    if "代理" in raw or "连接超时" in raw or "connection timed out" in low or "curl: (28)" in low:
        # client 层已中文化（含 fail-fast 文案），原样透出
        return raw
    if "401" in low or "token 无效" in low or "unauthorized" in low:
        return "该号 access_token 已失效，请重新刷新 token 后再生成"
    if "403" in low or "forbidden" in low or "cloudflare" in low:
        return "请求被风控拦截（403），换个代理或稍后重试"
    if "未找到可用 checkout" in raw or "stripe" in low:
        return f"OpenAI/Stripe 未返回可用链接，可能是该计划/promo 配置问题：{raw[:200]}"
    return (raw or "生成 checkout 链接失败")[:300]


def generate_link(
    run_id: str,
    *,
    plan: str = "team",
    return_mode: str = "long",
    seat_quantity: int = 1,
    promo_code: str = "",
    promo_campaign_id: str = "",
    aimizy_country: str = "",
    aimizy_currency: str = "",
    workspace_name: str = "",
    proxy: Optional[str] = None,
    # P4 模板化扩展字段（2026-05-25）
    schema_version: Optional[str] = None,
    url_locale: Optional[str] = None,
    extra_payload: Optional[dict] = None,
    # P6 暴露 checkout_ui_mode（默认 hosted；custom 用于半价 promo 等场景）
    checkout_ui_mode: str = "hosted",
) -> dict[str, Any]:
    """生成 hosted checkout 链接（不再依赖卡池——拆分自旧 generate_bind_link）。

    Args:
        run_id: Run 主键（用于查 access_token）
        plan: team / plus / pro / pro_lite
        return_mode: long（Stripe hosted URL）/ app（chatgpt.com 站内 checkout）
        seat_quantity: Team 专用座位数；非 Team 忽略
        promo_code: URL 优惠码（例 datroaiuk），拼到 cancel_url
        promo_campaign_id: payload 级 promo（仅 Team 路径覆盖默认 team-1-month-free）
        aimizy_country / aimizy_currency: 留空则用 config 默认值
        workspace_name: Team workspace 名（留空走 PaymentLinkGenerator 默认 MyTeam）
        proxy: 可选代理（默认走 config.proxy）

    Returns:
        {"link": "https://...", "plan": str, "return_mode": str}

    异常：
        ValueError: run 不存在 / 段位不对 / access_token 缺失 / 生成失败
    """
    plan_normalized = (plan or "").strip().lower()
    if plan_normalized not in _VALID_LINK_PLANS:
        raise ValueError(f"plan 必须是 {_VALID_LINK_PLANS} 之一，得到 {plan!r}")
    mode = (return_mode or "long").strip().lower()
    if mode not in _VALID_RETURN_MODES:
        raise ValueError(f"return_mode 必须是 {_VALID_RETURN_MODES} 之一，得到 {return_mode!r}")

    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        if run.account_tier != TIER_REGISTERED:
            raise ValueError(
                f"该号当前段位是 {run.account_tier!r}，"
                f"只有 registered 段位才能生成 checkout 链接（避免重复绑卡）"
            )
        access_token = _resolve_access_token(run)
        if not access_token:
            raise ValueError(
                "找不到该号的 access_token（既不在 Run.config_snapshot 也不在 accounts.csv）"
            )

    # 走 PaymentLinkGenerator（不放在 session 块里，避免长事务持有）
    from src.payment_link import PaymentLinkGenerator
    from src.config import load_config

    config = load_config()
    use_proxy = proxy if proxy is not None else (config.proxy or None)
    country = aimizy_country or config.aimizy_country
    currency = aimizy_currency or config.aimizy_currency

    gen_kwargs: dict[str, Any] = {
        "plan_type": plan_normalized,
        "proxy": use_proxy,
        "return_mode": mode,
        "aimizy_country": country,
        "aimizy_currency": currency,
        "seat_quantity": int(seat_quantity or 1),
    }
    if workspace_name:
        gen_kwargs["workspace_name"] = workspace_name
    if promo_code:
        gen_kwargs["promo_code"] = promo_code
    if promo_campaign_id:
        gen_kwargs["promo_campaign_id"] = promo_campaign_id
    # P4 模板化扩展字段透传（None / 空值时 PaymentLinkGenerator 走全局默认）
    if schema_version:
        gen_kwargs["schema_version"] = schema_version
    if url_locale:
        gen_kwargs["url_locale"] = url_locale
    if extra_payload and isinstance(extra_payload, dict):
        gen_kwargs["extra_payload"] = extra_payload
    # P6 checkout_ui_mode 透传（仅 plus 真生效；team/pro 接收但忽略）
    if checkout_ui_mode and checkout_ui_mode.lower() in ("hosted", "custom"):
        gen_kwargs["checkout_ui_mode"] = checkout_ui_mode.lower()

    started_at = datetime.now(timezone.utc)
    success, link = PaymentLinkGenerator.generate_checkout_link(access_token, **gen_kwargs)
    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

    if not success:
        # link 此时是错误原因（client 层已对连接超时做了中文友好化，
        # 这里再补 401/403 等业务错误的友好映射，让 UI toast 即可读懂）。
        friendly = _humanize_link_error(str(link))
        # 失败也留痕：/accounts 历史可查每次生成结果（成败/原因/耗时）
        with get_session() as session:
            session.add(RunEvent(
                run_id=run_id,
                event_type="link_generate_failed",
                state="payment",
                payload={
                    "plan": plan_normalized,
                    "return_mode": mode,
                    "reason": friendly,
                    "raw_error": str(link)[:500],
                    "elapsed_ms": elapsed_ms,
                },
            ))
            session.commit()
        raise ValueError(friendly)

    # 留痕：链接已生成，但还没绑卡（绑卡是独立的 assign_card 步骤）
    with get_session() as session:
        session.add(RunEvent(
            run_id=run_id,
            event_type="link_generated",
            state="payment",
            payload={
                "plan": plan_normalized,
                "return_mode": mode,
                "has_promo_code": bool(promo_code),
                "has_promo_campaign": bool(promo_campaign_id),
                "elapsed_ms": elapsed_ms,
            },
        ))
        session.commit()

    return {
        "link": link,
        "plan": plan_normalized,
        "return_mode": mode,
    }


def pix_plus_activate(run_id: str, sdk_code: str) -> dict[str, Any]:
    """PIX 渠道（baxigpt.com 卡密）开通 Plus。

    起账号自己的 AdsPower 浏览器 → 打开 baxigpt.com → 填卡密 → 验证 →
    填该号 access token → 点开通 Plus。**同步阻塞**跑完整个流程再返回。

    设计立场（与 generate_link 一致）：
      - 仅 registered 段位的号可开通（避免重复绑/开）
      - token 从 DB 自动取（_resolve_access_token 三级兜底）
      - **不晋级**：baxigpt 异步处理（1-10 分钟）+ OpenAI 同步（1-5 分钟），
        提交成功只代表「已下单」，晋级由运维确认 Plus 生效后手动点
      - 成功/失败都写 RunEvent 留痕（仅留卡密前缀，不留全卡密）

    Args:
        run_id: Run 主键
        sdk_code: 卡密（SDK），形如 BX-XXXXXXXX

    Returns:
        {"success": bool, "message": str, "detail": str}

    Raises:
        ValueError: run 不存在 / 段位不对 / token 缺失 / profile 缺失 / 卡密空
    """
    sdk = (sdk_code or "").strip()
    if not sdk:
        raise ValueError("卡密不能为空")

    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        if run.account_tier != TIER_REGISTERED:
            raise ValueError(
                f"该号当前段位是 {run.account_tier!r}，"
                f"只有 registered 段位才能开通 Plus（避免重复开）"
            )
        profile_id = (run.profile_id or "").strip()
        access_token = _resolve_access_token(run)

    if not access_token:
        raise ValueError(
            "找不到该号的 access_token（既不在 Run.openai_tokens / config_snapshot 也不在 accounts.csv）"
        )
    if not profile_id:
        raise ValueError("该号缺少 AdsPower profile_id，无法起浏览器开通 Plus")

    # 起浏览器跑 baxigpt 流程（不放在 session 块里，避免长事务持有）
    from src.config import load_config
    from src.orchestration.pix_plus import execute_pix_plus_activation
    from playwright.sync_api import sync_playwright

    config = load_config()
    with sync_playwright() as p:
        result = execute_pix_plus_activation(
            config,
            profile_id,
            sdk,
            access_token,
            proxy_url=config.proxy or "",
            playwright=p,
        )

    # 留痕（不晋级）：成功/失败各写一条对应 event
    success = bool(result.get("success"))
    with get_session() as session:
        session.add(RunEvent(
            run_id=run_id,
            event_type="pix_plus_submitted" if success else "pix_plus_failed",
            state="payment",
            payload={
                "channel": "baxigpt",
                "sdk_prefix": sdk[:5],  # 仅留前缀，不留全卡密
                "success": success,
                "message": str(result.get("message") or "")[:200],
            },
        ))
        session.commit()
    logger.info(
        "PIX 开通 Plus run=%s success=%s msg=%s",
        run_id[:12], success, str(result.get("message") or "")[:60],
    )
    return result


def _build_mail_api_for_run(run_id: str):
    """按 run 构造能收**这个号自己邮箱**验证码的 MailManager（失败降级 None）。

    刷新 token / 核验 Plus 未登录时走 magic link 自动登录，必须用该号注册时绑定的
    邮箱凭据收信。复用注册流的 ``_resolve_runtime_config``（内部已按 mail_account_id
    注入该号 client_id/refresh_token）+ ``_build_runtime_clients``，避免双处实现漂移。

    Returns:
        MailManager 实例；构造失败（缺 mail provider 配置 / 凭据异常等）则返回 None，
        此时未登录会如实返回「缺邮箱凭据无法自动登录」。
    """
    try:
        from src.api.worker import _resolve_runtime_config
        from main import _build_runtime_clients
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            runtime_config = _resolve_runtime_config(run)
        _, _, mail_api = _build_runtime_clients(runtime_config)
        logger.info("号池操作：已为 run=%s 构造该号 mail_api", run_id[:12])
        return mail_api
    except Exception as exc:
        logger.warning("号池操作：构造 mail_api 失败（未登录将无法自动登录）run=%s: %s", run_id[:12], exc)
        return None


def verify_plus(run_id: str) -> dict[str, Any]:
    """核验账号当前订阅状态（起浏览器读 chatgpt.com session 拿实时 plan）。

    未登录则用该号邮箱凭据自动 magic link 登录后再核验（缺凭据/登录失败如实报错）。
    PIX 开通后 baxigpt 异步 + OpenAI 同步要几分钟，「开通已提交」不代表生效。
    本函数起账号自己的 AdsPower 浏览器，读 session 接口拿**实时** plan，并把
    刷新到的新 token 存回 Run.openai_tokens（解决 DB 旧 token 永远显示 free）。

    Args:
        run_id: Run 主键

    Returns:
        {"plan", "is_plus", "logged_in", "message", "detail", "token_refreshed"}
        （不回传 fresh_token 本体，避免 token 进 API 响应体）

    Raises:
        ValueError: run 不存在 / profile 缺失
    """
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        profile_id = (run.profile_id or "").strip()
        email = (run.email or "").strip()
        password = run.password or ""
    if not profile_id:
        raise ValueError("该号缺少 AdsPower profile_id，无法起浏览器核验")

    from src.config import load_config
    from src.orchestration.verify_plus import execute_plus_verification
    from playwright.sync_api import sync_playwright

    config = load_config()

    # 构造该号的 mail_api（未登录时 magic link 自动登录拉验证码用）。
    # 与 refresh_token() 同一套构造，确保 mail_api 带**这个号**的收信凭据；
    # 失败则降级 None（此时未登录会如实返回「缺邮箱凭据无法自动登录」）。
    mail_api = _build_mail_api_for_run(run_id)

    with sync_playwright() as p:
        result = execute_plus_verification(
            config, profile_id,
            email=email, password=password, mail_api=mail_api,
            playwright=p,
        )

    # OpenAI 终态封禁（account_deactivated）：自动归档，与 refresh_token 同处理
    if result.get("account_deactivated"):
        logger.warning("核验 Plus 探测到停用号，自动归档 run=%s", run_id[:12])
        try:
            abandon(run_id, "account_deactivated")
        except Exception as exc:
            logger.error("停用号自动归档失败 run=%s: %s", run_id[:12], exc)
        return {
            "plan": result.get("plan") or "",
            "is_plus": False,
            "logged_in": False,
            "relogged_in": False,
            "message": str(result.get("message") or "该号已被 OpenAI 停用，已自动归档"),
            "detail": "account_deactivated",
            "token_refreshed": False,
            "account_deactivated": True,
        }

    # 把刷新到的新 token 存回 DB（保留旧字段，只覆盖 access_token + extracted_at）
    token_refreshed = False
    fresh_token = str(result.get("fresh_token") or "").strip()
    if fresh_token:
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                tokens = dict(run.openai_tokens or {})
                tokens["access_token"] = fresh_token
                tokens["extracted_at"] = datetime.now(timezone.utc).isoformat()
                run.openai_tokens = tokens
                run.updated_at = datetime.now(timezone.utc)
                session.add(run)
                session.commit()
                token_refreshed = True

    # 留痕
    with get_session() as session:
        session.add(RunEvent(
            run_id=run_id,
            event_type="plus_verified",
            state="payment",
            payload={
                "plan": str(result.get("plan") or ""),
                "is_plus": bool(result.get("is_plus")),
                "logged_in": bool(result.get("logged_in")),
                "relogged_in": bool(result.get("relogged_in")),
                "token_refreshed": token_refreshed,
            },
        ))
        session.commit()

    return {
        "plan": result.get("plan") or "",
        "is_plus": bool(result.get("is_plus")),
        "logged_in": bool(result.get("logged_in")),
        "relogged_in": bool(result.get("relogged_in")),
        "message": str(result.get("message") or ""),
        "detail": str(result.get("detail") or ""),
        "token_refreshed": token_refreshed,
        "account_deactivated": False,
    }


def refresh_token(run_id: str) -> dict[str, Any]:
    """刷新该号 access_token（token 过期时重新登录更新）。

    起账号自己的 AdsPower 浏览器：已登录直接读 session 拿新 token；
    **未登录则现场登录**（走 magic link，需该号邮箱凭据）后再读。
    新 token 存回 Run.openai_tokens（号池导出 / 生成链接 / 核验都依赖它）。

    Args:
        run_id: Run 主键

    Returns:
        {"plan", "is_plus", "logged_in", "relogged_in", "token_refreshed", "message", "detail"}

    Raises:
        ValueError: run 不存在 / profile 缺失
    """
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        profile_id = (run.profile_id or "").strip()
        email = (run.email or "").strip()
        password = run.password or ""
    if not profile_id:
        raise ValueError("该号缺少 AdsPower profile_id，无法起浏览器刷新 token")

    from src.config import load_config
    from src.orchestration.verify_plus import execute_token_refresh
    from playwright.sync_api import sync_playwright

    config = load_config()

    # 构造该号的 mail_api（未登录时 magic link 拉验证码用）。
    mail_api = _build_mail_api_for_run(run_id)

    with sync_playwright() as p:
        result = execute_token_refresh(
            config, profile_id, email, password, mail_api=mail_api, playwright=p,
        )

    # OpenAI 终态封禁（account_deactivated）：自动归档号，不再占用可用池。
    # 探测在浏览器侧已短路掉 magic link，这里只负责落库标记 + 返回提示运维。
    if result.get("account_deactivated"):
        logger.warning("刷新 token 探测到停用号，自动归档 run=%s", run_id[:12])
        try:
            abandon(run_id, "account_deactivated")
        except Exception as exc:  # 归档失败不应吞掉「号已停用」这个关键结论
            logger.error("停用号自动归档失败 run=%s: %s", run_id[:12], exc)
        return {
            "plan": result.get("plan") or "",
            "is_plus": False,
            "logged_in": False,
            "relogged_in": False,
            "token_refreshed": False,
            "account_deactivated": True,
            "message": str(result.get("message") or "该号已被 OpenAI 停用，已自动归档"),
            "detail": "account_deactivated",
        }

    # 存回新 token
    token_refreshed = False
    fresh_token = str(result.get("fresh_token") or "").strip()
    if fresh_token:
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                tokens = dict(run.openai_tokens or {})
                tokens["access_token"] = fresh_token
                tokens["extracted_at"] = datetime.now(timezone.utc).isoformat()
                run.openai_tokens = tokens
                run.updated_at = datetime.now(timezone.utc)
                session.add(run)
                session.commit()
                token_refreshed = True

    with get_session() as session:
        session.add(RunEvent(
            run_id=run_id,
            event_type="token_refreshed",
            state="payment",
            payload={
                "token_refreshed": token_refreshed,
                "relogged_in": bool(result.get("relogged_in")),
                "logged_in": bool(result.get("logged_in")),
                "plan": str(result.get("plan") or ""),
            },
        ))
        session.commit()

    return {
        "plan": result.get("plan") or "",
        "is_plus": bool(result.get("is_plus")),
        "logged_in": bool(result.get("logged_in")),
        "relogged_in": bool(result.get("relogged_in")),
        "token_refreshed": token_refreshed,
        "account_deactivated": False,
        "message": str(result.get("message") or ""),
        "detail": str(result.get("detail") or ""),
    }


# pix_plus / plus_verified 事件类型常量（列表状态读取用）
_PIX_PLUS_EVENT_TYPES = ("pix_plus_submitted", "pix_plus_failed", "plus_verified")


def get_pix_status_map(run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """批量取一组 run 的「最后一次 PIX 开通/核验」状态（列表展示用）。

    Returns:
        {run_id: {"kind", "is_plus", "plan", "at", "message"}}
        kind: "submitted" | "failed" | "verified_plus" | "verified_free" | ""（无记录）
        无记录的 run 不出现在返回 dict 里（前端按缺失渲染「未开通」）。
    """
    ids = [str(r).strip() for r in (run_ids or []) if str(r).strip()]
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with get_session() as session:
        stmt = (
            select(RunEvent)
            .where(RunEvent.run_id.in_(ids))  # type: ignore
            .where(RunEvent.event_type.in_(_PIX_PLUS_EVENT_TYPES))  # type: ignore
            .order_by(RunEvent.timestamp.asc())  # type: ignore
        )
        rows = list(session.exec(stmt).all())
    # 按 run_id 取时间最新的一条（asc 排序后后写覆盖前写）
    for ev in rows:
        payload = ev.payload or {}
        if ev.event_type == "plus_verified":
            is_plus = bool(payload.get("is_plus"))
            kind = "verified_plus" if is_plus else "verified_free"
        elif ev.event_type == "pix_plus_submitted":
            kind = "submitted"
            is_plus = False
        else:  # pix_plus_failed
            kind = "failed"
            is_plus = False
        out[ev.run_id] = {
            "kind": kind,
            "is_plus": is_plus,
            "plan": str(payload.get("plan") or ""),
            "at": ev.timestamp.isoformat() if ev.timestamp else "",
            "message": str(payload.get("message") or ""),
        }
    return out


def generate_link_standalone(
    access_token: str,
    *,
    plan: str = "team",
    return_mode: str = "long",
    seat_quantity: int = 1,
    promo_code: str = "",
    promo_campaign_id: str = "",
    aimizy_country: str = "",
    aimizy_currency: str = "",
    workspace_name: str = "",
    proxy: Optional[str] = None,
    schema_version: Optional[str] = None,
    url_locale: Optional[str] = None,
    extra_payload: Optional[dict] = None,
    checkout_ui_mode: str = "hosted",
) -> dict[str, Any]:
    """脱离 Run 表直接生成 checkout 链接 —— 用户在独立页面手动喂 access_token。

    与 ``generate_link(run_id, ...)`` 的差异：
      - 不查 Run 表、不写 RunEvent（独立页场景，无 run_id 上下文）
      - access_token 由调用方（API 层）从前端拿来
      - 其余字段与 generate_link 同语义，复用同一份校验 + PaymentLinkGenerator 路径

    设计立场：本函数**有意做薄** —— 只做参数校验与 PaymentLinkGenerator 调用，
    不接管 audit / 留痕（那是 API 路由层的事）。

    Args:
        access_token: ChatGPT API access token（不能为空）
        其余: 同 ``generate_link()``，详见上方注释

    Returns:
        ``{"link": "https://...", "plan": str, "return_mode": str}``

    Raises:
        ValueError: access_token 空 / plan 非法 / return_mode 非法 / 生成失败
    """
    token = (access_token or "").strip()
    if not token:
        raise ValueError("access_token 不能为空")

    plan_normalized = (plan or "").strip().lower()
    if plan_normalized not in _VALID_LINK_PLANS:
        raise ValueError(f"plan 必须是 {_VALID_LINK_PLANS} 之一，得到 {plan!r}")
    mode = (return_mode or "long").strip().lower()
    if mode not in _VALID_RETURN_MODES:
        raise ValueError(f"return_mode 必须是 {_VALID_RETURN_MODES} 之一，得到 {return_mode!r}")

    from src.config import load_config
    from src.payment_link import PaymentLinkGenerator

    config = load_config()
    use_proxy = proxy if proxy is not None else (config.proxy or None)
    country = aimizy_country or config.aimizy_country
    currency = aimizy_currency or config.aimizy_currency

    gen_kwargs: dict[str, Any] = {
        "plan_type": plan_normalized,
        "proxy": use_proxy,
        "return_mode": mode,
        "aimizy_country": country,
        "aimizy_currency": currency,
        "seat_quantity": int(seat_quantity or 1),
    }
    if workspace_name:
        gen_kwargs["workspace_name"] = workspace_name
    if promo_code:
        gen_kwargs["promo_code"] = promo_code
    if promo_campaign_id:
        gen_kwargs["promo_campaign_id"] = promo_campaign_id
    if schema_version:
        gen_kwargs["schema_version"] = schema_version
    if url_locale:
        gen_kwargs["url_locale"] = url_locale
    if extra_payload and isinstance(extra_payload, dict):
        gen_kwargs["extra_payload"] = extra_payload
    if checkout_ui_mode and checkout_ui_mode.lower() in ("hosted", "custom"):
        gen_kwargs["checkout_ui_mode"] = checkout_ui_mode.lower()

    success, link = PaymentLinkGenerator.generate_checkout_link(token, **gen_kwargs)
    if not success:
        raise ValueError(f"生成 checkout 链接失败: {link}")

    return {
        "link": link,
        "plan": plan_normalized,
        "return_mode": mode,
    }


def assign_card(run_id: str, card_key: str, *, note: str = "") -> dict[str, Any]:
    """登记"这个号准备用这张卡绑卡"（仅留痕，不调 ChatGPT API）。

    用于号池"💳 绑卡"按钮：运维选完卡 → 服务端校验卡存在/未作废 →
    写一条 RunEvent 备查。卡片本身不参与 checkout 链接生成。

    异常：ValueError = run 不存在 / 卡不存在 / 卡已作废
    """
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run_id 不存在: {run_id}")
        card = session.get(CardActivation, card_key)
        if card is None:
            raise ValueError(f"卡密不在卡池: {card_key[:8]}")
        if card.is_invalidated:
            raise ValueError(f"卡密已作废: {card_key[:8]} reason={card.invalidate_reason}")

        session.add(RunEvent(
            run_id=run_id,
            event_type="card_assigned",
            state="payment",
            payload={
                "card_key_prefix": card_key[:8],
                "card_last4": (card.card_number or "")[-4:],
                "note": (note or "")[:200],
            },
        ))
        session.commit()
        logger.info("号 %s 绑定卡 %s（仅留痕）", run_id[:12], card_key[:8])
        return {
            "run_id": run_id,
            "card_key": card_key,
            "card_last4": (card.card_number or "")[-4:],
        }


def generate_bind_link(
    run_id: str,
    *,
    card_key: str,
    plan: str = "team",
    proxy: Optional[str] = None,
) -> dict[str, Any]:
    """[Deprecated] 旧的"选卡 + 生成链接"一体化接口。

    保留兼容外部调用方。内部委托：先 assign_card 留痕，再 generate_link。
    新代码请直接用 generate_link（无卡）+ assign_card（独立留痕）。
    """
    plan_normalized = (plan or "").strip().lower()
    if plan_normalized not in ("team", "plus"):
        raise ValueError(f"plan 必须是 'team' 或 'plus'，得到 {plan!r}")

    # 校验顺序保持与旧行为一致：先 run/段位/token，再卡。
    # 否则"无 token 的号 + 不存在的卡"会抛卡错而非 token 错。
    from src.config import load_config
    config = load_config()

    link_result = generate_link(
        run_id,
        plan=plan_normalized,
        return_mode=config.payment_link_return_mode,
        proxy=proxy,
    )
    # 此时 run/段位/token 已通过；现在做卡校验 + 留痕
    assign_result = assign_card(run_id, card_key)

    # 兼容老监听方：deprecated 端点额外补写 bind_link_generated 事件
    # （新代码请用 link_generated + card_assigned）
    with get_session() as session:
        session.add(RunEvent(
            run_id=run_id,
            event_type="bind_link_generated",
            state="payment",
            payload={
                "plan": plan_normalized,
                "card_key_prefix": card_key[:8],
                "card_last4": assign_result["card_last4"],
                "deprecated_api": True,
            },
        ))
        session.commit()

    return {
        "link": link_result["link"],
        "plan": plan_normalized,
        "card_key": card_key,
        "card_last4": assign_result["card_last4"],
    }


# ── 导入 / 导出 ───────────────────────────────────────────

# 格式标识
FMT_CREDENTIALS_CSV = "credentials_csv"  # 历史 CSV 格式，仅保留导入端兼容老文件
FMT_CPA_JSON = "cpa_json"                # Codex CLI 兼容 JSON
FMT_FULL_JSON = "full_json"              # 账号全信息 JSON（账号凭证 + 令牌组）

# 导出菜单只暴露 2 个 JSON 格式（CSV 字段表达力不够嵌套结构）
_VALID_EXPORT_FORMATS = (FMT_FULL_JSON, FMT_CPA_JSON)
# 导入仍接受 CSV（向后兼容外部脚本写的历史 CSV 文件）
_VALID_IMPORT_FORMATS = (FMT_FULL_JSON, FMT_CPA_JSON, FMT_CREDENTIALS_CSV)

# 凭据 CSV 表头（仅 import 路径用；导出已删除 CSV）
_CREDENTIALS_FIELDS = (
    "email",
    "password",
    "mail_provider",
    "client_id",
    "refresh_token",
    "account_tier",
    "created_at",
)


def _load_mail_accounts_by_email(session, emails: set[str]) -> dict[str, MailAccount]:
    """按 email 批量取 MailAccount，用于 JOIN 解密 OAuth 凭据。"""
    if not emails:
        return {}
    rows = list(session.exec(select(MailAccount).where(MailAccount.email.in_(emails))).all())  # type: ignore
    return {(m.email or "").lower(): m for m in rows if m.email}


def export_pool(
    tier: str,
    fmt: str,
    *,
    run_ids: Optional[list[str]] = None,
    platform: Optional[str] = None,
) -> tuple[str, str, str, list[dict[str, str]]]:
    """导出指定段位的号池。

    Args:
        tier: registered / plus / team / abandoned
        fmt: credentials_csv（账号+OAuth） / cpa_json（CPA token 格式）
        run_ids: 可选，仅导出这些 run_id 的号（用于"导出选中"功能）。
                 传空列表 [] 视为"无匹配"，返回空内容；None 表示不过滤。
        platform: 可选过滤 openai / grok（None / "all" = 全平台）。
                  与 run_ids 取交集：选中导出也只导出匹配平台的行。

    Returns:
        (content, content_type, filename, skipped)
        - content: 单账号是 str（JSON 文本），多账号是 bytes（zip 字节流）
        - content_type: 单账号 application/json；多账号 application/zip
        - filename: 单账号 codex-{email}-{plan}.json；多账号 codex-{tier}-{count}-{ts}.zip
        - skipped: 被跳过的 Run 列表，每条 {"run_id", "email", "reason"}；
                   cpa_json 时可能非空（JWT 解析失败的账号会被跳过）；
                   full_json 时始终为空（解析失败时字段留空但仍导出）

    异常：ValueError 表示 tier 或 fmt 非法。
    """
    if tier not in _VALID_TIERS:
        raise ValueError(f"invalid tier: {tier}")
    if fmt not in _VALID_EXPORT_FORMATS:
        raise ValueError(f"invalid fmt: {fmt}")
    platform_filter = _normalize_platform(platform)

    # 规整 run_ids：去重 + 去空，全空视为 [] 而非 None
    ids_filter: Optional[list[str]] = None
    if run_ids is not None:
        ids_filter = [str(r).strip() for r in run_ids if str(r).strip()]

    with get_session() as session:
        stmt = (
            select(Run)
            .where(Run.account_tier == tier)
            .where(Run.status == "success")
        )
        if platform_filter is not None:
            stmt = stmt.where(Run.platform == platform_filter)
        stmt = stmt.order_by(Run.created_at.desc())  # type: ignore
        if ids_filter is not None:
            if not ids_filter:
                # 显式传了空列表 → 直接返回空导出（避免误导出整个 tier）
                runs: list[Run] = []
            else:
                stmt = stmt.where(Run.id.in_(ids_filter))  # type: ignore
                runs = list(session.exec(stmt).all())
        else:
            runs = list(session.exec(stmt).all())

        # 批量加载 MailAccount（full_json 需要带 OAuth 凭据），按 email 做 JOIN
        emails_lower = {(r.email or "").lower() for r in runs if r.email}
        mail_map = _load_mail_accounts_by_email(session, emails_lower) if fmt == FMT_FULL_JSON else {}

        # ── 序列化（按 fmt 走对应的 serializer，得到统一的 entries 形状）──
        skipped: list[dict[str, str]] = []
        if fmt == FMT_FULL_JSON:
            entries = _serialize_full_json_entries(runs, mail_map)
        else:  # FMT_CPA_JSON
            entries, skipped = _serialize_cpa_json_entries(runs)

        # ── 单账号 vs 多账号分支 ──
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if len(entries) == 0:
            # 空导出：返回空数组 + 兜底文件名
            return (
                "[]",
                "application/json; charset=utf-8",
                f"codex-empty-{tier}-{ts}.json",
                skipped,
            )

        if len(entries) == 1:
            # 单账号：直接返回账号 object，文件名带 email + plan
            entry = entries[0]
            filename = _build_codex_filename(entry["_filename_email"], entry["_filename_plan"])
            content_str = json.dumps(_strip_filename_meta(entry), ensure_ascii=False, indent=2)
            return (
                content_str,
                "application/json; charset=utf-8",
                filename,
                skipped,
            )

        # 多账号：打包成 zip，每条一个独立文件
        zip_bytes = _pack_zip(entries)
        filename = f"codex-{tier}-{len(entries)}-{ts}.zip"
        return (
            zip_bytes,
            "application/zip",
            filename,
            skipped,
        )


def _export_credentials_csv(runs: list[Run], mail_map: dict[str, MailAccount]) -> str:
    """格式 1：账号+密码+OAuth 凭据（CSV）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_CREDENTIALS_FIELDS))
    writer.writeheader()
    for r in runs:
        mail = mail_map.get((r.email or "").lower())
        writer.writerow({
            "email": r.email or "",
            "password": r.password or "",
            "mail_provider": r.mail_provider or "",
            "client_id": (mail.client_id if mail else "") or "",
            "refresh_token": (mail.refresh_token if mail else "") or "",
            "account_tier": r.account_tier or "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })
    return buf.getvalue()


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """解 JWT 第 2 段 base64 payload；失败抛 ValueError 让调用方决定怎么处理。

    JWT 格式: header.payload.signature
    payload 是 base64url 编码的 JSON。注意 base64url 用 -/_ 替代 +/，且不要尾部填充。
    """
    if not token or "." not in token:
        raise ValueError("JWT 格式异常：缺少 '.'")
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("JWT 必须至少 2 段（header.payload）")
    payload_b64 = parts[1]
    # 补 base64 填充（base64url 不带 =，长度需是 4 的倍数）
    padding = -len(payload_b64) % 4
    payload_b64 += "=" * padding
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
        return json.loads(raw)
    except Exception as exc:
        raise ValueError(f"JWT payload 解码失败：{exc}")


def _extract_account_id_and_exp(access_token: str) -> tuple[str, str]:
    """从 access_token JWT 解出 (chatgpt_account_id, expired ISO 字符串)。

    expired 用 UTC+8（北京时间）格式化为 ISO 8601 字符串，例：
        "2026-05-27T14:01:26+08:00"
    这与 Codex CLI 的样本格式一致；JWT exp 字段是 Unix 时间戳。

    Raises:
        ValueError: token 解不出或缺关键字段
    """
    data = _decode_jwt_payload(access_token)

    # chatgpt_account_id 在 ChatGPT 加的 namespaced claim 里
    auth_claims = data.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()
    if not account_id:
        raise ValueError("JWT 里缺 chatgpt_account_id")

    exp_unix = data.get("exp")
    if not isinstance(exp_unix, (int, float)):
        raise ValueError("JWT 里缺合法的 exp 字段")

    # +08:00 北京时间，与 Codex CLI 样本一致
    beijing_tz = timezone(__import__("datetime").timedelta(hours=8))
    expired_iso = datetime.fromtimestamp(exp_unix, tz=beijing_tz).isoformat()

    return account_id, expired_iso


def _extract_plan_type(access_token: str) -> str:
    """从 access_token JWT 解出 chatgpt_plan_type（free / plus / team / pro 等）。

    与 _extract_account_id_and_exp 不同，这里**容错**：任何错误返回空字符串，
    给文件名 / 元数据用，不阻断主流程。

    Returns:
        plan_type 字符串（小写），解析失败时返回 ""
    """
    if not access_token:
        return ""
    try:
        data = _decode_jwt_payload(access_token)
    except ValueError:
        return ""
    auth_claims = data.get("https://api.openai.com/auth") or {}
    plan = str(auth_claims.get("chatgpt_plan_type") or "").strip().lower()
    return plan


# ── 文件名安全化 ──────────────────────────────────────────
# email 可能含 / : \ 等非法字符，文件名需清洗
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._@\-]")


def _safe_for_filename(s: str, *, fallback: str = "unknown") -> str:
    """把任意字符串转成文件名安全字符；@ 和 - 和 . 保留（email 友好）"""
    cleaned = _SAFE_FILENAME_RE.sub("_", (s or "").strip())
    return cleaned or fallback


def _build_codex_filename(email: str, plan_type: str) -> str:
    """codex-{email}-{plan}.json；plan 为空时省略，email 缺失用 unknown"""
    safe_email = _safe_for_filename(email, fallback="unknown")
    if plan_type:
        return f"codex-{safe_email}-{plan_type}.json"
    return f"codex-{safe_email}.json"


def _serialize_cpa_json_entries(runs: list[Run]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """生成 cpa_json 每条账号的 dict（Codex CLI ~/.codex/auth.json 兼容格式）。

    每条结构：
        {
          "_filename_email": "..."        # 内部用：build_filename 取
          "_filename_plan": "free|plus|.."  # 内部用
          "access_token": "ey...",
          "account_id": "<uuid>",         # JWT 解出
          "disabled": false,              # 固定 false
          "email": "...",
          "expired": "2026-05-27T14:01:26+08:00",  # JWT exp 解，UTC+8
          "id_token": "...",
          "last_refresh": "...",
          "refresh_token": "...",
          "type": "codex"                 # 固定
        }

    设计：JWT 解析失败的 Run **跳过不导出**。

    Returns:
        (entries, skipped) —— entries 是要导出的 dict 列表（含内部 _filename_* 字段）
    """
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for r in runs:
        tokens = dict(r.openai_tokens or {})
        access_token = str(tokens.get("access_token") or "").strip()
        if not access_token:
            skipped.append({
                "run_id": r.id or "",
                "email": r.email or "",
                "reason": "access_token 为空（worker 未持久化 token，或老数据迁移前）",
            })
            continue
        try:
            account_id, expired_iso = _extract_account_id_and_exp(access_token)
        except ValueError as exc:
            skipped.append({
                "run_id": r.id or "",
                "email": r.email or "",
                "reason": f"JWT 解析失败: {exc}",
            })
            continue

        entries.append({
            "_filename_email": r.email or "",
            "_filename_plan": _extract_plan_type(access_token),
            "access_token": access_token,
            "account_id": account_id,
            "disabled": False,
            "email": r.email or "",
            "expired": expired_iso,
            "id_token": tokens.get("id_token") or "",
            "last_refresh": tokens.get("extracted_at") or "",
            "refresh_token": tokens.get("refresh_token") or "",
            "type": "codex",
        })
    return entries, skipped


def _serialize_full_json_entries(
    runs: list[Run], mail_map: dict[str, "MailAccount"],
) -> list[dict[str, Any]]:
    """生成 full_json 每条账号的 dict（账号凭证组 + 令牌组 + 运维元数据组）。

    每条结构：
        {
          "_filename_email": "..."        # 内部用
          "_filename_plan": "free|plus|.."  # 内部用
          # 账号凭证组
          "email": "...",
          "password": "...",
          "mail_provider": "applemail",
          "client_id": "...",            # MailAccount OAuth client
          "refresh_token": "...",        # MailAccount OAuth refresh（取邮件用）
          # 令牌组
          "access_token": "...",
          "openai_refresh_token": "...", # 区别于 OAuth refresh
          "id_token": "...",
          "account_id": "...",           # JWT 解出（失败留空）
          "expired": "...",              # JWT 解出（失败留空）
          "last_refresh": "...",
          # 运维元数据组（与列表页 _account_to_dict 对齐，导出不再丢失这些字段）
          "run_id": "...",               # Run 主键
          "platform": "openai|grok",     # 注册平台
          "account_tier": "registered|plus|team|abandoned",
          "register_name": "...",        # 注册时实际填写的姓名（About you 表单）
          "profile_id": "...",           # AdsPower profile
          "browser_provider": "...",
          "card_provider": "...",        # 绑卡用的卡商
          "ip_address": "...",           # 创建时出口 IP
          "ip_country": "...",           # 出口 IP 国家码
          "created_at": "..."            # ISO8601
        }

    设计：JWT 解析失败也照常导出（账号 + 令牌仍有价值，只是 account_id / expired 留空）。
    """
    entries: list[dict[str, Any]] = []
    for r in runs:
        tokens = dict(r.openai_tokens or {})
        access_token = str(tokens.get("access_token") or "").strip()

        account_id = ""
        expired_iso = ""
        if access_token:
            try:
                account_id, expired_iso = _extract_account_id_and_exp(access_token)
            except ValueError:
                pass

        mail = mail_map.get((r.email or "").lower())
        # 注册姓名取自 config_snapshot.identity（与列表页 _account_to_dict 同一来源）
        register_name = _extract_register_name(r)
        entries.append({
            "_filename_email": r.email or "",
            "_filename_plan": _extract_plan_type(access_token),
            # 账号凭证组
            "email": r.email or "",
            "password": r.password or "",
            "mail_provider": r.mail_provider or "",
            "client_id": (mail.client_id if mail else "") or "",
            "refresh_token": (mail.refresh_token if mail else "") or "",
            # 令牌组
            "access_token": access_token,
            "openai_refresh_token": tokens.get("refresh_token") or "",
            "id_token": tokens.get("id_token") or "",
            "account_id": account_id,
            "expired": expired_iso,
            "last_refresh": tokens.get("extracted_at") or "",
            # 运维元数据组（导出补全：平台 / 段位 / profile / 卡商 / IP / 创建时间等）
            "run_id": r.id or "",
            "platform": r.platform or PLATFORM_OPENAI,
            "account_tier": r.account_tier or "",
            "register_name": register_name,
            "profile_id": r.profile_id or "",
            "browser_provider": r.browser_provider or "",
            "card_provider": r.card_provider or "",
            "ip_address": r.ip_address or "",
            "ip_country": r.ip_country or "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })
    return entries


def _strip_filename_meta(entry: dict[str, Any]) -> dict[str, Any]:
    """去掉 _filename_* 内部字段（JSON 体不应暴露它们）"""
    return {k: v for k, v in entry.items() if not k.startswith("_filename_")}


def _pack_zip(entries: list[dict[str, Any]]) -> bytes:
    """把多条账号 entries 打包成 zip 字节流：每条一个 codex-{email}-{plan}.json"""
    import io as _io
    import zipfile

    # 处理同名冲突（极少：同 email + 同 plan 出现两次）：加 -N 后缀
    seen_names: dict[str, int] = {}
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            base_name = _build_codex_filename(entry["_filename_email"], entry["_filename_plan"])
            count = seen_names.get(base_name, 0)
            seen_names[base_name] = count + 1
            if count > 0:
                # 第二次出现的同名 → 在 .json 前加 -N
                name = base_name[:-5] + f"-{count + 1}.json"
            else:
                name = base_name
            content = json.dumps(_strip_filename_meta(entry), ensure_ascii=False, indent=2)
            zf.writestr(name, content)
    return buf.getvalue()


def import_pool(content: bytes, fmt: str) -> dict[str, Any]:
    """导入号池（"补号池"语义：外部账号入库为 status=success / account_tier=registered 的伪 Run）。

    Args:
        content: 上传文件的字节流
        fmt: full_json / cpa_json / credentials_csv（CSV 仅向后兼容历史文件）

    Returns:
        {"imported": N, "skipped": M, "errors": [...]}

    异常：ValueError 表示 fmt 非法或解析失败。
    """
    if fmt not in _VALID_IMPORT_FORMATS:
        raise ValueError(f"invalid fmt: {fmt}")

    # 解析 → 标准化行（dict 列表，至少含 email）
    try:
        text = content.decode("utf-8-sig")  # 兼容 BOM
    except UnicodeDecodeError:
        raise ValueError("文件不是 UTF-8 编码")

    if fmt == FMT_CREDENTIALS_CSV:
        rows = _parse_credentials_csv(text)
    elif fmt == FMT_FULL_JSON:
        rows = _parse_full_json(text)
    else:
        rows = _parse_cpa_json(text)

    imported = 0
    skipped = 0
    errors: list[str] = []

    # full_json 区分 OAuth refresh 和 OpenAI session refresh：
    #   row["refresh_token"]        = OAuth Outlook refresh（写 MailAccount）
    #   row["openai_refresh_token"] = ChatGPT session refresh（写 openai_tokens）
    # cpa_json 没有 OAuth 字段，只有一个 refresh_token = OpenAI session refresh
    # CSV 则反过来：只有 OAuth refresh，没有 OpenAI session
    def _openai_session_refresh(row: dict[str, Any]) -> str:
        if fmt == FMT_FULL_JSON:
            return str(row.get("openai_refresh_token") or "")
        if fmt == FMT_CPA_JSON:
            return str(row.get("refresh_token") or "")
        return ""

    def _has_oauth_credentials(row: dict[str, Any]) -> bool:
        # full_json / csv 都可能带 OAuth；cpa_json 不带
        return fmt in (FMT_FULL_JSON, FMT_CREDENTIALS_CSV)

    with get_session() as session:
        # 预取已存在的 email（用于去重）
        existing_emails = {
            (e or "").lower()
            for e in session.exec(select(Run.email)).all()  # type: ignore
            if e
        }

        for idx, row in enumerate(rows, start=1):
            email = (row.get("email") or "").strip().lower()
            if not email:
                errors.append(f"第 {idx} 行：缺少 email，跳过")
                skipped += 1
                continue
            if email in existing_emails:
                skipped += 1
                continue

            # openai_tokens 只在 full_json / cpa_json 时填（CSV 无 OpenAI token 字段）
            openai_tokens_payload: dict[str, Any] = {}
            if fmt in (FMT_FULL_JSON, FMT_CPA_JSON):
                openai_tokens_payload = {
                    "access_token": str(row.get("access_token") or ""),
                    "refresh_token": _openai_session_refresh(row),
                    "id_token": str(row.get("id_token") or ""),
                    "extracted_at": str(row.get("last_refresh") or ""),
                    "expires_at": str(row.get("expired") or ""),
                }

            run = Run(
                email=email,
                password=str(row.get("password") or ""),
                status="success",
                phase="imported",
                profile_id=str(row.get("profile_id") or ""),
                mail_provider=str(row.get("mail_provider") or ""),
                account_tier=TIER_REGISTERED,
                config_snapshot={
                    "imported": True,
                    "import_source": "external",
                    "import_format": fmt,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                },
                openai_tokens=openai_tokens_payload,
            )
            session.add(run)

            # 如果带了 OAuth 凭据（full_json / csv），同步建一个 MailAccount
            client_id = (row.get("client_id") or "").strip()
            refresh_token = (row.get("refresh_token") or "").strip()
            if _has_oauth_credentials(row) and client_id and refresh_token:
                # 复用已有 MailAccount（按 email 判重）
                exists = session.exec(
                    select(MailAccount).where(MailAccount.email == email)
                ).first()
                if exists is None:
                    session.add(MailAccount(
                        label=email,
                        provider_name=str(row.get("mail_provider") or "applemail"),
                        email=email,
                        client_id=client_id,
                        refresh_token=refresh_token,
                        is_active=True,
                    ))

            existing_emails.add(email)
            imported += 1

        session.commit()

    logger.info("号池导入完成 fmt=%s imported=%d skipped=%d errors=%d", fmt, imported, skipped, len(errors))
    return {"imported": imported, "skipped": skipped, "errors": errors}


def _parse_credentials_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _parse_json_rows(text: str, *, label: str) -> list[dict[str, Any]]:
    """通用解析：JSON 顶层可以是数组或单 object（兼容单账号导出 / 多账号 zip 解压后单文件 / 老数组格式）。

    - 数组：每个 item 是一条账号
    - object：被视为单条账号
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} 解析失败：{exc}")
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [dict(data)]
    raise ValueError(f"{label} 顶层必须是 JSON object 或 array")


def _parse_cpa_json(text: str) -> list[dict[str, Any]]:
    return _parse_json_rows(text, label="CPA JSON")


def _parse_full_json(text: str) -> list[dict[str, Any]]:
    """解析 full_json 文件。结构与 _parse_cpa_json 一致（兼容顶层 array 或单 object），
    但每条字段更丰富（含 password / OAuth client_id/refresh_token + openai_refresh_token）。"""
    return _parse_json_rows(text, label="Full JSON")


__all__ = [
    "TIER_REGISTERED",
    "TIER_PLUS",
    "TIER_TEAM",
    "TIER_ABANDONED",
    "PLATFORM_OPENAI",
    "PLATFORM_GROK",
    "FMT_CREDENTIALS_CSV",
    "FMT_CPA_JSON",
    "FMT_FULL_JSON",
    "list_pool",
    "get_account_detail",
    "promote",
    "abandon",
    "set_tags",
    "list_all_tags",
    "generate_link",
    "pix_plus_activate",
    "verify_plus",
    "refresh_token",
    "get_pix_status_map",
    "assign_card",
    "generate_bind_link",  # deprecated 但保留兼容
    "export_pool",
    "import_pool",
]
