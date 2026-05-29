# -*- coding: utf-8 -*-
"""SMS-Activate 接码平台 Provider 适配器。

设计立场：
- **SMSManager 保持不动**：HTTP 通信逻辑、getNumber/getStatus 调用都委托给现有
  ``src/sms.py`` 的 SMSManager 客户端
- **本适配器只做「外层编排」**：价格区间筛选 + 多国家降级 + 连通性自检
- **借鉴 FlowPilot 模式**（``/Volumes/workSpace/study/aiProject/FlowPilot/phone-sms/providers/five-sim.js``）：
  价格降级链 + country_fallback；但简化成「查 1 次当前价 + 主国家无货降级」——
  SMS-Activate 的 getNumber 不支持 maxPrice query 参数，所以无法像 5sim 那样
  从低到高扫一组价位

API 端点：
- ``getPrices``: 查指定 country+service 的当前价格（{country:{service:{cost, count}}}）
- ``getNumber``: 申领号码（由 SMSManager 调用）
- ``getStatus``: 轮询验证码（由 SMSManager 调用）
- ``getBalance``: 余额查询（test_connection 用）

约束：
- ``operator`` 与 ``max_price`` 互斥（SMS-Activate API 不支持组合，FlowPilot 实测）
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from src.models import SMSOrder
from src.providers.base import FieldSpec, register_provider
from src.providers.sms import SmsProvider
from src.sms import SMSManager

logger = logging.getLogger(__name__)

_API_URL = "https://api.sms-activate.org/steward.php"
_PRICE_QUERY_TIMEOUT = 60
_BALANCE_QUERY_TIMEOUT = 30


def _parse_price(value: object) -> Optional[float]:
    """容错解析价格字段 —— 空字符串返回 None，非法数字也返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_country_chain(primary: str, fallback: str) -> list[str]:
    """解析国家链：主国家 + fallback 列表去重。

    fallback 支持 ``;`` 和 ``,`` 双分隔符；自动去掉与主 country 重复的项与空白。
    """
    primary = str(primary or "").strip() or "6"
    chain = [primary]
    if fallback:
        for raw in re.split(r"[;,]", str(fallback)):
            c = raw.strip()
            if c and c != primary and c not in chain:
                chain.append(c)
    return chain


@register_provider(
    provider_type="sms",
    kind="sms_activate",
    display_name="SMS-Activate 接码平台",
    description="对接 sms-activate.org，支持价格区间筛选 + 多国家降级",
    schema=(
        FieldSpec(
            name="api_key",
            type="secret",
            required=True,
            description="SMS-Activate API key（去 sms-activate.org 后台获取）",
        ),
        FieldSpec(
            name="country",
            type="str",
            required=True,
            default="6",
            description="主国家代码（6=印尼, 0=俄罗斯, 12=英国, 187=美国, 22=印度）",
        ),
        FieldSpec(
            name="country_fallback",
            type="str",
            required=False,
            default="",
            description="备选国家代码列表，逗号或分号分隔（如 '0;22;12'）；主国家无货时按序降级",
        ),
        FieldSpec(
            name="max_price",
            type="str",
            required=False,
            default="",
            description="单号价格上限（卢布 RUB，留空不限）；超过则跳过该国家",
        ),
        FieldSpec(
            name="min_price",
            type="str",
            required=False,
            default="",
            description="单号价格下限（卢布 RUB，留空不限）；低于则视为可疑回收号跳过",
        ),
        FieldSpec(
            name="operator",
            type="str",
            required=False,
            default="any",
            description="运营商代码或 'any'；注意与 max_price 互斥（API 限制）",
        ),
        FieldSpec(
            name="service",
            type="str",
            required=False,
            default="dr",
            description="服务代码（dr=OpenAI/ChatGPT, go=Google, tg=Telegram）",
        ),
        FieldSpec(
            name="max_retries",
            type="int",
            required=False,
            default=30,
            description="轮询验证码最大次数（每次约 5 秒间隔，默认 30 = 共 ~150 秒）",
        ),
        FieldSpec(
            name="proxy",
            type="str",
            required=False,
            default="",
            description="出口代理 URL，可选；如 socks5h://...",
        ),
    ),
)
class SmsActivateProvider(SmsProvider):
    """SMS-Activate 适配器：委托 SMSManager 做 HTTP，在外层加价格/国家降级。"""

    def __init__(
        self,
        api_key: str,
        country: str = "6",
        country_fallback: str = "",
        max_price: str = "",
        min_price: str = "",
        operator: str = "any",
        service: str = "dr",
        max_retries: int = 30,
        proxy: str = "",
    ) -> None:
        if not str(api_key or "").strip():
            raise ValueError("SMS-Activate api_key 不能为空")

        self._api_key = api_key
        self._country_chain = _parse_country_chain(country, country_fallback)
        self._max_price = _parse_price(max_price)
        self._min_price = _parse_price(min_price)
        self._operator = str(operator or "any").strip().lower() or "any"
        self._service = str(service or "dr").strip() or "dr"
        try:
            self._max_retries = int(max_retries) if max_retries else 30
        except (TypeError, ValueError):
            self._max_retries = 30
        self._proxy = proxy or ""

        # FlowPilot 实测：operator 指定具体值时不能同时用 maxPrice
        if self._operator != "any" and self._max_price is not None:
            raise ValueError(
                "operator 与 max_price 互斥（SMS-Activate API 不支持组合），"
                "请将 operator 设为 'any' 或清空 max_price"
            )

        # 每个国家一个 SMSManager 实例（country 是 SMSManager 构造期参数）
        self._managers: dict[str, SMSManager] = {
            c: SMSManager(api_key=api_key, country=c, proxy=proxy)
            for c in self._country_chain
        }

    @property
    def country_chain(self) -> list[str]:
        """暴露给测试断言用。"""
        return list(self._country_chain)

    def get_number(self, service: str = "") -> Optional[SMSOrder]:
        """按国家链 + 价格区间逐级降级申号。"""
        svc = str(service or "").strip() or self._service
        need_price_check = self._max_price is not None or self._min_price is not None

        for country in self._country_chain:
            if need_price_check:
                price = self._query_current_price(country, svc)
                if price is None:
                    logger.warning(
                        "sms_activate: country=%s service=%s 价格查询失败，降级",
                        country, svc,
                    )
                    continue
                if self._max_price is not None and price > self._max_price:
                    logger.info(
                        "sms_activate: country=%s 当前价 %.2f > max %.2f，降级",
                        country, price, self._max_price,
                    )
                    continue
                if self._min_price is not None and price < self._min_price:
                    logger.info(
                        "sms_activate: country=%s 当前价 %.2f < min %.2f，跳过",
                        country, price, self._min_price,
                    )
                    continue
                logger.info(
                    "sms_activate: country=%s 当前价 %.2f 符合区间，申号",
                    country, price,
                )

            order = self._managers[country].get_number(service=svc)
            if order is not None:
                logger.info(
                    "sms_activate: 申领成功 country=%s order=%s phone=%s",
                    country, order.order_id, order.phone_number,
                )
                return order
            logger.warning(
                "sms_activate: country=%s 申领失败/无货，尝试下一国家", country,
            )

        logger.error(
            "sms_activate: 全部 %d 个国家均无可用号码 (chain=%s)",
            len(self._country_chain), self._country_chain,
        )
        return None

    def get_code(self, order_id: str, max_retries: int = 0) -> Optional[str]:
        """轮询验证码。SMSManager.get_code 不感知 country，复用首个 manager。"""
        retries = int(max_retries) if max_retries else self._max_retries
        first_manager = next(iter(self._managers.values()))
        return first_manager.get_code(order_id, max_retries=retries)

    def test_connection(self) -> dict:
        """调 getBalance 验证 api_key 有效性。

        SMS-Activate 响应格式：``ACCESS_BALANCE:42.50`` 或 ``BAD_KEY`` / ``ERROR_SQL`` 等。
        """
        params = {"api_key": self._api_key, "action": "getBalance"}
        proxies = (
            {"http": self._proxy, "https": self._proxy} if self._proxy else None
        )
        try:
            resp = requests.get(
                _API_URL,
                params=params,
                timeout=_BALANCE_QUERY_TIMEOUT,
                proxies=proxies,
            )
            text = resp.text.strip()
        except requests.RequestException as exc:
            return {
                "ok": False,
                "message": f"请求异常: {exc}（若国内 IP 直连，请确认 proxy 字段已配置）",
                "balance": None,
            }

        if text.startswith("ACCESS_BALANCE:"):
            try:
                bal = float(text.split(":", 1)[1])
            except (ValueError, IndexError):
                return {
                    "ok": False,
                    "message": f"余额解析失败，原始响应: {text}",
                    "balance": None,
                }
            return {
                "ok": True,
                "message": f"连通正常，余额 {bal:.2f} RUB",
                "balance": bal,
            }

        return {
            "ok": False,
            "message": f"API 返回: {text}（常见错误: BAD_KEY=密钥无效, ERROR_SQL=平台故障）",
            "balance": None,
        }

    def _query_current_price(self, country: str, service: str) -> Optional[float]:
        """调 getPrices 拿当前 country+service 的单价。

        响应结构：``{country: {service: {cost: float, count: int}}}``
        失败时返回 None，让调用方降级到下一国家。
        """
        params = {
            "api_key": self._api_key,
            "action": "getPrices",
            "service": service,
            "country": country,
        }
        proxies = (
            {"http": self._proxy, "https": self._proxy} if self._proxy else None
        )
        try:
            resp = requests.get(
                _API_URL,
                params=params,
                timeout=_PRICE_QUERY_TIMEOUT,
                proxies=proxies,
            )
            payload = resp.json()
            cost = payload[str(country)][str(service)]["cost"]
            return float(cost)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "sms_activate getPrices 异常 (country=%s service=%s): %s",
                country, service, exc,
            )
            return None
