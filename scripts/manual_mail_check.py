# -*- coding: utf-8 -*-
"""手动邮件连通性检查脚本（不参与 pytest 自动收集）。"""

import os

from dotenv import load_dotenv

from src.mail import MailManager
from src.utils import setup_logger

logger = setup_logger(name="ManualMailCheck")


def main() -> None:
    load_dotenv()

    email_provider_base_url = os.getenv("EMAIL_PROVIDER_BASE_URL", "") or os.getenv("MAIL_DOMAIN", "")
    email_provider_api_key = os.getenv("EMAIL_PROVIDER_API_KEY", "")
    refresh_token = os.getenv("MAIL_REFRESH_TOKEN", "")
    client_id = os.getenv("MAIL_CLIENT_ID", "")
    mail_domain = os.getenv("MAIL_DOMAIN", "")
    proxy = os.getenv("PROXY", "")
    test_email = os.getenv("TEST_EMAIL") or os.getenv("TASK_EMAIL") or "test@example.com"

    logger.info("=" * 50)
    logger.info("开始手动检查邮件验证码链路")
    logger.info("目标邮箱: %s", test_email)
    logger.info("EMAIL_PROVIDER_BASE_URL: %s", email_provider_base_url or "<empty>")
    logger.info("MAIL_DOMAIN(兼容字段): %s", mail_domain or "<empty>")
    logger.info("=" * 50)

    if not email_provider_base_url:
        logger.error("缺少 EMAIL_PROVIDER_BASE_URL，无法继续。")
        return
    if not all([refresh_token, client_id]):
        logger.error("缺少 MAIL_REFRESH_TOKEN 或 MAIL_CLIENT_ID，无法继续。")
        return

    mail_api = MailManager(
        base_url=email_provider_base_url,
        api_key=email_provider_api_key,
        refresh_token=refresh_token,
        client_id=client_id,
        proxy=proxy,
    )

    latest_mail = mail_api.get_latest_mail(test_email)
    logger.info("get_latest_mail 当前为兼容占位接口，返回: %s", latest_mail)

    try:
        session_mode = mail_api.ensure_runtime_ready(test_email)
        logger.info("邮件服务运行态预检通过，session_mode=%s", session_mode)
    except Exception as exc:
        logger.error("邮件运行态预检失败: %s", exc)
        return

    try:
        code = mail_api.get_verification_code(test_email, wait_timeout=20)
    except Exception as exc:
        logger.error("邮件链路检查失败: %s", exc)
        return
    if code:
        logger.info("成功捕获验证码: %s", code)
    else:
        logger.info("未捕获到验证码（测试环境中可接受）。")


if __name__ == "__main__":
    main()
