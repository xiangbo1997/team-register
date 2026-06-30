# -*- coding: utf-8 -*-
"""多国合成卡数据中心：国家 → (BIN 前缀集 / 本地兜底地址池 / 电话格式 / 地址字段形态)。

设计动机：
  原 `synthetic_visa.py` + `billing_addresses.py` 整条合成卡链路写死美国
  （BIN 4147/4100、24 个美国地址、NANP 电话）。本模块把「国家」这一维度
  集中成一张映射表，供 synthetic_visa / billing_addresses 查询。

与在线地址生成器（`online_identity.py`）的关系：
  - 在线 API（randomuser.me / fakerapi.it）是**首选**真实地址/姓名来源。
  - 本模块的 `fallback_addresses` 是**在线 API 失败时的本地兜底池**，
    保证断网 / 限流 / SG/HK 不被在线 API 支持时仍能生成。

地址字段形态差异（各国邮政体系不同）：
  - US：有 state（2 字母州码）+ 5 位 ZIP + NANP area_code
  - GB：无 state（state=""）+ UK postcode（如 "EC1A 1BB"）+ 城市电话区号（如 "20"）
  - CA：有 province（state 存省码）+ 6 位 postal code（如 "M5H 2N2"）+ NANP area_code
  - SG：无 state + 6 位数字邮编 + 无区号（area_code 复用为移动号段首位 "8"/"9"）
  - HK：无 state + 无邮编（zip_code=""）+ 无区号（area_code 复用为移动号段首位 "5"/"6"/"9"）

⚠️ BIN 真实性说明：
  下列 BIN 前缀均为真实存在的该国发卡行 IIN（Issuer Identification Number），
  逐条标注发卡行来源。合成卡只过 Luhn + BIN 段预校验、**不能真实扣款**，
  BIN 真实性的意义在于「发卡国与账单地址国一致」，降低 Stripe/PayPal 的
  AVS 国家不匹配风控信号。
"""
from __future__ import annotations

from dataclasses import dataclass

# BillingAddress 是纯数据类，留在 billing_addresses.py。
# 这里单向 import 它（profiles → addresses 的 dataclass，无循环）；
# billing_addresses.py 的函数体内则延迟 import 本模块，打破循环依赖。
from src.fintech.billing_addresses import BillingAddress

# 支持的国家（ISO 3166-1 alpha-2）
SUPPORTED_COUNTRIES: tuple[str, ...] = ("US", "GB", "CA", "SG", "HK", "JP")
DEFAULT_COUNTRY: str = "US"


@dataclass(frozen=True)
class CountryProfile:
    """单个国家的合成卡生成画像。"""
    country_code: str
    # BIN 前缀集合：每个元素是一个 tuple[int,...]（4-6 位），生成卡时随机选一个
    bin_prefixes: tuple[tuple[int, ...], ...]
    # 电话格式类型："nanp"(US/CA) / "gb" / "sg" / "hk"，供 generate_phone 分派
    phone_kind: str
    # 本地兜底地址池（在线 API 失败时用）
    fallback_addresses: tuple[BillingAddress, ...]
    # 该国是否有 state/province 概念（影响表单展示与字段填充）
    has_state: bool
    # 邮编字段的展示标签（前端用，区分 ZIP / Postcode / Postal code）
    postal_label: str


# ─────────────────────────────────────────────────────────────────
# US 地址池（迁移自 billing_addresses._ADDRESS_POOL，PayPal AVS 友好）
# 区号匹配 state，故意不用网红地址（白宫 / 帝国大厦）
# ─────────────────────────────────────────────────────────────────
_US_ADDRESSES: tuple[BillingAddress, ...] = (
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


# ─────────────────────────────────────────────────────────────────
# GB 地址池（无 state；zip_code 存 UK postcode；area_code 存城市电话区号）
# postcode 与 city 取真实公开商业地址（伦敦金融城 / 曼彻斯特 / 伯明翰等）
# ─────────────────────────────────────────────────────────────────
_GB_ADDRESSES: tuple[BillingAddress, ...] = (
    BillingAddress("1 Poultry", "London", "", "EC2R 8EJ", "20"),
    BillingAddress("30 St Mary Axe", "London", "", "EC3A 8BF", "20"),
    BillingAddress("20 Fenchurch St", "London", "", "EC3M 3BY", "20"),
    BillingAddress("100 Bishopsgate", "London", "", "EC2N 4AG", "20"),
    BillingAddress("1 Spinningfields", "Manchester", "", "M3 3JE", "161"),
    BillingAddress("3 Hardman St", "Manchester", "", "M3 3HF", "161"),
    BillingAddress("103 Colmore Row", "Birmingham", "", "B3 3AG", "121"),
    BillingAddress("1 Wellington Pl", "Leeds", "", "LS1 4AP", "113"),
)


# ─────────────────────────────────────────────────────────────────
# CA 地址池（state 存省码；zip_code 存 6 位 postal code；area_code 为 NANP 区号）
# ─────────────────────────────────────────────────────────────────
_CA_ADDRESSES: tuple[BillingAddress, ...] = (
    BillingAddress("100 King St W", "Toronto", "ON", "M5X 1A9", "416"),
    BillingAddress("199 Bay St", "Toronto", "ON", "M5L 1G9", "416"),
    BillingAddress("66 Wellington St W", "Toronto", "ON", "M5K 1A1", "647"),
    BillingAddress("1055 Dunsmuir St", "Vancouver", "BC", "V7X 1L4", "604"),
    BillingAddress("666 Burrard St", "Vancouver", "BC", "V6C 3P6", "604"),
    BillingAddress("1000 De La Gauchetiere St W", "Montreal", "QC", "H3B 4W5", "514"),
    BillingAddress("855 2 St SW", "Calgary", "AB", "T2P 4J8", "403"),
    BillingAddress("10180 101 St NW", "Edmonton", "AB", "T5J 3S4", "780"),
)


# ─────────────────────────────────────────────────────────────────
# SG 地址池（无 state；zip_code 存 6 位邮编；city 固定 Singapore；
# area_code 复用为移动号段首位 "8"/"9"——SG 无区号概念）
# 取真实公开商业楼宇地址（莱佛士坊 / 滨海湾 / 乌节路等）
# ─────────────────────────────────────────────────────────────────
_SG_ADDRESSES: tuple[BillingAddress, ...] = (
    BillingAddress("1 Raffles Pl", "Singapore", "", "048616", "9"),
    BillingAddress("6 Battery Rd", "Singapore", "", "049909", "9"),
    BillingAddress("10 Marina Blvd", "Singapore", "", "018983", "8"),
    BillingAddress("2 Orchard Turn", "Singapore", "", "238801", "9"),
    BillingAddress("9 Raffles Pl", "Singapore", "", "048619", "8"),
    BillingAddress("80 Robinson Rd", "Singapore", "", "068898", "9"),
    BillingAddress("5 Temasek Blvd", "Singapore", "", "038985", "8"),
    BillingAddress("1 Fullerton Sq", "Singapore", "", "049178", "9"),
)


# ─────────────────────────────────────────────────────────────────
# HK 地址池（无 state；无邮编 zip_code=""；
# area_code 复用为移动号段首位 "5"/"6"/"9"——HK 无区号概念）
# 取真实公开商业楼宇地址（中环 / 金钟 / 尖沙咀等）
# ─────────────────────────────────────────────────────────────────
_HK_ADDRESSES: tuple[BillingAddress, ...] = (
    BillingAddress("8 Connaught Pl", "Central, Hong Kong", "", "", "9"),
    BillingAddress("1 Garden Rd", "Central, Hong Kong", "", "", "6"),
    BillingAddress("15 Queen's Rd Central", "Central, Hong Kong", "", "", "5"),
    BillingAddress("88 Queensway", "Admiralty, Hong Kong", "", "", "9"),
    BillingAddress("18 Salisbury Rd", "Tsim Sha Tsui, Kowloon", "", "", "6"),
    BillingAddress("1 Peking Rd", "Tsim Sha Tsui, Kowloon", "", "", "5"),
    BillingAddress("979 King's Rd", "Quarry Bay, Hong Kong", "", "", "9"),
    BillingAddress("33 Canton Rd", "Tsim Sha Tsui, Kowloon", "", "", "6"),
)


# ─────────────────────────────────────────────────────────────────
# JP 地址池（有都道府县存 state；zip_code 存 7 位邮编 NNN-NNNN；
# area_code 存城市电话区号：东京 "3" / 大阪 "6" / 名古屋 "52" / 横滨 "45"）
# 取真实公开商业楼宇地址（东京千代田/港区、大阪、名古屋、横滨等）
# 注：line1 用罗马字（romaji）以兼容 Stripe/PayPal 拉丁字符表单
# ─────────────────────────────────────────────────────────────────
_JP_ADDRESSES: tuple[BillingAddress, ...] = (
    BillingAddress("1-6-1 Marunouchi", "Chiyoda-ku, Tokyo", "Tokyo", "100-0005", "3"),
    BillingAddress("2-7-2 Marunouchi", "Chiyoda-ku, Tokyo", "Tokyo", "100-0005", "3"),
    BillingAddress("1-9-1 Roppongi", "Minato-ku, Tokyo", "Tokyo", "106-0032", "3"),
    BillingAddress("3-1-1 Shibuya", "Shibuya-ku, Tokyo", "Tokyo", "150-0002", "3"),
    BillingAddress("1-1-2 Dojima", "Kita-ku, Osaka", "Osaka", "530-0003", "6"),
    BillingAddress("2-4-9 Umeda", "Kita-ku, Osaka", "Osaka", "530-0001", "6"),
    BillingAddress("3-20-27 Meieki", "Nakamura-ku, Nagoya", "Aichi", "450-0002", "52"),
    BillingAddress("2-19-12 Minatomirai", "Nishi-ku, Yokohama", "Kanagawa", "220-0012", "45"),
)


# ─────────────────────────────────────────────────────────────────
# 国家画像注册表
# ─────────────────────────────────────────────────────────────────
_PROFILES: dict[str, CountryProfile] = {
    "US": CountryProfile(
        country_code="US",
        # 4147 = Chase Visa；4100 = Wells Fargo / Apple Card / Cash App Card（金融科技 Visa）
        bin_prefixes=((4, 1, 4, 7), (4, 1, 0, 0)),
        phone_kind="nanp",
        fallback_addresses=_US_ADDRESSES,
        has_state=True,
        postal_label="ZIP",
    ),
    "GB": CountryProfile(
        country_code="GB",
        # 4658 = Barclays Visa Debit (UK)；4751 = Lloyds/Halifax Visa Debit (UK)；5301 = NatWest Mastercard (UK)
        bin_prefixes=((4, 6, 5, 8), (4, 7, 5, 1), (5, 3, 0, 1)),
        phone_kind="gb",
        fallback_addresses=_GB_ADDRESSES,
        has_state=False,
        postal_label="Postcode",
    ),
    "CA": CountryProfile(
        country_code="CA",
        # 4519 = TD Canada Trust Visa；4506 = RBC Royal Bank Visa；5191 = Scotiabank Mastercard
        bin_prefixes=((4, 5, 1, 9), (4, 5, 0, 6), (5, 1, 9, 1)),
        phone_kind="nanp",
        fallback_addresses=_CA_ADDRESSES,
        has_state=True,
        postal_label="Postal code",
    ),
    "SG": CountryProfile(
        country_code="SG",
        # 4385 = OCBC Visa (SG)；5520 = UOB Mastercard (SG)。避开 4111 公开测试段。
        bin_prefixes=((4, 3, 8, 5), (5, 5, 2, 0)),
        phone_kind="sg",
        fallback_addresses=_SG_ADDRESSES,
        has_state=False,
        postal_label="Postal code",
    ),
    "HK": CountryProfile(
        country_code="HK",
        # 4622 = HSBC Hong Kong Visa；4033 = Hang Seng Bank Visa (HK)；5161 = Standard Chartered HK Mastercard
        bin_prefixes=((4, 6, 2, 2), (4, 0, 3, 3), (5, 1, 6, 1)),
        phone_kind="hk",
        fallback_addresses=_HK_ADDRESSES,
        has_state=False,
        postal_label="",
    ),
    "JP": CountryProfile(
        country_code="JP",
        # 4980 = SMBC (三井住友) Visa；4541 = MUFG (三菱UFJ) Visa；5334 = Rakuten (乐天) Mastercard
        bin_prefixes=((4, 9, 8, 0), (4, 5, 4, 1), (5, 3, 3, 4)),
        phone_kind="jp",
        fallback_addresses=_JP_ADDRESSES,
        has_state=True,  # 都道府县存 state 字段（Tokyo / Osaka / Aichi ...）
        postal_label="郵便番号",
    ),
}


def normalize_country(country: str | None) -> str:
    """规范化国家代码为大写；空/None 回退默认国。"""
    if not country:
        return DEFAULT_COUNTRY
    return str(country).strip().upper()


def get_profile(country: str | None = None) -> CountryProfile:
    """查国家画像。

    Args:
        country: ISO alpha-2 国家码（大小写不敏感）；None 回退 US。

    Returns:
        CountryProfile 实例。

    Raises:
        ValueError: 国家不在 SUPPORTED_COUNTRIES 时。
    """
    code = normalize_country(country)
    profile = _PROFILES.get(code)
    if profile is None:
        raise ValueError(
            f"不支持的国家代码 {code!r}；当前支持: {', '.join(SUPPORTED_COUNTRIES)}"
        )
    return profile


def bin_prefix_strings(country: str | None = None) -> tuple[str, ...]:
    """返回该国所有 BIN 前缀的字符串形式（如 ('4147','4100')），供 API 校验/前端下拉用。"""
    profile = get_profile(country)
    return tuple("".join(str(d) for d in prefix) for prefix in profile.bin_prefixes)


__all__ = [
    "SUPPORTED_COUNTRIES",
    "DEFAULT_COUNTRY",
    "CountryProfile",
    "get_profile",
    "normalize_country",
    "bin_prefix_strings",
]
