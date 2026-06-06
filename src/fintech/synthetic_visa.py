# -*- coding: utf-8 -*-
"""多国合成卡生成器（按国家选 BIN + Luhn 校验和 + 在线/本地地址）。

BIN 来源：`country_profiles.py` 按国家维护真实发卡行 BIN（US/GB/CA/SG/HK）。
  - Luhn 算法校验和：保证生成的 16 位卡号通过 PayPal / Stripe 预校验
  - 过期日期：当前年 +2~+5 年，月份随机
  - 地址/姓名：优先在线生成器（`online_identity.py`），失败回退本地池

设计用途（与 EfunCard / X988Card 等真实虚拟卡的区别）：
  - 真实卡：能扣款、走真实 3DS、有真实持卡人 KYC
  - 合成卡：**卡号合法但无法真实扣款**；PayPal "试扣 $1" 阶段会失败
            （但失败码是 CARD_GENERIC_ERROR 而非 RESTRICTED_USER —— 不会让账号被永久封）

适合场景：
  - PayPal guest checkout 当一次性"占位卡"（PayPal 拒卡后用户能换真卡）
  - 测试 OpenAI checkout 链生成流程不真扣钱
  - 反爬"摆烂"：让 PayPal 看见卡尝试但不实际付款，避开 RESTRICTED_USER 永封

⚠️ 不适合：
  - OpenAI 真实 Plus / Team 订阅支付（卡试扣会失败 → 支付失败）
  - 任何需要真实扣款的场景
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Final

# [DEPRECATED] 旧的写死 US BIN 段，现已迁移到 country_profiles 的 US profile。
# 保留仅为向后兼容，新代码不要引用——BIN 选取统一走 country_profiles.get_profile()。
_PAYPAL_FRIENDLY_BIN_PREFIXES: Final[tuple[tuple[int, ...], ...]] = (
    (4, 1, 4, 7),  # Chase Visa
    (4, 1, 0, 0),  # Wells Fargo / Apple Card / Cash App Card
)
_CARD_LENGTH: Final[int] = 16


@dataclass(frozen=True)
class SyntheticCard:
    """合成 Visa/MC 卡（Luhn 合法的随机卡号）。

    跟 src.models.CardInfo 保持同样字段命名以便互操作，但不强制类型继承
    （避免污染真实卡的字段语义）。
    """
    card_number: str
    expiry_month: str  # 'MM'
    expiry_year: str   # 'YY'
    cvv: str           # 3 位
    bin_prefix: str    # 4 位 BIN（4147 / 4100 等）
    country: str = "US"  # 发卡国 ISO alpha-2（按 country_profiles 的 BIN 集生成）

    @property
    def expiry_display(self) -> str:
        """与 CardInfo.expiry_display 同接口，便于复用填表代码。"""
        return f"{self.expiry_month}/{self.expiry_year}"

    @property
    def last_four(self) -> str:
        return self.card_number[-4:]


def _luhn_check_digit(partial_digits: list[int]) -> int:
    """计算 Luhn 校验位。

    partial_digits: 15 位前缀（不含校验位），最高位在 index 0
    返回：第 16 位校验位 (0-9)

    Luhn 算法：
      1. 从右向左数（含本次新加的校验位），每偶数位 ×2，>9 减 9
      2. 全部数字相加，校验位让 sum % 10 == 0
    """
    reversed_digits = list(reversed(partial_digits))
    total = 0
    # reversed_digits[0] 现在是第 15 位（最低位前缀）
    # 加上校验位后，校验位会在最低位（reversed[0]），所以前缀位置 0..14 在算 sum 时 index 是 1..15
    # 简化：直接按 PayPal Auto Filler line 419 的算法
    for index, digit in enumerate(reversed_digits):
        if index % 2 == 0:
            # 索引 0 对应原本最低位前缀（=反转后第 1 位），这一位翻倍
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - (total % 10)) % 10


def generate_synthetic_visa(
    *,
    country: str = "US",
    bin_prefix: tuple[int, ...] | None = None,
    seed: str | int | None = None,
) -> SyntheticCard:
    """生成一张合成卡（按国家选 BIN）。

    Args:
        country: ISO alpha-2 国家码；决定 BIN 前缀集（见 country_profiles）。默认 US。
        bin_prefix: 显式指定 BIN（覆盖 country 默认集）；None 则从该国 BIN 集随机选
        seed: 确定性种子（同一 seed 永远生成同一张卡）；None 用真随机

    Returns:
        SyntheticCard 实例（card_number 已通过 Luhn 校验）
    """
    from src.fintech.country_profiles import get_profile, normalize_country

    cc = normalize_country(country)
    rng = random.Random(seed) if seed is not None else random
    if bin_prefix:
        chosen_prefix = bin_prefix
    else:
        chosen_prefix = rng.choice(get_profile(cc).bin_prefixes)

    digits = list(chosen_prefix)
    while len(digits) < _CARD_LENGTH - 1:
        digits.append(rng.randint(0, 9))

    check_digit = _luhn_check_digit(digits)
    digits.append(check_digit)

    card_number = "".join(str(d) for d in digits)
    bin_str = "".join(str(d) for d in chosen_prefix)

    # 过期日期（B2）：当前年 +2 ~ +5 年，偏态分布贴近真实新发卡（+3/+4 年居多）
    # 月份避开当月（"刚发即到期窗"是 PayPal 异常信号）
    current_now = datetime.utcnow()
    current_year = current_now.year % 100  # 26 (= 2026)
    current_month = current_now.month
    expiry_year_offset = rng.choices(
        [2, 3, 4, 5],
        weights=[20, 30, 30, 20],
        k=1,
    )[0]
    expiry_year = f"{(current_year + expiry_year_offset) % 100:02d}"
    # 月份 1-12 随机，避开当月（"刚发即到期窗"信号）
    month_choices = [m for m in range(1, 13) if m != current_month]
    expiry_month = f"{rng.choice(month_choices):02d}"

    # CVV（B1）：000-999 全空间（原 100-999 漏 0XX = 10% 统计指纹漏洞）
    cvv = f"{rng.randint(0, 999):03d}"

    return SyntheticCard(
        card_number=card_number,
        expiry_month=expiry_month,
        expiry_year=expiry_year,
        cvv=cvv,
        bin_prefix=bin_str,
        country=cc,
    )


@dataclass(frozen=True)
class SyntheticCardKit:
    """合成卡 + 账单地址 + 持卡人姓名 + 电话的完整表单套件（多国）。

    设计：所有字段一次性生成，互相一致（地址/电话同国；姓名与地址同源），
    避免 AVS 关联多账号风控。地址字段形态随国家不同（GB/SG/HK 无 state 等，
    见 country_profiles）。
    """
    card: SyntheticCard
    # 持卡人 / 账单姓名（表单 First name + Last name）
    first_name: str
    last_name: str
    full_name: str
    # 账单地址
    address_line1: str
    address_city: str
    address_state: str
    address_zip: str
    # 电话（按国家格式）
    phone: str
    # 国家 + 邮编标签（前端展示用，区分 ZIP/Postcode/Postal code）
    country: str = "US"
    postal_label: str = "ZIP"
    # 数据来源标记（"online" / "fallback"），便于审计与排障
    source: str = "fallback"

    def to_form_payload(self) -> dict[str, str]:
        """转成扁平 dict 便于前端复制 / 后端透传。"""
        return {
            "card_number": self.card.card_number,
            "expiry_month": self.card.expiry_month,
            "expiry_year": self.card.expiry_year,
            "expiry_display": self.card.expiry_display,
            "cvv": self.card.cvv,
            "bin_prefix": self.card.bin_prefix,
            "last_four": self.card.last_four,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "address_line1": self.address_line1,
            "address_city": self.address_city,
            "address_state": self.address_state,
            "address_zip": self.address_zip,
            "phone": self.phone,
            "country": self.country,
            "postal_label": self.postal_label,
            "source": self.source,
        }


def generate_synthetic_visa_kit(
    *,
    country: str = "US",
    use_online: bool = True,
    http_get=None,
    bin_prefix: tuple[int, ...] | None = None,
    seed: str | int | None = None,
    gender: str | None = None,
    override_first_name: str | None = None,
    override_last_name: str | None = None,
) -> SyntheticCardKit:
    """一次性生成表单完整套件（卡 + 地址 + 姓名 + 电话），按国家。

    地址/姓名来源优先级：
      1. 在线生成器（randomuser.me / fakerapi.it，真实感更强）—— 仅 use_online=True
         且非 seed 模式时启用。
      2. 本地兜底池（country_profiles 各国地址 + identity_generator 姓名）。
    在线失败/字段缺失/SG-HK 不被支持 → 自动回退本地池（绝不阻塞）。

    Args:
        country: ISO alpha-2 国家码（US/GB/CA/SG/HK）。默认 US。
        use_online: 是否尝试在线地址源；seed 模式下强制 False（保确定性）。
        http_get: 注入的 HTTP 函数（测试 mock 用）。
        bin_prefix: 卡 BIN 段（None 走该国 BIN 集随机）
        seed: 确定性种子（同 seed 永远生成同一套件；强制走本地池不调在线）
        gender: 'm' / 'f' / None（影响本地姓名池生成）
        override_first_name: 注入持卡人名（对齐 OpenAI 账户；优先级最高）
        override_last_name: 注入持卡人姓（对齐 OpenAI 账户；优先级最高）

    Returns:
        SyntheticCardKit 实例，所有字段同国一致；`source` 标记 online/fallback。

    Note:
        override_first_name / override_last_name 必须**同时**传入或同时为 None；
        只传其一时另一个回退到本地姓名，会破坏 first/last 同源一致性。
    """
    # 延迟 import 避免 fintech 包内循环依赖
    from src.fintech.billing_addresses import (
        generate_phone,
        pick_address_with_cooldown,
        pick_random_address,
    )
    from src.fintech.country_profiles import get_profile, normalize_country
    from src.fintech.online_identity import fetch_online_identity
    from src.services.identity_generator import generate_identity

    cc = normalize_country(country)
    profile = get_profile(cc)

    # 1. 生成卡（按国家 BIN）
    card = generate_synthetic_visa(country=cc, bin_prefix=bin_prefix, seed=seed)

    # 2. 在线优先拿地址（+ 可能的姓名/电话）；seed 模式不调在线（保确定性）
    online = None
    if use_online and seed is None:
        online = fetch_online_identity(cc, http_get=http_get)

    has_name_override = bool(override_first_name and override_last_name)
    source = "fallback"

    # 3. 决定姓名：注入 > 在线 > 本地池
    if has_name_override:
        first_name = override_first_name.strip()
        last_name = override_last_name.strip()
        full_name = f"{first_name} {last_name}"
    elif online is not None and online.has_name:
        first_name = online.first_name
        last_name = online.last_name
        full_name = f"{first_name} {last_name}"
    else:
        identity = generate_identity(gender=gender)
        first_name = identity.first_name
        last_name = identity.last_name
        full_name = identity.full_name

    # 4. 决定地址：在线 > 本地池
    if online is not None and online.has_address:
        source = "online"
        addr_line1 = online.line1
        addr_city = online.city
        addr_state = online.state
        addr_zip = online.postal
        online_phone = online.phone
        area_code_hint = ""  # 在线地址无本地 area_code，电话走默认号段
    else:
        # 本地兜底池：seed 模式确定性映射；无 seed 走 24h cooldown（A1 风控护栏）
        if seed is not None:
            address = pick_random_address(seed=f"{seed}-addr", country=cc)
        else:
            address = pick_address_with_cooldown(
                used_within_hours=24.0, max_uses=3, country=cc
            )
        addr_line1 = address.line1
        addr_city = address.city
        addr_state = address.state
        addr_zip = address.zip_code
        online_phone = ""
        area_code_hint = address.area_code

    # 5. 电话：在线返回电话则优先用；否则按国家格式本地生成
    if online_phone:
        phone = online_phone
    else:
        phone_seed = seed if seed is not None else card.card_number
        phone = generate_phone(
            country=cc,
            area_code=area_code_hint or None,
            seed=f"{phone_seed}-phone",
        )

    return SyntheticCardKit(
        card=card,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        address_line1=addr_line1,
        address_city=addr_city,
        address_state=addr_state,
        address_zip=addr_zip,
        phone=phone,
        country=cc,
        postal_label=profile.postal_label,
        source=source,
    )


def is_luhn_valid(card_number: str) -> bool:
    """验证卡号是否通过 Luhn 校验（运维 / 测试用）。"""
    digits = [int(c) for c in card_number if c.isdigit()]
    if len(digits) < 12 or len(digits) > 19:
        return False
    reversed_digits = list(reversed(digits))
    total = 0
    for index, digit in enumerate(reversed_digits):
        if index % 2 == 1:  # 反转后从 0 开始，校验位本身在 0；翻倍的是 index 1, 3, 5...
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
