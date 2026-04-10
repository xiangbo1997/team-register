# -*- coding: utf-8 -*-
"""手动邮件连通性检查脚本（不参与 pytest 自动收集）。"""

import os

from dotenv import load_dotenv

from src.mail import MailManager
from src.utils import setup_logger

logger = setup_logger(name="ManualMailCheck")


def main() -> None:
    load_dotenv()

    refresh_token = os.getenv("MAIL_REFRESH_TOKEN", "")
    client_id = os.getenv("MAIL_CLIENT_ID", "")
    mail_domain = os.getenv("MAIL_DOMAIN", "")
    proxy = os.getenv("PROXY", "")
    test_email = os.getenv("TEST_EMAIL") or os.getenv("TASK_EMAIL") or "test@example.com"

    logger.info("=" * 50)
    logger.info("开始手动检查邮件验证码链路")
    logger.info("目标邮箱: %s", test_email)
    logger.info("MAIL_DOMAIN(兼容字段): %s", mail_domain or "<empty>")
    logger.info("=" * 50)

    if not all([refresh_token, client_id]):
        logger.error("缺少 MAIL_REFRESH_TOKEN 或 MAIL_CLIENT_ID，无法继续。")
        return

    mail_api = MailManager(
        base_url=mail_domain,
        refresh_token=refresh_token,
        client_id=client_id,
        proxy=proxy,
    )

    latest_mail = mail_api.get_latest_mail(test_email)
    logger.info("get_latest_mail 当前为兼容占位接口，返回: %s", latest_mail)

    code = mail_api.get_verification_code(test_email, wait_timeout=20)
    if code:
        logger.info("成功捕获验证码: %s", code)
    else:
        logger.info("未捕获到验证码（测试环境中可接受）。")


if __name__ == "__main__":
    main()
