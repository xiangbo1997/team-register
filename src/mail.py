# -*- coding: utf-8 -*-
"""
邮件服务模块 (集成 email-provider)

复用 email-provider 的实现，绕过 Cloudflare 防护。
"""

import os
import sys
import logging
from typing import Optional, Any

# 将同级的 email-provider 添加到 sys.path
_provider_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "email-provider")
if _provider_path not in sys.path:
    sys.path.append(_provider_path)

from core.base_mailbox import create_local_mailbox

logger = logging.getLogger(__name__)


class MailManager:
    """基于 email-provider 的邮件服务 API 包装器"""

    def __init__(self, base_url: str, refresh_token: str, client_id: str, proxy: str = "") -> None:
        """
        初始化邮件客户端。

        当前主流程直接复用本地 email-provider，因此 base_url 仅作为兼容保留字段。
        """
        if not all([refresh_token, client_id]):
            raise ValueError("邮件服务配置不完整: refresh_token、client_id 均不能为空")

        self._base_url = base_url.rstrip("/")
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._proxy = proxy

    def _get_mailbox(self, email: str):
        """构造 AppleMailMailbox 实例"""
        # email-provider 的 AppleMail 格式: email----password----client_id----refresh_token
        accounts_text = f"{email}----dummy_pass----{self._client_id}----{self._refresh_token}"
        return create_local_mailbox(
            provider="applemail",
            extra={"applemail_accounts": accounts_text},
            proxy=self._proxy
        )

    def get_latest_mail(self, email: str, mailbox: str = "INBOX") -> Optional[Any]:
        # 向后兼容占位，如果不需要可以用不到
        logger.warning("get_latest_mail() 已废弃，通过 email-provider 进行自动化轮询。")
        return None

    def get_verification_code(self, email: str, wait_timeout: int = 60) -> Optional[str]:
        """直连轮询（现已替换为内部 email-provider 方案）"""
        return self._poll_code_with_provider(email, wait_timeout)

    def get_verification_code_via_browser(self, email: str, page, wait_timeout: int = 120) -> Optional[str]:
        """
        之前使用 Playwright 绕开 CF。
        现在借助 email-provider 的 curl_cffi 会话模拟了真实浏览器环境，无需再弹窗。
        """
        logger.info("开始通过 email-provider 获取验证码 (脱离浏览器依赖): %s", email)
        return self._poll_code_with_provider(email, wait_timeout)

    def _poll_code_with_provider(self, email: str, wait_timeout: int) -> Optional[str]:
        try:
            mailbox = self._get_mailbox(email)
            # 不调用 get_email()（它会 _clear_mailbox 导致新邮件被清除）
            # 直接用 MailboxAccount 构造，避免丢失验证码邮件
            from core.base_mailbox import MailboxAccount
            account = MailboxAccount(email=email, account_id=email)
            
            code = mailbox.wait_for_code(account=account, timeout=wait_timeout)
            if code:
                logger.info("成功捕获邮件验证码: %s", code)
                return code
        except TimeoutError:
            logger.warning("获取邮件验证码超时。")
        except Exception as e:
            logger.error("获取邮件验证码异常: %s", e)
        return None

    def clear_mailbox(self, email: str, folder: str = "inbox") -> bool:
        """
        由于 create_local_mailbox(applemail).get_email() 已包含了 clear 操作
        此处可以忽略主动触发
        """
        try:
            mailbox = self._get_mailbox(email)
            mailbox.get_email() # 内部自动调用 _clear_mailbox
            return True
        except Exception as e:
            logger.warning("清空邮箱期间出错 (非致命): %s", e)
            return False
