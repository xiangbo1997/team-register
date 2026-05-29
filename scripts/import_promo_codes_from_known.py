# -*- coding: utf-8 -*-
"""把 gpt-promo-scanner/known_codes.json 里历史验证过的 promo 码导入 LinkTemplate 表。

来源文件：/Volumes/workSpace/study/aiProject/gpt-promo-scanner/known_codes.json
含两部分：
  - valid: { "US": [{code, company, price_usd, ...}], "GB": [...], ... }
  - expired: [{code, region, company, note}]

导入策略（每个码 = 一条 LinkTemplate 行）：
  - name 命名约定: `promo-{country}-{code}` —— 全局唯一前缀避免与已有模板冲突
  - plan="team"（promo-scanner 项目都是 ChatGPT Team 码）
  - seat_quantity=2（原 README "2 人席位月付税前实付"）
  - aimizy_country / aimizy_currency 从 JSON 取
  - promo_code 直接填，promo_campaign_id 留空（不动 team-1-month-free 默认）
  - notes 记入 last_eligibility_metadata.import_note（不是 LinkTemplate 主字段）

幂等：
  - 按 name 判重，已存在的跳过（不覆盖用户手动改动）
  - --apply 默认 dry-run 模式；带 --apply 才真正写库

用法:
    python scripts/import_promo_codes_from_known.py            # dry-run
    python scripts/import_promo_codes_from_known.py --apply    # 真写库
    python scripts/import_promo_codes_from_known.py --apply --source /custom/path/known_codes.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# 允许脚本从仓库根目录运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.engine import get_session  # noqa: E402
from src.db.models import LinkTemplate  # noqa: E402
from src.services.link_template_service import create_template  # noqa: E402

logger = logging.getLogger("import_promo_codes")

_DEFAULT_SOURCE = "/Volumes/workSpace/study/aiProject/gpt-promo-scanner/known_codes.json"

# 国家 → 币种（valid 部分 price_local 是字符串没法直接拆，用映射表兜底）
# 大部分国家 known_codes.json 里的样本足够推断，这里给出 fallback
_COUNTRY_CURRENCY = {
    "US": "USD", "GB": "GBP", "AU": "AUD", "BR": "BRL", "CA": "CAD",
    "DE": "EUR", "ES": "EUR", "FR": "EUR", "IN": "INR", "JP": "JPY",
    "KE": "USD", "NG": "NGN", "NZ": "NZD", "ZA": "ZAR", "TH": "THB",
    "SG": "SGD", "PH": "PHP",
}


def _norm_name(country: str, code: str) -> str:
    """模板名命名约定：`promo-{country_lower}-{code_lower}`，保证全局唯一前缀"""
    return f"promo-{country.lower()}-{code.lower()}"


def _expired_to_country(item: dict) -> str:
    """过期码 JSON 用的是 'region' 字段，统一转成 ISO 大写"""
    return str(item.get("region") or "").strip().upper()


def _iter_valid(payload: dict):
    """yield (country, item_dict)"""
    valid = payload.get("valid") or {}
    for country, items in valid.items():
        country_upper = country.upper()
        for item in items or []:
            yield country_upper, item


def _iter_expired(payload: dict):
    """yield (country, item_dict)"""
    for item in payload.get("expired") or []:
        yield _expired_to_country(item), item


def _existing_names() -> set:
    """一次查所有 LinkTemplate.name 做去重"""
    with get_session() as session:
        rows = session.exec(LinkTemplate.__table__.select()).all()
        return {r.name for r in rows}


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=_DEFAULT_SOURCE,
        help=f"known_codes.json 路径（默认 {_DEFAULT_SOURCE}）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；缺省时为 dry-run",
    )
    args = parser.parse_args()

    src = Path(args.source)
    if not src.exists():
        logger.error("源文件不存在: %s", src)
        sys.exit(2)

    payload = json.loads(src.read_text(encoding="utf-8"))
    existing = _existing_names() if args.apply else set()
    # dry-run 模式也查一次，让输出更直观
    if not args.apply:
        existing = _existing_names()

    plan_create: list[dict] = []
    skip_existing: list[str] = []

    # ── valid 部分 ────────────────────────────────────
    for country, item in _iter_valid(payload):
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        name = _norm_name(country, code)
        if name in existing:
            skip_existing.append(name)
            continue
        currency = _COUNTRY_CURRENCY.get(country, "")
        plan_create.append({
            "name": name,
            "plan": "team",
            "seat_quantity": 2,
            "promo_code": code,
            "promo_campaign_id": "",
            "aimizy_country": country,
            "aimizy_currency": currency,
            "workspace_name": "",
            "return_mode": "long",
            "proxy_id": None,
            "_source": "valid",
            "_company": str(item.get("company") or ""),
            "_price_usd": item.get("price_usd"),
            "_price_local": item.get("price_local") or "",
            "_discount_pct": item.get("discount_pct"),
        })

    # ── expired 部分 ──────────────────────────────────
    for country, item in _iter_expired(payload):
        code = str(item.get("code") or "").strip()
        if not code or not country:
            continue
        name = _norm_name(country, code)
        if name in existing:
            skip_existing.append(name)
            continue
        currency = _COUNTRY_CURRENCY.get(country, "")
        plan_create.append({
            "name": name,
            "plan": "team",
            "seat_quantity": 2,
            "promo_code": code,
            "promo_campaign_id": "",
            "aimizy_country": country,
            "aimizy_currency": currency,
            "workspace_name": "",
            "return_mode": "long",
            "proxy_id": None,
            "_source": "expired",
            "_company": str(item.get("company") or ""),
            "_note": str(item.get("note") or ""),
        })

    # ── 输出计划 ──────────────────────────────────────
    print()
    print(f"📦 计划导入 {len(plan_create)} 条新模板（跳过 {len(skip_existing)} 条已存在）")
    print()
    for entry in plan_create:
        kind = "✅" if entry["_source"] == "valid" else "❌"
        suffix = (
            f" (USD {entry.get('_price_usd')}, {entry.get('_price_local')})"
            if entry["_source"] == "valid" and entry.get("_price_usd")
            else f" — {entry.get('_note', '')}"
        )
        print(f"  {kind} {entry['name']:<45s} {entry['_company']}{suffix}")

    if not args.apply:
        print()
        print("[dry-run] 没有真正写库。加 --apply 真正执行。")
        return

    # ── 真正写库 ──────────────────────────────────────
    created = 0
    failed: list[tuple[str, str]] = []
    for entry in plan_create:
        try:
            create_template(
                name=entry["name"],
                plan=entry["plan"],
                seat_quantity=entry["seat_quantity"],
                promo_code=entry["promo_code"],
                promo_campaign_id=entry["promo_campaign_id"],
                aimizy_country=entry["aimizy_country"],
                aimizy_currency=entry["aimizy_currency"],
                workspace_name=entry["workspace_name"],
                return_mode=entry["return_mode"],
                proxy_id=entry["proxy_id"],
            )
            created += 1
        except Exception as exc:
            failed.append((entry["name"], str(exc)))

    print()
    print(f"✅ 已创建 {created} 条")
    if failed:
        print(f"❌ 失败 {len(failed)} 条:")
        for name, err in failed:
            print(f"   - {name}: {err}")


if __name__ == "__main__":
    main()
