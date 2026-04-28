# -*- coding: utf-8 -*-
"""卡预热（热卡）链路诊断脚本（Phase A + 可选 provider 探活）。

用法：
    python scripts/diagnose_warmup.py                   # 只扫 DB（默认，零副作用）
    python scripts/diagnose_warmup.py --probe-providers # 额外向 email-provider 发探活请求
    python scripts/diagnose_warmup.py --db sqlite:///team_register.db

默认行为：只读扫描 ``team_register.db``，把热卡相关 4 段状态打成可读报告：

    1. 卡死 Run 扫描        — phase IN ('warming_up_card','warming_up_account')
                              且 status NOT IN ('cancelled','abandoned') 的 Run
                              重点看 next_action_at 是否过期超 1 小时
    2. pro_warmup 池健康度  — MailAccount(role=pro_warmup) 的 is_active /
                              cooldown_until / consecutive_failures 分布
    3. CardActivation 缓存  — 已作废 + 即将到期 + 孤儿条目（never used）
    4. 最近异常事件         — RunEvent.event_type IN ('error','warning')
                              且 run_id 关联到 warmup runs

可选 ``--probe-providers``：额外做 V1+V2+V3 三连测，向 ``email.feixingqi.shop``
直接打 ``POST /managed-sessions``，区分 "cfworker 短窗 5xx" / "邮箱不在配置池" /
"参数错配" 三种 warmup 失败模式。每个 provider 探活会创建 1 个 60s 租期的 session，
完成后立刻释放，不消耗注册流程的邮箱配额。

零副作用（默认）：不写库、不连 AdsPower / X988 / 邮件服务。脱敏：card_number
只显示后 4 位，sms_api 用 '<set>' / '<empty>' 替代，refresh_token / client_id 不打印。

加 ``--probe-providers`` 时**会向 email-provider 发 HTTP 请求**（仍是 read-only/lease），
这些请求不写本地 DB。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlmodel import Session, select  # noqa: E402

from src.db.engine import get_engine  # noqa: E402
from src.db.models import (  # noqa: E402
    CardActivation,
    MailAccount,
    Run,
    RunEvent,
)
from src.services.card_activation_service import (  # noqa: E402
    WARMUP_CARD_CACHE_MAX_AGE_DAYS,
    _is_card_still_valid,
)
from src.services.config_service import (  # noqa: E402
    WARMUP_COOLDOWN_MINUTES,
    WARMUP_MAX_CONSECUTIVE_FAILURES,
)


# ── 通用工具 ──────────────────────────────────────


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite naive datetime → 补 UTC，None 保持 None。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_dt(dt: Optional[datetime]) -> str:
    aware = _ensure_aware(dt)
    return aware.isoformat(timespec="seconds") if aware else "<none>"


def _fmt_age(dt: Optional[datetime], now: datetime) -> str:
    aware = _ensure_aware(dt)
    if aware is None:
        return "<n/a>"
    delta = now - aware
    if delta.total_seconds() < 0:
        # 未来时刻
        future = -delta
        return f"future +{int(future.total_seconds() // 60)}min"
    days = delta.days
    hours = int(delta.total_seconds() // 3600) - days * 24
    minutes = int(delta.total_seconds() // 60) % 60
    if days >= 1:
        return f"{days}d{hours}h ago"
    if hours >= 1:
        return f"{hours}h{minutes}m ago"
    return f"{minutes}m ago"


def _redact_email(email: str) -> str:
    if "@" not in email:
        return email or "<empty>"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***@{domain}"


def _last4(card_number: str) -> str:
    s = str(card_number or "")
    return s[-4:] if len(s) >= 4 else "????"


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"=== {title}")
    print("=" * 70)


# ── Phase A.1: Run 卡死扫描 ──────────────────────────


def _diagnose_warmup_runs(session: Session, now: datetime) -> list[str]:
    """打印 warmup phase 上的活跃 Run，返回其 run_id 用于第 4 段查事件。"""
    _section("1. 卡死 Run 扫描（phase IN warming_up_*，status 非终态）")

    stmt = (
        select(Run)
        .where(
            Run.phase.in_(("warming_up_card", "warming_up_account")),  # type: ignore[attr-defined]
            Run.status != "cancelled",
            Run.status != "abandoned",
            Run.status != "success",
        )
        .order_by(Run.updated_at.desc())  # type: ignore[arg-type]
    )
    rows = list(session.exec(stmt).all())
    if not rows:
        print("  （无 warmup phase 上的活跃 Run）")
        return []

    print(f"  共 {len(rows)} 条 warmup-phase Run")
    print()
    print(
        f"  {'run_id':<10} {'phase':<22} {'status':<10} {'next_action_at':<22} "
        f"{'attempts':>9} {'blocked':>8} reason"
    )
    print("  " + "-" * 100)

    stuck_run_ids: list[str] = []
    one_hour_ago = now - timedelta(hours=1)
    for run in rows:
        next_at = _ensure_aware(run.next_action_at)
        next_at_str = _fmt_dt(next_at)
        is_stuck_overdue = next_at is not None and next_at < one_hour_ago
        marker = " ⚠ overdue>1h" if is_stuck_overdue else ""
        attempts_marker = (
            " ⚠"
            if int(getattr(run, "warmup_pro_attempts", 0) or 0) >= 3
            else ""
        )
        reason = (run.error_reason or "").strip()
        if len(reason) > 40:
            reason = reason[:37] + "..."
        print(
            f"  {run.id[:8]:<10} {run.phase:<22} {run.status:<10} {next_at_str:<22} "
            f"{run.warmup_pro_attempts:>9}{attempts_marker} "
            f"{run.warmup_blocked_count:>8} {reason}{marker}"
        )
        if is_stuck_overdue or int(getattr(run, "warmup_pro_attempts", 0) or 0) >= 3:
            stuck_run_ids.append(run.id)

    if stuck_run_ids:
        print()
        print(f"  ⚠ 检测到 {len(stuck_run_ids)} 条疑似卡死 Run（next_action_at 过期 >1h "
              f"或 warmup_pro_attempts ≥3）")
    return [r.id for r in rows]


# ── Phase A.2: pro_warmup 池健康度 ──────────────────


def _diagnose_warmup_pool(session: Session, now: datetime) -> None:
    _section("2. pro_warmup 号池健康度（cooldown / consecutive_failures / is_active）")

    stmt = (
        select(MailAccount)
        .where(MailAccount.role == "pro_warmup")
        .order_by(MailAccount.last_used_at.desc().nullsfirst())  # type: ignore[attr-defined]
    )
    accounts = list(session.exec(stmt).all())

    if not accounts:
        print("  （pro_warmup 号池为空 —— 卡预热将永远 select 返回 None）")
        return

    total = len(accounts)
    active = sum(1 for a in accounts if a.is_active)
    disabled = total - active
    in_cooldown = 0
    near_disable = 0
    suspect_zombie_cooldown = 0  # 该 bug 在计划 C5 中讨论

    for a in accounts:
        cooldown_until = _ensure_aware(a.cooldown_until)
        if a.is_active and cooldown_until and cooldown_until > now:
            in_cooldown += 1
        if a.is_active and int(a.consecutive_failures or 0) >= WARMUP_MAX_CONSECUTIVE_FAILURES - 1:
            near_disable += 1
        # 疑似"重启即被锁住"：is_active=True 但 cooldown_until 还在未来
        # 且账号近期没被预热挑过（last_used_at 比 cooldown_until 更早，说明禁用→重启留下的残留）
        if (
            a.is_active
            and cooldown_until is not None
            and cooldown_until > now
            and (a.last_used_at is None or _ensure_aware(a.last_used_at) < cooldown_until - timedelta(minutes=WARMUP_COOLDOWN_MINUTES + 1))
        ):
            suspect_zombie_cooldown += 1

    print(f"  总数={total}  is_active={active}  is_active=False={disabled}")
    print(f"  当前在冷却中（is_active=True 且 cooldown_until > now）: {in_cooldown}")
    print(f"  即将自动禁用（consecutive_failures ≥ {WARMUP_MAX_CONSECUTIVE_FAILURES - 1}）: {near_disable}")
    if suspect_zombie_cooldown:
        print(
            f"  ⚠ 疑似 zombie cooldown（重启后被旧 cooldown 锁死）: {suspect_zombie_cooldown}"
        )
    if active == 0:
        print("  ⚠ 所有 pro_warmup 账号都已 is_active=False —— 号池告罄")
    elif active - in_cooldown == 0:
        print("  ⚠ 所有 active 账号都在冷却中 —— select 此刻必然返回 None")

    print()
    print(
        f"  {'id':<10} {'email':<30} {'active':<7} {'cooldown_until':<22} "
        f"{'fails':>5} {'last_used':<14} reason"
    )
    print("  " + "-" * 110)
    for a in accounts:
        cooldown_str = _fmt_dt(a.cooldown_until)
        last_used = _fmt_age(a.last_used_at, now)
        fails = int(a.consecutive_failures or 0)
        fails_marker = (
            "⚠" if fails >= WARMUP_MAX_CONSECUTIVE_FAILURES - 1 else ""
        )
        reason = (a.last_failure_reason or "").strip()
        if len(reason) > 40:
            reason = reason[:37] + "..."
        print(
            f"  {a.id[:8]:<10} {_redact_email(a.email):<30} "
            f"{'yes' if a.is_active else 'no':<7} "
            f"{cooldown_str:<22} "
            f"{fails:>4}{fails_marker} {last_used:<14} {reason}"
        )


# ── Phase A.3: CardActivation 缓存 ─────────────────


def _diagnose_card_activations(session: Session, now: datetime) -> None:
    _section("3. CardActivation 缓存（X988 verify 历史 + 即将到期 + 已作废）")

    stmt = select(CardActivation).order_by(CardActivation.activated_at.desc())  # type: ignore[arg-type]
    rows = list(session.exec(stmt).all())
    if not rows:
        print("  （card_activations 表为空 —— 没有任何已 verify 的卡）")
        return

    total = len(rows)
    invalidated = sum(1 for r in rows if r.is_invalidated)
    valid = sum(1 for r in rows if not r.is_invalidated and _is_card_still_valid(r))
    expired_or_age = total - invalidated - valid
    orphans = sum(1 for r in rows if (r.use_count or 0) == 0)

    print(f"  总数={total}  valid={valid}  expired_or_max_age={expired_or_age}  "
          f"invalidated={invalidated}  orphans(use_count=0)={orphans}")
    print(f"  max_age 阈值 = {WARMUP_CARD_CACHE_MAX_AGE_DAYS} 天")

    # 列出所有非作废条目，按 activated_at desc
    print()
    print(
        f"  {'cdk':<14} {'last4':<6} {'exp':<8} {'activated':<14} "
        f"{'used':>5} {'last_used':<14} status"
    )
    print("  " + "-" * 100)
    for r in rows:
        cdk = (r.card_key or "")[:8] + "..." if r.card_key else "<empty>"
        exp = f"{r.expiry_month}/{r.expiry_year[-2:]}" if r.expiry_year and r.expiry_month else "?/??"
        activated = _fmt_age(r.activated_at, now)
        last_used = _fmt_age(r.last_used_at, now) if r.last_used_at else "<never>"

        if r.is_invalidated:
            status = f"INVALID: {(r.invalidate_reason or '')[:30]}"
        elif _is_card_still_valid(r):
            status = "valid"
        else:
            status = "expired_or_max_age"

        print(
            f"  {cdk:<14} {_last4(r.card_number):<6} {exp:<8} {activated:<14} "
            f"{int(r.use_count or 0):>5} {last_used:<14} {status}"
        )


# ── Phase A.4: 最近异常事件 ────────────────────────


def _diagnose_recent_events(
    session: Session, now: datetime, warmup_run_ids: list[str]
) -> None:
    _section("4. 最近异常事件（warmup runs 范围 + event_type IN error/warning，最多 30 条）")

    if not warmup_run_ids:
        print("  （没有 warmup-phase Run，跳过事件查询）")
        return

    stmt = (
        select(RunEvent)
        .where(
            RunEvent.run_id.in_(warmup_run_ids),  # type: ignore[attr-defined]
            RunEvent.event_type.in_(("error", "warning")),  # type: ignore[attr-defined]
        )
        .order_by(RunEvent.timestamp.desc())  # type: ignore[arg-type]
        .limit(30)
    )
    rows = list(session.exec(stmt).all())
    if not rows:
        print("  （这些 Run 没有 error/warning 事件 —— 失败可能没经 EventBroadcaster）")
        return

    print(f"  共 {len(rows)} 条最近 error/warning 事件（仅取最近 30 条）")
    print()
    for ev in rows:
        ts = _fmt_age(ev.timestamp, now)
        run_short = (ev.run_id or "")[:8]
        msg = ""
        if isinstance(ev.payload, dict):
            msg = str(ev.payload.get("message") or "")
        if len(msg) > 80:
            msg = msg[:77] + "..."
        print(
            f"  [{ts:<12}] run={run_short} state={ev.state or '-':<14} "
            f"type={ev.event_type:<8} {msg}"
        )


# ── Phase A.5（可选）: Email Provider 探活 V1+V2+V3 ─────


def _probe_providers(session: Session) -> None:
    """对号池实际用到的 mail provider 做活性探针。

    设计意图：上一轮事件证明，warmup 失败时最难定位的就是 "现在到底是远程
    服务挂了，还是邮箱与配置不匹配，还是参数错"。本探针发 3 类请求：

      V1: 用号池里某个真实 email + config_name → 命中 "邮箱+配置匹配，远程是否健康"
      V2: 不传 email，让服务端自动分配 + config_name → 命中 "auto-allocate 是否能通"
      V3: 不传 config_name → 命中 "服务端没拿到 cfworker_api_url 时是否会 500"

    每次探活都会立刻 complete(result="success", reason="diagnostic")
    释放 lease，不会持续占用号池。

    依赖 .env 的 EMAIL_PROVIDER_BASE_URL / EMAIL_PROVIDER_API_KEY /
    MAIL_CONFIG_NAME 配置；缺哪个就报哪个。
    """
    _section("5. Email Provider 探活（V1+V2+V3 三连测，需要 EMAIL_PROVIDER_API_KEY）")

    # 延迟导入，避免默认路径加载这些模块
    from src.config import load_config
    from src.providers.mail import HttpMailProvider

    try:
        config = load_config()
    except Exception as exc:
        print(f"  ⚠ load_config 失败，跳过探活: {exc}")
        return

    base_url = (config.email_provider_base_url or "").strip()
    api_key = (config.email_provider_api_key or "").strip()
    provider_name = (config.email_provider_name or "").strip()
    config_name = (config.mail_config_name or "").strip()

    if not base_url or not api_key or not provider_name:
        print(f"  ⚠ 缺少 email-provider 必填配置，跳过探活")
        print(f"     base_url={'set' if base_url else 'MISSING'}  "
              f"api_key={'set' if api_key else 'MISSING'}  "
              f"provider={'set' if provider_name else 'MISSING'}")
        return

    print(f"  base_url     : {base_url}")
    print(f"  provider     : {provider_name}")
    print(f"  config_name  : {config_name or '<empty>'}")
    print()

    provider = HttpMailProvider(base_url=base_url, api_key=api_key)

    # 找一个号池里的真实 email 做 V1（优先 active 账号；没有就拿任意一个）
    sample_email = ""
    pool_query = (
        select(MailAccount)
        .where(MailAccount.role == "pro_warmup")
        .where(MailAccount.provider_name == provider_name)
    )
    candidates = list(session.exec(pool_query).all())
    active = [a for a in candidates if a.is_active]
    if active:
        sample_email = active[0].email
        sample_label = f"{_redact_email(sample_email)} (active)"
    elif candidates:
        sample_email = candidates[0].email
        sample_label = f"{_redact_email(sample_email)} (disabled)"
    else:
        sample_label = "<no pool email available>"

    print(f"  {'探针':<70} {'结果'}")
    print("  " + "-" * 92)

    # V1: 已知 email + config_name
    label = f"V1 {provider_name}+{config_name or '<no_cfg>'} email={sample_label}"
    if sample_email:
        result = _safe_probe(provider, provider_name, config_name, email=sample_email)
    else:
        result = "skip (号池为空)"
    print(f"  {label:<70} {result}")

    # V2: auto-allocate + config_name
    label = f"V2 {provider_name}+{config_name or '<no_cfg>'} (auto-allocate)"
    result = _safe_probe(provider, provider_name, config_name, email="")
    print(f"  {label:<70} {result}")

    # V3: 不传 config_name（注册和 warmup 实际不会这么调；纯诊断对照）
    label = f"V3 {provider_name} 无 config_name (auto-allocate)"
    result = _safe_probe(provider, provider_name, "", email="")
    print(f"  {label:<70} {result}")

    print()
    print("  解读：")
    print("    - V1+V2 都 ✅ → email-provider 健康，warmup 失败是别的原因")
    print("    - V1 ❌ V2 ✅ → 这个邮箱与当前 config 不匹配（孤儿账号）")
    print("    - V1 ❌ V2 ❌ V3 ✅ → cfworker 这个具体 provider 实现挂了或缺参")
    print("    - V1 ❌ V2 ❌ V3 ❌ → email-provider 整体异常 / 鉴权失败")


def _safe_probe(
    provider: "HttpMailProvider",  # type: ignore[name-defined]  # noqa: F821
    provider_name: str,
    config_name: str,
    *,
    email: str,
) -> str:
    """单次探活，返回简洁结果字符串。

    成功 → '✅ 200 (auto-released)'
    失败 → '❌ <错误简述>'
    """
    import requests
    from src.providers.mail import MailServiceError, MailRuntimeIncompatibleError

    session_obj = None
    try:
        session_obj = provider.create_session(
            provider=provider_name,
            purpose="diagnostic_probe",
            session_mode="managed",
            email=email,
            lease_seconds=60,
            config_name=config_name,
        )
        return f"✅ 200  session={session_obj.session_id[:8]}... email={_redact_email(session_obj.email or '<auto>')}"
    except (MailServiceError, MailRuntimeIncompatibleError) as exc:
        return f"❌ {type(exc).__name__}: {str(exc)[:80]}"
    except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return f"❌ HTTP {status or 'network'}: {str(exc)[:80]}"
    except ConnectionError as exc:
        return f"❌ ConnectionError: {str(exc)[:80]}"
    except Exception as exc:  # pragma: no cover  防御性兜底
        return f"❌ {type(exc).__name__}: {str(exc)[:80]}"
    finally:
        # 立即释放 lease，不占号池配额
        if session_obj is not None:
            try:
                provider.complete(session_obj, result="success", reason="diagnostic")
            except Exception:
                pass


# ── 入口 ──────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="扫描热卡链路状态报告（默认只读 DB；--probe-providers 额外向 email-provider 探活）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="数据库连接串（默认读 DATABASE_URL 环境变量，再回退到 sqlite:///team_register.db）",
    )
    parser.add_argument(
        "--probe-providers",
        action="store_true",
        help="额外向 email-provider 发 V1+V2+V3 探活请求（需要 EMAIL_PROVIDER_API_KEY）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,  # 只显示 warning+ 的日志，避免 SQLModel info 噪声
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.db:
        os.environ["DATABASE_URL"] = args.db

    engine = get_engine()
    print(f"诊断时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"数据库   ：{engine.url.render_as_string(hide_password=True)}")
    print(f"阈值     ：cooldown={WARMUP_COOLDOWN_MINUTES}min  "
          f"max_consec_fail={WARMUP_MAX_CONSECUTIVE_FAILURES}  "
          f"card_max_age={WARMUP_CARD_CACHE_MAX_AGE_DAYS}d")

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        warmup_run_ids = _diagnose_warmup_runs(session, now)
        _diagnose_warmup_pool(session, now)
        _diagnose_card_activations(session, now)
        _diagnose_recent_events(session, now, warmup_run_ids)
        if args.probe_providers:
            _probe_providers(session)

    print()
    print("=" * 70)
    print("=== 诊断结束")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
