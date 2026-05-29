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


def _account_to_dict(run: Run) -> dict[str, Any]:
    """脱敏序列化（不暴露 password / token）。"""
    return {
        "run_id": run.id,
        "email": run.email,
        "email_redacted": _redact_email(run.email),
        "profile_id": run.profile_id,
        "browser_provider": run.browser_provider,
        "card_provider": run.card_provider,
        "mail_provider": run.mail_provider,
        "status": run.status,
        "phase": run.phase,
        "account_tier": run.account_tier,
        "card_key": run.card_key,
        "card_bin": run.card_bin,
        "is_card_warmed_up": bool(run.is_card_warmed_up),
        "ip_address": run.ip_address or "",
        "ip_country": run.ip_country or "",
        "error_reason": run.error_reason,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "updated_at": run.updated_at.isoformat() if run.updated_at else "",
    }


def list_pool(
    *,
    tier: str = TIER_REGISTERED,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出指定段位的号。

    默认只展示 status=success 的号 —— 失败的号不算"账号"，不该入池。
    """
    if tier not in _VALID_TIERS:
        raise ValueError(f"invalid tier: {tier}")
    with get_session() as session:
        stmt = (
            select(Run)
            .where(Run.account_tier == tier)
            .where(Run.status == "success")
            .order_by(Run.created_at.desc())  # type: ignore
            .limit(max(1, min(limit, 200)))
        )
        rows = list(session.exec(stmt).all())
        return [_account_to_dict(r) for r in rows]


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


_VALID_LINK_PLANS = ("team", "plus", "pro", "pro_lite")
_VALID_RETURN_MODES = ("long", "app")


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

    success, link = PaymentLinkGenerator.generate_checkout_link(access_token, **gen_kwargs)
    if not success:
        raise ValueError(f"生成 checkout 链接失败: {link}")

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
            },
        ))
        session.commit()

    return {
        "link": link,
        "plan": plan_normalized,
        "return_mode": mode,
    }


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
) -> tuple[str, str, str, list[dict[str, str]]]:
    """导出指定段位的号池。

    Args:
        tier: registered / plus / team / abandoned
        fmt: credentials_csv（账号+OAuth） / cpa_json（CPA token 格式）
        run_ids: 可选，仅导出这些 run_id 的号（用于"导出选中"功能）。
                 传空列表 [] 视为"无匹配"，返回空内容；None 表示不过滤。

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

    # 规整 run_ids：去重 + 去空，全空视为 [] 而非 None
    ids_filter: Optional[list[str]] = None
    if run_ids is not None:
        ids_filter = [str(r).strip() for r in run_ids if str(r).strip()]

    with get_session() as session:
        stmt = (
            select(Run)
            .where(Run.account_tier == tier)
            .where(Run.status == "success")
            .order_by(Run.created_at.desc())  # type: ignore
        )
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
    """生成 full_json 每条账号的 dict（账号凭证组 + 令牌组）。

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
          "last_refresh": "..."
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
    "FMT_CREDENTIALS_CSV",
    "FMT_CPA_JSON",
    "FMT_FULL_JSON",
    "list_pool",
    "get_account_detail",
    "promote",
    "abandon",
    "generate_link",
    "assign_card",
    "generate_bind_link",  # deprecated 但保留兼容
    "export_pool",
    "import_pool",
]
