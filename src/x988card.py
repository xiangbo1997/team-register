# -*- coding: utf-8 -*-
"""
X988Card 虚拟信用卡模块

对接 cards.779.chat / card.988.chat 卡商：
- POST `/api/exchange/verify` 一次性返回卡号 / CVV / 过期 / 账单地址 / 3DS 短信 API
- 3DS 验证码通过 API 返回的 `sms_api` URL 直接拉取（不依赖 SMS-Activate）

设计与 NodeCard / EfunCard 共用 `CardInfo`，让 `CardProvider` 抽象层无感知差异。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.fintech.bin_lookup import lookup_bin_country
from src.models import CardInfo

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://cards.779.chat"
_REQUEST_TIMEOUT = 15
# 3DS 验证码常见格式：4-8 位数字
_OTP_PATTERN = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


class X988Card:
    """X988 虚拟信用卡客户端。"""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        request_timeout: int = _REQUEST_TIMEOUT,
    ) -> None:
        """初始化。

        Args:
            base_url: API 基础地址，默认 https://cards.779.chat
            request_timeout: 单次请求超时（秒）
        """
        self._base_url = base_url.rstrip("/")
        self._request_timeout = max(1, int(request_timeout))
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "http://card.988.chat",
            "Referer": "http://card.988.chat/",
        }
        # 缓存最近一次 verify 的元信息，wait_for_3ds 等需要复用 sms_api / phone
        self._last_meta: dict[str, str] = {}
        # 兼容上层日志和测试钩子
        self.last_lookup_meta: dict[str, object] = {}

    # ── 内部 ──────────────────────────────────────

    @staticmethod
    def _parse_card(data: dict) -> Optional[CardInfo]:
        """从 X988 verify 响应的 ``content`` 字段构造 CardInfo。"""
        try:
            card_number = str(data["card_number"])
            cvv = str(data["cvv"])
            expiry_raw = str(data["expiry_date"])  # "2030/2"
            parts = expiry_raw.split("/")
            if len(parts) != 2:
                logger.warning("X988 expiry_date 格式异常: %r", expiry_raw)
                return None
            expiry_year = parts[0].strip()
            expiry_month = parts[1].strip().zfill(2)
            if len(expiry_year) == 2:
                expiry_year = f"20{expiry_year}"
            return CardInfo(
                card_number=card_number,
                expiry_month=expiry_month,
                expiry_year=expiry_year,
                cvv=cvv,
                name_on_card=str(data.get("name", "") or ""),
                status="ACTIVE",
                billing_address=str(data.get("address", "") or ""),
                bin_country=lookup_bin_country(card_number),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("X988 响应解析失败: %s", exc)
            return None

    # ── 对外 API（与 NodeCard 同形）────────────────

    def get_card(self, card_key: str) -> Optional[CardInfo]:
        """通过卡密换取卡信息。一次 POST 拿到全部字段，无需 query/activate 多步。"""
        self._last_meta = {}
        self.last_lookup_meta = {}
        url = f"{self._base_url}/api/exchange/verify"
        try:
            resp = requests.post(
                url,
                json={"key": card_key},
                headers=self._headers,
                timeout=self._request_timeout,
            )
        except requests.RequestException as exc:
            logger.error("X988 verify 请求失败: %s", exc)
            self.last_lookup_meta = {"status": "failed", "reason": "network_error"}
            return None

        if resp.status_code != 200:
            logger.error(
                "X988 verify HTTP %d: %s", resp.status_code, resp.text[:200]
            )
            self.last_lookup_meta = {
                "status": "failed",
                "reason": f"http_{resp.status_code}",
            }
            return None

        try:
            payload = resp.json()
        except ValueError:
            logger.error("X988 verify 返回非 JSON: %s", resp.text[:200])
            self.last_lookup_meta = {"status": "failed", "reason": "invalid_json"}
            return None

        if not payload.get("success"):
            logger.error("X988 verify 业务失败: %s", payload)
            self.last_lookup_meta = {
                "status": "failed",
                "reason": "verify_unsuccessful",
                "raw": payload,
            }
            return None

        content = payload.get("content") or {}
        card_meta = payload.get("card") or {}
        card_info = self._parse_card(content)
        if card_info is None:
            self.last_lookup_meta = {"status": "failed", "reason": "parse_error"}
            return None

        # 缓存 sms_api / phone，wait_for_3ds 需要
        self._last_meta = {
            "key": str(card_meta.get("key", card_key) or card_key),
            "sms_api": str(content.get("sms_api", "") or ""),
            "phone": str(content.get("phone", "") or ""),
            "category": str(card_meta.get("category", "") or ""),
            "expires_at": str(card_meta.get("expires_at", "") or ""),
            "activated_at": str(card_meta.get("activated_at", "") or ""),
        }
        self.last_lookup_meta = {
            "status": "active",
            "phone": self._last_meta["phone"],
            "sms_api_present": bool(self._last_meta["sms_api"]),
        }
        logger.info(
            "X988 verify 成功: last4=%s phone=%s sms_api=%s",
            card_info.card_number[-4:],
            self._last_meta["phone"][-4:] if self._last_meta["phone"] else "",
            "yes" if self._last_meta["sms_api"] else "no",
        )
        return card_info

    def cancel_card(self, card_key: str) -> bool:  # noqa: ARG002
        """X988 一次激活即标记 used，不暴露 cancel API；返回 True 维持接口契约。"""
        return True

    def get_billing(self, card_key: str) -> Optional[dict[str, str]]:  # noqa: ARG002
        """billing 信息已包含在 verify 响应里，由 CardProvider 适配层组装。"""
        return None

    def wait_for_3ds(self, card_key: str, timeout_sec: int = 300) -> Optional[str]:
        """轮询 X988 提供的 sms_api 拉取 3DS 验证码。

        返回提取到的验证码字符串；超时或失败返回 None。
        """
        sms_api = self._last_meta.get("sms_api") or ""
        if not sms_api:
            logger.error("X988 wait_for_3ds 缺少 sms_api，请先调 get_card。")
            return None

        logger.info("X988 监控 3DS 验证码 (超时 %ds)...", timeout_sec)
        start = time.time()
        attempt = 0
        seen_codes: set[str] = set()
        while time.time() - start < timeout_sec:
            attempt += 1
            try:
                resp = requests.get(sms_api, timeout=self._request_timeout)
            except requests.RequestException as exc:
                logger.warning("X988 拉取短信失败 [%d]: %s", attempt, exc)
                time.sleep(5)
                continue

            text = resp.text or ""
            # 优先 JSON 形态（兼容不同 SMS 服务），失败则按纯文本处理
            data: object = None
            try:
                data = resp.json()
            except ValueError:
                data = None

            extract_target = ""
            if isinstance(data, dict):
                # 常见字段：sms / message / content / data
                for key in ("sms", "message", "content", "data", "text"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        extract_target = val
                        break
                # 嵌套：data.sms 等
                if not extract_target and isinstance(data.get("data"), dict):
                    inner = data["data"]
                    for key in ("sms", "message", "content", "text"):
                        val = inner.get(key)
                        if isinstance(val, str) and val.strip():
                            extract_target = val
                            break
            if not extract_target:
                extract_target = text

            match = _OTP_PATTERN.search(extract_target)
            if match:
                code = match.group(1)
                if code in seen_codes:
                    # 同一条短信被反复返回，等下一轮
                    time.sleep(3)
                    continue
                seen_codes.add(code)
                logger.info("X988 捕获 3DS 验证码: %s", code)
                return code

            # 未匹配到，等待后重试
            time.sleep(3)

        logger.warning("X988 wait_for_3ds 超时（%ds）。", timeout_sec)
        return None
