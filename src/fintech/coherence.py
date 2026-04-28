# -*- coding: utf-8 -*-
"""
身份一致性校验器

Stripe 风控主要受 4 个维度国家的一致性影响：
- 虚拟卡 BIN 所在国
- 代理 IP 所在国
- SMS 手机号所在国
- 账单地址所在国

本模块提供快速失败的一致性检测，避免在明显错配时继续走完整流程。
"""

from dataclasses import dataclass, field


# SMS-Activate 平台常见国家编码 → ISO 3166-1 alpha-2 映射
# 参考：https://sms-activate.org/en/api2 "getPrices" 接口的 country 字段
_SMS_COUNTRY_MAP: dict[str, str] = {
    "0": "RU",    # 俄罗斯
    "1": "UA",    # 乌克兰
    "2": "KZ",    # 哈萨克斯坦
    "6": "ID",    # 印度尼西亚
    "12": "US",   # 美国（Google Voice）
    "16": "GB",   # 英国
    "36": "CA",   # 加拿大
    "43": "DE",   # 德国
    "78": "FR",   # 法国
    "117": "PT",  # 葡萄牙
    "129": "GR",  # 希腊
    "187": "US",  # 美国
}


@dataclass(frozen=True)
class CoherenceReport:
    """一致性校验报告"""
    ok: bool
    card_country: str
    proxy_country: str
    sms_country: str  # 已归一化为 ISO alpha-2
    billing_country: str
    mismatches: list[str] = field(default_factory=list)
    severity: str = "ok"  # "ok" | "warn" | "block"
    rationale: str = ""


def _normalize_country(value: str) -> str:
    """归一化国家代码为大写 ISO alpha-2；空字符串返回空串"""
    if not value:
        return ""
    return str(value).strip().upper()


def _normalize_sms_country(code: str) -> str:
    """
    归一化 SMS 平台国家代码。

    支持两种输入：
    - SMS-Activate 数字编码（如 "6"、"187"），通过映射表转换
    - 已经是 ISO alpha-2（如 "US"、"ID"），直接大写返回
    - 未知编码 → 返回空字符串（视为未知）
    """
    if not code:
        return ""
    raw = str(code).strip()
    if not raw:
        return ""
    # 纯数字走映射表
    if raw.isdigit():
        return _SMS_COUNTRY_MAP.get(raw, "")
    # 否则视为已经是 ISO 代码，仅校验长度
    upper = raw.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return ""


def validate_identity_coherence(
    *,
    card_bin_country: str,
    proxy_country: str,
    sms_country_code: str,
    billing_country: str,
) -> CoherenceReport:
    """
    校验 4 维身份国家一致性。

    规则：
    - 任一字段为空/未知 → severity="block"
    - 4 国全等 → severity="ok"，ok=True
    - 恰好 1 处错配（且无未知）→ severity="warn"，ok=False
    - 2+ 处错配 → severity="block"，ok=False
    """
    card = _normalize_country(card_bin_country)
    proxy = _normalize_country(proxy_country)
    sms = _normalize_sms_country(sms_country_code)
    billing = _normalize_country(billing_country)

    # 先检查未知字段（空串即视为未知）
    unknown_fields: list[str] = []
    if not card:
        unknown_fields.append("card")
    if not proxy:
        unknown_fields.append("proxy")
    if not sms:
        unknown_fields.append("sms")
    if not billing:
        unknown_fields.append("billing")

    # 两两比对产生人类可读的错配说明
    pairs = [
        ("card_vs_proxy", card, proxy),
        ("card_vs_sms", card, sms),
        ("card_vs_billing", card, billing),
        ("proxy_vs_sms", proxy, sms),
        ("proxy_vs_billing", proxy, billing),
        ("sms_vs_billing", sms, billing),
    ]
    mismatches: list[str] = []
    for label, a, b in pairs:
        # 双方都已知且不相等 → 记录错配
        if a and b and a != b:
            mismatches.append(f"{label}: {a} != {b}")

    # 分级判定：以"字段为单位"统计与多数国家不一致的字段数
    # 规则原文：恰好 1 处错配（如仅 SMS 与其余三者不同）→ warn；2+ 错配 → block
    # 因此统计"偏离多数"的字段数，而非两两错配对数。
    fields = {
        "card": card,
        "proxy": proxy,
        "sms": sms,
        "billing": billing,
    }
    # 统计各国家的出现次数，找出多数国家
    country_counts: dict[str, int] = {}
    for value in fields.values():
        country_counts[value] = country_counts.get(value, 0) + 1
    majority_country, majority_count = max(
        country_counts.items(), key=lambda kv: kv[1]
    )
    # 偏离多数的字段（不含未知字段，未知已在上面单独处理）
    deviant_fields = [
        name for name, value in fields.items() if value != majority_country
    ]

    if unknown_fields:
        severity = "block"
        ok = False
        rationale = (
            f"字段缺失或未知: {', '.join(unknown_fields)}；"
            f"无法评估 4 维一致性，直接阻断。"
        )
    elif not mismatches:
        severity = "ok"
        ok = True
        rationale = f"四维国家全部一致（{card}），身份一致性通过。"
    elif len(deviant_fields) == 1:
        severity = "warn"
        ok = False
        deviant_name = deviant_fields[0]
        deviant_value = fields[deviant_name]
        rationale = (
            f"仅 {deviant_name}={deviant_value} 偏离多数国家 {majority_country}，"
            f"存在中等风险。"
        )
    else:
        severity = "block"
        ok = False
        rationale = (
            f"检测到 {len(mismatches)} 处错配，身份分布为 {country_counts}，"
            f"高风险，已阻断。"
        )

    return CoherenceReport(
        ok=ok,
        card_country=card,
        proxy_country=proxy,
        sms_country=sms,
        billing_country=billing,
        mismatches=mismatches,
        severity=severity,
        rationale=rationale,
    )


__all__ = ["CoherenceReport", "validate_identity_coherence"]
