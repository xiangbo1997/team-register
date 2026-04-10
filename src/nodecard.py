# -*- coding: utf-8 -*-
"""
NodeCard 虚拟信用卡模块

基于 NodeCard 开放 API 对接文档（2026-03-14），提供兑换、状态查询、
3DS 验证码轮询等能力。与 EfunCard 共用 CardInfo 数据模型。
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from src.models import CardInfo
from src.utils import human_delay

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.node-card.com"
_REQUEST_TIMEOUT = 15


class NodeCard:
    """NodeCard 虚拟信用卡交互客户端"""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        merchant_dict_id: Optional[int] = None,
        platform_id: Optional[int] = None,
    ) -> None:
        """
        初始化 NodeCard 客户端。

        Args:
            base_url: API 基础地址
            merchant_dict_id: 商户 ID（当模板限制商户时必填）
            platform_id: 指定兑换平台 ID（不传则按卡密模板所属平台）
        """
        self._base_url = base_url.rstrip("/")
        self._merchant_dict_id = merchant_dict_id
        self._platform_id = platform_id
        self._headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        self.last_query_meta: dict[str, object] = {}
        self.last_redeem_meta: dict[str, object] = {}
        self.last_lookup_meta: dict[str, object] = {}

    @staticmethod
    def _parse_card_info(data: dict) -> Optional[CardInfo]:
        """从 NodeCard redeem 响应构造 CardInfo。"""
        try:
            card_number = str(data["card_number"])
            cvv = str(data["cvv"])
            exp = str(data["exp"])  # 格式 "03/29"

            parts = exp.split("/")
            if len(parts) != 2:
                return None
            expiry_month = parts[0]
            expiry_year = parts[1] if len(parts[1]) == 4 else f"20{parts[1]}"

            # 用 expire_time 构造 auto_cancel_at
            expire_time = data.get("expire_time", 0)
            auto_cancel_at = ""
            if expire_time:
                auto_cancel_at = datetime.fromtimestamp(
                    int(expire_time), tz=timezone.utc
                ).isoformat()

            # 用 redeem_time 构造 created_at
            redeem_time = data.get("redeem_time", 0)
            created_at = ""
            if redeem_time:
                created_at = datetime.fromtimestamp(
                    int(redeem_time), tz=timezone.utc
                ).isoformat()

            return CardInfo(
                card_number=card_number,
                expiry_month=expiry_month,
                expiry_year=expiry_year,
                cvv=cvv,
                name_on_card=str(data.get("name_on_card", "") or ""),
                status="ACTIVE",
                created_at=created_at,
                auto_cancel_at=auto_cancel_at,
                billing_address=str(data.get("full_billing_address", "") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("NodeCard 响应解析失败: %s", exc)
            return None

    def query_status(self, card_key: str) -> str:
        """
        查询卡密状态。

        Returns:
            状态字符串: "none" / "unused" / "redeeming" / "used"
        """
        self.last_query_meta = {}
        try:
            resp = requests.get(
                f"{self._base_url}/api/open/card/status",
                params={"card_key": card_key},
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            data = resp.json()
            if data.get("code") == 1:
                status = data.get("data", {}).get("status", "none")
                self.last_query_meta = {
                    "status": status,
                    "http_status": resp.status_code,
                    "used_time": data.get("data", {}).get("used_time", 0),
                }
                return status

            self.last_query_meta = {
                "status": "api_failure",
                "http_status": resp.status_code,
                "message": data.get("msg", ""),
            }
            return "none"
        except requests.RequestException as exc:
            self.last_query_meta = {
                "status": "request_exception",
                "message": str(exc),
            }
            logger.warning("NodeCard 状态查询异常: %s", exc)
            return "none"

    def redeem(self, card_key: str) -> Optional[CardInfo]:
        """
        兑换卡密，获取虚拟卡信息。

        Args:
            card_key: 卡密

        Returns:
            成功返回 CardInfo，失败返回 None
        """
        logger.info("正在兑换 NodeCard 卡密...")
        self.last_redeem_meta = {}
        payload = {"card_key": card_key}
        if self._merchant_dict_id is not None:
            payload["merchant_dict_id"] = str(self._merchant_dict_id)
        if self._platform_id is not None:
            payload["platform_id"] = str(self._platform_id)

        try:
            resp = requests.post(
                f"{self._base_url}/api/open/card/redeem",
                data=payload,
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            data = resp.json()
            if data.get("code") == 1:
                card = self._parse_card_info(data.get("data", {}))
                if card:
                    self.last_redeem_meta = {
                        "status": "success",
                        "data_keys": sorted(list(data.get("data", {}).keys())),
                        "merchant": data.get("data", {}).get("merchant_name", ""),
                        "available_hours": data.get("data", {}).get("available_hours", 0),
                    }
                    logger.info(
                        "NodeCard 兑换成功: %s****%s  商户: %s  剩余: %dh",
                        card.card_number[:4],
                        card.card_number[-4:],
                        data.get("data", {}).get("merchant_name", ""),
                        data.get("data", {}).get("available_hours", 0),
                    )
                    return card

                self.last_redeem_meta = {
                    "status": "shape_mismatch",
                    "data_keys": sorted(list(data.get("data", {}).keys())),
                }
                logger.error("NodeCard 兑换成功但数据解析失败: %s", data)
            else:
                self.last_redeem_meta = {
                    "status": "api_failure",
                    "message": data.get("msg", "未知错误"),
                }
                logger.error("NodeCard 兑换失败: %s", data.get("msg", "未知错误"))

        except requests.RequestException as exc:
            self.last_redeem_meta = {
                "status": "request_exception",
                "message": str(exc),
            }
            logger.error("NodeCard API 请求异常: %s", exc)

        return None

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        """
        获取卡密对应的可用卡片。

        已使用且未过期的卡密会直接返回已兑换卡信息（由 API 端处理）。
        """
        self.last_lookup_meta = {}

        # 先查状态
        status = self.query_status(card_key)
        logger.info("NodeCard 卡密状态: %s", status)

        if status == "none":
            self.last_lookup_meta = {
                "status": "failed",
                "reason": "card_key_not_found",
                "query_meta": dict(self.last_query_meta),
            }
            logger.error("NodeCard 卡密不存在。")
            return None

        if status == "redeeming":
            self.last_lookup_meta = {
                "status": "failed",
                "reason": "redeeming_in_progress",
                "query_meta": dict(self.last_query_meta),
            }
            logger.warning("NodeCard 卡密正在兑换中，请稍后重试。")
            return None

        # unused 或 used（未过期） → 调 redeem，API 会返回卡信息
        card = self.redeem(card_key)
        if card:
            self.last_lookup_meta = {
                "status": "success",
                "source": "redeem",
                "card_status": status,
                "query_meta": dict(self.last_query_meta),
                "redeem_meta": dict(self.last_redeem_meta),
            }
            return card

        self.last_lookup_meta = {
            "status": "failed",
            "card_status": status,
            "query_meta": dict(self.last_query_meta),
            "redeem_meta": dict(self.last_redeem_meta),
        }
        return None

    def query_transactions(self, card_key: str) -> list[dict]:
        """查询卡密的交易记录。"""
        try:
            resp = requests.post(
                f"{self._base_url}/api/open/card/transactions",
                data={"card_key": card_key},
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            data = resp.json()
            if data.get("code") == 1:
                return data.get("data", {}).get("transactions", [])
            logger.warning("NodeCard 交易查询失败: %s", data.get("msg", ""))
        except requests.RequestException as exc:
            logger.warning("NodeCard 交易查询异常: %s", exc)
        return []

    def query_merchants(self) -> list[dict]:
        """获取商户列表。"""
        try:
            resp = requests.post(
                f"{self._base_url}/api/open/merchant/list",
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            data = resp.json()
            if data.get("code") == 1:
                return data.get("data", {}).get("list", [])
        except requests.RequestException as exc:
            logger.warning("NodeCard 商户列表查询异常: %s", exc)
        return []

    def query_capacity(self, platform_id: Optional[int] = None) -> list[dict]:
        """查询平台容量。"""
        params = {}
        if platform_id is not None:
            params["platform_id"] = str(platform_id)
        try:
            resp = requests.get(
                f"{self._base_url}/api/open/platform/capacity",
                params=params,
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            data = resp.json()
            if data.get("code") == 1:
                return data.get("data", {}).get("list", [])
        except requests.RequestException as exc:
            logger.warning("NodeCard 容量查询异常: %s", exc)
        return []

    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        """
        轮询交易记录等待 3DS 验证码。

        NodeCard 没有专用 3DS 端点，通过交易记录的 content 字段提取。
        当交易状态为 pending 且 content 包含验证码时返回。
        """
        logger.info("NodeCard 监控 3DS 验证码 (超时 %ds)...", timeout_sec)
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < timeout_sec:
            attempt += 1
            txns = self.query_transactions(card_key)
            for txn in txns:
                content = str(txn.get("content", "") or "")
                status = str(txn.get("status", "") or "").lower()
                failure = str(txn.get("failureReason", "") or "")

                # 交易失败时提前退出
                if failure:
                    logger.warning("NodeCard 交易失败: %s", failure)
                    return None

                # 从 content 中提取 OTP（通常是 4-8 位数字）
                if content:
                    import re
                    otp_match = re.search(r"\b(\d{4,8})\b", content)
                    if otp_match:
                        otp = otp_match.group(1)
                        logger.info("NodeCard 捕获 3DS 验证码: %s", otp)
                        return otp

            human_delay(8, 12)

        logger.warning("NodeCard 3DS 验证码监控超时。")
        return None
