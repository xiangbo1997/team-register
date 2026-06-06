"""回填历史 LinkTemplate 的 promo 结构化字段（P4 一次性脚本）。

对所有 last_eligibility_metadata 非空的模板，用 extract_promo_fields 抽 5 个字段
回填到 promo_percent_off / promo_duration_months / promo_expires_at /
promo_max_redemptions / promo_applicable_plans，让号池升级弹窗能按折扣排序/显示。

幂等：重复跑只会用最新 metadata 重抽，不会重复写脏数据。
用法：python scripts/backfill_promo_metadata.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import LinkTemplate
from src.services.promo_metadata_extract import extract_promo_fields


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 promo 结构化字段")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    updated = 0
    scanned = 0
    with get_session() as session:
        rows = list(session.exec(
            select(LinkTemplate).where(LinkTemplate.last_eligibility_metadata != None)  # noqa: E711
        ).all())
        for tpl in rows:
            scanned += 1
            fields = extract_promo_fields(tpl.last_eligibility_metadata)
            changed = False
            if fields["percent_off"] is not None and tpl.promo_percent_off != fields["percent_off"]:
                tpl.promo_percent_off = fields["percent_off"]; changed = True
            if fields["duration_months"] is not None and tpl.promo_duration_months != fields["duration_months"]:
                tpl.promo_duration_months = fields["duration_months"]; changed = True
            if fields["expires_at"] is not None and tpl.promo_expires_at != fields["expires_at"]:
                tpl.promo_expires_at = fields["expires_at"]; changed = True
            if fields["max_redemptions"] is not None and tpl.promo_max_redemptions != fields["max_redemptions"]:
                tpl.promo_max_redemptions = fields["max_redemptions"]; changed = True
            if fields["applicable_plans"] and tpl.promo_applicable_plans != fields["applicable_plans"]:
                tpl.promo_applicable_plans = fields["applicable_plans"]; changed = True
            if changed:
                updated += 1
                print(f"  {tpl.name}: pct={fields['percent_off']} months={fields['duration_months']} "
                      f"plans={fields['applicable_plans']!r}")
                if not args.dry_run:
                    session.add(tpl)
        if not args.dry_run:
            session.commit()

    mode = "(dry-run)" if args.dry_run else ""
    print(f"\n扫描 {scanned} 个模板，{'将' if args.dry_run else '已'}更新 {updated} 个 {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
