# -*- coding: utf-8 -*-
"""
指纹 / 代理健康度评分器

在 AdsPower 浏览器连接成功但尚未跳转到 OpenAI 之前，
通过 ``page.evaluate`` 采集浏览器指纹与 IP 信息，按预定义规则打分：

- WebRTC 泄漏：扣 40 分并直接判定为 ``fail``
- IP 国家信息缺失：扣 20 分
- IP 国家与期望国家不一致：扣 30 分
- Canvas 指纹哈希为空或命中已知无头签名：扣 15 分
- UserAgent 含 ``HeadlessChrome``：扣 25 分
- 时区与 IP 国家不匹配（基于内置映射）：扣 20 分
- 浏览器语言为空：扣 5 分

最终根据得分输出 ``pass`` / ``warn`` / ``fail`` 评判，供调用方决定是否
放弃当前浏览器配置并轮换。

本模块不引入任何硬性外部依赖（如 Playwright），
``page`` 在类型上以 ``Any`` 存在，便于测试用 ``MagicMock`` 注入。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量与内置映射
# ---------------------------------------------------------------------------

# 已知无头浏览器 Canvas 指纹特征（十六进制字符串片段）。
# 说明：真实环境中 Chromium Headless 会产生一组高熵但在同版本间稳定的 hash，
# 这里仅保留几个典型的空/零值签名作为兜底判据，其余签名由后续迭代补齐。
_KNOWN_HEADLESS_CANVAS_HASHES: frozenset[str] = frozenset(
    {
        # 完全空白画布的 SHA-1（空字符串或纯 data:, 的哈希）
        "da39a3ee5e6b4b0d3255bfef95601890afd80709",  # SHA-1("")
        "adc83b19e793491b1c6ea0fd8b46cd9f32e592fc",  # SHA-1("\n")
    }
)

# 时区 → ISO alpha-2 国家代码的最小映射，未覆盖时跳过一致性检查。
_TIMEZONE_COUNTRY_MAP: dict[str, str] = {
    "Europe/London": "GB",
    "Asia/Tokyo": "JP",
    "Asia/Shanghai": "CN",
    "Asia/Hong_Kong": "HK",
    "Asia/Singapore": "SG",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
}

# 前缀匹配：America/* 归属北美（US/CA），此处统一按 US 比对，CA 另行放行。
_AMERICA_PREFIX = "America/"
_AMERICA_COUNTRIES: frozenset[str] = frozenset({"US", "CA"})


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FingerprintReport:
    """指纹健康度评分报告。"""

    score: int
    ip_country: str
    ip_address: str
    webrtc_leaked_ips: list[str]
    canvas_hash: str
    user_agent: str
    timezone: str
    language: str
    issues: list[str] = field(default_factory=list)
    verdict: str = "pass"


# ---------------------------------------------------------------------------
# 浏览器侧采集脚本
# ---------------------------------------------------------------------------

_CANVAS_SCRIPT = """
() => {
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 220;
    canvas.height = 30;
    const ctx = canvas.getContext('2d');
    ctx.textBaseline = 'top';
    ctx.font = "14px 'Arial'";
    ctx.fillStyle = '#f60';
    ctx.fillRect(125, 1, 62, 20);
    ctx.fillStyle = '#069';
    ctx.fillText('fingerprint-canary-éç', 2, 15);
    return canvas.toDataURL();
  } catch (e) {
    return '';
  }
}
"""

_NAVIGATOR_SCRIPT = """
() => ({
  userAgent: navigator.userAgent || '',
  language: navigator.language || '',
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
})
"""

# WebRTC 泄漏检测：创建 RTCPeerConnection，取 ICE 候选中的公网 IP。
# 使用 a=candidate 行解析；仅保留非内网、非 mDNS 的候选。
_WEBRTC_SCRIPT = """
() => new Promise((resolve) => {
  const leaked = new Set();
  try {
    const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
    pc.createDataChannel('probe');
    pc.onicecandidate = (evt) => {
      if (!evt || !evt.candidate || !evt.candidate.candidate) return;
      const parts = evt.candidate.candidate.split(' ');
      const ip = parts[4];
      if (!ip) return;
      if (ip.endsWith('.local')) return;
      if (/^(10\\.|127\\.|192\\.168\\.|169\\.254\\.|172\\.(1[6-9]|2\\d|3[0-1])\\.)/.test(ip)) return;
      if (/^::1$|^fc|^fd|^fe80/i.test(ip)) return;
      leaked.add(ip);
    };
    pc.createOffer().then((offer) => pc.setLocalDescription(offer)).catch(() => {});
    setTimeout(() => {
      try { pc.close(); } catch (e) {}
      resolve(Array.from(leaked));
    }, 2500);
  } catch (e) {
    resolve([]);
  }
})
"""

_IPINFO_SCRIPT = """
() => fetch('https://ipinfo.io/json', { cache: 'no-store' })
  .then((r) => r.json())
  .catch(() => ({}))
"""


# ---------------------------------------------------------------------------
# 采集辅助
# ---------------------------------------------------------------------------


def _safe_evaluate(page: Any, script: str, default: Any) -> Any:
    """包装 ``page.evaluate``，捕获异常返回默认值。"""
    try:
        return page.evaluate(script)
    except Exception as exc:  # noqa: BLE001 — 浏览器脚本异常需统一降级
        logger.warning("page.evaluate 执行失败，返回默认值。script 摘要=%s err=%s", script[:48], exc)
        return default


def _hash_canvas(data_url: str) -> str:
    """对 Canvas dataURL 做 SHA-1，返回 40 位十六进制字符串。"""
    if not data_url:
        return ""
    return hashlib.sha1(data_url.encode("utf-8")).hexdigest()


def _default_ip_lookup(page: Any) -> dict:
    """默认 IP 归属查询：通过浏览器 ``fetch`` 调 ipinfo.io，离线时返回空字典。"""
    try:
        data = page.evaluate(_IPINFO_SCRIPT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ipinfo 查询失败：%s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _timezone_country(tz: str) -> str:
    """根据时区推断国家代码，未覆盖返回空串。"""
    if not tz:
        return ""
    if tz in _TIMEZONE_COUNTRY_MAP:
        return _TIMEZONE_COUNTRY_MAP[tz]
    if tz.startswith(_AMERICA_PREFIX):
        return "US"
    return ""


def _timezone_consistent_with_country(tz: str, country: str) -> bool:
    """判断时区与 IP 国家是否一致。无法判定时返回 True（跳过扣分）。"""
    inferred = _timezone_country(tz)
    if not inferred or not country:
        return True
    if tz.startswith(_AMERICA_PREFIX):
        return country.upper() in _AMERICA_COUNTRIES
    return inferred.upper() == country.upper()


# ---------------------------------------------------------------------------
# 评分主流程
# ---------------------------------------------------------------------------


def score_page_fingerprint(
    page: Any,
    *,
    expected_country: str = "",
    min_score: int = 90,
    ip_lookup: Optional[Callable[[], dict]] = None,
) -> FingerprintReport:
    """
    对已连接的 Playwright ``page`` 做一次指纹健康评分。

    Args:
        page: Playwright ``Page`` 对象（或等价 mock）。
        expected_country: 期望的 IP 国家 ISO alpha-2；为空则跳过国家匹配扣分。
        min_score: ``pass`` 的最低得分阈值。
        ip_lookup: 可注入的 IP 归属查询函数，测试时用于避免真实网络。

    Returns:
        ``FingerprintReport``：包含原始采集值、问题清单、得分与评判。
    """

    issues: list[str] = []

    # --- 1) 基础 navigator 信息 ---
    nav = _safe_evaluate(page, _NAVIGATOR_SCRIPT, {}) or {}
    user_agent = str(nav.get("userAgent", "") or "")
    language = str(nav.get("language", "") or "")
    timezone = str(nav.get("timezone", "") or "")

    # --- 2) Canvas 指纹 ---
    canvas_data_url = _safe_evaluate(page, _CANVAS_SCRIPT, "") or ""
    canvas_hash = _hash_canvas(str(canvas_data_url))

    # --- 3) WebRTC 泄漏 ---
    raw_webrtc = _safe_evaluate(page, _WEBRTC_SCRIPT, []) or []
    webrtc_leaked_ips = [str(ip) for ip in raw_webrtc if isinstance(ip, str) and ip]

    # --- 4) IP 归属 ---
    try:
        lookup_payload = ip_lookup() if ip_lookup else _default_ip_lookup(page)
    except Exception as exc:  # noqa: BLE001 — 离线或接口异常需降级
        logger.warning("IP 归属查询抛出异常，降级为空：%s", exc)
        lookup_payload = {}

    ip_address = str((lookup_payload or {}).get("ip", "") or "")
    ip_country = str((lookup_payload or {}).get("country", "") or "").upper()

    # --- 5) 打分 ---
    score = 100
    force_fail = False

    if webrtc_leaked_ips:
        score -= 40
        force_fail = True
        issues.append(f"WebRTC 泄漏公网 IP: {', '.join(webrtc_leaked_ips)}")

    if not ip_country:
        score -= 20
        issues.append("IP 归属国家未知或查询失败")

    if expected_country and ip_country and ip_country != expected_country.upper():
        score -= 30
        issues.append(
            f"IP 国家与期望不符：expected={expected_country.upper()} actual={ip_country}"
        )

    if not canvas_hash:
        score -= 15
        issues.append("Canvas 指纹采集失败（hash 为空）")
    elif canvas_hash in _KNOWN_HEADLESS_CANVAS_HASHES:
        score -= 15
        issues.append(f"Canvas 指纹命中已知无头签名：{canvas_hash}")

    if "HeadlessChrome" in user_agent:
        score -= 25
        issues.append("UserAgent 暴露 HeadlessChrome")

    if not _timezone_consistent_with_country(timezone, ip_country):
        score -= 20
        issues.append(
            f"时区与 IP 国家不一致：tz={timezone} ip_country={ip_country}"
        )

    if not language:
        score -= 5
        issues.append("navigator.language 为空")

    # 得分下限：防止出现负分导致后续阈值比较混乱
    score = max(score, 0)

    # --- 6) 评判 ---
    if force_fail:
        verdict = "fail"
    elif score >= min_score:
        verdict = "pass"
    elif score >= 70:
        verdict = "warn"
    else:
        verdict = "fail"

    return FingerprintReport(
        score=score,
        ip_country=ip_country,
        ip_address=ip_address,
        webrtc_leaked_ips=webrtc_leaked_ips,
        canvas_hash=canvas_hash,
        user_agent=user_agent,
        timezone=timezone,
        language=language,
        issues=issues,
        verdict=verdict,
    )
