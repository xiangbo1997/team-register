# -*- coding: utf-8 -*-
"""
Efuncard 虚拟信用卡支付模块

负责 CDK 激活、卡片信息获取和 3DS 验证码轮询。
"""

import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import requests

from src.models import CardInfo
from src.utils import human_delay

logger = logging.getLogger(__name__)

# 默认 API 基础路径
_DEFAULT_BASE_URL = "https://card.efuncard.com/api/external"

# 请求超时（秒）
_REQUEST_TIMEOUT = 10
_QUERY_NOT_FOUND_MESSAGES = {
    "not found",
    "card not found",
    "no card",
    "未找到",
    "不存在",
    "未激活",
    "激活码未使用",
}


class EfunCard:
    """Efuncard 虚拟信用卡交互客户端"""

    def __init__(self, token: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        """
        初始化 Efuncard 客户端。

        Args:
            token: API 鉴权 token
            base_url: API 基础路径，便于测试时注入
        """
        if not token:
            raise ValueError("Efuncard token 不能为空")

        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.last_query_meta: dict[str, object] = {}
        self.last_redeem_meta: dict[str, object] = {}
        self.last_lookup_meta: dict[str, object] = {}

    @staticmethod
    def _parse_timestamp(value: object) -> Optional[datetime]:
        """把接口返回的 ISO 时间转换成带时区的 UTC 时间。"""
        if not value:
            return None
        try:
            normalized = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _resolve_valid_until(self, payload: dict, max_age_sec: int) -> Optional[datetime]:
        """优先使用 autoCancelAt，缺失时退回到 createdAt + 1h。"""
        auto_cancel_at = self._parse_timestamp(payload.get("autoCancelAt"))
        if auto_cancel_at:
            return auto_cancel_at

        created_at = self._parse_timestamp(payload.get("createdAt"))
        if created_at:
            return created_at + timedelta(seconds=max_age_sec)
        return None

    def query(self, cdk: str, max_age_sec: int = 3600) -> Optional[CardInfo]:
        """
        查询已激活 CDK 关联的卡片信息。

        说明：
        - 卡只能激活一次，重复使用必须优先 query；
        - 若返回 ACTIVE 且未过 autoCancelAt（或 createdAt + 1h），则直接复用。
        """
        encoded_cdk = quote(cdk, safe="")
        self.last_query_meta = {}
        try:
            resp = requests.get(
                f"{self._base_url}/cards/query/{encoded_cdk}",
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            if getattr(resp, "status_code", None) == 404:
                self.last_query_meta = {
                    "status": "not_found",
                    "http_status": 404,
                }
                logger.info("CDK 尚未激活或未找到关联卡，可执行首次激活。")
                return None
            data = resp.json()
            payload = data.get("data", {}) or {}
            if data.get("success"):
                card = CardInfo.from_api_response(payload)
                if not card:
                    self.last_query_meta = {
                        "status": "shape_mismatch",
                        "http_status": getattr(resp, "status_code", None),
                        "data_keys": sorted(list(payload.keys())),
                    }
                    logger.error("CDK 查询成功但返回数据格式异常: %s", data)
                    return None

                card_status = str(payload.get("status", "") or "").upper()
                if not card_status:
                    self.last_query_meta = {
                        "status": "missing_status",
                        "http_status": getattr(resp, "status_code", None),
                        "data_keys": sorted(list(payload.keys())),
                    }
                    logger.warning("CDK 查询成功但缺少卡状态字段，停止复用。")
                    return None
                if card_status and card_status != "ACTIVE":
                    self.last_query_meta = {
                        "status": "inactive",
                        "http_status": getattr(resp, "status_code", None),
                        "card_status": card_status,
                    }
                    logger.warning("CDK 已有关联卡，但状态不是 ACTIVE: %s", card_status)
                    return None

                valid_until = self._resolve_valid_until(payload, max_age_sec=max_age_sec)
                if not valid_until:
                    self.last_query_meta = {
                        "status": "missing_valid_until",
                        "http_status": getattr(resp, "status_code", None),
                        "card_status": card_status,
                        "data_keys": sorted(list(payload.keys())),
                    }
                    logger.warning("CDK 查询成功但缺少有效期字段，停止复用。")
                    return None
                if valid_until and valid_until <= datetime.now(timezone.utc):
                    self.last_query_meta = {
                        "status": "expired",
                        "http_status": getattr(resp, "status_code", None),
                        "valid_until": valid_until.isoformat(),
                    }
                    logger.warning("CDK 查询到卡片，但已超过可复用时效。")
                    return None

                self.last_query_meta = {
                    "status": "success",
                    "http_status": getattr(resp, "status_code", None),
                    "data_keys": sorted(list(payload.keys())),
                    "valid_until": valid_until.isoformat() if valid_until else "",
                }
                logger.info("查询到可复用卡片信息，跳过再次激活。")
                return card

            message = str(data.get("message") or data.get("error") or "未知错误")
            normalized_message = " ".join(message.lower().split())
            query_status = "not_found" if normalized_message in _QUERY_NOT_FOUND_MESSAGES else "api_failure"
            self.last_query_meta = {
                "status": query_status,
                "http_status": getattr(resp, "status_code", None),
                "message": message,
                "data_keys": sorted(list(payload.keys())),
            }
            if query_status == "not_found":
                logger.info("CDK 查询未命中关联卡，可执行首次激活: %s", message)
            else:
                logger.info("CDK 查询失败，避免重复激活: %s", message)
        except requests.RequestException as exc:
            self.last_query_meta = {
                "status": "request_exception",
                "message": str(exc),
            }
            logger.warning("Efuncard 卡片查询异常: %s", exc)
        except ValueError as exc:
            self.last_query_meta = {
                "status": "invalid_json",
                "message": str(exc),
            }
            logger.warning("Efuncard 卡片查询返回了不可解析 JSON。")

        return None

    def redeem(self, cdk: str) -> Optional[CardInfo]:
        """
        激活 CDK 并获取虚拟卡信息。

        Args:
            cdk: Efuncard CDK 兑换码

        Returns:
            成功返回 CardInfo，失败返回 None
        """
        logger.info("正在激活 CDK: %s", cdk)
        self.last_redeem_meta = {}
        try:
            resp = requests.post(
                f"{self._base_url}/redeem",
                json={"code": cdk},
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
            data = resp.json()

            if data.get("success"):
                card = CardInfo.from_api_response(data.get("data", {}))
                if card:
                    self.last_redeem_meta = {
                        "status": "success",
                        "data_keys": sorted(list((data.get("data", {}) or {}).keys())),
                    }
                    logger.info("CDK 激活成功，获取卡片信息。")
                    return card
                self.last_redeem_meta = {
                    "status": "shape_mismatch",
                    "data_keys": sorted(list((data.get("data", {}) or {}).keys())),
                }
                logger.error("CDK 激活成功但返回数据格式异常: %s", data)
            else:
                self.last_redeem_meta = {
                    "status": "api_failure",
                    "message": data.get("message", "未知错误"),
                    "data_keys": sorted(list((data.get("data", {}) or {}).keys())),
                }
                logger.error("CDK 激活失败: %s", data.get("message", "未知错误"))

        except requests.RequestException as exc:
            self.last_redeem_meta = {
                "status": "request_exception",
                "message": str(exc),
            }
            logger.error("Efuncard API 请求异常: %s", exc)

        return None

    def get_card(self, cdk: str, max_age_sec: int = 3600) -> Optional[CardInfo]:
        """
        获取当前 CDK 对应的可用卡片。

        优先 query 复用 1 小时内已激活的卡；仅在查询未命中时才尝试 redeem，
        以避免再次触发一次性激活限制。
        """
        self.last_lookup_meta = {}

        card = self.query(cdk, max_age_sec=max_age_sec)
        if card:
            self.last_lookup_meta = {
                "status": "success",
                "source": "query",
                "query_meta": dict(self.last_query_meta),
            }
            return card

        query_meta = dict(self.last_query_meta)
        query_status = str(query_meta.get("status", "") or "")
        if query_status != "not_found":
            self.last_lookup_meta = {
                "status": "failed",
                "source": "query_only",
                "query_meta": query_meta,
            }
            logger.warning("query 未明确表明可首次激活，停止 redeem。status=%s", query_status or "unknown")
            return None

        card = self.redeem(cdk)
        if card:
            self.last_lookup_meta = {
                "status": "success",
                "source": "redeem",
                "query_meta": query_meta,
                "redeem_meta": dict(self.last_redeem_meta),
            }
            return card

        # 某些情况下 redeem 端已经把卡激活了，但返回体缺字段或提示已激活；
        # 此时再 query 一次可以把真正可用的卡信息捞回来，避免浪费一次机会。
        if self.last_redeem_meta.get("status") in {"shape_mismatch", "api_failure"}:
            fallback_card = self.query(cdk, max_age_sec=max_age_sec)
            if fallback_card:
                self.last_lookup_meta = {
                    "status": "success",
                    "source": "query_after_redeem",
                    "query_meta": dict(self.last_query_meta),
                    "redeem_meta": dict(self.last_redeem_meta),
                }
                return fallback_card

        self.last_lookup_meta = {
            "status": "failed",
            "query_meta": query_meta,
            "redeem_meta": dict(self.last_redeem_meta),
        }
        return None

    def wait_for_3ds(self, cdk: str, timeout_sec: int = 300) -> Optional[str]:
        """
        轮询获取 3DS 验证码。

        Args:
            cdk: 关联的 CDK
            timeout_sec: 最大等待秒数

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info("正在监控 3DS 验证码 (超时 %ds)...", timeout_sec)
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < timeout_sec:
            attempt += 1
            try:
                resp = requests.post(
                    f"{self._base_url}/3ds/verify",
                    json={"code": cdk, "minutes": 5},
                    headers=self._headers,
                    timeout=_REQUEST_TIMEOUT,
                )
                data = resp.json()
                verifications = data.get("data", {}).get("verifications", [])
                if data.get("success") and verifications:
                    otp = verifications[0]["otp"]
                    logger.info("成功获取 3DS 验证码: %s", otp)
                    return otp

            except requests.RequestException as exc:
                logger.warning("获取 3DS 验证码失败 (尝试 %d): %s", attempt, exc)

            # 轮询间隔 8~12 秒
            human_delay(8, 12)

        logger.warning("监控 3DS 验证码超时。")
        return None
