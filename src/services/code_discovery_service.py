# -*- coding: utf-8 -*-
"""促销码主动发现服务

参考 gpt-promo-scanner 的 discover_codes 模式，但只搬"发现+落库+进度"，
不搬 Clash 切节点（与 team-register 的 proxy_service 重叠）。

核心流程：
  1. 用户在 dashboard 选国家 + 模式（seeds / cross_matrix）+ 可选自定义词
  2. promo_seeds.build_candidates(country) 生成 1500-3000 候选码
  3. 复用 promo_eligibility_service._resolve_token_for_verification 借 token
  4. 复用 proxy_service.get_active_proxy_by_country 选代理
  5. 串行调 check_eligibility，1.0s 间隔（比 bulk_verify 的 0.5s 翻倍留 CF 余量）
  6. ELIGIBLE 立即建 LinkTemplate（重名跳过，天然幂等）；EXISTS 只计数不落库
  7. 每条 emit_sync 推 SSE 进度；401/403 立即停（避免一连串误判 + 反风控）
  8. 支持 cancel + status 查询；与 bulk_verify 全局互斥（共享 lock）

设计：
  - daemon 线程跑，进程重启丢任务记录可接受（与 bulk_verify 同口径）
  - broadcaster 由调用方注入（避免 services → api 反向依赖）
  - lock 共用 promo_eligibility_service.PROMO_VERIFY_LOCK；冲突直接抛 BusyError
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from src.data.promo_seeds import (
    COUNTRY_SUFFIXES,
    build_candidates,
    build_cross_matrix,
)
from src.db.models import EligibilityStatus
from src.promo_eligibility import check_eligibility
from src.proxy_clients.dynamic_pool import DynamicProxyPool
from src.services.event_service import EventBroadcaster
from src.services.link_template_service import create_template
from src.services.promo_eligibility_service import (
    PROMO_VERIFY_LOCK,
    PromoVerifyError,
    _resolve_proxy_url_for_country,
    _resolve_token_for_verification,
)
from src.services.proxy_provider_service import get_provider
from src.services.proxy_service import get_proxy

logger = logging.getLogger(__name__)

# 调用间隔（秒）：比 bulk_verify 的 0.5s 翻倍，给 Cloudflare 留余量
_DISCOVER_DELAY_SEC = 1.0

# 任务上限：单次发现最多扫多少条；防止误传超大字典把账号 token 烧光
_MAX_CANDIDATES = 5000

# 内存任务表（task_id -> _DiscoverState）；进程重启清空（与 bulk_verify 同口径）
_RUNNING: dict[str, "_DiscoverState"] = {}
_REGISTRY_LOCK = threading.Lock()


class BusyError(Exception):
    """已有同类长任务在跑（discover 或 bulk_verify）；API 层映射为 409。"""


class DiscoveryNotFound(Exception):
    """task_id 不存在；API 层映射为 404。"""


@dataclass
class _DiscoverState:
    """单个 discover 任务的运行时状态。"""

    task_id: str
    country: str            # cross_matrix 模式下为 "CROSS"
    mode: str               # "seeds" | "cross_matrix"
    total: int
    started_at: datetime
    cancelled: threading.Event = field(default_factory=threading.Event)

    # 进度
    processed: int = 0
    eligible_found: int = 0
    exists_found: int = 0
    not_found_count: int = 0
    error_count: int = 0

    # 终态
    finished_at: Optional[datetime] = None
    final_status: str = "running"   # running | completed | cancelled | error
    error_message: str = ""
    used_run_id: str = ""

    # ── 代理可观测（P3 引入；dynamic_provider 模式才有意义）────
    proxy_source: str = "static_proxy"   # dynamic_provider | static_proxy | direct
    proxy_rotations: int = 0             # IP 总轮换次数
    rotations_by_403: int = 0            # 因 403 主动 force_rotate 次数

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "country": self.country,
            "mode": self.mode,
            "total": self.total,
            "processed": self.processed,
            "eligible_found": self.eligible_found,
            "exists_found": self.exists_found,
            "not_found_count": self.not_found_count,
            "error_count": self.error_count,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "final_status": self.final_status,
            "error_message": self.error_message,
            "used_run_id": self.used_run_id,
            "cancelled": self.cancelled.is_set(),
            "proxy_source": self.proxy_source,
            "proxy_rotations": self.proxy_rotations,
            "rotations_by_403": self.rotations_by_403,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _acquire_promo_lock_or_raise() -> None:
    """尝试拿 PROMO_VERIFY_LOCK；拿不到抛 BusyError。

    与 bulk_verify_all_templates 共用同一把锁，互斥两类长任务避免：
      - 抢同一个 access_token 引发 ChatGPT 端会话错乱
      - Cloudflare 限流叠加

    **必须由调用方在任务结束（含异常）时调 PROMO_VERIFY_LOCK.release()**；
    discover 把锁所有权移交给 daemon 线程，daemon 的 finally 负责释放。
    """
    if not PROMO_VERIFY_LOCK.acquire(blocking=False):
        raise BusyError("已有 promo 长任务（bulk_verify 或 discover）在跑，请等待完成或先取消")

    with _REGISTRY_LOCK:
        for state in _RUNNING.values():
            if state.final_status == "running":
                # 同类型并发也拒（理论上 lock 已挡住，留作 defense-in-depth）
                PROMO_VERIFY_LOCK.release()
                raise BusyError(f"已有 discover 任务在跑: task_id={state.task_id}")


def _create_template_if_new(
    country: str,
    code: str,
    *,
    aimizy_currency: str = "",
) -> Optional[int]:
    """尝试为新发现的 ELIGIBLE 码建模板；重名则跳过返回 None。

    命名约定与 promo_import_service._norm_name 一致：promo-{country}-{code}
    """
    name = f"promo-{country.lower()}-{code.lower()}"
    try:
        result = create_template(
            name=name,
            plan="team",
            seat_quantity=2,
            promo_code=code,
            aimizy_country=country.upper(),
            aimizy_currency=aimizy_currency.upper(),
            return_mode="long",
        )
        return int(result["id"])
    except ValueError:
        # 重名（已存在）或参数校验失败 — 都视为"已收录"，跳过
        logger.debug("跳过已存在模板 name=%s", name)
        return None
    except Exception as exc:
        logger.warning("建模板失败 name=%s err=%s", name, exc)
        return None


def _country_default_currency(country: str) -> str:
    """国家 → 默认币种，借用 promo_import_service 的常量。"""
    from src.services.promo_import_service import _COUNTRY_CURRENCY
    return _COUNTRY_CURRENCY.get(country.upper(), "")


def _run_discovery(
    task_id: str,
    candidate_pairs: list[tuple[str, str]],
    broadcaster: Optional[EventBroadcaster],
    delay_sec: float,
    *,
    proxy_source: str = "static_proxy",
    proxy_provider_id: Optional[int] = None,
    static_proxy_id: Optional[int] = None,
) -> None:
    """daemon 线程入口：串行扫描候选 + 实时落库 + SSE 推送。

    Args:
        task_id: 与外层 SSE 订阅的 task_id 一致
        candidate_pairs: [(country, code), ...]，单国家模式所有 country 相同
        broadcaster: 可选；用于 SSE 推送
        delay_sec: 调用间睡眠（防 CF）
        proxy_source: 代理来源策略
            "dynamic_provider" — 走 ProxyProvider 表，DynamicProxyPool 每 N 条换 IP
            "static_proxy"     — 走 Proxy 表（固定 IP）；id None 时按国家自动选
            "direct"           — 不用代理（预计很快被 CF 403）
        proxy_provider_id: dynamic_provider 模式必填
        static_proxy_id: static_proxy 模式可选；None 时按 country 自动选
    """
    state = _RUNNING[task_id]
    state.proxy_source = proxy_source

    def _emit(event_type: str, **payload: Any) -> None:
        if broadcaster is None:
            return
        try:
            broadcaster.emit_sync(
                run_id=task_id,
                event_type=event_type,
                payload={**state.snapshot(), **payload},
            )
        except Exception as exc:  # noqa: BLE001 - SSE 失败不阻断主流程
            logger.debug("emit_sync 失败 task=%s err=%s", task_id, exc)

    # ── 准备代理来源（动态池 / 静态池一次性预取） ─────
    # dynamic_pool 字典按 country 分桶：cross_matrix 跨国时不同 country 用不同池
    # （同一池只能拉同一国家的 IP，跨国必须重置）
    dynamic_pools: dict[str, DynamicProxyPool] = {}
    dynamic_provider_dict: Optional[dict[str, Any]] = None
    if proxy_source == "dynamic_provider":
        if proxy_provider_id is None:
            state.final_status = "error"
            state.error_message = "proxy_source=dynamic_provider 时 proxy_provider_id 必填"
            _emit("discovery.error", reason="proxy_provider_id_required")
            return
        dynamic_provider_dict = get_provider(proxy_provider_id, with_secrets=True)
        if dynamic_provider_dict is None or not dynamic_provider_dict.get("is_active"):
            state.final_status = "error"
            state.error_message = (
                f"动态供应商 id={proxy_provider_id} 不存在或未启用"
            )
            _emit("discovery.error", reason="provider_not_active")
            return

    static_url_for_all: Optional[str] = None
    if proxy_source == "static_proxy" and static_proxy_id is not None:
        proxy_record = get_proxy(int(static_proxy_id), with_url=True)
        if proxy_record is None or not proxy_record.get("is_active"):
            state.final_status = "error"
            state.error_message = (
                f"静态代理 id={static_proxy_id} 不存在或未启用"
            )
            _emit("discovery.error", reason="static_proxy_not_active")
            return
        static_url_for_all = str(proxy_record.get("url") or "")

    def _acquire_proxy_url(country_code: str) -> Optional[str]:
        """按 proxy_source 取当前 (country) 应该用的代理 URL。

        - dynamic_provider: 按 country 复用 / 新建 pool，调 next_proxy_url 触发轮换
        - static_proxy:     id 指定时全程用同一个；否则按 country 走旧 fallback
        - direct:           返 None
        """
        if proxy_source == "direct":
            return None
        if proxy_source == "dynamic_provider":
            if country_code not in dynamic_pools:
                assert dynamic_provider_dict is not None
                dynamic_pools[country_code] = DynamicProxyPool(
                    provider=dynamic_provider_dict,
                    country=country_code,
                )
            pool = dynamic_pools[country_code]
            url, _country = pool.next_proxy_url()
            # 同步 stats 到 state，让 SSE 推送可观测（跨国时所有 pool 求和）
            state.proxy_rotations = sum(
                p.stats()["total_rotations"] for p in dynamic_pools.values()
            )
            state.rotations_by_403 = sum(
                p.stats()["rotations_by_403"] for p in dynamic_pools.values()
            )
            return url or None
        # static_proxy
        if static_url_for_all is not None:
            return static_url_for_all
        # static_proxy 且未指定 id → 按 country 走旧 fallback 行为
        resolved_url, _matched = _resolve_proxy_url_for_country(
            country_code, template_proxy_id=None,
        )
        return resolved_url

    def _force_rotate_current(country_code: str, *, reason: str) -> None:
        """403 时主动让该 country 的动态池换 IP；非 dynamic 模式 no-op。"""
        if proxy_source != "dynamic_provider":
            return
        pool = dynamic_pools.get(country_code)
        if pool is None:
            return
        pool.force_rotate(reason=reason)
        state.proxy_rotations = sum(p.stats()["total_rotations"] for p in dynamic_pools.values())
        state.rotations_by_403 = sum(p.stats()["rotations_by_403"] for p in dynamic_pools.values())

    try:
        # ── 一次性 resolve token（中途过期会被 401 检测到）─────
        token, used_run_id = _resolve_token_for_verification(None)
        state.used_run_id = used_run_id
        _emit("discovery.started", message=f"开始发现 {state.country}")

        last_country: Optional[str] = None

        for country, code in candidate_pairs:
            if state.cancelled.is_set():
                logger.info("discover 任务被取消 task=%s processed=%d/%d",
                            task_id, state.processed, state.total)
                state.final_status = "cancelled"
                break

            proxy_url = _acquire_proxy_url(country)

            # 国家切换时推一个事件（cross_matrix 模式下有用）
            if last_country != country:
                _emit("discovery.country_switch",
                      country=country, has_proxy=bool(proxy_url))
                last_country = country

            try:
                result = check_eligibility(
                    access_token=token,
                    code=code,
                    proxy_url=proxy_url,
                )
            except Exception as exc:  # noqa: BLE001 - 单条异常不阻断扫描
                state.processed += 1
                state.error_count += 1
                logger.warning("discover 单条异常 code=%s err=%s", code, exc)
                _emit("discovery.progress", code=code, status="error", error=str(exc))
                if state.processed < state.total:
                    time.sleep(delay_sec)
                continue

            state.processed += 1

            # ── 401: token 过期，立即停（避免后续全报 not_found 误判）──
            if result.http_status == 401:
                state.final_status = "error"
                state.error_message = "access_token 过期 (401)，已提前终止"
                _emit("discovery.error", reason="token_expired", code=code)
                break

            # ── 403: Cloudflare 拦截 ──
            # dynamic_provider 模式：主动换 IP 再试 1 次；仍 403 才真停
            # 其它模式：保持旧行为，立即停（无 IP 可换）
            if result.http_status == 403:
                if proxy_source == "dynamic_provider":
                    _force_rotate_current(country, reason="403")
                    _emit("discovery.progress",
                          code=code, status="error",
                          error="403 已触发轮换重试")
                    new_proxy_url = _acquire_proxy_url(country)
                    try:
                        retry = check_eligibility(
                            access_token=token,
                            code=code,
                            proxy_url=new_proxy_url,
                        )
                    except Exception as exc:  # noqa: BLE001
                        retry = None
                        logger.warning(
                            "discover 403 重试异常 code=%s err=%s", code, exc,
                        )
                    if retry is None or retry.http_status == 403:
                        state.final_status = "error"
                        state.error_message = (
                            "Cloudflare 持续拦截 (403)，轮换 IP 后仍失败；建议暂停或换供应商"
                        )
                        _emit("discovery.error",
                              reason="cloudflare_blocked_persistent", code=code)
                        break
                    # 重试成功 → 用 retry 结果继续后续判定
                    result = retry
                else:
                    state.final_status = "error"
                    state.error_message = (
                        "Cloudflare 拦截 (403)，已提前终止；建议换代理或等待"
                    )
                    _emit("discovery.error", reason="cloudflare_blocked", code=code)
                    break

            if result.status == EligibilityStatus.ELIGIBLE:
                state.eligible_found += 1
                tpl_id = _create_template_if_new(
                    country, code,
                    aimizy_currency=_country_default_currency(country),
                )
                _emit("discovery.eligible",
                      code=code, country=country, template_id=tpl_id)
            elif result.status == EligibilityStatus.EXISTS:
                state.exists_found += 1
                _emit("discovery.progress", code=code, status="exists")
            elif result.status == EligibilityStatus.NOT_FOUND:
                state.not_found_count += 1
            else:
                # unknown / error
                state.error_count += 1
                _emit("discovery.progress",
                      code=code, status=result.status, error=result.error)

            # 防 CF 间隔（最后一条不睡）
            if state.processed < state.total and delay_sec > 0:
                time.sleep(delay_sec)

        if state.final_status == "running":
            state.final_status = "completed"

    except PromoVerifyError as exc:
        # _resolve_token_for_verification 抛出：没有 completed 账号 / 没 token
        state.final_status = "error"
        state.error_message = f"{exc.code}: {exc.message}"
        _emit("discovery.error", reason=exc.code, message=exc.message)
    except Exception as exc:  # noqa: BLE001 - 兜底
        logger.exception("discover 任务异常 task=%s", task_id)
        state.final_status = "error"
        state.error_message = f"unexpected: {exc}"
        _emit("discovery.error", reason="unexpected", message=str(exc))
    finally:
        state.finished_at = _utc_now()
        _emit("discovery.completed")
        logger.info(
            "discover 完成 task=%s status=%s processed=%d eligible=%d exists=%d error=%d",
            task_id, state.final_status, state.processed,
            state.eligible_found, state.exists_found, state.error_count,
        )
        # 释放 promo 长任务互斥锁；锁所有权由 start_discovery 在 acquire 后移交过来。
        # 即使 daemon 异常退出，finally 保证释放，避免锁永久持有挡住后续任务。
        try:
            PROMO_VERIFY_LOCK.release()
        except RuntimeError:
            # daemon 重复释放（不应该发生）；保险起见吞掉。
            logger.warning("PROMO_VERIFY_LOCK release 异常（可能未持有） task=%s", task_id)


def start_discovery(
    country: str,
    *,
    mode: str = "seeds",
    extra_words: Iterable[str] = (),
    cross_countries: Optional[Iterable[str]] = None,
    broadcaster: Optional[EventBroadcaster] = None,
    delay_sec: float = _DISCOVER_DELAY_SEC,
    proxy_source: str = "static_proxy",
    proxy_provider_id: Optional[int] = None,
    static_proxy_id: Optional[int] = None,
) -> dict[str, Any]:
    """启动一次 promo 码发现任务。

    Args:
        country: 单国家模式下的国家码（如 "GB"）；cross_matrix 模式忽略此参数
        mode: "seeds" 用 build_candidates；"cross_matrix" 用 build_cross_matrix
        extra_words: 用户自定义关键词（会做 normalize）
        cross_countries: 仅 cross_matrix 模式生效；默认用所有有字典的国家
        broadcaster: 可选 SSE 广播器；None 则不推事件
        delay_sec: 每条调用间隔，默认 1.0s
        proxy_source: 代理来源 — dynamic_provider | static_proxy | direct
        proxy_provider_id: dynamic_provider 模式必填的 ProxyProvider id
        static_proxy_id: static_proxy 模式可选；None 时按国家自动选

    Returns:
        {"task_id": ..., "total": ..., "country": ..., "mode": ...}

    Raises:
        BusyError: 已有其他长任务在跑
        ValueError: 参数非法 / 候选为空
    """
    mode_norm = (mode or "seeds").strip().lower()
    if mode_norm not in ("seeds", "cross_matrix"):
        raise ValueError(f"mode 必须是 seeds / cross_matrix 之一，得到 {mode!r}")

    # 代理来源参数校验（早失败，避免线程里才发现）
    safe_proxy_source = (proxy_source or "static_proxy").strip().lower()
    if safe_proxy_source not in ("dynamic_provider", "static_proxy", "direct"):
        raise ValueError(
            f"proxy_source 必须是 dynamic_provider / static_proxy / direct，得到 {proxy_source!r}"
        )
    if safe_proxy_source == "dynamic_provider" and proxy_provider_id is None:
        raise ValueError("proxy_source=dynamic_provider 时 proxy_provider_id 必填")

    # 先做参数校验再拿锁，避免 ValueError 路径浪费锁占用。
    extra_words_list = [w for w in (extra_words or ()) if str(w).strip()]

    candidate_pairs: list[tuple[str, str]]
    state_country: str

    if mode_norm == "seeds":
        cc = (country or "").strip().upper()
        if cc not in COUNTRY_SUFFIXES:
            raise ValueError(f"不支持的国家码: {country!r}（需为 ISO 大写两字母）")
        codes = build_candidates(cc, extra_words=extra_words_list)
        candidate_pairs = [(cc, code) for code in codes]
        state_country = cc
    else:
        countries_list = list(cross_countries or COUNTRY_SUFFIXES.keys())
        candidate_pairs = build_cross_matrix(
            countries_list, extra_words=extra_words_list,
        )
        state_country = "CROSS"

    if not candidate_pairs:
        raise ValueError("候选码为空；检查国家码 / 字典文件 / extra_words")
    if len(candidate_pairs) > _MAX_CANDIDATES:
        raise ValueError(
            f"候选码过多 {len(candidate_pairs)} > {_MAX_CANDIDATES}；请收窄国家/关键词"
        )

    # 拿锁——daemon 启动后，锁所有权移交给 daemon 线程，由其 finally 释放。
    # 本函数返回前如果任何步骤失败（线程未真正启动），必须释放避免泄漏。
    _acquire_promo_lock_or_raise()

    try:
        task_id = uuid.uuid4().hex
        state = _DiscoverState(
            task_id=task_id,
            country=state_country,
            mode=mode_norm,
            total=len(candidate_pairs),
            started_at=_utc_now(),
        )

        with _REGISTRY_LOCK:
            _RUNNING[task_id] = state

        thread = threading.Thread(
            target=_run_discovery,
            args=(task_id, candidate_pairs, broadcaster, delay_sec),
            kwargs={
                "proxy_source": safe_proxy_source,
                "proxy_provider_id": proxy_provider_id,
                "static_proxy_id": static_proxy_id,
            },
            name=f"promo-discover-{task_id[:8]}",
            daemon=True,
        )
        thread.start()
    except BaseException:
        # 线程未启动成功 → 锁不会被 daemon 释放，本路径必须自己释放。
        try:
            PROMO_VERIFY_LOCK.release()
        except RuntimeError:
            pass
        raise

    logger.info(
        "discover 启动 task=%s country=%s mode=%s total=%d",
        task_id, state_country, mode_norm, len(candidate_pairs),
    )

    return {
        "task_id": task_id,
        "country": state_country,
        "mode": mode_norm,
        "total": len(candidate_pairs),
        "started_at": state.started_at.isoformat(),
    }


def cancel_discovery(task_id: str) -> bool:
    """请求取消任务；返回 True 表示取消标志已设置（线程会在下一条循环退出）。

    Raises:
        DiscoveryNotFound: task_id 不存在
    """
    with _REGISTRY_LOCK:
        state = _RUNNING.get(task_id)
    if state is None:
        raise DiscoveryNotFound(task_id)
    if state.final_status != "running":
        return False
    state.cancelled.set()
    return True


def get_discovery_status(task_id: str) -> dict[str, Any]:
    """查询单个任务的快照。"""
    with _REGISTRY_LOCK:
        state = _RUNNING.get(task_id)
    if state is None:
        raise DiscoveryNotFound(task_id)
    return state.snapshot()


def list_recent_discoveries(limit: int = 10) -> list[dict[str, Any]]:
    """按 started_at 倒序列最近 N 个任务（含已完成）。"""
    with _REGISTRY_LOCK:
        states = sorted(
            _RUNNING.values(),
            key=lambda s: s.started_at,
            reverse=True,
        )[: max(1, int(limit or 10))]
    return [s.snapshot() for s in states]


__all__ = [
    "start_discovery",
    "cancel_discovery",
    "get_discovery_status",
    "list_recent_discoveries",
    "BusyError",
    "DiscoveryNotFound",
]
