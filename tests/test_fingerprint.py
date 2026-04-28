# -*- coding: utf-8 -*-
"""指纹健康度评分器单元测试"""

import unittest
from unittest.mock import MagicMock

from src.infra.fingerprint import (
    FingerprintReport,
    score_page_fingerprint,
)


def _build_page(
    *,
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0 Safari/537.36",
    language: str = "en-US",
    timezone: str = "America/New_York",
    canvas_data: str = "data:image/png;base64,CANVASCANARY",
    webrtc_leaks: list[str] | None = None,
) -> MagicMock:
    """构造一个伪造的 Playwright ``page``，按脚本关键字路由返回值。"""

    webrtc_leaks = webrtc_leaks or []
    nav_payload = {
        "userAgent": user_agent,
        "language": language,
        "timezone": timezone,
    }

    def fake_evaluate(script: str):
        if "navigator.userAgent" in script:
            return nav_payload
        if "createElement('canvas')" in script:
            return canvas_data
        if "RTCPeerConnection" in script:
            return list(webrtc_leaks)
        if "ipinfo.io" in script:
            # 默认路径不应被命中，测试走 ip_lookup 注入
            raise AssertionError("默认 ipinfo 路径不应在测试中被调用")
        raise AssertionError(f"未预期的 evaluate 调用: {script[:60]}")

    page = MagicMock()
    page.evaluate.side_effect = fake_evaluate
    return page


class TestFingerprintScorer(unittest.TestCase):
    """``score_page_fingerprint`` 主流程测试"""

    def test_clean_fingerprint_passes(self):
        """干净指纹得满分且评判为 pass"""
        page = _build_page()
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            min_score=90,
            ip_lookup=lookup,
        )

        self.assertIsInstance(report, FingerprintReport)
        self.assertEqual(report.score, 100)
        self.assertEqual(report.verdict, "pass")
        self.assertEqual(report.issues, [])
        self.assertEqual(report.ip_country, "US")
        self.assertEqual(report.ip_address, "203.0.113.10")
        self.assertEqual(report.webrtc_leaked_ips, [])
        self.assertTrue(report.canvas_hash)
        self.assertEqual(report.language, "en-US")
        self.assertEqual(report.timezone, "America/New_York")
        lookup.assert_called_once()

    def test_webrtc_leak_forces_fail(self):
        """WebRTC 泄漏必定 fail，即使其它信号干净"""
        page = _build_page(webrtc_leaks=["203.0.113.99"])
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.verdict, "fail")
        self.assertEqual(report.score, 60)  # 100 - 40
        self.assertIn("203.0.113.99", report.webrtc_leaked_ips)
        self.assertTrue(any("WebRTC" in issue for issue in report.issues))

    def test_webrtc_leak_fail_regardless_of_high_min_score(self):
        """WebRTC 泄漏优先级高于得分阈值判断"""
        page = _build_page(webrtc_leaks=["198.51.100.5"])
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            min_score=50,  # 即使阈值很低，仍应 fail
            ip_lookup=lookup,
        )
        self.assertEqual(report.verdict, "fail")

    def test_ip_country_mismatch_penalized(self):
        """IP 国家与期望不符应扣 30 分并写入 issues"""
        page = _build_page()
        lookup = MagicMock(return_value={"ip": "1.2.3.4", "country": "CN"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        # 100 - 30 (国家不符) - 20 (时区 America/* 与 CN 不符) = 50
        self.assertEqual(report.score, 50)
        self.assertEqual(report.verdict, "fail")
        self.assertTrue(any("IP 国家与期望不符" in issue for issue in report.issues))

    def test_headless_user_agent_penalized(self):
        """UserAgent 含 HeadlessChrome 应扣 25 分"""
        page = _build_page(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120.0 Safari/537.36",
        )
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.score, 75)  # 100 - 25
        self.assertEqual(report.verdict, "warn")
        self.assertTrue(any("HeadlessChrome" in issue for issue in report.issues))

    def test_timezone_country_mismatch_penalized(self):
        """时区与 IP 国家不一致时扣 20 分"""
        page = _build_page(timezone="Asia/Shanghai")
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.score, 80)  # 100 - 20
        self.assertEqual(report.verdict, "warn")
        self.assertTrue(any("时区与 IP 国家不一致" in issue for issue in report.issues))

    def test_injected_ip_lookup_is_used_not_default(self):
        """注入的 ip_lookup 应被调用，默认 ipinfo 路径不应触发"""
        page = _build_page()
        sentinel = {"ip": "198.51.100.77", "country": "GB"}
        lookup = MagicMock(return_value=sentinel)

        report = score_page_fingerprint(
            page,
            expected_country="GB",
            ip_lookup=lookup,
        )

        lookup.assert_called_once_with()
        self.assertEqual(report.ip_address, "198.51.100.77")
        self.assertEqual(report.ip_country, "GB")
        # 校验默认 ipinfo 脚本确实没被 evaluate 调用
        for call in page.evaluate.call_args_list:
            self.assertNotIn("ipinfo.io", call.args[0])

    def test_offline_ip_lookup_degrades_gracefully(self):
        """ip_lookup 抛异常时 ip_country 为空且不崩溃"""
        page = _build_page()

        def raising_lookup() -> dict:
            raise ConnectionError("offline")

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=raising_lookup,
        )

        self.assertEqual(report.ip_country, "")
        self.assertEqual(report.ip_address, "")
        # 100 - 20（国家未知）= 80；国家为空时不触发“不符”扣分
        self.assertEqual(report.score, 80)
        self.assertEqual(report.verdict, "warn")
        self.assertTrue(any("未知或查询失败" in issue for issue in report.issues))

    def test_empty_canvas_penalized(self):
        """Canvas 采集失败时扣 15 分"""
        page = _build_page(canvas_data="")
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.canvas_hash, "")
        self.assertEqual(report.score, 85)  # 100 - 15
        self.assertTrue(any("Canvas" in issue for issue in report.issues))

    def test_empty_language_penalized(self):
        """navigator.language 为空时扣 5 分"""
        page = _build_page(language="")
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.score, 95)
        self.assertEqual(report.verdict, "pass")  # 95 >= 90 仍算通过
        self.assertTrue(any("language" in issue for issue in report.issues))

    def test_ca_timezone_accepts_canada_ip(self):
        """America/* 时区应同时接受 US / CA 作为一致国家"""
        page = _build_page(timezone="America/Toronto")
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "CA"})

        report = score_page_fingerprint(
            page,
            expected_country="CA",
            ip_lookup=lookup,
        )

        self.assertEqual(report.score, 100)
        self.assertEqual(report.verdict, "pass")

    def test_unknown_timezone_skips_consistency_check(self):
        """内置映射未覆盖的时区跳过一致性检查，不扣分"""
        page = _build_page(timezone="Africa/Nairobi")
        lookup = MagicMock(return_value={"ip": "203.0.113.10", "country": "US"})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.score, 100)
        self.assertEqual(report.verdict, "pass")

    def test_empty_ip_country_skips_mismatch_penalty(self):
        """IP 国家为空只扣“未知”20 分，不再额外扣“不符”30 分"""
        page = _build_page()
        lookup = MagicMock(return_value={"ip": "", "country": ""})

        report = score_page_fingerprint(
            page,
            expected_country="US",
            ip_lookup=lookup,
        )

        self.assertEqual(report.score, 80)
        self.assertTrue(any("未知或查询失败" in issue for issue in report.issues))
        self.assertFalse(any("期望不符" in issue for issue in report.issues))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
