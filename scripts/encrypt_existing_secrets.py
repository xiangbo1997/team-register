# -*- coding: utf-8 -*-
"""
就地加密历史数据库里遗留的明文敏感字段。

用法::

    export DB_ENCRYPTION_KEY="$(python -c 'from src.db.crypto import generate_key; print(generate_key())')"
    python scripts/encrypt_existing_secrets.py

脚本幂等：
- 对已经带 ``enc:v1:`` 前缀的字段跳过
- 仅处理 ``Run.password`` / ``Run.card_key`` / ``MailAccount.client_id`` /
  ``MailAccount.refresh_token`` 四个字段
- 先 dry-run 打印计划，需要加 ``--apply`` 才真正写库

完成后，请备份旧 DB，并把 ``DB_ENCRYPTION_KEY`` 持久化到 ``.env``。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 允许脚本从仓库根目录运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from src.db import crypto
from src.db.engine import get_engine

logger = logging.getLogger("encrypt_existing_secrets")

_TARGETS = [
    ("runs", "id", ["password", "card_key"]),
    ("mail_accounts", "id", ["client_id", "refresh_token"]),
]


def _plan_and_apply(apply: bool) -> dict:
    if not os.getenv("DB_ENCRYPTION_KEY"):
        raise SystemExit(
            "DB_ENCRYPTION_KEY 未设置。请先 export 一把 key 再运行本脚本。"
        )

    stats: dict[str, dict[str, int]] = {}
    engine = get_engine()

    with engine.begin() as conn:
        for table, pk, columns in _TARGETS:
            stats[table] = {"total": 0, "already_encrypted": 0, "to_encrypt": 0, "updated": 0}
            rows = conn.execute(text(f"SELECT {pk}, {', '.join(columns)} FROM {table}")).all()
            stats[table]["total"] = len(rows)

            for row in rows:
                pk_value = row[0]
                updates: dict[str, str] = {}
                for idx, col in enumerate(columns, start=1):
                    value = row[idx]
                    if value is None or value == "":
                        continue
                    if crypto.is_ciphertext(value):
                        stats[table]["already_encrypted"] += 1
                        continue
                    stats[table]["to_encrypt"] += 1
                    updates[col] = crypto.encrypt_value(value)

                if updates and apply:
                    set_clause = ", ".join(f"{col} = :{col}" for col in updates)
                    params = {**updates, "pk": pk_value}
                    conn.execute(
                        text(f"UPDATE {table} SET {set_clause} WHERE {pk} = :pk"),
                        params,
                    )
                    stats[table]["updated"] += len(updates)

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正执行写入，默认仅 dry-run")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("[%s] 开始扫描历史明文敏感字段…", mode)
    stats = _plan_and_apply(apply=args.apply)

    for table, info in stats.items():
        logger.info(
            "%s: total=%d already_encrypted_cells=%d to_encrypt_cells=%d updated_cells=%d",
            table,
            info["total"],
            info["already_encrypted"],
            info["to_encrypt"],
            info["updated"],
        )

    if not args.apply:
        logger.info("未加 --apply，以上为 DRY-RUN 计划，数据库未被修改。")
    else:
        logger.info("迁移完成。建议立即备份数据库并把 DB_ENCRYPTION_KEY 写入 .env。")


if __name__ == "__main__":
    main()
