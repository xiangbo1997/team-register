# -*- coding: utf-8 -*-
"""
SMS-Activate 接码模块

负责获取手机号和等待短信验证码。
"""

import logging
import time
from typing import Optional

import requests

from src.models import SMSOrder
from src.utils import human_delay

logger = logging.getLogger(__name__)

# SMS-Activate API 端点
_API_URL = "https://api.sms-activate.org/steward.php"

# 请求超时（秒）
_REQUEST_TIMEOUT = 60

# 瞬时网络错误重试参数：覆盖 SOCKS5 代理转发 sms-activate.org 偶发的 SSL EOF /
# 连接瞬时失败 / 代理握手中断。仅作用于 getNumber 申号入口；
# get_code 已经自带 30×5s 轮询，等价于内置重试，不再叠加。
_MAX_TRANSIENT_RETRIES = 4          # 共最多 5 次尝试（首次 + 4 次重试）
_RETRY_BACKOFF_BASE_SECONDS = 1.5   # 指数退避基数：1.5/3.0/4.5/6.0s（见 _RETRY_BACKOFF_CAP_SECONDS 封顶）
_RETRY_BACKOFF_CAP_SECONDS = 6.0    # 退避上限：住宅代理 SSL EOF 是间歇抖动，长等无益，封顶避免单轮拖太久


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

        # 重试机制只覆盖网络层瞬时错误（SSL EOF / 连接超时 / SOCKS5 代理握手中断等）；
        # 业务层错误（NO_NUMBERS / BAD_KEY / ERROR_SQL）说明 API 端点本身已正常响应，
        # 重试只会浪费时间和退避秒数，所以见到响应文本就直接 return None。
        total_attempts = _MAX_TRANSIENT_RETRIES + 1
        for attempt in range(total_attempts):
            try:
                resp = requests.get(
                    self._api_url, params=params, timeout=_REQUEST_TIMEOUT, proxies=self._proxies,
                )
                text = resp.text.strip()

                if "ACCESS_NUMBER" in text:
                    parts = text.split(":")
                    order = SMSOrder(order_id=parts[1], phone_number=parts[2])
                    logger.info("成功获取手机号: %s (订单: %s)", order.phone_number, order.order_id)
                    return order

                # 业务层失败：不重试
                logger.error("获取手机号失败，API 返回: %s", text)
                return None

            except requests.RequestException as exc:
                is_last = attempt == total_attempts - 1
                if is_last:
                    logger.error(
                        "SMS getNumber 瞬时异常 (尝试 %d/%d，已达上限): %s",
                        attempt + 1, total_attempts, exc,
                    )
                else:
                    backoff = min(
                        _RETRY_BACKOFF_BASE_SECONDS * (attempt + 1),
                        _RETRY_BACKOFF_CAP_SECONDS,
                    )
                    logger.warning(
                        "SMS getNumber 瞬时异常 (尝试 %d/%d): %s；%.1fs 后重试",
                        attempt + 1, total_attempts, exc, backoff,
                    )
                    time.sleep(backoff)

        return None

    def request_retry(self, order_id: str) -> bool:
        """请求该号码重新接收下一条短信（号码复用）。

        SMS-Activate 协议 ``setStatus`` action：``status=3`` 表示「请求重新发码」，
        让平台保持该号继续等待下一条短信，从而一个号给多个账号复用收码。
        成功响应为 ``ACCESS_RETRY_GET``。

        Args:
            order_id: 订单 ID（来自 get_number）

        Returns:
            True 表示平台已接受复用请求，False 表示失败（号已结束/取消/网络异常）
        """
        logger.info("请求号码复用（重新发码, 订单: %s）...", order_id)
        try:
            resp = requests.get(
                self._api_url,
                params={
                    "api_key": self._api_key,
                    "action": "setStatus",
                    "status": "3",
                    "id": order_id,
                },
                timeout=_REQUEST_TIMEOUT,
                proxies=self._proxies,
            )
            text = resp.text.strip()
        except requests.RequestException as exc:
            logger.warning("号码复用请求异常 (订单: %s): %s", order_id, exc)
            return False

        if "ACCESS_RETRY_GET" in text:
            logger.info("号码复用请求成功 (订单: %s)", order_id)
            return True
        logger.warning("号码复用请求被拒绝 (订单: %s)，API 返回: %s", order_id, text)
        return False

    def cancel_number(self, order_id: str) -> bool:
        """取消激活（释放号码）。号被 OpenAI 拒/收不到码时调用，避免继续计费。

        SMS-Activate 协议 ``setStatus`` action：``status=8`` 表示取消激活。
        成功响应为 ``ACCESS_CANCEL``。

        Returns:
            True 表示已取消，False 表示失败（号已完成/网络异常）
        """
        logger.info("取消号码激活（释放, 订单: %s）...", order_id)
        try:
            resp = requests.get(
                self._api_url,
                params={
                    "api_key": self._api_key,
                    "action": "setStatus",
                    "status": "8",
                    "id": order_id,
                },
                timeout=_REQUEST_TIMEOUT,
                proxies=self._proxies,
            )
            text = resp.text.strip()
        except requests.RequestException as exc:
            logger.warning("取消号码激活异常 (订单: %s): %s", order_id, exc)
            return False

        if "ACCESS_CANCEL" in text:
            logger.info("号码已取消 (订单: %s)", order_id)
            return True
        # HeroSMS 规定号申领后须等 120 秒才能取消（minActivationTime）。
        # 这不是异常，是平台业务限制——降级为 info，避免污染错误日志。
        if "EARLY_CANCEL_DENIED" in text or "Minimum activation period" in text:
            logger.info("号码暂不可取消（未满最短激活期 120s，订单: %s），将自然到期释放", order_id)
            return False
        logger.warning("取消号码失败 (订单: %s)，API 返回: %s", order_id, text)
        return False

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
