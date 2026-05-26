# -*- coding: utf-8 -*-
"""合成 Visa 卡生成器（PayPal 友好 BIN + Luhn 校验和）。

移植自 PayPal Auto Filler 浏览器脚本（v36.9.5）的 buildHostedVisaCard 函数：
  - BIN 段：4147xx（Chase / Capital One Visa Premier）、4100xx（Wells Fargo / Apple Card / Cash App Card）
  - Luhn 算法校验和：保证生成的 16 位卡号通过 PayPal / Stripe 预校验
  - 过期日期：当前年 +2~+5 年，月份随机

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

# PayPal Auto Filler v36.9.5 实测 PayPal 拒绝率最低的两个 US BIN 段
# 4147xx = Chase / Capital One Visa（实体银行）
# 4100xx = Wells Fargo / Apple Card / Cash App Card（金融科技）
_PAYPAL_FRIENDLY_BIN_PREFIXES: Final[tuple[tuple[int, ...], ...]] = (
    (4, 1, 4, 7),
    (4, 1, 0, 0),
)
_CARD_LENGTH: Final[int] = 16


@dataclass(frozen=True)
class SyntheticCard:
    """合成 Visa 卡（Luhn 合法的随机卡号）。

    跟 src.models.CardInfo 保持同样字段命名以便互操作，但不强制类型继承
    （避免污染真实卡的字段语义）。
    """
    card_number: str
    expiry_month: str  # 'MM'
    expiry_year: str   # 'YY'
    cvv: str           # 3 位
    bin_prefix: str    # 4 位 BIN（4147 / 4100 等）

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
    bin_prefix: tuple[int, ...] | None = None,
    seed: str | int | None = None,
) -> SyntheticCard:
    """生成一张合成 Visa 卡。

    Args:
        bin_prefix: 显式指定 4 位 BIN；None 则从 PayPal 友好集合随机选
        seed: 确定性种子（同一 seed 永远生成同一张卡）；None 用真随机

    Returns:
        SyntheticCard 实例（card_number 已通过 Luhn 校验）
    """
    rng = random.Random(seed) if seed is not None else random
    chosen_prefix = bin_prefix if bin_prefix else rng.choice(_PAYPAL_FRIENDLY_BIN_PREFIXES)

    digits = list(chosen_prefix)
    while len(digits) < _CARD_LENGTH - 1:
        digits.append(rng.randint(0, 9))

    check_digit = _luhn_check_digit(digits)
    digits.append(check_digit)

    card_number = "".join(str(d) for d in digits)
    bin_str = "".join(str(d) for d in chosen_prefix)

    # 过期日期：当前年 +2 ~ +5 年（避免太短被 PayPal 视为预扣即将到期）
    current_year = datetime.utcnow().year % 100  # 26 (= 2026)
    expiry_year_offset = rng.randint(2, 5)
    expiry_year = f"{(current_year + expiry_year_offset) % 100:02d}"
    expiry_month = f"{rng.randint(1, 12):02d}"

    cvv = f"{rng.randint(100, 999):03d}"

    return SyntheticCard(
        card_number=card_number,
        expiry_month=expiry_month,
        expiry_year=expiry_year,
        cvv=cvv,
        bin_prefix=bin_str,
    )


@dataclass(frozen=True)
class SyntheticCardKit:
    """合成卡 + 账单地址 + 持卡人姓名 + 电话的完整 PayPal 表单套件。

    设计：所有字段一次性生成，互相一致（state-zip-area_code 三元组匹配；
    姓名与电话独立但都符合 US 风格），避免 PayPal AVS 关联多账号风控。
    """
    card: SyntheticCard
    # 持卡人 / 账单姓名（PayPal 表单 First name + Last name）
    first_name: str
    last_name: str
    full_name: str
    # 账单地址
    address_line1: str
    address_city: str
    address_state: str
    address_zip: str
    # 电话（区号匹配 state）
    phone: str

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
        }


def generate_synthetic_visa_kit(
    *,
    bin_prefix: tuple[int, ...] | None = None,
    seed: str | int | None = None,
    gender: str | None = None,
) -> SyntheticCardKit:
    """一次性生成 PayPal 表单完整套件（卡 + 地址 + 姓名 + 电话）。

    Args:
        bin_prefix: 卡 BIN 段（None 走默认 4147/4100 随机）
        seed: 确定性种子（同 seed 永远生成同一套件，跨字段一致性自动保证）
        gender: 'm' / 'f' / None（影响姓名生成）

    Returns:
        SyntheticCardKit 实例，所有字段互相一致
    """
    # 延迟 import 避免 fintech 包内循环依赖（如果有）
    from src.fintech.billing_addresses import generate_us_phone, pick_random_address
    from src.services.identity_generator import generate_identity

    # 1. 生成卡
    card = generate_synthetic_visa(bin_prefix=bin_prefix, seed=seed)

    # 2. 生成姓名（identity_generator 复用主注册流的姓名池）
    identity = generate_identity(gender=gender)

    # 3. 选地址（用 seed 保证同 seed 同卡同地址；卡是 seed 主导，地址用 card_number 子种子）
    address_seed = seed if seed is not None else card.card_number
    address = pick_random_address(seed=f"{address_seed}-addr")

    # 4. 生成电话（区号跟地址 state 匹配）
    phone_seed = seed if seed is not None else card.card_number
    phone = generate_us_phone(area_code=address.area_code, seed=f"{phone_seed}-phone")

    return SyntheticCardKit(
        card=card,
        first_name=identity.first_name,
        last_name=identity.last_name,
        full_name=identity.full_name,
        address_line1=address.line1,
        address_city=address.city,
        address_state=address.state,
        address_zip=address.zip_code,
        phone=phone,
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
