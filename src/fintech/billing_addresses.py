# -*- coding: utf-8 -*-
"""PayPal AVS 友好的美国住宅地址池。

设计：
  - 精选 24 个公开真实美国中产社区地址（ZIP + state + city 三向校验通过 USPS）
  - 区号 (area code) 跟 state 一致，避免 PayPal AVS + phone 风控关联
  - 故意**不用** 350 5th Ave（Empire State）/ 1600 Pennsylvania Ave（白宫）等网红地址

来源：
  - 用户实战 L2 通过的 200 Hudson St / 1455 Market St
  - 公开 Google Maps 真实街道地址（office building + apartment complex）
  - 美国公开邮政编码数据库验证 ZIP+state+city 一致性

风控避坑：
  ❌ PO Box（PayPal 拒）
  ❌ UPS Store / Mailbox Etc 虚拟邮件地址
  ❌ 同一地址 ≥ 5 次（同地址多账号关联）
  ✅ 大城市中产社区
  ✅ 区号匹配 state
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
    """美国住宅地址（PayPal AVS 友好）。"""
    line1: str
    city: str
    state: str       # 2-letter code (e.g. "NY", "CA")
    zip_code: str    # 5-digit US ZIP
    area_code: str   # 跟 state 一致的电话区号

    def to_dict(self) -> dict[str, str]:
        return {
            "line1": self.line1,
            "city": self.city,
            "state": self.state,
            "zip": self.zip_code,
            "area_code": self.area_code,
        }


# 精选 24 个 PayPal AVS 友好地址（ZIP + state + city + area_code 全部交叉验证）
_ADDRESS_POOL: tuple[BillingAddress, ...] = (
    # New York 曼哈顿（212/646/917 区号）
    BillingAddress("200 Hudson St", "New York", "NY", "10013", "212"),
    BillingAddress("350 7th Ave", "New York", "NY", "10001", "646"),
    BillingAddress("60 W 23rd St", "New York", "NY", "10010", "212"),
    BillingAddress("75 9th Ave", "New York", "NY", "10011", "212"),
    BillingAddress("411 Lafayette St", "New York", "NY", "10003", "646"),
    BillingAddress("100 William St", "New York", "NY", "10038", "212"),
    # Brooklyn (718/347/929)
    BillingAddress("85 Pierrepont St", "Brooklyn", "NY", "11201", "718"),
    BillingAddress("130 Bedford Ave", "Brooklyn", "NY", "11249", "347"),
    # San Francisco (415/628)
    BillingAddress("1455 Market St", "San Francisco", "CA", "94103", "415"),
    BillingAddress("555 California St", "San Francisco", "CA", "94104", "628"),
    BillingAddress("123 Mission St", "San Francisco", "CA", "94105", "415"),
    BillingAddress("800 Brannan St", "San Francisco", "CA", "94103", "628"),
    # Los Angeles (213/323)
    BillingAddress("1010 Wilshire Blvd", "Los Angeles", "CA", "90017", "213"),
    BillingAddress("888 S Figueroa St", "Los Angeles", "CA", "90017", "323"),
    BillingAddress("6300 Wilshire Blvd", "Los Angeles", "CA", "90048", "323"),
    # Seattle (206/425)
    BillingAddress("1201 3rd Ave", "Seattle", "WA", "98101", "206"),
    BillingAddress("710 2nd Ave", "Seattle", "WA", "98104", "206"),
    # Chicago (312/773)
    BillingAddress("233 S Wacker Dr", "Chicago", "IL", "60606", "312"),
    BillingAddress("875 N Michigan Ave", "Chicago", "IL", "60611", "312"),
    BillingAddress("600 W Chicago Ave", "Chicago", "IL", "60654", "312"),
    # Boston (617/857)
    BillingAddress("100 Federal St", "Boston", "MA", "02110", "617"),
    BillingAddress("125 High St", "Boston", "MA", "02110", "857"),
    # Austin TX (512)
    BillingAddress("301 Congress Ave", "Austin", "TX", "78701", "512"),
    # Denver CO (303/720)
    BillingAddress("1200 17th St", "Denver", "CO", "80202", "303"),
)


def list_all_addresses() -> list[BillingAddress]:
    """返回地址池全部地址（运维查看用）。"""
    return list(_ADDRESS_POOL)


def pick_random_address(seed: str | None = None) -> BillingAddress:
    """从地址池随机选一个地址。

    Args:
        seed: 确定性种子；同 seed 永远返回同一地址（用于绑同账号同卡）
              None 走真随机
    """
    if seed is not None:
        import random
        rng = random.Random(seed)
        return rng.choice(_ADDRESS_POOL)
    # 真随机用 secrets（cryptographically secure）
    return secrets.choice(_ADDRESS_POOL)


def generate_us_phone(area_code: str, seed: str | None = None) -> str:
    """生成跟 area_code 匹配的美国电话号码。

    格式：(XXX) YYY-ZZZZ
    避坑：
      - 中间 3 位 (YYY) 避开 555（PayPal 视为测试号）
      - 中间 3 位首位避开 0/1（不合 NANP 规范）

    Args:
        area_code: 3 位区号（跟 state 一致；e.g. "212" for NY）
        seed: 确定性种子
    """
    if seed is not None:
        import random
        rng = random.Random(seed)
    else:
        rng = secrets.SystemRandom()

    # NANP 规则：中间 3 位首位 2-9
    central_first = rng.randint(2, 9)
    central_rest = rng.randint(0, 99)
    # 避开 555 测试号段
    while central_first == 5 and central_rest == 55:
        central_first = rng.randint(2, 9)
        central_rest = rng.randint(0, 99)
    central = f"{central_first}{central_rest:02d}"
    last_four = rng.randint(0, 9999)
    return f"({area_code}) {central}-{last_four:04d}"


# ── 地址冷却（A1）─────────────────────────────────────────────────
#
# 问题：24 个静态地址，规模化使用同地址会被 PayPal address velocity rule 标记
#       （`200 Hudson St` / `1455 Market St` 已被本系统多次复用，注释自证）。
# 方案：内存窗口计数 — 24h 内同地址 ≥ max_uses 次时从可用池剔除，
#       超出窗口的旧时间戳自动淘汰，进程重启清零（数据少时够用）。
# 线程安全：用模块级 lock 保护 deque/dict，FastAPI 多线程下不会脏读。
_USAGE_LOCK = threading.Lock()
_USAGE_LOG: Dict[str, Deque[float]] = {}


def _address_key(addr: "BillingAddress") -> str:
    """地址唯一键 = line1 + zip（line1 单独可能重名，zip 单独跨城重复）。"""
    return f"{addr.line1}|{addr.zip_code}"


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
) -> BillingAddress:
    """从地址池选地址，自动剔除"冷却窗口内已用过 ≥ max_uses 次"的地址。

    Args:
        used_within_hours: 冷却窗口（小时），默认 24h
        max_uses: 单地址在窗口内最多用几次，默认 3
        seed: 同 `pick_random_address` 的 seed 语义；**注意**：seed 会绕过 cooldown
              过滤（确定性优先），仅当 seed=None 时启用冷却

    Returns:
        BillingAddress，并在内部记录一次使用计数

    Raises:
        无；池被全冷却时降级返回真随机（避免阻塞业务），并写 warning 日志
    """
    # seed 模式优先确定性，跳过 cooldown 过滤
    if seed is not None:
        addr = pick_random_address(seed=seed)
        with _USAGE_LOCK:
            _record_usage(_address_key(addr))
        return addr

    window_seconds = float(used_within_hours) * 3600.0
    pool = _ADDRESS_POOL

    with _USAGE_LOCK:
        now = time.time()
        available: Tuple[BillingAddress, ...] = tuple(
            addr for addr in pool
            if _prune_usage(_address_key(addr), window_seconds, now=now) < max_uses
        )
        # 全池冷却时降级到真随机（不阻塞业务）
        if not available:
            import logging
            logging.getLogger(__name__).warning(
                "billing_addresses: 全池 %d 个地址都已 cooldown，降级真随机",
                len(pool),
            )
            chosen = secrets.choice(pool)
        else:
            chosen = secrets.choice(available)
        _record_usage(_address_key(chosen), now=now)
        return chosen


def address_usage_snapshot(used_within_hours: float = 24.0) -> Dict[str, int]:
    """运维查询用：返回当前窗口内每个地址 key 的使用次数（淘汰过期后）。"""
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
