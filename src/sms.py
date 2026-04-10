# -*- coding: utf-8 -*-
"""
SMS-Activate 接码模块

负责获取手机号和等待短信验证码。
"""

import logging
from typing import Optional

import requests

from src.models import SMSOrder
from src.utils import human_delay

logger = logging.getLogger(__name__)

# SMS-Activate API 端点
_API_URL = "https://api.sms-activate.org/steward.php"

# 请求超时（秒）
_REQUEST_TIMEOUT = 60


class SMSManager:
    """SMS-Activate 接码平台客户端"""

    def __init__(self, api_key: str, country: str = "6", api_url: str = _API_URL, proxy: str = "") -> None:
        """
        初始化 SMS 客户端。

        Args:
            api_key: SMS-Activate API 密钥
            country: 国家代码（6=印度尼西亚, 0=俄罗斯, 12=英国）
            api_url: API 端点，便于测试时注入
            proxy: 可选代理 URL (e.g. socks5h://...)
        """
        if not api_key:
            raise ValueError("SMS API key 不能为空")

        self._api_key = api_key
        self._country = country
        self._api_url = api_url
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    def get_number(self, service: str = "dr") -> Optional[SMSOrder]:
        """
        获取指定服务的手机号。

        Args:
            service: 服务代码（'dr' = OpenAI/ChatGPT）

        Returns:
            成功返回 SMSOrder，失败返回 None
        """
        logger.info("请求获取手机号 (国家: %s, 服务: %s)...", self._country, service)
        params = {
            "api_key": self._api_key,
            "action": "getNumber",
            "service": service,
            "country": self._country,
        }

        try:
            resp = requests.get(self._api_url, params=params, timeout=_REQUEST_TIMEOUT, proxies=self._proxies)
            text = resp.text.strip()

            if "ACCESS_NUMBER" in text:
                parts = text.split(":")
                order = SMSOrder(order_id=parts[1], phone_number=parts[2])
                logger.info("成功获取手机号: %s (订单: %s)", order.phone_number, order.order_id)
                return order

            logger.error("获取手机号失败，API 返回: %s", text)

        except requests.RequestException as exc:
            logger.error("SMS API 请求异常: %s", exc)

        return None

    def get_code(self, order_id: str, max_retries: int = 30) -> Optional[str]:
        """
        轮询等待短信验证码。

        Args:
            order_id: 订单 ID（来自 get_number）
            max_retries: 最大重试次数（每次约 5 秒间隔）

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info("等待短信验证码 (订单: %s)...", order_id)

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(
                    self._api_url,
                    params={
                        "api_key": self._api_key,
                        "action": "getStatus",
                        "id": order_id,
                    },
                    timeout=_REQUEST_TIMEOUT,
                    proxies=self._proxies
                )
                text = resp.text.strip()

                if "STATUS_OK" in text:
                    code = text.split(":")[1]
                    logger.info("成功获取短信验证码: %s", code)
                    return code

            except requests.RequestException as exc:
                logger.warning("检查验证码状态异常 (尝试 %d/%d): %s", attempt, max_retries, exc)

            human_delay(4, 6)

        logger.warning("获取短信验证码超时 (共尝试 %d 次)。", max_retries)
        return None
