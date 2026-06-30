# -*- coding: utf-8 -*-
"""5sim 接码平台 Provider（5sim.net，原生 JSON API）。

与 SMS-Activate 兼容族（hero/grizzly/smsbower）不同，5sim 是**原生异构协议**：
- 鉴权：``Authorization: Bearer <api_key>``（而非 query 里的 api_key）
- 取号：``GET /v1/user/buy/activation/{country}/{operator}/{product}`` → JSON ``{id, phone, ...}``
- 取码：``GET /v1/user/check/{id}`` → JSON ``{status, sms:[{code, text}, ...]}``
- 余额：``GET /v1/user/profile`` → JSON ``{balance, ...}``

因此**不复用 SMSManager**（那是 sms-activate 字符串协议 ``ACCESS_NUMBER:`` 的专用客户端），
本类直接在 SmsProvider ABC 下自带 requests + JSON 实现。

设计立场（精简到 ABC 三方法）：
- 只实现 ``get_number`` / ``get_code`` / ``test_connection``，签名与 SMSManager 一致，
  靠**鸭子类型**在 main.py 的 ``sms_api`` 位置无缝顶替。
- 国家链降级（主国家无货 → 按序尝试 fallback），与兼容族对齐。
- **不移植** GuJumpgate 扩展里的 reuse/cancel/ban/价格优先级排序/多轮重试等富交互
  —— 自动化后端用不到，移植即增加无谓复杂度。
- 价格上限 ``max_price`` 作为可选过滤：取号前查 ``/v1/guest/prices`` 当前价，
  超限则跳过该国家（与 sms_activate 的价格降级语义对齐）。

参考来源：GuJumpgate ``phone-sms/providers/five-sim.js``（取其端点与字段，舍其交互层）。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

from src.models import SMSOrder
from src.providers.base import FieldSpec, register_provider
from src.providers.sms import SmsProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://5sim.net"
_REQUEST_TIMEOUT = 30
_POLL_INTERVAL_SECONDS = 5
# 取码轮询的终态状态：命中即停止轮询（订单已死，再等也无码）
_TERMINAL_STATUSES = frozenset({"CANCELED", "BANNED", "FINISHED", "TIMEOUT"})

# 申号瞬时错误重试参数（对齐 src/sms.py SMSManager.get_number 的指数退避模式）。
# 覆盖 5sim 服务端偶发的：空 body（批量并发期间瞬态）/ 5xx / 429 限流 /
# 网络层 SSL EOF / 连接超时。仅作用于 _buy_activation 申号入口；
# get_code 已自带轮询（等价内置重试），不叠加。
_MAX_TRANSIENT_RETRIES = 3          # 共最多 4 次尝试（首次 + 3 次重试）
_RETRY_BACKOFF_BASE_SECONDS = 1.5   # 指数退避基数：1.5/3.0/4.5s（见 _RETRY_BACKOFF_CAP_SECONDS 封顶）
_RETRY_BACKOFF_CAP_SECONDS = 6.0    # 退避上限：瞬时抖动长等无益，封顶避免单轮拖太久
# 业务层失败状态码：5sim 明确拒绝（鉴权/欠费/参数错/真无货），重试无意义，fail-fast。
# 注意 404 归类为业务错（产品/国家 slug 不存在），不重试。
_FATAL_STATUS_CODES = frozenset({400, 401, 402, 403, 404, 422})


def _parse_price(value: object) -> Optional[float]:
    """容错解析价格 —— 空/非法返回 None。"""
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
    """主国家 + fallback 列表去重（与 sms_activate._parse_country_chain 同语义）。"""
    primary = str(primary or "").strip() or "vietnam"
    chain = [primary]
    if fallback:
        for raw in re.split(r"[;,]", str(fallback)):
            c = raw.strip()
            if c and c != primary and c not in chain:
                chain.append(c)
    return chain


def _extract_code(text: object) -> str:
    """从短信文本/code 字段抠出 4-8 位验证码。"""
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"\b(\d{4,8})\b", s)
    return m.group(1) if m else ""


@register_provider(
    provider_type="sms",
    kind="five_sim",
    display_name="5sim 接码平台",
    description="对接 5sim.net（原生 JSON API，Bearer 鉴权），支持多国家降级 + 价格上限筛选",
    schema=(
        FieldSpec(
            name="api_key",
            type="secret",
            required=True,
            description="5sim API key（去 5sim.net 后台 → API 页生成的 JWT token）",
        ),
        FieldSpec(
            name="country",
            type="str",
            required=True,
            default="vietnam",
            description="主国家 slug（vietnam=越南, indonesia=印尼, england=英国, usa=美国, russia=俄罗斯）",
            choices=("vietnam", "indonesia", "england", "usa", "russia"),
        ),
        FieldSpec(
            name="country_fallback",
            type="str",
            required=False,
            default="",
            description="备选国家 slug 列表，逗号或分号分隔（如 'indonesia;england'）；主国家无货时按序降级",
        ),
        FieldSpec(
            name="product",
            type="str",
            required=False,
            default="openai",
            description="产品代码（openai=OpenAI/ChatGPT）；5sim 用产品名而非数字服务码",
        ),
        FieldSpec(
            name="operator",
            type="str",
            required=False,
            default="any",
            description="运营商 slug 或 'any'（任意运营商）",
        ),
        FieldSpec(
            name="max_price",
            type="str",
            required=False,
            default="",
            description="单号价格上限（5sim 账户货币 RUB，留空不限）；超过则跳过该国家",
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
class FiveSimProvider(SmsProvider):
    """5sim 适配器：原生 JSON API，鸭子类型顶替 SMSManager。"""

    BASE_URL = _BASE_URL

    def __init__(
        self,
        api_key: str,
        country: str = "vietnam",
        country_fallback: str = "",
        product: str = "openai",
        operator: str = "any",
        max_price: str = "",
        max_retries: int = 30,
        proxy: str = "",
    ) -> None:
        if not str(api_key or "").strip():
            raise ValueError("5sim api_key 不能为空")

        self._api_key = str(api_key).strip()
        self._country_chain = _parse_country_chain(country, country_fallback)
        self._product = str(product or "openai").strip() or "openai"
        self._operator = str(operator or "any").strip() or "any"
        self._max_price = _parse_price(max_price)
        try:
            self._max_retries = int(max_retries) if max_retries else 30
        except (TypeError, ValueError):
            self._max_retries = 30
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    @property
    def country_chain(self) -> list[str]:
        """暴露给测试断言用。"""
        return list(self._country_chain)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def get_number(self, service: str = "") -> Optional[SMSOrder]:
        """按国家链 + 价格上限逐级降级申号。

        Args:
            service: 5sim 用 product 概念（如 'openai'）；非空时覆盖默认 product

        Returns:
            成功返回 SMSOrder（order_id=5sim activation id, phone_number=号码），失败 None
        """
        product = str(service or "").strip() or self._product

        for country in self._country_chain:
            if self._max_price is not None:
                price = self._query_min_price(country, product)
                if price is None:
                    logger.warning("5sim: country=%s product=%s 价格查询失败，降级", country, product)
                    continue
                if price > self._max_price:
                    logger.info("5sim: country=%s 当前最低价 %.2f > max %.2f，降级", country, price, self._max_price)
                    continue
                logger.info("5sim: country=%s 当前最低价 %.2f 符合上限，申号", country, price)

            order = self._buy_activation(country, product)
            if order is not None:
                logger.info(
                    "5sim: 申领成功 country=%s order=%s phone=%s",
                    country, order.order_id, order.phone_number,
                )
                return order
            logger.warning("5sim: country=%s 申领失败/无货，尝试下一国家", country)

        logger.error(
            "5sim: 全部 %d 个国家均无可用号码 (chain=%s)",
            len(self._country_chain), self._country_chain,
        )
        return None

    def get_code(self, order_id: str, max_retries: int = 0) -> Optional[str]:
        """轮询 ``/v1/user/check/{id}`` 等待验证码。

        终态状态（CANCELED/BANNED/FINISHED/TIMEOUT）命中即停止，不空等到超时。
        """
        retries = int(max_retries) if max_retries else self._max_retries
        url = f"{self.BASE_URL}/v1/user/check/{order_id}"

        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(
                    url, headers=self._headers(),
                    timeout=_REQUEST_TIMEOUT, proxies=self._proxies,
                )
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("5sim 查询验证码异常 (尝试 %d/%d): %s", attempt, retries, exc)
                time.sleep(_POLL_INTERVAL_SECONDS)
                continue

            code = self._extract_code_from_check(payload)
            if code:
                logger.info("5sim: 成功获取验证码: %s", code)
                return code

            status = str(payload.get("status") or "").strip().upper() if isinstance(payload, dict) else ""
            if status in _TERMINAL_STATUSES:
                logger.warning("5sim: 订单 %s 进入终态 %s，停止轮询", order_id, status)
                return None

            time.sleep(_POLL_INTERVAL_SECONDS)

        logger.warning("5sim: 获取验证码超时 (共尝试 %d 次)", retries)
        return None

    def test_connection(self) -> dict:
        """调 ``/v1/user/profile`` 验证 token 有效性 + 取余额。"""
        url = f"{self.BASE_URL}/v1/user/profile"
        try:
            resp = requests.get(
                url, headers=self._headers(),
                timeout=_REQUEST_TIMEOUT, proxies=self._proxies,
            )
        except requests.RequestException as exc:
            return {
                "ok": False,
                "message": f"请求异常: {exc}（若国内 IP 直连，请确认 proxy 字段已配置）",
                "balance": None,
            }

        if resp.status_code in (401, 403):
            return {
                "ok": False,
                "message": f"token 无效或权限不足 (HTTP {resp.status_code})",
                "balance": None,
            }
        try:
            payload = resp.json()
            bal = float(payload.get("balance"))
        except (ValueError, TypeError):
            return {
                "ok": False,
                "message": f"余额解析失败，原始响应: {resp.text[:200]}",
                "balance": None,
            }
        return {
            "ok": True,
            "message": f"连通正常，余额 {bal:.2f} RUB",
            "balance": bal,
        }

    def _buy_activation(self, country: str, product: str) -> Optional[SMSOrder]:
        """调 ``/v1/user/buy/activation/{country}/{operator}/{product}`` 申号。

        重试策略（对齐 SMSManager.get_number）：
        - **瞬时错误**（网络异常 / 5xx / 429 限流 / 空 body / 非 JSON）→ 指数退避重试，
          应对 5sim 服务端批量并发期间偶发的空响应抖动（即本次故障的根因）。
        - **业务错误**（400/401/402/403/404/422 等明确拒绝，或 200 但无号码）→ fail-fast，
          重试无意义；状态码与 body 片段写入日志，避免"Expecting value"掩盖真因。
        """
        url = f"{self.BASE_URL}/v1/user/buy/activation/{country}/{self._operator}/{product}"
        total_attempts = _MAX_TRANSIENT_RETRIES + 1

        for attempt in range(1, total_attempts + 1):
            transient_reason: Optional[str] = None
            try:
                resp = requests.get(
                    url, headers=self._headers(),
                    timeout=_REQUEST_TIMEOUT, proxies=self._proxies,
                )
            except requests.RequestException as exc:
                transient_reason = f"网络异常: {exc}"
            else:
                status = resp.status_code
                body_preview = resp.text[:200]
                # 业务层失败：明确拒绝，不重试
                if status in _FATAL_STATUS_CODES:
                    logger.error(
                        "5sim 申号被拒 (country=%s HTTP %d): %r",
                        country, status, body_preview,
                    )
                    return None
                # 服务端瞬时错误（5xx / 429 限流）→ 重试
                if status >= 500 or status == 429:
                    transient_reason = f"服务端瞬时错误 HTTP {status}: {body_preview!r}"
                else:
                    try:
                        payload = resp.json()
                    except ValueError:
                        # 200/2xx 但 body 非 JSON（空 body / 纯文本）→ 视为瞬时抖动重试
                        transient_reason = f"非 JSON 响应 HTTP {status}: {body_preview!r}"
                    else:
                        if not isinstance(payload, dict):
                            logger.warning("5sim 申号返回非预期结构 (country=%s): %r", country, payload)
                            return None
                        activation_id = str(payload.get("id") or "").strip()
                        phone = str(payload.get("phone") or "").strip()
                        if activation_id and phone:
                            return SMSOrder(order_id=activation_id, phone_number=phone)
                        # 200 但无号码（如 no free phones 的 JSON 形态）→ 业务无货，不重试
                        logger.warning(
                            "5sim 申号无可用号码 (country=%s): %s",
                            country, str(payload)[:200],
                        )
                        return None

            # 走到这里说明是瞬时错误：退避后重试（除非已是最后一次）
            is_last = attempt == total_attempts
            if is_last:
                logger.error(
                    "5sim 申号瞬时失败 (country=%s 尝试 %d/%d，已达上限): %s",
                    country, attempt, total_attempts, transient_reason,
                )
                return None
            backoff = min(
                _RETRY_BACKOFF_BASE_SECONDS * attempt,
                _RETRY_BACKOFF_CAP_SECONDS,
            )
            logger.warning(
                "5sim 申号瞬时失败 (country=%s 尝试 %d/%d): %s；%.1fs 后重试",
                country, attempt, total_attempts, transient_reason, backoff,
            )
            time.sleep(backoff)

        return None

    def _query_min_price(self, country: str, product: str) -> Optional[float]:
        """调 ``/v1/guest/prices?country=&product=`` 取当前最低价（无需鉴权）。

        响应结构嵌套较深（country → product → operator → {cost, count}），
        递归收集所有 cost 取最小值；失败返回 None 让调用方降级。
        """
        url = f"{self.BASE_URL}/v1/guest/prices"
        try:
            resp = requests.get(
                url, params={"country": country, "product": product},
                headers={"Accept": "application/json"},
                timeout=_REQUEST_TIMEOUT, proxies=self._proxies,
            )
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("5sim getPrices 异常 (country=%s): %s", country, exc)
            return None

        costs = list(self._collect_costs(payload))
        return min(costs) if costs else None

    @staticmethod
    def _collect_costs(node: object):
        """递归遍历价格 payload，yield 所有 in-stock 的 cost。"""
        if isinstance(node, dict):
            cost = _parse_price(node.get("cost"))
            count = node.get("count")
            in_stock = count is None or (_parse_price(count) or 0) > 0
            if cost is not None and in_stock:
                yield cost
            for value in node.values():
                yield from FiveSimProvider._collect_costs(value)
        elif isinstance(node, list):
            for item in node:
                yield from FiveSimProvider._collect_costs(item)

    @staticmethod
    def _extract_code_from_check(payload: object) -> str:
        """从 ``/v1/user/check`` 响应的 ``sms[]`` 数组取最新验证码。"""
        if not isinstance(payload, dict):
            return ""
        sms_list = payload.get("sms")
        if isinstance(sms_list, list):
            # 从最新一条往前找
            for msg in reversed(sms_list):
                if not isinstance(msg, dict):
                    continue
                code = _extract_code(msg.get("code")) or _extract_code(msg.get("text"))
                if code:
                    return code
        # 兜底：顶层 code 字段
        return _extract_code(payload.get("code"))
