# -*- coding: utf-8 -*-
"""在线地址/姓名生成器（首选真实数据源 + 静默失败降级）。

设计动机：
  合成卡需要配套的「真实感」账单地址 + 姓名 + 电话。本地静态池
  （`country_profiles.fallback_addresses`）地址量有限且会重复，规模化使用
  易被 AVS velocity 风控标记。在线生成器能提供更丰富的真实地址。

数据源路由：
  - US / GB / CA → randomuser.me（免费、无需 key、稳定，支持 21 国，含这三国）
  - SG / HK      → fakerapi.it（randomuser.me 不支持新加坡/香港）
  - 其余情况     → 返回 None，由调用方回退本地池

健壮性约定（仿 `bin_lookup.py` 的"静默失败"模式）：
  - 任何网络异常 / 超时（3s）/ 非 200 / JSON 解析失败 / 关键字段缺失
    → 一律返回 None（**绝不抛异常**），写 warning 日志。
  - 合成卡是手动生成场景，在线失败可接受地回退本地池，不能阻塞生成。
  - `http_get` 可注入，便于单测 mock，不打真实网络。

⚠️ 注意：本模块拿到的是**第三方随机生成**的假数据（非真人 PII），
  仅用于合成卡的表单填充占位，不涉及真实身份信息。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SEC = 3

# randomuser.me 支持的 nat 集合里我们用到的三国（其余国家不走此源）
_RANDOMUSER_NATS = {"US", "GB", "CA"}
# fakerapi.it 兜底覆盖的国家。
# ⚠️ 2026-05-29 实测：fakerapi.it /addresses 接口**无视 country_code 参数**，
#    请求 JP 返回 BF（布基纳法索）、请求 SG 返回 KZ（哈萨克斯坦），邮编格式也不符。
#    故暂时清空——SG/HK/JP 直接走本地预置真实地址池，不发无效的 3s 超时请求。
#    `_parse_fakerapi` 的国家校验逻辑保留：未来若换可信的 fakerapi 兼容端点，
#    把对应国家加回此集合即可复用。
_FAKERAPI_COUNTRIES: set[str] = set()

_RANDOMUSER_URL = "https://randomuser.me/api/?nat={nat}&results=1&inc=name,location,phone"
_FAKERAPI_URL = "https://fakerapi.it/api/v2/addresses?_quantity=1&country_code={cc}"


@dataclass(frozen=True)
class OnlineIdentity:
    """在线生成的身份+地址套件（统一字段，吸收两个 API 的结构差异）。

    state 对无州概念的国家（GB/SG/HK）为空串；
    postal 对无邮编的国家（HK）为空串；
    phone 可能为空（fakerapi 地址接口不返回电话，由调用方本地补）。
    """
    first_name: str
    last_name: str
    line1: str
    city: str
    state: str
    postal: str
    phone: str = ""

    @property
    def has_name(self) -> bool:
        return bool(self.first_name and self.last_name)

    @property
    def has_address(self) -> bool:
        return bool(self.line1 and self.city)


def _default_http_get(url: str) -> dict:
    """默认 HTTP 实现：仅 200 + 可解析 JSON 时返回 payload，否则抛 RuntimeError。"""
    resp = requests.get(url, timeout=_HTTP_TIMEOUT_SEC)
    status = getattr(resp, "status_code", None)
    if status != 200:
        raise RuntimeError(f"online_identity HTTP {status}")
    return resp.json()


def _safe_str(value: object) -> str:
    """把任意值安全转字符串并 strip；None → ""。"""
    if value is None:
        return ""
    return str(value).strip()


def _parse_randomuser(payload: dict) -> Optional[OnlineIdentity]:
    """解析 randomuser.me 响应。

    结构（官方文档稳定）：
      results[0].name.{first,last}
      results[0].location.{street.{number,name}, city, state, postcode}
      results[0].phone
    任何关键字段缺失返回 None。
    """
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    rec = results[0]
    if not isinstance(rec, dict):
        return None

    name = rec.get("name") or {}
    location = rec.get("location") or {}
    street = location.get("street") or {}

    first = _safe_str(name.get("first"))
    last = _safe_str(name.get("last"))
    # street 可能是 {"number":123,"name":"Main St"} 或直接字符串
    if isinstance(street, dict):
        number = _safe_str(street.get("number"))
        st_name = _safe_str(street.get("name"))
        line1 = f"{number} {st_name}".strip()
    else:
        line1 = _safe_str(street)

    city = _safe_str(location.get("city"))
    state = _safe_str(location.get("state"))
    # postcode 可能是数字或字符串
    postal = _safe_str(location.get("postcode"))
    phone = _safe_str(rec.get("phone")) or _safe_str(rec.get("cell"))

    identity = OnlineIdentity(
        first_name=first,
        last_name=last,
        line1=line1,
        city=city,
        state=state,
        postal=postal,
        phone=phone,
    )
    # 至少要有姓名 + 地址主体，否则视为无效
    if not (identity.has_name and identity.has_address):
        return None
    return identity


def _parse_fakerapi(payload: dict, expected_cc: str) -> Optional[OnlineIdentity]:
    """解析 fakerapi.it /addresses 响应。

    结构：
      data[0].{street, streetName, buildingNumber, city, zipcode, country, country_code}
    fakerapi 地址接口**不返回姓名/电话**，姓名由调用方用本地池补。
    若返回的国家与请求不符（fakerapi 对部分国家会 fallback），视为无效返回 None。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    rec = data[0]
    if not isinstance(rec, dict):
        return None

    # fakerapi 不保证一定带 country_code；若带了且不符则判无效
    returned_cc = _safe_str(rec.get("country_code")).upper()
    if returned_cc and returned_cc != expected_cc.upper():
        logger.warning(
            "fakerapi 返回国家 %s 与请求 %s 不符，判无效（回退本地池）",
            returned_cc, expected_cc,
        )
        return None

    # 街道：优先 street，其次 streetName + buildingNumber 拼接
    line1 = _safe_str(rec.get("street"))
    if not line1:
        bn = _safe_str(rec.get("buildingNumber"))
        sn = _safe_str(rec.get("streetName"))
        line1 = f"{bn} {sn}".strip()

    city = _safe_str(rec.get("city"))
    postal = _safe_str(rec.get("zipcode"))

    identity = OnlineIdentity(
        first_name="",  # fakerapi 地址接口无姓名，调用方本地补
        last_name="",
        line1=line1,
        city=city,
        state="",  # SG/HK 无 state
        postal=postal,
        phone="",
    )
    if not identity.has_address:
        return None
    return identity


def fetch_online_identity(
    country: str,
    *,
    http_get: Callable[[str], dict] | None = None,
) -> Optional[OnlineIdentity]:
    """拉取指定国家的在线身份+地址。

    Args:
        country: ISO alpha-2 国家码（大小写不敏感）。
        http_get: 可注入的 HTTP 获取函数（测试 mock 用）；None 走 requests。

    Returns:
        OnlineIdentity（成功）或 None（不支持的国家 / 任何失败 → 调用方回退本地池）。
        注意：fakerapi 来源的 OnlineIdentity.first_name/last_name 为空，
        调用方需用本地姓名池补全。
    """
    cc = (country or "").strip().upper()
    if cc not in _RANDOMUSER_NATS and cc not in _FAKERAPI_COUNTRIES:
        return None

    fetch = http_get if http_get is not None else _default_http_get

    try:
        if cc in _RANDOMUSER_NATS:
            url = _RANDOMUSER_URL.format(nat=cc.lower())
            payload = fetch(url)
            return _parse_randomuser(payload)
        else:  # fakerapi（SG/HK）
            url = _FAKERAPI_URL.format(cc=cc)
            payload = fetch(url)
            return _parse_fakerapi(payload, expected_cc=cc)
    except Exception as exc:  # noqa: BLE001 — 静默失败，回退本地池
        logger.warning("在线身份拉取失败 (country=%s): %s", cc, exc)
        return None


__all__ = ["OnlineIdentity", "fetch_online_identity"]
