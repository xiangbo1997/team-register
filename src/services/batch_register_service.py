# -*- coding: utf-8 -*-
"""
批量注册调度服务

设计立场：
  - 一次创建 N 个 Run（数据库行），返回 batch_id 给前端
  - **多 profile 并发**：profile_ids 中每个 profile 都启一个独立 dispatch 子线程，
    profile 之间并行；同一 profile 内部仍按抖动间隔串行（物理约束：一个 AdsPower
    profile 同一时刻只能开一个浏览器）
  - 号到 profile 的分配：round-robin（N 号循环分配到 M profile）
  - 间隔抖动 = 防 OpenAI / Stripe 风控聚类（整齐间隔本身是机器人特征）
  - 默认 mode=register_only（批量注册场景下，绑卡留给手动 / 一键绑卡走普号池）
  - 复用 identity_generator 生成 first/last/email_local/birthdate 一致三元组
  - 共用同一 password（运维方便）
  - email 字段留空，让 cfworker provider 自己用 email_local 拼上池里的 domain（解决 OpenAI 风控聚类）

注意：
  - 这是个 *后台调度*，不是同步 RPC。HTTP 端点立刻返回 batch_id，前端轮询查进度
  - 后台异常都 catch + 写到 batch_status，前端能看见
  - 不复用 ConfigService.get_config_snapshot() 的整个 snapshot（避免每个 Run 落几 KB JSON），
    只挑相关字段（task_mode / batch_id / identity）写进 config_snapshot
  - 并发上限受 worker._current_max_workers() 制约：profile 数 > max_workers 时
    超出的 per-profile dispatch 子线程会被 worker._executor 排队
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import Run
from src.services.identity_generator import Identity, generate_unique_identities

logger = logging.getLogger(__name__)


# ── 内存级 batch 状态（不落 DB —— 重启即丢，是有意为之：批量任务只在当前进程跑） ──

@dataclass
class _BatchState:
    """单批的运行时状态。"""
    batch_id: str
    total: int
    submitted: int = 0
    failed: int = 0
    status: str = "running"  # running / completed / cancelled / failed
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    error: str = ""
    run_ids: list[str] = field(default_factory=list)
    cancelled: bool = False  # 调用方写 cancel 标志，调度线程检查
    # 多 profile 并发：profile_id → 该 profile 负责的 run_ids（round-robin 切分）
    # 单 profile 场景下 = {profile_id: run_ids}（与历史行为兼容）
    profile_buckets: dict[str, list[str]] = field(default_factory=dict)


# 全局 batch 注册表 (process-local)
_batches: dict[str, _BatchState] = {}
_batches_lock = threading.RLock()


def _get_batch(batch_id: str) -> Optional[_BatchState]:
    with _batches_lock:
        return _batches.get(batch_id)


def get_batch_status(batch_id: str) -> Optional[dict[str, Any]]:
    """前端轮询用。返回 None = batch 不存在。"""
    state = _get_batch(batch_id)
    if state is None:
        return None
    with _batches_lock:
        return {
            "batch_id": state.batch_id,
            "total": state.total,
            "submitted": state.submitted,
            "failed": state.failed,
            "status": state.status,
            "cancelled": state.cancelled,
            "started_at": state.started_at.isoformat(),
            "finished_at": state.finished_at.isoformat() if state.finished_at else "",
            "error": state.error,
            "run_ids": list(state.run_ids),
            "progress_pct": int(100 * (state.submitted + state.failed) / max(state.total, 1)),
            "profile_buckets": {pid: list(rids) for pid, rids in state.profile_buckets.items()},
            "concurrency": len(state.profile_buckets) if state.profile_buckets else 1,
        }


def list_recent_batches(limit: int = 20) -> list[dict[str, Any]]:
    """列出近期所有 batch（按 started_at 降序）。"""
    with _batches_lock:
        states = sorted(_batches.values(), key=lambda s: s.started_at, reverse=True)[:limit]
    return [
        {
            "batch_id": s.batch_id,
            "total": s.total,
            "submitted": s.submitted,
            "failed": s.failed,
            "status": s.status,
            "started_at": s.started_at.isoformat(),
            "progress_pct": int(100 * (s.submitted + s.failed) / max(s.total, 1)),
        }
        for s in states
    ]


def cancel_batch(batch_id: str) -> bool:
    """请求取消某个 batch（已 submit 的任务不撤回，仅停止后续 submit）。"""
    state = _get_batch(batch_id)
    if state is None:
        return False
    with _batches_lock:
        state.cancelled = True
        state.status = "cancelled"
    return True


# ── 主入口 ────────────────────────────────────────────────────


def _normalize_profile_ids(
    profile_ids: Optional[list[str]] = None,
    profile_id: str = "",
) -> list[str]:
    """把入参折叠成"非空、去重、保序"的 profile 列表。

    - profile_ids 与 profile_id 二选一（也允许同传，会合并）
    - 空字符串 / 仅空白被剔除
    - 重复保留首次出现的顺序（dict.fromkeys 保序去重）
    - 全空 → 返回 []，由调用方决定是否报错
    """
    candidates: list[str] = []
    if profile_ids:
        candidates.extend(profile_ids)
    if profile_id:
        candidates.append(profile_id)
    cleaned = [str(p).strip() for p in candidates]
    cleaned = [p for p in cleaned if p]
    return list(dict.fromkeys(cleaned))


def _round_robin_buckets(run_ids: list[str], profile_ids: list[str]) -> dict[str, list[str]]:
    """把 run_ids 用 round-robin 分配到 profile_ids，返回 profile → run_ids。

    例：5 个 run + 2 个 profile → {p1: [r0, r2, r4], p2: [r1, r3]}
    确保每个 profile 都至少出现在 dict 里（哪怕只分到 0 个，便于上层显示并发槽）。
    """
    buckets: dict[str, list[str]] = {pid: [] for pid in profile_ids}
    if not profile_ids:
        return buckets
    for idx, rid in enumerate(run_ids):
        target = profile_ids[idx % len(profile_ids)]
        buckets[target].append(rid)
    return buckets


def start_batch(
    *,
    count: int,
    profile_id: str = "",
    profile_ids: Optional[list[str]] = None,
    password: str,
    interval_min_sec: float,
    interval_max_sec: float,
    mode: str = "register_only",
    browser_provider: str = "",
    card_provider: str = "",
    mail_provider: str = "",
    mail_account_id: str = "",
    gender: Optional[str] = None,
    broadcaster: Any = None,  # EventBroadcaster；调度线程内 submit_task 时用
) -> dict[str, Any]:
    """启动一个批量注册任务（多 profile 并发版）。

    创建 N 个 Run 立即落库（status=pending），按 round-robin 把 N 个号分配到
    M 个 profile，然后起 M 个 per-profile 子调度线程并行跑（同一 profile 内串行）。
    立即返回 batch_id 不阻塞 HTTP。

    Args:
        count: 目标账号数 (1-100)
        profile_id: AdsPower profile（兼容老入参，单 profile）
        profile_ids: AdsPower profile 列表，每个 profile 一个并发槽（推荐）；
                     与 profile_id 二选一或同传（会合并去重）
        password: 所有号共用密码
        interval_min_sec / interval_max_sec: 间隔抖动范围（秒）
        mode: "register_only"（默认）/ "full"
        gender: 'm' / 'f' / None（混合）
        broadcaster: EventBroadcaster 实例（worker 需要）

    Returns:
        {batch_id, total, run_ids, profile_buckets, concurrency, message}

    异常：
        ValueError: 参数非法
    """
    # 校验
    if count < 1 or count > 100:
        raise ValueError(f"count 必须在 1-100，得到 {count}")
    normalized_profiles = _normalize_profile_ids(profile_ids=profile_ids, profile_id=profile_id)
    if not normalized_profiles:
        raise ValueError("profile_id / profile_ids 至少要填一个有效值")
    # 上限对齐 worker._MAX_WORKERS_MAX（避免起出超线程池容量的子调度）
    if len(normalized_profiles) > 32:
        raise ValueError(
            f"profile 数量超出上限 32（得到 {len(normalized_profiles)}），"
            "请减少 profile 或提高 AppConfig.max_workers"
        )
    if not password.strip():
        raise ValueError("password 必须填")
    if interval_min_sec < 0 or interval_max_sec < interval_min_sec:
        raise ValueError(
            f"间隔范围非法：min={interval_min_sec} max={interval_max_sec}"
        )
    if mode not in ("full", "register_only"):
        raise ValueError(f"mode 必须是 full / register_only，得到 {mode!r}")

    # 提醒（不阻塞）：profile 比号还多 → 一些 profile 拿不到任务
    if len(normalized_profiles) > count:
        logger.warning(
            "profile 数 (%d) > count (%d)，将有 %d 个 profile 拿不到任务",
            len(normalized_profiles), count, len(normalized_profiles) - count,
        )

    # 生成 N 个唯一身份
    identities = generate_unique_identities(count, gender=gender)
    if len(identities) != count:
        # generate_unique_identities 永远返回 count 个（但允许极少数重复）
        # 这里对碰撞容忍，调用方继续
        logger.warning("身份生成器返回 %d 个（请求 %d 个）", len(identities), count)

    batch_id = "batch-" + uuid.uuid4().hex[:12]
    state = _BatchState(batch_id=batch_id, total=count)

    # 立即创建 N 个 Run（status=pending），获得 run_ids
    # 每个 Run 的 profile_id 按 round-robin 落到对应 profile
    run_ids: list[str] = []
    with get_session() as session:
        for idx, ident in enumerate(identities):
            target_profile = normalized_profiles[idx % len(normalized_profiles)]
            run = Run(
                # email 留空 —— managed mode 下 cfworker 会用 email_local 拼 domain
                # （但要靠 worker 把 email_local 透给 cfworker。简化版直接落 email_local@cfworker_default_domain）
                email="",  # 由 worker 启动后从 config_snapshot.identity 取 email_local + cfworker domain 拼
                password=password,
                profile_id=target_profile,
                card_key="",  # 批量场景：register_only 无需 card_key；full 模式下从池里挑（暂不实现）
                browser_provider=browser_provider,
                card_provider=card_provider,
                mail_provider=mail_provider,
                mail_account_id=mail_account_id,
                status="pending",
                phase="registration",
                retry_mode="restart",
                config_snapshot={
                    "task_mode": mode,
                    "batch_id": batch_id,
                    "identity": {
                        "first_name": ident.first_name,
                        "last_name": ident.last_name,
                        "email_local": ident.email_local,
                        "birthdate": ident.birthdate,
                        "full_name": ident.full_name,
                    },
                },
            )
            session.add(run)
            session.flush()  # 得到 run.id
            run_ids.append(run.id)
        session.commit()

    state.run_ids = run_ids
    state.profile_buckets = _round_robin_buckets(run_ids, normalized_profiles)
    with _batches_lock:
        _batches[batch_id] = state

    # 起后台协调线程：内部派发 M 个 per-profile 子线程并行跑
    thread = threading.Thread(
        target=_dispatch_coordinator,
        args=(state, interval_min_sec, interval_max_sec, broadcaster),
        daemon=True,
        name=f"batch-{batch_id}",
    )
    thread.start()

    concurrency = len(normalized_profiles)
    if concurrency == 1:
        msg = (
            f"批量任务已启动（单 profile 串行）："
            f"逐个完成后等 {interval_min_sec}-{interval_max_sec}s 再启动下一个"
        )
    else:
        msg = (
            f"批量任务已启动（{concurrency} profile 并发）："
            f"{count} 个号 round-robin 分到 {concurrency} 个 profile，每个 profile 内部按 "
            f"{interval_min_sec}-{interval_max_sec}s 抖动间隔串行"
        )

    return {
        "batch_id": batch_id,
        "total": count,
        "run_ids": run_ids,
        "profile_buckets": {pid: list(rids) for pid, rids in state.profile_buckets.items()},
        "concurrency": concurrency,
        "message": msg,
    }


# Run 终态集合：进入这些状态才视为"任务结束"，可以启动下一个
_RUN_TERMINAL_STATUSES: frozenset[str] = frozenset({"success", "failed", "cancelled"})

# 单任务等待终态的硬上限（秒）。
# 30 分钟覆盖最坏的人工接管场景；超时后不强制 cancel 任务，只记 warning 继续，
# 避免一个卡死任务阻塞整个 batch（卡死任务由运维另行处理）。
_DEFAULT_RUN_WAIT_TIMEOUT_SEC = 30 * 60.0

# 终态轮询间隔（秒）：2s 在反馈实时性 vs DB 压力间取折中
_DEFAULT_RUN_POLL_INTERVAL_SEC = 2.0


def _wait_for_run_terminal(
    run_id: str,
    *,
    state: _BatchState,
    max_wait_sec: float = _DEFAULT_RUN_WAIT_TIMEOUT_SEC,
    poll_interval_sec: float = _DEFAULT_RUN_POLL_INTERVAL_SEC,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
) -> str:
    """阻塞等待单个 Run 跑到终态，返回终态字符串。

    设计要点：
    - 每 ``poll_interval_sec`` 秒查一次 ``Run.status``，命中终态立刻返回
    - 终态：``success`` / ``failed`` / ``cancelled``（与 worker._update_run_status 对齐）
    - 超时（``max_wait_sec``）返回 ``"timeout"``，**不**强制 cancel 任务（避免吞掉真在跑的注册）
    - 每轮 poll 之间检查 ``state.cancelled``，用户取消整个 batch 时立刻 break 返回 ``"cancelled"``
    - DB 异常静默：单次查询失败仅 debug log，下一轮重试，避免数据库抖动炸 batch

    sleep_fn / monotonic_fn 注入点便于单测不真睡。
    """
    started = monotonic_fn()
    deadline = started + max(1.0, float(max_wait_sec))
    while True:
        # 先查是否被外部取消整个 batch（最高优先级，立刻 break）
        with _batches_lock:
            if state.cancelled:
                logger.info("batch %s wait_for_run_terminal: state.cancelled 被设置，立刻返回", state.batch_id)
                return "cancelled"

        # 查 Run.status
        try:
            with get_session() as session:
                run = session.get(Run, run_id)
                current_status = (run.status if run else "") or ""
        except Exception as exc:
            logger.debug("batch %s wait_for_run_terminal: DB 查询异常（将重试）: %s", state.batch_id, exc)
            current_status = ""

        if current_status in _RUN_TERMINAL_STATUSES:
            return current_status

        # 超时检查
        now = monotonic_fn()
        if now >= deadline:
            logger.warning(
                "batch %s run_id=%s 等待终态超时（%.0fs，当前 status=%s），跳过等待继续下一个",
                state.batch_id, run_id, max_wait_sec, current_status or "unknown",
            )
            return "timeout"

        # 拆成小片睡，便于 cancel 立刻响应
        slept = 0.0
        while slept < poll_interval_sec:
            chunk = min(0.5, poll_interval_sec - slept)
            sleep_fn(chunk)
            slept += chunk
            with _batches_lock:
                if state.cancelled:
                    break


def _dispatch_loop(
    state: _BatchState,
    interval_min: float,
    interval_max: float,
    broadcaster: Any,
    *,
    run_wait_timeout_sec: float = _DEFAULT_RUN_WAIT_TIMEOUT_SEC,
    run_poll_interval_sec: float = _DEFAULT_RUN_POLL_INTERVAL_SEC,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
) -> None:
    """后台调度线程：**严格串行**逐个 submit_task → 等终态 → 抖动间隔 → 下一个。

    interval 语义（重要）：
        ``interval_min..max`` 是"上一个 Run 跑到终态后再等多久才启动下一个"，
        **不是**"两次 submit 之间的固定间隔"。这与单 profile_id 共用浏览器
        的物理约束一致：上一个浏览器会话必须释放，下一个才能用。

    串行屏障：
        每次 submit_task 后调用 _wait_for_run_terminal 阻塞，直到 Run 进入
        success/failed/cancelled/timeout 之一才启动下一个。多浏览器并发场景
        请用户开多个 batch（不同 profile_id），不在本调度器范围内。

    sleep_fn / monotonic_fn 注入点便于单测。
    """
    # lazy import 避免循环依赖（worker → batch_register_service → worker）
    from src.api.worker import submit_task

    logger.info(
        "batch %s 开始调度：total=%d 完成后等待间隔=%.1f-%.1fs（严格串行）",
        state.batch_id, state.total, interval_min, interval_max,
    )

    for idx, run_id in enumerate(state.run_ids):
        with _batches_lock:
            if state.cancelled:
                logger.info("batch %s 已取消，停止后续 submit", state.batch_id)
                break

        # ── Step 1: submit_task（异步入池）──
        try:
            ok = submit_task(run_id, broadcaster) if broadcaster else False
            if not broadcaster:
                # 没 broadcaster 是配置问题，直接标失败（保护）
                logger.error("batch %s submit_task 缺 broadcaster", state.batch_id)
                with _batches_lock:
                    state.failed += 1
                continue
            elif ok:
                with _batches_lock:
                    state.submitted += 1
            else:
                with _batches_lock:
                    state.failed += 1
                continue
        except Exception as exc:
            logger.exception("batch %s 第 %d 个 submit 异常", state.batch_id, idx)
            with _batches_lock:
                state.failed += 1
                if not state.error:
                    state.error = f"{type(exc).__name__}: {str(exc)[:180]}"
            continue

        # ── Step 2: 阻塞等当前 Run 跑到终态（核心屏障）──
        terminal_status = _wait_for_run_terminal(
            run_id,
            state=state,
            max_wait_sec=run_wait_timeout_sec,
            poll_interval_sec=run_poll_interval_sec,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        logger.info(
            "batch %s 第 %d/%d 个任务终态=%s (run_id=%s)",
            state.batch_id, idx + 1, state.total, terminal_status, run_id,
        )

        # 整 batch 被取消 → 不再启动下一个
        if terminal_status == "cancelled":
            with _batches_lock:
                if state.cancelled:
                    break

        # ── Step 3: 抖动间隔（仅最后一个不需要等）──
        if idx < len(state.run_ids) - 1:
            wait_sec = random.uniform(interval_min, interval_max)
            # 拆成 1s 一片睡，便于 cancel 中断
            slept = 0.0
            while slept < wait_sec:
                sleep_fn(min(1.0, wait_sec - slept))
                slept += 1.0
                with _batches_lock:
                    if state.cancelled:
                        break

    with _batches_lock:
        state.finished_at = datetime.now(timezone.utc)
        if state.cancelled:
            state.status = "cancelled"
        elif state.failed == state.total:
            state.status = "failed"
        else:
            state.status = "completed"
    logger.info(
        "batch %s 调度结束：submitted=%d failed=%d status=%s",
        state.batch_id, state.submitted, state.failed, state.status,
    )


# ── 多 profile 并发协调器 ─────────────────────────────────────


def _dispatch_loop_for_profile(
    state: _BatchState,
    profile_id: str,
    run_ids: list[str],
    interval_min: float,
    interval_max: float,
    broadcaster: Any,
    *,
    run_wait_timeout_sec: float = _DEFAULT_RUN_WAIT_TIMEOUT_SEC,
    run_poll_interval_sec: float = _DEFAULT_RUN_POLL_INTERVAL_SEC,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
) -> None:
    """单个 profile 的串行调度子线程（per-profile worker）。

    语义与 _dispatch_loop 完全一致，只是把对象从 state.run_ids 换成本 profile 的 bucket。
    **不**写 state.finished_at/status —— 那由 coordinator 在所有 per-profile worker
    退出后统一写。

    submitted/failed 计数共享 state（_batches_lock 保护），所以多个 profile worker 并发
    更新计数是安全的。
    """
    from src.api.worker import submit_task

    if not run_ids:
        logger.info("batch %s profile=%s 无任务，子线程立即退出", state.batch_id, profile_id)
        return

    logger.info(
        "batch %s profile=%s 子调度启动：bucket_size=%d 抖动=%.1f-%.1fs",
        state.batch_id, profile_id, len(run_ids), interval_min, interval_max,
    )

    for idx, run_id in enumerate(run_ids):
        with _batches_lock:
            if state.cancelled:
                logger.info(
                    "batch %s profile=%s 已取消，停止后续 submit",
                    state.batch_id, profile_id,
                )
                break

        # ── Step 1: submit_task ──
        try:
            ok = submit_task(run_id, broadcaster) if broadcaster else False
            if not broadcaster:
                logger.error(
                    "batch %s profile=%s submit_task 缺 broadcaster",
                    state.batch_id, profile_id,
                )
                with _batches_lock:
                    state.failed += 1
                continue
            elif ok:
                with _batches_lock:
                    state.submitted += 1
            else:
                with _batches_lock:
                    state.failed += 1
                continue
        except Exception as exc:
            logger.exception(
                "batch %s profile=%s 第 %d 个 submit 异常",
                state.batch_id, profile_id, idx,
            )
            with _batches_lock:
                state.failed += 1
                if not state.error:
                    state.error = f"{type(exc).__name__}: {str(exc)[:180]}"
            continue

        # ── Step 2: 阻塞等当前 Run 终态 ──
        terminal_status = _wait_for_run_terminal(
            run_id,
            state=state,
            max_wait_sec=run_wait_timeout_sec,
            poll_interval_sec=run_poll_interval_sec,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        logger.info(
            "batch %s profile=%s 第 %d/%d 终态=%s (run_id=%s)",
            state.batch_id, profile_id, idx + 1, len(run_ids), terminal_status, run_id,
        )

        # batch 整体取消 → 退出
        if terminal_status == "cancelled":
            with _batches_lock:
                if state.cancelled:
                    break

        # ── Step 3: 抖动间隔（profile 内部串行节奏，profile 之间无关）──
        if idx < len(run_ids) - 1:
            wait_sec = random.uniform(interval_min, interval_max)
            slept = 0.0
            while slept < wait_sec:
                sleep_fn(min(1.0, wait_sec - slept))
                slept += 1.0
                with _batches_lock:
                    if state.cancelled:
                        break

    logger.info(
        "batch %s profile=%s 子调度结束",
        state.batch_id, profile_id,
    )


def _dispatch_coordinator(
    state: _BatchState,
    interval_min: float,
    interval_max: float,
    broadcaster: Any,
    *,
    run_wait_timeout_sec: float = _DEFAULT_RUN_WAIT_TIMEOUT_SEC,
    run_poll_interval_sec: float = _DEFAULT_RUN_POLL_INTERVAL_SEC,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
) -> None:
    """顶层协调员：为每个 profile 起 per-profile 子线程并行跑，join 后写 final state。

    profile_buckets 为空（理论上不该发生，因为 start_batch 已校验）→ 回退到旧
    _dispatch_loop 走单线程串行。这是兜底，不是预期路径。
    """
    if not state.profile_buckets:
        logger.warning(
            "batch %s coordinator: profile_buckets 为空，回退到单线程 _dispatch_loop",
            state.batch_id,
        )
        _dispatch_loop(
            state, interval_min, interval_max, broadcaster,
            run_wait_timeout_sec=run_wait_timeout_sec,
            run_poll_interval_sec=run_poll_interval_sec,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        return

    concurrency = len(state.profile_buckets)
    logger.info(
        "batch %s coordinator 启动：total=%d concurrency=%d 抖动=%.1f-%.1fs",
        state.batch_id, state.total, concurrency, interval_min, interval_max,
    )

    workers: list[threading.Thread] = []
    for profile_id, run_ids in state.profile_buckets.items():
        t = threading.Thread(
            target=_dispatch_loop_for_profile,
            args=(state, profile_id, list(run_ids), interval_min, interval_max, broadcaster),
            kwargs={
                "run_wait_timeout_sec": run_wait_timeout_sec,
                "run_poll_interval_sec": run_poll_interval_sec,
                "sleep_fn": sleep_fn,
                "monotonic_fn": monotonic_fn,
            },
            daemon=True,
            name=f"batch-{state.batch_id}-{profile_id}",
        )
        t.start()
        workers.append(t)

    # 等所有 per-profile worker 退出
    for t in workers:
        t.join()

    # 统一写 final state
    with _batches_lock:
        state.finished_at = datetime.now(timezone.utc)
        if state.cancelled:
            state.status = "cancelled"
        elif state.failed == state.total:
            state.status = "failed"
        else:
            state.status = "completed"
    logger.info(
        "batch %s coordinator 结束：submitted=%d failed=%d status=%s",
        state.batch_id, state.submitted, state.failed, state.status,
    )


def requeue_runs(
    run_ids: list[str],
    broadcaster: Any,
    *,
    interval_min_sec: float = 0.0,
    interval_max_sec: float = 0.0,
) -> dict[str, Any]:
    """复用 batch dispatcher 把已有 Run 重新派发到 per-profile 串行队列。

    retry 路由专用：retry_task 路由不再裸调 submit_task（在并发 retry 下会被静默丢弃），
    改走这里 —— 按 Run.profile_id 自动分桶，同一 profile 内串行、profile 之间并行。
    单个 run 也能用（profile_buckets 单桶单元素）。

    与 start_batch 的区别：
      - **不创建** Run，run_ids 必须已经在 DB（status 应为 pending）
      - 不写 _batches 注册表（retry 是即发即忘，避免污染 list_recent_batches）
      - 抖动默认 0（retry 不是批量首发，无抗风控聚类语义）

    Args:
        run_ids: 已存在的 Run id 列表
        broadcaster: EventBroadcaster 实例（submit_task 需要）
        interval_min_sec / interval_max_sec: profile 内部抖动间隔（默认 0 = 立即）

    Returns:
        {requeued: N, profile_buckets: {pid: [...]}, concurrency: M}
    """
    if not run_ids:
        return {"requeued": 0, "profile_buckets": {}, "concurrency": 0}

    # 从 DB 读每个 run 的 profile_id 做分桶
    with get_session() as session:
        rows = session.exec(select(Run).where(Run.id.in_(run_ids))).all()  # type: ignore[attr-defined]
        run_to_profile: dict[str, str] = {r.id: (r.profile_id or "").strip() for r in rows}

    # 缺 profile_id 的 Run 不能用 per-profile dispatcher（dispatcher 是按 profile 串行）
    # 这里把它们归到 "_no_profile" 桶让 dispatcher 顺序跑（与单 profile 等价）
    buckets: dict[str, list[str]] = {}
    for rid in run_ids:
        pid = run_to_profile.get(rid) or "_no_profile"
        buckets.setdefault(pid, []).append(rid)

    # 起临时 state（不写 _batches）+ coordinator
    state = _BatchState(
        batch_id="requeue-" + uuid.uuid4().hex[:8],
        total=len(run_ids),
        run_ids=list(run_ids),
        profile_buckets=buckets,
    )

    thread = threading.Thread(
        target=_dispatch_coordinator,
        args=(state, interval_min_sec, interval_max_sec, broadcaster),
        daemon=True,
        name=state.batch_id,
    )
    thread.start()

    return {
        "requeued": len(run_ids),
        "profile_buckets": {pid: list(rids) for pid, rids in buckets.items()},
        "concurrency": len(buckets),
    }


__all__ = [
    "start_batch",
    "requeue_runs",
    "get_batch_status",
    "list_recent_batches",
    "cancel_batch",
]
