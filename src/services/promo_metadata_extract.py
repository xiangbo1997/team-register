# -*- coding: utf-8 -*-
"""promo metadata 结构化字段抽取（P4）。

从 LinkTemplate.last_eligibility_metadata（ChatGPT promotions API 的原始响应，
或导入时写的 import_note）里抽出 5 个结构化字段，供号池升级弹窗按折扣力度排序/展示：
  - promo_percent_off       折扣百分比（0-100 int）
  - promo_duration_months   折扣月数
  - promo_expires_at        过期时间（datetime, UTC）
  - promo_max_redemptions   最大兑换次数
  - promo_applicable_plans  适用计划 CSV（如 "team,plus"）

设计：**多路径容错**——ChatGPT 真实 metadata schema 尚未拿到稳定样本（活码难找 + CF
限流），所以不赌单一字段路径，而是依次尝试业界/历史已知的多种结构，哪个命中用哪个：
  1. metadata.discount.{percent_off, duration_in_months, ...}（Stripe coupon 风格）
  2. metadata.promotion.discount.* / promo_code_metadata.discount.*（嵌套变体）
  3. metadata.discount.value（项目 seed / 测试占位结构）
  4. import_note 文本（导入时写的 "discount=50% | months=12 | ..."）

真实 schema 一旦确认，只需在 _PERCENT_PATHS 等列表里补一条路径即可，不改抽取逻辑。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── 各字段的候选路径（按优先级，命中即停）──────────────────
# 路径用点号分隔；遇到 list 时取第 0 个元素（如 applicable_products[0]）。
_PERCENT_PATHS = [
    "metadata.discount.percent_off",
    "metadata.promotion.discount.percent_off",
    "promo_code_metadata.discount.percent_off",
    "metadata.discount.value",            # seed / 测试占位
    "metadata.percent_off",
]
_DURATION_PATHS = [
    "metadata.discount.duration_in_months",
    "metadata.promotion.discount.duration_in_months",
    "promo_code_metadata.discount.duration_in_months",
    "metadata.duration_in_months",
    "metadata.discount.duration_months",
]
_EXPIRES_PATHS = [
    "metadata.discount.expires_at",
    "metadata.promotion.discount.expires_at",
    "promo_code_metadata.discount.expires_at",
    "metadata.expires_at",
    "metadata.promotion.expires_at",
]
_MAX_REDEMPTIONS_PATHS = [
    "metadata.discount.max_redemptions",
    "metadata.promotion.discount.max_redemptions",
    "promo_code_metadata.discount.max_redemptions",
    "metadata.max_redemptions",
]
_PLANS_PATHS = [
    "metadata.discount.plan",
    "metadata.promotion.plan_type",
    "promo_code_metadata.discount.plan",
    "metadata.plan_type",
    "metadata.applicable_plans",
    "metadata.discount.applicable_products",
]


def _dig(data: Any, path: str) -> Any:
    """按点号路径取值；遇 list 取第 0 个；任何一步缺失返回 None。"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            if not cur:
                return None
            cur = cur[0]
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first_hit(data: dict, paths: list[str]) -> Any:
    """依次试各路径，返回第一个非 None 命中值。"""
    for p in paths:
        v = _dig(data, p)
        if v is not None:
            return v
    return None


def _coerce_int(v: Any) -> Optional[int]:
    """转 int；浮点（如 percent_off=100.0）截断；失败返回 None。"""
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _coerce_expires(v: Any) -> Optional[datetime]:
    """过期时间归一化为 UTC datetime。

    支持：Unix 时间戳（int/float 秒）、ISO 8601 字符串。失败返回 None。
    """
    if v is None:
        return None
    # Unix 时间戳
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # ISO 字符串
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # 纯数字字符串也当时间戳
        if s.isdigit():
            return _coerce_expires(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _coerce_plans(v: Any) -> str:
    """适用计划归一化为 CSV 字符串。

    支持：字符串（"chatgpt-team" → "team"）、list（取每项归一）、
    含 'team'/'plus'/'pro' 关键词的提取。失败返回 ""。
    """
    if v is None:
        return ""
    items: list[str] = []
    if isinstance(v, str):
        items = [v]
    elif isinstance(v, list):
        items = [str(x) for x in v if x]
    else:
        return ""
    plans: list[str] = []
    for raw in items:
        low = raw.lower()
        for plan in ("team", "plus", "pro"):
            if plan in low and plan not in plans:
                plans.append(plan)
    # 没匹配到关键词就原样保留（去 chatgpt- 前缀）
    if not plans and items:
        cleaned = [x.replace("chatgpt-", "").strip() for x in items if x.strip()]
        return ",".join(cleaned)[:120]
    return ",".join(plans)


# import_note 文本格式（promo_import_service._build_import_note）：
# "source=valid | company=X | price_usd=25 | discount=50% | months=12"
_NOTE_DISCOUNT_RE = re.compile(r"discount=(\d+(?:\.\d+)?)\s*%")
_NOTE_MONTHS_RE = re.compile(r"months=(\d+)")


def _extract_from_import_note(note: str) -> dict[str, Any]:
    """从 import_note 文本抽折扣% / 月数（导入码的唯一力度来源）。"""
    out: dict[str, Any] = {}
    if not note:
        return out
    m = _NOTE_DISCOUNT_RE.search(note)
    if m:
        out["percent_off"] = _coerce_int(m.group(1))
    m = _NOTE_MONTHS_RE.search(note)
    if m:
        out["duration_months"] = _coerce_int(m.group(1))
    return out


def extract_promo_fields(metadata: Optional[dict]) -> dict[str, Any]:
    """从 last_eligibility_metadata 抽 5 个结构化字段。

    Args:
        metadata: LinkTemplate.last_eligibility_metadata（dict 或 None）

    Returns:
        {
          "percent_off": int | None,
          "duration_months": int | None,
          "expires_at": datetime | None,
          "max_redemptions": int | None,
          "applicable_plans": str,          # CSV，可能为 ""
        }
        全部抽不到时各字段为 None / ""（调用方据此判断要不要写库）。
    """
    if not isinstance(metadata, dict):
        return {
            "percent_off": None, "duration_months": None,
            "expires_at": None, "max_redemptions": None,
            "applicable_plans": "",
        }

    result = {
        "percent_off": _coerce_int(_first_hit(metadata, _PERCENT_PATHS)),
        "duration_months": _coerce_int(_first_hit(metadata, _DURATION_PATHS)),
        "expires_at": _coerce_expires(_first_hit(metadata, _EXPIRES_PATHS)),
        "max_redemptions": _coerce_int(_first_hit(metadata, _MAX_REDEMPTIONS_PATHS)),
        "applicable_plans": _coerce_plans(_first_hit(metadata, _PLANS_PATHS)),
    }

    # API 路径没抽到的字段，从 import_note 文本兜底（导入码场景）
    note = metadata.get("import_note")
    if isinstance(note, str) and note:
        note_fields = _extract_from_import_note(note)
        if result["percent_off"] is None and "percent_off" in note_fields:
            result["percent_off"] = note_fields["percent_off"]
        if result["duration_months"] is None and "duration_months" in note_fields:
            result["duration_months"] = note_fields["duration_months"]

    return result
