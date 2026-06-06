# -*- coding: utf-8 -*-
"""多国 AVS 友好住宅地址池 + 电话生成（选址冷却 + 多国路由）。

设计：
  - 各国真实公开商业/住宅地址集中在 `country_profiles.py`，本模块负责
    「按国家选址 + 24h 冷却 + 按国家生成电话」的逻辑。
  - 地址数据形态见 `country_profiles.py`（US 有 state/ZIP，GB 有 postcode 无 state，
    SG 6 位邮编，HK 无邮编等）。
  - `BillingAddress` 纯数据类定义在本模块（被 country_profiles 单向 import）；
    本模块函数体内延迟 import country_profiles，打破循环依赖。

注：在线地址生成器（`online_identity.py`）是**首选**真实地址来源，
    本模块地址池是在线 API 失败时的**本地兜底**。

风控避坑（沿用）：
  ❌ PO Box / 虚拟邮件地址 / 同一地址高频复用
  ✅ 大城市商业社区；区号/号段匹配地区
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple


@dataclass(frozen=True)
class BillingAddress:
    """住宅/商业地址（多国通用）。

    字段语义按国家不同：
      - state：US 为 2 字母州码 / CA 为省码；GB/SG/HK 为空串（无 state 概念）
      - zip_code：US 5 位 ZIP / CA 6 位 postal code / GB UK postcode /
                  SG 6 位邮编；HK 为空串（无邮编）
      - area_code：US/CA 为 NANP 电话区号；GB 为城市电话区号；
                   SG/HK 复用为移动号段首位占位（无区号概念）
    """
    line1: str
    city: str
    state: str
    zip_code: str
    area_code: str

    def to_dict(self) -> dict[str, str]:
        return {
            "line1": self.line1,
            "city": self.city,
            "state": self.state,
            "zip": self.zip_code,
            "area_code": self.area_code,
        }


def _pool_for(country: str | None) -> tuple[BillingAddress, ...]:
    """取指定国家的本地兜底地址池（延迟 import 打破循环依赖）。"""
    from src.fintech.country_profiles import get_profile
    return get_profile(country).fallback_addresses


def list_all_addresses(country: str | None = None) -> list[BillingAddress]:
    """返回指定国家地址池全部地址（运维查看用）。默认 US（向后兼容）。"""
    return list(_pool_for(country or "US"))


def pick_random_address(seed: str | None = None, country: str | None = None) -> BillingAddress:
    """从指定国家地址池随机选一个地址。

    Args:
        seed: 确定性种子；同 seed 永远返回同一地址（用于绑同账号同卡）
              None 走真随机
        country: ISO alpha-2 国家码；默认 US（向后兼容）
    """
    pool = _pool_for(country or "US")
    if seed is not None:
        import random
        rng = random.Random(seed)
        return rng.choice(pool)
    # 真随机用 secrets（cryptographically secure）
    return secrets.choice(pool)


def generate_phone(
    country: str | None = None,
    area_code: str | None = None,
    seed: str | None = None,
) -> str:
    """按国家生成电话号码（多国分派）。

    Args:
        country: ISO alpha-2 国家码；默认 US
        area_code: US/CA 的 NANP 区号 / GB 城市区号 / SG/HK 的移动号段首位占位
        seed: 确定性种子

    Returns:
        - US/CA(nanp): "(XXX) YYY-ZZZZ"
        - GB:          "+44 20 XXXX XXXX"（area_code 为城市区号，如 "20"）
        - SG:          "+65 XXXX XXXX"（首位取 area_code 占位的 8/9，否则随机 8/9）
        - HK:          "+852 XXXX XXXX"（首位取 area_code 占位的 5/6/9）
    """
    from src.fintech.country_profiles import get_profile
    kind = get_profile(country or "US").phone_kind

    if seed is not None:
        import random
        rng = random.Random(seed)
    else:
        rng = secrets.SystemRandom()

    if kind == "nanp":
        return _gen_nanp_phone(area_code or "212", rng)
    if kind == "gb":
        return _gen_gb_phone(area_code or "20", rng)
    if kind == "sg":
        return _gen_sg_phone(area_code or "9", rng)
    if kind == "hk":
        return _gen_hk_phone(area_code or "9", rng)
    if kind == "jp":
        return _gen_jp_phone(area_code or "3", rng)
    # 兜底走 NANP
    return _gen_nanp_phone(area_code or "212", rng)


def _gen_nanp_phone(area_code: str, rng) -> str:
    """NANP (US/CA): (XXX) YYY-ZZZZ；中间首位 2-9，避开 555 测试段。"""
    central_first = rng.randint(2, 9)
    central_rest = rng.randint(0, 99)
    while central_first == 5 and central_rest == 55:
        central_first = rng.randint(2, 9)
        central_rest = rng.randint(0, 99)
    central = f"{central_first}{central_rest:02d}"
    last_four = rng.randint(0, 9999)
    return f"({area_code}) {central}-{last_four:04d}"


def _gen_gb_phone(area_code: str, rng) -> str:
    """GB: +44 <区号> XXXX XXXX（伦敦 020 区号去前导 0 为 "20"）。"""
    part1 = rng.randint(1000, 9999)
    part2 = rng.randint(1000, 9999)
    return f"+44 {area_code} {part1} {part2}"


def _gen_sg_phone(first_digit: str, rng) -> str:
    """SG: +65 <8/9>XXX XXXX（移动号 8 位，首位 8 或 9）。"""
    fd = first_digit if first_digit in ("8", "9", "6") else rng.choice(["8", "9"])
    rest3 = rng.randint(0, 999)
    last4 = rng.randint(0, 9999)
    return f"+65 {fd}{rest3:03d} {last4:04d}"


def _gen_hk_phone(first_digit: str, rng) -> str:
    """HK: +852 <5/6/9>XXX XXXX（移动号 8 位，首位 5/6/9）。"""
    fd = first_digit if first_digit in ("5", "6", "9") else rng.choice(["5", "6", "9"])
    rest3 = rng.randint(0, 999)
    last4 = rng.randint(0, 9999)
    return f"+852 {fd}{rest3:03d} {last4:04d}"


def _gen_jp_phone(area_code: str, rng) -> str:
    """JP: +81 <区号> XXXX XXXX（东京 03 区号去前导 0 为 "3"）。"""
    part1 = rng.randint(1000, 9999)
    part2 = rng.randint(1000, 9999)
    return f"+81 {area_code} {part1} {part2}"


def generate_us_phone(area_code: str, seed: str | None = None) -> str:
    """生成美国 NANP 电话（向后兼容薄封装；新代码用 generate_phone）。"""
    return generate_phone(country="US", area_code=area_code, seed=seed)


# ── 地址冷却（A1）─────────────────────────────────────────────────
#
# 问题：24 个静态地址，规模化使用同地址会被 PayPal address velocity rule 标记
#       （`200 Hudson St` / `1455 Market St` 已被本系统多次复用，注释自证）。
# 方案：内存窗口计数 — 24h 内同地址 ≥ max_uses 次时从可用池剔除，
#       超出窗口的旧时间戳自动淘汰，进程重启清零（数据少时够用）。
# 线程安全：用模块级 lock 保护 deque/dict，FastAPI 多线程下不会脏读。
_USAGE_LOCK = threading.Lock()
_USAGE_LOG: Dict[str, Deque[float]] = {}


def _address_key(addr: "BillingAddress", country: str = "US") -> str:
    """地址唯一键 = country + line1 + zip。

    加 country 前缀避免跨国地址碰撞（HK zip 为空时尤需 country+line1 区分），
    并让不同国家的地址各自独立计算冷却额度。
    """
    return f"{(country or 'US').upper()}|{addr.line1}|{addr.zip_code}"


def _prune_usage(key: str, window_seconds: float, now: Optional[float] = None) -> int:
    """淘汰窗口外的时间戳，返回剩余次数（**调用方需持锁**）。"""
    now = now if now is not None else time.time()
    dq = _USAGE_LOG.get(key)
    if dq is None:
        return 0
    cutoff = now - window_seconds
    while dq and dq[0] < cutoff:
        dq.popleft()
    if not dq:
        _USAGE_LOG.pop(key, None)
        return 0
    return len(dq)


def _record_usage(key: str, now: Optional[float] = None) -> None:
    """记录一次使用（**调用方需持锁**）。"""
    now = now if now is not None else time.time()
    dq = _USAGE_LOG.setdefault(key, deque())
    dq.append(now)


def pick_address_with_cooldown(
    *,
    used_within_hours: float = 24.0,
    max_uses: int = 3,
    seed: Optional[str] = None,
    country: Optional[str] = None,
) -> BillingAddress:
    """从指定国家地址池选地址，自动剔除"冷却窗口内已用过 ≥ max_uses 次"的地址。

    Args:
        used_within_hours: 冷却窗口（小时），默认 24h
        max_uses: 单地址在窗口内最多用几次，默认 3
        seed: 同 `pick_random_address` 的 seed 语义；**注意**：seed 会绕过 cooldown
              过滤（确定性优先），仅当 seed=None 时启用冷却
        country: ISO alpha-2 国家码；默认 US（向后兼容）

    Returns:
        BillingAddress，并在内部记录一次使用计数

    Raises:
        无；池被全冷却时降级返回真随机（避免阻塞业务），并写 warning 日志
    """
    cc = (country or "US").upper()
    # seed 模式优先确定性，跳过 cooldown 过滤
    if seed is not None:
        addr = pick_random_address(seed=seed, country=cc)
        with _USAGE_LOCK:
            _record_usage(_address_key(addr, cc))
        return addr

    window_seconds = float(used_within_hours) * 3600.0
    pool = _pool_for(cc)

    with _USAGE_LOCK:
        now = time.time()
        available: Tuple[BillingAddress, ...] = tuple(
            addr for addr in pool
            if _prune_usage(_address_key(addr, cc), window_seconds, now=now) < max_uses
        )
        # 全池冷却时降级到真随机（不阻塞业务）
        if not available:
            import logging
            logging.getLogger(__name__).warning(
                "billing_addresses: %s 全池 %d 个地址都已 cooldown，降级真随机",
                cc, len(pool),
            )
            chosen = secrets.choice(pool)
        else:
            chosen = secrets.choice(available)
        _record_usage(_address_key(chosen, cc), now=now)
        return chosen


def address_usage_snapshot(used_within_hours: float = 24.0) -> Dict[str, int]:
    """运维查询用：返回当前窗口内每个地址 key 的使用次数（淘汰过期后）。

    key 格式为 "COUNTRY|line1|zip"（见 _address_key）。
    """
    window_seconds = float(used_within_hours) * 3600.0
    snapshot: Dict[str, int] = {}
    with _USAGE_LOCK:
        now = time.time()
        for key in list(_USAGE_LOG.keys()):
            count = _prune_usage(key, window_seconds, now=now)
            if count > 0:
                snapshot[key] = count
    return snapshot


def reset_usage_log() -> None:
    """单测用：清空使用日志（生产代码不要调）。"""
    with _USAGE_LOCK:
        _USAGE_LOG.clear()
