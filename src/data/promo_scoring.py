# -*- coding: utf-8 -*-
"""促销码候选启发式打分与优先级排序（纯函数层）

背景：
  code_discovery_service 串行扫候选码，1.0s/条（反 Cloudflare 节奏，不能并行），
  借来的 token 寿命有限（中途 401 整任务中止）。build_candidates() 返回的是
  **字母序** 列表，高命中码（KNOWN_BASES / 公司全名 / 历史命中过的码）被埋在中段，
  常在 token 过期前扫不到。

本模块在不改 build_candidates 的前提下，对已 build 的候选列表做启发式重排序，
让最可能命中的码先扫。设计：
  - 纯函数、零 IO、零 DB —— 历史信号由 service 层组装成 ScanHistory dataclass 传入，
    便于单测（无需 mock）。
  - 打分透明可加：各信号独立权重相加，越高越先扫。
  - 死码（历史 NOT_FOUND）默认仅沉底不删（防 OpenAI 重新上架旧码时漏掉）。

与 promo_seeds 的关系：
  - 复用 promo_seeds.KNOWN_BASES / COUNTRY_SUFFIXES。
  - split_suffix() 是 build_candidates 拼后缀的逆操作（拆 base/suffix）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.data.promo_seeds import COUNTRY_SUFFIXES, KNOWN_BASES


@dataclass(frozen=True)
class ScanHistory:
    """从 LinkTemplate + ScannedCode 读出的历史信号快照。

    service 层组装，纯层只读。所有集合元素应为小写。
    """

    hit_codes: frozenset[str] = frozenset()      # 历史 ELIGIBLE/EXISTS 命中码
    dead_codes: frozenset[str] = frozenset()     # 新鲜期内 NOT_FOUND 码 → 沉底/剪枝
    hit_bases: frozenset[str] = frozenset()      # 命中码拆出的有效词根（反馈环）
    hit_suffixes: frozenset[str] = frozenset()   # 命中码用到的后缀族（反馈环）


@dataclass(frozen=True)
class ScoreWeights:
    """各启发式信号的权重；可注入覆盖（便于实验调参）。"""

    known_base: float = 100.0        # base ∈ KNOWN_BASES（高频历史种子）
    history_hit: float = 80.0        # 该 code 历史 EXISTS/ELIGIBLE 命中过
    exact_company: float = 60.0      # base 等于某公司主变体（非缩写全名）
    base_match: float = 40.0         # base ∈ hit_bases（反馈环：命中码的兄弟）
    suffix_match: float = 10.0       # suffix ∈ hit_suffixes（反馈环）
    bare_word: float = 8.0           # 无后缀裸词（promo 码常见形态）
    short_penalty: float = -5.0      # 长度 3-4 的噪声变体降权
    initials_penalty: float = -15.0  # 纯首字母缩写（mt/kc 噪声）降权
    dead_penalty: float = -1000.0    # 新鲜期 NOT_FOUND → 沉底（不删，只降权）


def split_suffix(code: str, country: str) -> tuple[str, str]:
    """把候选码拆成 (base, suffix)，用 COUNTRY_SUFFIXES[country] 反向最长匹配。

    build_candidates 是 word + suffix 拼出来的，这里是逆操作。

    示例（GB 后缀 uk/gb/couk）：
      'datroaiuk'  → ('datroai', 'uk')
      'datroaicouk'→ ('datroai', 'couk')   # couk 比 uk 长，最长优先
      'datroai'    → ('datroai', '')       # 裸词无后缀

    Args:
        code: 候选码（小写或任意大小写，内部按小写匹配）
        country: ISO 国家码（大写，如 "GB"）

    Returns:
        (base, suffix)；无后缀时 suffix 为空串。纯函数。
    """
    cc = (country or "").strip().upper()
    low = (code or "").strip().lower()
    suffixes = COUNTRY_SUFFIXES.get(cc, ())
    # 最长后缀优先，避免 'couk' 被 'uk' 抢先匹配成残缺 base
    best = ""
    for suffix in suffixes:
        if low.endswith(suffix) and len(low) > len(suffix) and len(suffix) > len(best):
            best = suffix
    if best:
        return low[: -len(best)], best
    return low, ""


def score_candidate(
    code: str,
    country: str,
    *,
    known_bases: frozenset[str],
    company_mains: frozenset[str],
    history: ScanHistory,
    weights: ScoreWeights = ScoreWeights(),
) -> float:
    """单候选启发式打分；权重相加，越高越先扫。纯函数。

    Args:
        code: 候选码
        country: ISO 国家码（大写）
        known_bases: 高频种子词集合（小写），通常 frozenset(KNOWN_BASES)
        company_mains: 公司名主变体集合（小写，全名级，非首字母缩写）
        history: 历史信号快照
        weights: 权重表

    Returns:
        分值（float）。越高越优先。
    """
    low = (code or "").strip().lower()
    if not low:
        return weights.dead_penalty  # 空码当死码处理（理论上不会进来）

    base, suffix = split_suffix(low, country)
    score = 0.0

    # ── 正向信号 ──
    if base in known_bases:
        score += weights.known_base
    if low in history.hit_codes:
        score += weights.history_hit
    if base in company_mains:
        score += weights.exact_company
    if base in history.hit_bases:
        score += weights.base_match
    if suffix and suffix in history.hit_suffixes:
        score += weights.suffix_match
    if not suffix:
        score += weights.bare_word

    # ── 负向信号（把 normalize 噪声推到尾部）──
    if len(low) <= 4:
        score += weights.short_penalty
    # 纯首字母缩写：base 很短（≤3）且不是已知种子/公司/历史命中 → 大概率噪声
    if (
        len(base) <= 3
        and base not in known_bases
        and base not in company_mains
        and base not in history.hit_bases
    ):
        score += weights.initials_penalty

    # ── 死码沉底（最高优先级负权，压过一切正向信号）──
    if low in history.dead_codes:
        score += weights.dead_penalty

    return score


def prioritize_candidates(
    codes: Iterable[str],
    country: str,
    *,
    known_bases: frozenset[str] = frozenset(KNOWN_BASES),
    company_mains: frozenset[str] = frozenset(),
    history: ScanHistory = ScanHistory(),
    weights: ScoreWeights = ScoreWeights(),
    drop_dead: bool = False,
) -> list[str]:
    """对已 build 的候选列表按启发式分值重排序（高分先扫）。纯函数。

    排序 key = (-score, code)：分值降序；**平分时按字母序**，保证确定性（测试可断言）。

    Args:
        codes: 已 build 的候选码列表（如 build_candidates 输出）
        country: ISO 国家码（大写）
        known_bases: 高频种子词集合（默认 KNOWN_BASES）
        company_mains: 公司名主变体集合（service 层从字典组装）
        history: 历史信号快照
        weights: 权重表
        drop_dead: True 时剔除 history.dead_codes（再扫模式省 token）；
                   默认 False 仅沉底（用户决策：防 OpenAI 重新上架旧码漏掉）

    Returns:
        重排序后的候选码列表。drop_dead=False 时 len 不变。
    """
    items = [str(c).strip() for c in codes if str(c).strip()]
    if drop_dead and history.dead_codes:
        items = [c for c in items if c.lower() not in history.dead_codes]

    scored = [
        (
            score_candidate(
                c,
                country,
                known_bases=known_bases,
                company_mains=company_mains,
                history=history,
                weights=weights,
            ),
            c,
        )
        for c in items
    ]
    # 分值降序；平分按字母序（确定性）
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [c for _score, c in scored]


__all__ = [
    "ScanHistory",
    "ScoreWeights",
    "split_suffix",
    "score_candidate",
    "prioritize_candidates",
]
