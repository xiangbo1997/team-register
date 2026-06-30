# -*- coding: utf-8 -*-
"""promo 码导入服务

把 gpt-promo-scanner/known_codes.json 里的促销码批量导入 LinkTemplate 表。

设计要点：
  - 同源真相：脚本（scripts/import_promo_codes_from_known.py）与 API（POST /api/link-templates/import-from-scanner）共用同一个 import_from_known_codes()
  - 幂等：按 LinkTemplate.name 去重（命名约定 promo-{country}-{code}）
  - 失败容错：单条失败不阻断整体导入，最后汇总返回
  - 来源信息存入 last_eligibility_metadata.import_note，不污染主字段
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import LinkTemplate
from src.services.link_template_service import create_template

logger = logging.getLogger(__name__)

# 默认源路径 = workspace 同级 gpt-promo-scanner 项目
DEFAULT_KNOWN_CODES_PATH = Path("/Volumes/workSpace/study/aiProject/gpt-promo-scanner/known_codes.json")

# 国家 → 币种 fallback（known_codes.json 用 price_local 字符串无法直接拆币种）
_COUNTRY_CURRENCY: dict[str, str] = {
    "US": "USD", "GB": "GBP", "AU": "AUD", "BR": "BRL", "CA": "CAD",
    "DE": "EUR", "ES": "EUR", "FR": "EUR", "IN": "INR", "JP": "JPY",
    "KE": "USD", "NG": "NGN", "NZ": "NZD", "ZA": "ZAR", "TH": "THB",
    "SG": "SGD", "PH": "PHP",
}


def _norm_name(country: str, code: str) -> str:
    """命名约定：promo-{country_lower}-{code_lower}；保证全局唯一前缀避免与用户手动命名冲突。"""
    return f"promo-{country.lower()}-{code.lower()}"


def _iter_valid(payload: dict):
    """yield (country, item_dict) 来自 payload.valid"""
    valid = payload.get("valid") or {}
    for country, items in valid.items():
        country_upper = country.upper()
        for item in items or []:
            yield country_upper, item


def _iter_expired(payload: dict):
    """yield (country, item_dict) 来自 payload.expired；过期码用 region 字段而非 country key"""
    for item in payload.get("expired") or []:
        country = str(item.get("region") or "").strip().upper()
        yield country, item


def _iter_all_valid_names(payload: dict):
    """yield 所有 (country, code, name) 三元组（valid + expired），跳过 country / code 为空的不合规项。

    主循环和 skipped_existing 计数共用这个过滤器，保证统计口径一致。
    """
    for country, item in list(_iter_valid(payload)) + list(_iter_expired(payload)):
        code = str(item.get("code") or "").strip()
        if not code or not country:
            continue
        yield country, code, _norm_name(country, code)


def _existing_names() -> set[str]:
    """一次查所有 LinkTemplate.name 做去重。"""
    with get_session() as session:
        rows = list(session.exec(select(LinkTemplate)).all())
        return {r.name for r in rows}


def _existing_business_keys() -> set[tuple[str, str]]:
    """一次查所有 (country_upper, code_lower) 业务键。

    用于去重早期手工建的"别名条目"（如 'CA promo datroaica' 与 'promo-ca-datroaica'
    name 不同但 (country, code) 相同 → 应视为重复，避免一份码导入产生多条模板）。
    """
    with get_session() as session:
        rows = list(session.exec(
            select(LinkTemplate).where(LinkTemplate.promo_code != "")
        ).all())
        return {
            ((r.aimizy_country or "").upper(), (r.promo_code or "").lower())
            for r in rows
        }


def _build_import_note(item: dict, source: str) -> str:
    """生成 import_note：包含来源、公司、价格/过期备注，存到 last_eligibility_metadata.import_note。"""
    parts = [f"source={source}"]
    company = str(item.get("company") or "").strip()
    if company:
        parts.append(f"company={company}")
    if source == "valid":
        price_usd = item.get("price_usd")
        price_local = str(item.get("price_local") or "").strip()
        if price_usd is not None:
            parts.append(f"price_usd={price_usd}")
        if price_local:
            parts.append(f"price_local={price_local}")
        discount = item.get("discount_pct")
        if discount is not None:
            parts.append(f"discount={discount}%")
        months = item.get("duration_months")
        if months is not None:
            parts.append(f"months={months}")
    else:
        note = str(item.get("note") or "").strip()
        if note:
            parts.append(f"note={note}")
    return " | ".join(parts)


def _write_import_note(template_id: int, note: str) -> None:
    """把 import_note 写入 last_eligibility_metadata；不存在则创建 dict。

    顺带抽 P4 结构化字段（导入码的折扣力度来自 import_note 文本），让导入的码
    在号池升级弹窗里也能按折扣排序/显示，而不必等单独 verify。
    """
    if not note:
        return
    with get_session() as session:
        tpl = session.get(LinkTemplate, template_id)
        if tpl is None:
            return
        metadata = dict(tpl.last_eligibility_metadata or {})
        metadata["import_note"] = note
        tpl.last_eligibility_metadata = metadata

        # P4：从 import_note 抽折扣力度字段（容错抽取器同时认 note 和 API metadata）
        from src.services.promo_metadata_extract import extract_promo_fields
        fields = extract_promo_fields(metadata)
        if fields["percent_off"] is not None:
            tpl.promo_percent_off = fields["percent_off"]
        if fields["duration_months"] is not None:
            tpl.promo_duration_months = fields["duration_months"]

        session.add(tpl)
        session.commit()


def import_from_known_codes(
    source_path: Optional[Path] = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """从 known_codes.json 导入促销码到 LinkTemplate 表。

    Args:
        source_path: known_codes.json 路径；None 时用 DEFAULT_KNOWN_CODES_PATH
        dry_run: True 时只计划不写库

    Returns:
        {
          "source": str,                 # 实际使用的源路径
          "planned": int,                # 计划新建条数
          "skipped_existing": int,       # 已存在跳过条数
          "created": int,                # 真正写库成功条数（dry_run 时为 0）
          "failed": list[{name, error}], # 写库失败明细
          "dry_run": bool,
          "details": list[{name, country, code, source}],  # 计划/已创建条目摘要
        }

    Raises:
        FileNotFoundError: 源文件不存在
        json.JSONDecodeError: 源文件 JSON 格式错误
    """
    src = source_path or DEFAULT_KNOWN_CODES_PATH
    if not src.exists():
        raise FileNotFoundError(f"known_codes.json 不存在: {src}")

    payload = json.loads(src.read_text(encoding="utf-8"))
    return _import_payload(payload, source_label=str(src), dry_run=dry_run)


def import_from_uploaded(raw: bytes, *, dry_run: bool = False) -> dict[str, Any]:
    """从上传的 JSON 字节流导入（不依赖服务器本地文件）。

    与 import_from_known_codes 共用 _import_payload 核心逻辑（同源真相）。

    Args:
        raw: 上传文件的原始字节（known_codes.json 同结构）
        dry_run: True 时只计划不写库

    Returns:
        同 import_from_known_codes

    Raises:
        ValueError: JSON 解析失败 / 结构非法（调用方映射成 400）
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"文件不是合法 UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象（含 valid / expired 字段）")
    if "valid" not in payload and "expired" not in payload:
        raise ValueError("JSON 缺少 valid / expired 字段，格式与 known_codes.json 不符")
    return _import_payload(payload, source_label="upload", dry_run=dry_run)


def _import_payload(
    payload: dict, *, source_label: str, dry_run: bool = False
) -> dict[str, Any]:
    """核心导入逻辑：吃一个 payload dict，计划 + 去重 + 写库。

    被 import_from_known_codes（读本地文件）和 import_from_uploaded（上传）共用。
    """
    existing = _existing_names()
    existing_keys = _existing_business_keys()

    plan: list[dict] = []

    def _is_duplicate(country: str, code: str, name: str) -> bool:
        """同时按 name 与 (country, code) 业务键去重。

        早期手工建的"别名条目"（name='CA promo datroaica'）与新规范名
        ('promo-ca-datroaica') name 不同但实际是同一业务码 → 视为重复。
        """
        if name in existing:
            return True
        key = (country.upper(), code.lower())
        return key in existing_keys

    # ── valid 部分 ─────────────────────────────────
    for country, item in _iter_valid(payload):
        code = str(item.get("code") or "").strip()
        if not code or not country:
            continue
        name = _norm_name(country, code)
        if _is_duplicate(country, code, name):
            continue
        plan.append({
            "name": name,
            "country": country,
            "code": code,
            "currency": _COUNTRY_CURRENCY.get(country, ""),
            "source": "valid",
            "_raw": item,
        })

    # ── expired 部分 ──────────────────────────────
    for country, item in _iter_expired(payload):
        code = str(item.get("code") or "").strip()
        if not code or not country:
            continue
        name = _norm_name(country, code)
        if _is_duplicate(country, code, name):
            continue
        plan.append({
            "name": name,
            "country": country,
            "code": code,
            "currency": _COUNTRY_CURRENCY.get(country, ""),
            "source": "expired",
            "_raw": item,
        })

    # 统一过滤口径：和主循环用同一个 _iter_all_valid_names()，
    # 避免空 country / 空 code 项被算进 skipped_existing 造成数字虚高。
    # 跳过既包括 name 命中也包括 (country, code) 业务键命中（覆盖早期别名）。
    skipped_existing = 0
    for c, code_str, name in _iter_all_valid_names(payload):
        if name in existing or (c.upper(), code_str.lower()) in existing_keys:
            skipped_existing += 1

    result: dict[str, Any] = {
        "source": source_label,
        "planned": len(plan),
        "skipped_existing": skipped_existing,
        "created": 0,
        "failed": [],
        "dry_run": dry_run,
        "details": [
            {"name": p["name"], "country": p["country"], "code": p["code"], "source": p["source"]}
            for p in plan
        ],
    }

    if dry_run:
        logger.info("import dry-run: planned=%d skipped=%d", len(plan), skipped_existing)
        return result

    # ── 真正写库 ──────────────────────────────────
    for entry in plan:
        try:
            tpl = create_template(
                name=entry["name"],
                plan="team",
                seat_quantity=2,
                promo_code=entry["code"],
                promo_campaign_id="",
                aimizy_country=entry["country"],
                aimizy_currency=entry["currency"],
                workspace_name="",
                return_mode="long",
                proxy_id=None,
            )
            note = _build_import_note(entry["_raw"], entry["source"])
            _write_import_note(tpl["id"], note)
            result["created"] += 1
        except Exception as exc:
            logger.warning("导入失败 name=%s err=%s", entry["name"], exc)
            result["failed"].append({"name": entry["name"], "error": str(exc)})

    logger.info(
        "import 完成: created=%d failed=%d skipped=%d",
        result["created"], len(result["failed"]), skipped_existing,
    )
    return result


__all__ = ["import_from_known_codes", "import_from_uploaded", "DEFAULT_KNOWN_CODES_PATH"]
