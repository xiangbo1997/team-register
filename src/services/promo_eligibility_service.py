# -*- coding: utf-8 -*-
"""promo eligibility 业务编排

把以下三件事拼起来：
  1. 从 LinkTemplate 取 promo_code + aimizy_country
  2. 通过 proxy_service.get_active_proxy_by_country(country) 选代理出口
  3. 复用某个现有 Run 的 access_token（_resolve_access_token），调 promo_eligibility.check_eligibility
  4. 把验证结果回写到 LinkTemplate.last_eligibility_* 三个字段

设计约束：
  - **不**新建表存 promo 状态 —— 复用 LinkTemplate
  - **不**新建 token 管理 —— 复用 account_pool_service._resolve_access_token
  - **不**新建代理逻辑 —— 复用 proxy_service.get_active_proxy_by_country
  - 失败时 status 写 "error"，error 字段写进 metadata，便于 dashboard 排查
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Any, Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import EligibilityStatus, LinkTemplate, Run
from src.promo_eligibility import EligibilityResult, check_eligibility
from src.services.account_pool_service import _resolve_access_token
from src.services.proxy_service import (
    get_active_proxy_by_country,
    get_proxy,
    list_proxies,
)

# 批量验证调用间默认间隔（秒）；ChatGPT promotions API 短时间高频会触发 Cloudflare
_BULK_DELAY_SEC = 0.5

# 全局互斥锁：bulk_verify_all_templates 与 code_discovery_service 共享一把锁，
# 防止两个长任务同时跑（会抢同一个 access_token、叠加 Cloudflare 限流）。
# code_discovery_service 通过 acquire(blocking=False) 探测占用状态。
PROMO_VERIFY_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PromoVerifyError(Exception):
    """前置检查失败（找不到模板 / token 缺失 / 代理缺失）—— API 层应映射成 4xx。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _resolve_token_for_verification(run_id: Optional[str]) -> tuple[str, str]:
    """选一个 Run 的 access_token 用来验证 promo 码。

    Args:
        run_id: 用户在 dashboard 指定的运维用账号；None 时取最近一个成功注册的账号

    Returns:
        (access_token, used_run_id)；token 为空时由调用方抛 PromoVerifyError
    """
    with get_session() as session:
        run: Optional[Run] = None
        if run_id:
            run = session.get(Run, run_id)
            if run is None:
                raise PromoVerifyError("run_not_found", f"run_id 不存在: {run_id}")
        else:
            # 兜底：取最近一个 status='success' 的 Run（worker.py 写入的终态）。
            # 按 created_at 排序而非 id —— Run.id 是 UUID 字符串，字典序≠时间序。
            run = session.exec(
                select(Run)
                .where(Run.status == "success")
                .order_by(Run.created_at.desc())  # type: ignore[arg-type]
            ).first()
            if run is None:
                raise PromoVerifyError(
                    "no_account",
                    "数据库里没有 status='success' 的账号，无法借用 token；"
                    "请先完成至少一次注册，或在 dashboard 上手动指定 run_id"
                )

        token = _resolve_access_token(run)
        if not token:
            raise PromoVerifyError(
                "no_token",
                f"账号 {run.email!r} 上找不到 access_token（既不在 openai_tokens 也不在 config_snapshot/accounts.csv）"
            )
        return token, run.id


def _resolve_proxy_url_for_country(
    country: str, *, template_proxy_id: Optional[int]
) -> tuple[Optional[str], bool]:
    """选代理出口。

    优先级：
      1. LinkTemplate.proxy_id（用户已显式绑定）→ (url, True)
      2. proxy_service.get_active_proxy_by_country(country) 选第一个 active → (url, True)
      3. **fallback**：任意 active 代理 → (url, False)
      4. 都没有 → (None, False)，client 端会用直连

    理由：ChatGPT promo eligibility API 不要求"代理国家匹配 promo 国家"——
    CF 边界层只看 IP 干净度；业务层用 user_not_eligible (status=exists) 表达
    地区不匹配，**不是 error**。所以任意干净 IP 都能拿到业务结论，比直连被
    CF 403 强得多。country_matched=False 意味着结论可能是 exists 而非
    eligible，运营据此过滤即可。

    Returns:
        (url, country_matched)；url 可能为 None；country_matched=False 表示
        用了 fallback 或没代理，需要业务侧关注地区匹配性
    """
    if template_proxy_id is not None:
        proxy = get_proxy(template_proxy_id, with_url=True)
        if proxy and proxy.get("url"):
            return str(proxy["url"]), True  # 显式指定视为 matched
        logger.warning("LinkTemplate.proxy_id=%s 已失效 (代理被删/无 url)，回退按国家选",
                       template_proxy_id)

    if country:
        proxy = get_active_proxy_by_country(country, with_url=True)
        if proxy and proxy.get("url"):
            return str(proxy["url"]), True

    # fallback：任意 active 代理；避免无对应国家代理时走直连必 403
    for p in list_proxies(include_inactive=False):
        full = get_proxy(p["id"], with_url=True)
        if full and full.get("url"):
            logger.info(
                "country=%s 无对应国家代理，fallback 到 id=%s country=%s",
                country, p["id"], p.get("country") or "(unset)",
            )
            return str(full["url"]), False

    return None, False


def _result_to_metadata_dict(
    result: EligibilityResult,
    used_run_id: str,
    proxy_country: str,
    proxy_resolved: bool,
    proxy_country_matched: bool,
) -> dict:
    """把 EligibilityResult 序列化为可入库 JSON。

    保留：原始 metadata、reason_code/message、http_status、error、本次用的运维信息。

    诊断字段含义：
      proxy_resolved=False        → 没用代理（直连，必被 CF 403）
      proxy_resolved=True + matched=True   → 用了对应国家代理（理想，结论可信）
      proxy_resolved=True + matched=False  → fallback 到其他国家代理；status=exists
                                              可能仅因地区不匹配，不能直接判定无效
    """
    base: dict[str, Any] = {
        "reason_code": result.reason_code,
        "reason_message": result.reason_message,
        "http_status": result.http_status,
        "error": result.error,
        "used_run_id": used_run_id,
        "proxy_country": proxy_country,
        "proxy_resolved": proxy_resolved,
        "proxy_country_matched": proxy_country_matched,
    }
    if result.metadata_raw:
        base["metadata"] = result.metadata_raw
    return base


def _write_back_eligibility_result(
    template_id: int,
    result: EligibilityResult,
    *,
    used_run_id: str,
    proxy_country: str,
    proxy_resolved: bool,
    proxy_country_matched: bool,
) -> tuple[datetime, dict]:
    """回写 eligibility 结果到 LinkTemplate.last_eligibility_*。

    幂等保留 import_note 等历史 metadata 字段（这是 verify_link_template 和
    bulk_verify_all_templates 共同的行为约束 —— 防止单条/批量验证抹掉导入备注）。

    Args:
        template_id: 目标模板 id
        result: client 返回的 EligibilityResult
        used_run_id: 本次借用的 Run id（写进 metadata）
        proxy_country: 本次模板的国家码（写进 metadata，便于历史回溯）
        proxy_resolved: 是否真选到代理（False = 直连，写进 metadata 供运维诊断）
        proxy_country_matched: 是否用了对应国家代理（False = fallback，status=exists
                               可能仅因地区不匹配；status=eligible 时此字段无影响）

    Returns:
        (now, metadata_dict)；并发删除时 metadata_dict 仍返回，但不写库
    """
    now = _utc_now()
    metadata_dict = _result_to_metadata_dict(
        result,
        used_run_id=used_run_id,
        proxy_country=proxy_country,
        proxy_resolved=proxy_resolved,
        proxy_country_matched=proxy_country_matched,
    )
    with get_session() as session:
        tpl = session.get(LinkTemplate, template_id)
        if tpl is None:
            # 并发删除：回写时已没了模板，跳过 DB 写但仍返回 metadata 给调用方
            logger.warning("回写时 template_id=%s 已被删除，跳过 DB 更新", template_id)
            return now, metadata_dict

        # 保留 import_note 等历史 metadata 字段（首次导入后单独验证不应抹掉来源备注）
        existing_meta = dict(tpl.last_eligibility_metadata or {})
        if "import_note" in existing_meta:
            metadata_dict["import_note"] = existing_meta["import_note"]

        tpl.last_eligibility_status = result.status
        tpl.last_eligibility_check_at = now
        tpl.last_eligibility_metadata = metadata_dict
        session.add(tpl)
        session.commit()
    return now, metadata_dict


def verify_link_template(template_id: int, *, run_id: Optional[str] = None) -> dict:
    """验证某个 LinkTemplate 的 promo_code 当前是否可用。

    Args:
        template_id: LinkTemplate 主键
        run_id: 可选，借用哪个账号的 access_token 调 ChatGPT API；
                None 时取最近 status='success' 的 Run

    Returns:
        dict 包含：template_id、status、checked_at、metadata、used_run_id

    Raises:
        PromoVerifyError: 前置检查失败（模板不存在 / 没有可用 token / promo_code 为空）
    """
    # 1. 取模板
    with get_session() as session:
        tpl = session.get(LinkTemplate, template_id)
        if tpl is None:
            raise PromoVerifyError("template_not_found", f"模板不存在: id={template_id}")
        if not tpl.promo_code:
            raise PromoVerifyError(
                "no_promo_code",
                f"模板 {tpl.name!r} 没有 promo_code，无需验证"
            )
        promo_code = tpl.promo_code
        country = tpl.aimizy_country or ""
        template_proxy_id = tpl.proxy_id

    # 2. 选 token + 代理
    token, used_run_id = _resolve_token_for_verification(run_id)
    proxy_url, country_matched = _resolve_proxy_url_for_country(
        country, template_proxy_id=template_proxy_id,
    )
    proxy_resolved = bool(proxy_url)

    logger.info(
        "promo eligibility 开始 template_id=%s code=%s country=%s used_run=%s proxy=%s matched=%s",
        template_id, promo_code, country,
        used_run_id[:12] if used_run_id else "",
        "yes" if proxy_resolved else "none",
        country_matched,
    )

    # 3. 调 client
    result = check_eligibility(access_token=token, code=promo_code, proxy_url=proxy_url)

    # 4. 回写 DB（共享逻辑保留 import_note）
    now, metadata_dict = _write_back_eligibility_result(
        template_id, result,
        used_run_id=used_run_id,
        proxy_country=country,
        proxy_resolved=proxy_resolved,
        proxy_country_matched=country_matched,
    )

    logger.info(
        "promo eligibility 完成 template_id=%s code=%s status=%s",
        template_id, promo_code, result.status,
    )

    return {
        "template_id": template_id,
        "promo_code": promo_code,
        "status": result.status,
        "checked_at": now.isoformat(),
        "metadata": metadata_dict,
        "used_run_id": used_run_id,
    }


def bulk_verify_all_templates(
    *,
    run_id: Optional[str] = None,
    delay_sec: float = _BULK_DELAY_SEC,
    stop_on_token_error: bool = True,
) -> dict[str, Any]:
    """批量验证所有 promo_code 非空的 LinkTemplate。

    Args:
        run_id: 借哪个账号的 access_token；None 时取最近 status='success'
        delay_sec: 每两次调用间睡眠秒数（防 Cloudflare 限流），默认 0.5s
        stop_on_token_error: True 时遇到 401（token 失效）立即停止，避免一连串误判 not_found

    Returns:
        {
          "total": int,            # 实际尝试验证的模板数
          "verified": int,         # 成功调通 ChatGPT API 的数量（含 eligible/exists/not_found）
          "by_status": {status: count},
          "failed": list[{template_id, name, error}],  # 调用异常或前置失败
          "used_run_id": str,
          "stopped_early": bool,   # token 失效提前停时为 True
        }

    Raises:
        PromoVerifyError: 前置检查失败（没有可用 token / 没有任何 promo 模板 / 已有任务在跑）
    """
    # 0. 与 code_discovery_service 互斥；持锁直到函数返回
    if not PROMO_VERIFY_LOCK.acquire(blocking=False):
        raise PromoVerifyError(
            "busy",
            "已有 promo 长任务（bulk_verify 或 discover）在跑，请等待完成或先取消"
        )
    try:
        # 1. 提前 resolve token，避免每条 verify 都查一遍 DB
        token, used_run_id = _resolve_token_for_verification(run_id)

        # 2. 取所有有 promo_code 的模板
        with get_session() as session:
            templates = list(session.exec(
                select(LinkTemplate)
                .where(LinkTemplate.promo_code != "")  # type: ignore[arg-type]
                .order_by(LinkTemplate.aimizy_country, LinkTemplate.promo_code)  # type: ignore
            ).all())

        if not templates:
            raise PromoVerifyError("no_promo_templates", "没有任何带 promo_code 的模板，无需验证")

        result: dict[str, Any] = {
            "total": len(templates),
            "verified": 0,
            "by_status": {},
            "failed": [],
            "used_run_id": used_run_id,
            "stopped_early": False,
        }

        for idx, tpl in enumerate(templates):
            try:
                template_id = tpl.id
                promo_code = tpl.promo_code
                country = tpl.aimizy_country or ""
                template_proxy_id = tpl.proxy_id

                proxy_url, country_matched = _resolve_proxy_url_for_country(
                    country, template_proxy_id=template_proxy_id,
                )
                proxy_resolved = bool(proxy_url)

                api_result = check_eligibility(access_token=token, code=promo_code, proxy_url=proxy_url)

                # 回写 DB（与 verify_link_template 共用 _write_back_eligibility_result，
                # 该函数已处理 import_note 保留 + 并发删除的情况）
                _write_back_eligibility_result(
                    template_id, api_result,
                    used_run_id=used_run_id,
                    proxy_country=country,
                    proxy_resolved=proxy_resolved,
                    proxy_country_matched=country_matched,
                )

                result["verified"] += 1
                result["by_status"][api_result.status] = result["by_status"].get(api_result.status, 0) + 1

                # token 失效检测：401 的 error 字段含 "access_token 无效或过期"
                if stop_on_token_error and api_result.status == EligibilityStatus.ERROR and api_result.http_status == 401:
                    logger.warning("批量验证中检测到 token 失效 (401)，提前停止；已处理 %d/%d", idx + 1, len(templates))
                    result["stopped_early"] = True
                    break

            except Exception as exc:
                logger.warning("批量验证单条失败 template_id=%s name=%s err=%s", tpl.id, tpl.name, exc)
                result["failed"].append({
                    "template_id": tpl.id,
                    "name": tpl.name,
                    "error": str(exc),
                })

            # 防限流间隔（最后一条不睡）
            if idx < len(templates) - 1 and delay_sec > 0:
                time.sleep(delay_sec)

        logger.info(
            "批量验证完成 total=%d verified=%d failed=%d by_status=%s stopped_early=%s",
            result["total"], result["verified"], len(result["failed"]),
            result["by_status"], result["stopped_early"],
        )
        return result
    finally:
        PROMO_VERIFY_LOCK.release()


__all__ = [
    "verify_link_template",
    "bulk_verify_all_templates",
    "PromoVerifyError",
    "PROMO_VERIFY_LOCK",
]
