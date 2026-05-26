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
from dataclasses import dataclass


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
