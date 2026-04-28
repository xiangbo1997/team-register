# -*- coding: utf-8 -*-
"""一次性触发 execute_card_warmup（不跑注册主流程）。

用法：
    python scripts/run_warmup_only.py

复用 .env 当前 CARD_PROVIDER（默认 x988card）。当 X988 cdk 已在 card_activations
DB 缓存里命中时不会再消耗 X988 verify 名额；否则会回源 X988 调一次 verify。

预热账号从 mail_accounts(role=pro_warmup) 池里挑。两轮 $200 + $100 跑完即视为成功。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from main import _build_runtime_clients
from src.config import load_config
from src.models import CardInfo
from src.orchestration.warmup import execute_card_warmup
from src.services.config_service import ConfigService
from src.utils import setup_logger


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性触发卡片预热流程")
    parser.add_argument(
        "--card-key",
        required=True,
        help="X988 / EfunCard / NodeCard 的 cdk（必填）",
    )
    args = parser.parse_args()

    setup_logger()
    logger = logging.getLogger("RunWarmupOnly")

    load_dotenv(_PROJECT_ROOT / ".env")
    config = load_config()
    if not config.enable_card_warmup:
        logger.error("ENABLE_CARD_WARMUP 未启用（.env 里设 ENABLE_CARD_WARMUP=true）")
        return 1

    card_api, _sms_api, mail_api = _build_runtime_clients(config)
    if card_api is None:
        logger.error("card_api 构造失败（CARD_PROVIDER=%s）", config.card_provider)
        return 1

    logger.info("=" * 60)
    logger.info("卡片预热触发：")
    logger.info("  CARD_PROVIDER : %s", config.card_provider)
    logger.info("  card_key      : %s***%s", args.card_key[:6], args.card_key[-4:])
    logger.info("=" * 60)

    # 1) get_card 拿卡（X988 走 DB 缓存命中即不消耗 verify 名额）
    card = card_api.get_card(args.card_key)
    if card is None:
        logger.error("get_card 失败 meta=%s", getattr(card_api, "last_lookup_meta", {}))
        return 1
    logger.info(
        "卡信息：%s***%s exp=%s/%s name=%s",
        card.card_number[:4], card.card_number[-4:],
        card.expiry_month, card.expiry_year, card.name_on_card,
    )

    # 2) ConfigService 注入 mail_api（让 pro_account_login 走 magic link 路径）
    svc = ConfigService()
    svc.mail_api = mail_api  # type: ignore[attr-defined]

    # 3) 触发预热（外部已开 sync_playwright，避免在 worker 线程嵌套）
    with sync_playwright() as p:
        ok = execute_card_warmup(
            config,
            card,
            card_api,
            args.card_key,
            svc=svc,
            proxy_url=config.proxy or "",
            playwright=p,
        )

    logger.info("=" * 60)
    if ok:
        logger.info("预热成功（两轮 navigate+select+fill+submit 全部跑完）")
        return 0
    logger.error("预热失败（详见日志）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
