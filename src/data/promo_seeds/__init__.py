# -*- coding: utf-8 -*-
"""促销码候选词种子与组合算法

参考 gpt-promo-scanner/discover_codes.py 的候选码生成机制：
  - KNOWN_BASES：高频种子词（历史已验证过有效的基础码片段）
  - COUNTRY_SUFFIXES：每个国家可能使用的后缀（如 GB → uk/gb/couk）
  - 公司名词典：按国家分文件外置 JSON（uk_companies.json 等），便于运维扩充
  - normalize()：把"Made Tech"爆破成 madetech/mt/made 等变体
  - build_candidates(country)：笛卡尔积 → normalize → 拼后缀 → 去重

设计：
  - 字典纯数据外置，便于运维改 JSON 不发版
  - 高频种子硬编码，避免一次扫描漏掉核心命中
  - 不在这里调网络/不写 DB，纯函数好测
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional

# 默认字典目录（相对本文件）；config 层可通过 PROMO_SEEDS_DIR 覆盖
_DEFAULT_SEEDS_DIR = Path(__file__).resolve().parent


# ============================================================
# 高频种子词（历史有效的 base，所有国家都会拼一遍）
# ============================================================
KNOWN_BASES: tuple[str, ...] = (
    "aibuildgroup", "wildmango", "firstfocus", "codestone",
    "talentgenius", "thealloynetwork", "alongside", "monicai",
    "thinkingmachines",
    "trintel", "noranalytos", "infoseekai", "datroai", "vouchapi",
)


# ============================================================
# 国家 → 后缀映射（promo 码尾巴常见模式）
# 一个国家可有多个后缀，会全部组合一遍
# ============================================================
COUNTRY_SUFFIXES: dict[str, tuple[str, ...]] = {
    "GB": ("uk", "gb", "couk"),
    "US": ("us", "usa"),
    "AU": ("au", "aus"),
    "CA": ("ca", "can"),
    "DE": ("de", "ger"),
    "FR": ("fr", "fra"),
    "ES": ("es", "esp"),
    "IT": ("it", "ita"),
    "NL": ("nl", "ned"),
    "IE": ("ie", "ire"),
    "NZ": ("nz", "nzl"),
    "BR": ("br", "bra"),
    "ZA": ("za",),
    "KE": ("ke",),
    "NG": ("ng",),
    "JP": ("jp", "jpn"),
    "IN": ("in", "ind"),
    "SG": ("sg", "sgp"),
    "KR": ("kr", "kor"),
    "SE": ("se", "swe"),
    "NO": ("no", "nor"),
    "DK": ("dk", "dnk"),
    "FI": ("fi", "fin"),
    "CH": ("ch", "che"),
    "AT": ("at", "aut"),
    "BE": ("be", "bel"),
    "MX": ("mx", "mex"),
    "AE": ("ae", "uae"),
    "SA": ("sa",),
    "IL": ("il", "isr"),
    "TR": ("tr", "tur"),
    "PL": ("pl", "pol"),
    "CZ": ("cz", "cze"),
    "RO": ("ro", "rou"),
    "PH": ("ph", "phl"),
    "TH": ("th", "tha"),
    "MY": ("my", "mys"),
    "ID": ("id", "idn"),
    "VN": ("vn", "vnm"),
    "HK": ("hk",),
    "TW": ("tw",),
}


# 词尾常见公司类型后缀（normalize 时会脱掉，再加回各种国家后缀生成更多变体）
_COMPANY_TAIL_SUFFIXES: tuple[str, ...] = (
    "technologies", "services", "solutions", "group", "international",
    "consulting", "systems", "software", "security", "labs", "digital",
    "global", "partners", "limited", "ltd", "corp", "inc",
)


def normalize(name: str) -> list[str]:
    """把公司名爆破成候选词变体。

    示例：
      "Made Tech" → ["madetech", "made", "mt"]
      "The AI Build Group" → ["aibuildgroup", "the", "tabg", "theai"]
      "Trading 212" → ["trading212", "trading", "t2", ...]

    Args:
        name: 原始公司名（带空格/连字符/符号皆可）

    Returns:
        去重后的变体列表（小写、无空格、无标点）
    """
    if not name:
        return []
    raw = name.strip()
    base = re.sub(r"[\s\-_./,&+']", "", raw).lower()
    variants: set[str] = set()
    if base:
        variants.add(base)

    # 去掉常见 "the" 前缀
    if base.startswith("the") and len(base) > 3:
        variants.add(base[3:])

    # 拆分词，生成首字母缩写 / 首词 / 前两词
    words = [w for w in re.split(r"[\s\-_./,&+']+", raw.lower()) if w]
    if len(words) > 1:
        initials = "".join(w[0] for w in words if w)
        if initials:
            variants.add(initials)
        variants.add(words[0])
        variants.add("".join(words[:2]))

    # 脱掉公司类型后缀（technologies/labs/etc）
    for suffix in _COMPANY_TAIL_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            variants.add(base[: -len(suffix)])

    # 去空 + 去重 + 排序保证稳定
    return sorted(v for v in variants if v)


# ============================================================
# 字典加载
# ============================================================

def _load_companies_json(country: str, seeds_dir: Path) -> list[str]:
    """读取 {country.lower()}_companies.json；不存在返回空列表。"""
    path = seeds_dir / f"{country.lower()}_companies.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data if isinstance(x, str) and x.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def list_supported_countries(seeds_dir: Optional[Path] = None) -> list[str]:
    """列出有字典文件的国家码（用于前端国家下拉的提示）。"""
    base = seeds_dir or _DEFAULT_SEEDS_DIR
    out: list[str] = []
    if base.exists():
        for path in base.glob("*_companies.json"):
            cc = path.stem.replace("_companies", "").upper()
            if cc in COUNTRY_SUFFIXES:
                out.append(cc)
    return sorted(out)


# ============================================================
# 候选码生成（核心 API）
# ============================================================

def build_candidates(
    country: str,
    *,
    extra_words: Iterable[str] = (),
    seeds_dir: Optional[Path] = None,
    include_known_bases: bool = True,
) -> list[str]:
    """生成某个国家的候选 promo 码列表。

    流程：
      1. 起始词池 = KNOWN_BASES + 公司名 normalize 后的变体 + extra_words normalize
      2. 笛卡尔积：每个词 × 该国家所有后缀（COUNTRY_SUFFIXES[country]）
      3. 同时保留"无后缀"版本（部分 promo 码不带国家后缀）
      4. 去重 + 排序

    Args:
        country: ISO 国家码，大写（如 "GB"）
        extra_words: 用户自定义关键词（运维补充，会跟字典词一样做 normalize）
        seeds_dir: 字典目录覆盖（默认本 package 内）；测试时方便注入
        include_known_bases: False 时跳过 KNOWN_BASES（适用于"只扫公司词典"场景）

    Returns:
        候选码列表，已去重排序。典型 GB 国家 1500-3000 条
    """
    cc = (country or "").strip().upper()
    if cc not in COUNTRY_SUFFIXES:
        return []

    base = seeds_dir or _DEFAULT_SEEDS_DIR
    suffixes = COUNTRY_SUFFIXES[cc]

    word_pool: set[str] = set()
    if include_known_bases:
        word_pool.update(KNOWN_BASES)

    for company in _load_companies_json(cc, base):
        for variant in normalize(company):
            word_pool.add(variant)

    for word in extra_words or ():
        for variant in normalize(word):
            word_pool.add(variant)

    candidates: set[str] = set()
    for word in word_pool:
        candidates.add(word)  # 无后缀
        for suffix in suffixes:
            candidates.add(word + suffix)

    # 去掉太短的（< 3 个字符，几乎肯定 not_found 且浪费配额）
    return sorted(c for c in candidates if len(c) >= 3)


def build_cross_matrix(
    countries: Iterable[str],
    *,
    extra_words: Iterable[str] = (),
    seeds_dir: Optional[Path] = None,
) -> list[tuple[str, str]]:
    """跨国家矩阵：每个国家 × 该国家的候选码。

    用途：同一基础词在多个国家同时扫描，发现"哪些国家命中同一品牌"。

    Args:
        countries: 要扫描的国家码列表
        extra_words: 通用关键词（所有国家都会加）
        seeds_dir: 同 build_candidates

    Returns:
        [(country, candidate_code), ...]；按 country 分组、code 内排序
    """
    pairs: list[tuple[str, str]] = []
    for cc in countries or ():
        cc_upper = (cc or "").strip().upper()
        if cc_upper not in COUNTRY_SUFFIXES:
            continue
        for code in build_candidates(cc_upper, extra_words=extra_words, seeds_dir=seeds_dir):
            pairs.append((cc_upper, code))
    return pairs


__all__ = [
    "KNOWN_BASES",
    "COUNTRY_SUFFIXES",
    "normalize",
    "list_supported_countries",
    "build_candidates",
    "build_cross_matrix",
]
