# -*- coding: utf-8 -*-
"""Sentinel PoW 框架测试。

覆盖：
- SentinelProvider ABC + 默认 NoOpProvider 行为（永不抛异常）
- try_get_sentinel_token 顶层封装（provider=None / 成功 / 失败 / 抛异常 全静默）
- build_provider_from_config 工厂路由（noop / pure_python / 缺失字段）
- PurePythonProvider 算法层（_PurePythonTokenBuilder 单元测试，不打网络）
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.automation.sentinel import (
    DEFAULT_SDK_VERSION,
    NoOpProvider,
    PurePythonProvider,
    SentinelAttempt,
    SentinelProvider,
    _PurePythonTokenBuilder,
    build_provider_from_config,
    try_get_sentinel_token,
)


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------

class _StubProvider(SentinelProvider):
    """测试用：返回预设结果的 provider。"""

    def __init__(self, *, success: bool, token: str = "", raise_exc: bool = False):
        self._success = success
        self._token = token
        self._raise = raise_exc
        self.calls: list[dict] = []

    @property
    def provider_name(self) -> str:
        return "stub"

    def generate(self, *, flow="authorize_continue", user_agent="", device_id="", proxy=None):
        self.calls.append(
            {"flow": flow, "user_agent": user_agent, "device_id": device_id, "proxy": proxy}
        )
        if self._raise:
            raise RuntimeError("stub explosion")
        return SentinelAttempt(
            success=self._success,
            provider_name=self.provider_name,
            duration_ms=1,
            token=self._token,
            rationale="stub",
        )


# ---------------------------------------------------------------------------
# NoOpProvider
# ---------------------------------------------------------------------------

class TestNoOpProvider(unittest.TestCase):
    def test_provider_name_is_noop(self):
        self.assertEqual(NoOpProvider().provider_name, "noop")

    def test_generate_always_returns_failure(self):
        result = NoOpProvider().generate(flow="register")
        self.assertFalse(result.success)
        self.assertEqual(result.token, "")
        self.assertEqual(result.provider_name, "noop")

    def test_generate_does_not_raise_with_any_kwargs(self):
        # NoOp 必须接受任何 kwargs 组合不抛
        NoOpProvider().generate(
            flow="x", user_agent="y", device_id="z", proxy="socks5://1.2.3.4:1080"
        )


# ---------------------------------------------------------------------------
# try_get_sentinel_token 顶层封装
# ---------------------------------------------------------------------------

class TestTryGetSentinelToken(unittest.TestCase):
    def test_none_provider_returns_empty(self):
        self.assertEqual(try_get_sentinel_token(None, flow="register"), "")

    def test_noop_provider_returns_empty(self):
        self.assertEqual(try_get_sentinel_token(NoOpProvider()), "")

    def test_success_provider_returns_token(self):
        stub = _StubProvider(success=True, token="fake-sentinel-token-xyz")
        self.assertEqual(try_get_sentinel_token(stub), "fake-sentinel-token-xyz")

    def test_failed_provider_returns_empty(self):
        stub = _StubProvider(success=False)
        self.assertEqual(try_get_sentinel_token(stub), "")

    def test_provider_raising_returns_empty(self):
        # 即使 provider.generate 抛异常，顶层封装也要静默吞掉
        stub = _StubProvider(success=False, raise_exc=True)
        self.assertEqual(try_get_sentinel_token(stub), "")

    def test_provider_called_with_correct_kwargs(self):
        stub = _StubProvider(success=True, token="t")
        try_get_sentinel_token(
            stub, flow="register", user_agent="UA/1.0", device_id="did", proxy="http://p:1"
        )
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["flow"], "register")
        self.assertEqual(stub.calls[0]["user_agent"], "UA/1.0")
        self.assertEqual(stub.calls[0]["device_id"], "did")
        self.assertEqual(stub.calls[0]["proxy"], "http://p:1")


# ---------------------------------------------------------------------------
# build_provider_from_config 工厂
# ---------------------------------------------------------------------------

class TestBuildProviderFromConfig(unittest.TestCase):
    def test_noop_strategy_returns_noop(self):
        cfg = SimpleNamespace(sentinel_strategy="noop")
        provider = build_provider_from_config(cfg)
        self.assertIsInstance(provider, NoOpProvider)

    def test_missing_strategy_defaults_to_noop(self):
        cfg = SimpleNamespace()
        provider = build_provider_from_config(cfg)
        self.assertIsInstance(provider, NoOpProvider)

    def test_unknown_strategy_falls_back_to_noop(self):
        cfg = SimpleNamespace(sentinel_strategy="quickjs_node_voodoo")
        provider = build_provider_from_config(cfg)
        self.assertIsInstance(provider, NoOpProvider)

    def test_pure_python_strategy(self):
        cfg = SimpleNamespace(
            sentinel_strategy="pure_python",
            sentinel_sdk_version="20260124ceb8",
            sentinel_impersonate="chrome120",
            sentinel_timeout_ms=10000,
        )
        provider = build_provider_from_config(cfg)
        self.assertIsInstance(provider, PurePythonProvider)
        self.assertEqual(provider.sdk_version, "20260124ceb8")
        self.assertEqual(provider.impersonate, "chrome120")
        self.assertEqual(provider.timeout_seconds, 10.0)

    def test_pure_python_uses_defaults_when_fields_missing(self):
        cfg = SimpleNamespace(sentinel_strategy="pure_python")
        provider = build_provider_from_config(cfg)
        self.assertIsInstance(provider, PurePythonProvider)
        self.assertEqual(provider.sdk_version, DEFAULT_SDK_VERSION)
        self.assertEqual(provider.impersonate, "chrome120")
        # timeout 默认 10s
        self.assertEqual(provider.timeout_seconds, 10.0)


# ---------------------------------------------------------------------------
# _PurePythonTokenBuilder 算法单元测试
# ---------------------------------------------------------------------------

class TestPurePythonTokenBuilder(unittest.TestCase):
    def test_fnv1a_32_known_vectors(self):
        # FNV-1a 32-bit 已知测试向量（来自维基百科与 RFC 草案）
        # 经过尾部混淆步骤（h ^= h >> 16; * 2246822507; h ^= h >> 13; * 3266489909; h ^= h >> 16）
        # 这里只验证函数返回固定 8 字符 hex（稳定性测试）
        result_a = _PurePythonTokenBuilder._fnv1a_32("hello")
        result_b = _PurePythonTokenBuilder._fnv1a_32("hello")
        self.assertEqual(result_a, result_b, "同输入应得到同输出")
        self.assertEqual(len(result_a), 8, "应为 8 字符 hex")
        # 确认对不同输入产生不同输出
        self.assertNotEqual(
            _PurePythonTokenBuilder._fnv1a_32("a"),
            _PurePythonTokenBuilder._fnv1a_32("b"),
        )

    def test_requirements_token_format(self):
        builder = _PurePythonTokenBuilder(device_id="did", user_agent="UA")
        token = builder.generate_requirements_token()
        self.assertTrue(
            token.startswith("gAAAAAC"),
            f"requirements token 必须以 gAAAAAC 开头，实际: {token[:20]}",
        )

    def test_pow_token_with_zero_difficulty_returns_immediately(self):
        # difficulty="0" 时第一次循环就会匹配（因为 hex 字符 <= "0" 只有 '0'）
        # 但 FNV-1a 输出空间均匀，第一次匹配概率 1/16 → 几次循环内必中
        builder = _PurePythonTokenBuilder(device_id="did", user_agent="UA")
        token = builder.generate_pow_token(seed="test-seed", difficulty="0")
        self.assertTrue(
            token.startswith("gAAAAAB"),
            f"PoW token 必须以 gAAAAAB 开头，实际: {token[:20]}",
        )
        # 不应是 ERROR_PREFIX 降级路径（"0" 难度极低，500k 内必中）
        self.assertNotIn(
            _PurePythonTokenBuilder.ERROR_PREFIX, token,
            "0 难度时不应触发降级路径",
        )

    def test_sdk_version_appears_in_config(self):
        # _get_config()[6] 是 sdk.js URL，应包含 sdk_version
        builder = _PurePythonTokenBuilder(sdk_version="20990101aaaa")
        config = builder._get_config()
        self.assertIn("20990101aaaa", config[5])
        self.assertIn("sentinel.openai.com", config[5])


# ---------------------------------------------------------------------------
# PurePythonProvider 集成层（mock 网络）
# ---------------------------------------------------------------------------

class TestPurePythonProviderNetworkMocked(unittest.TestCase):
    def test_challenge_fetch_failure_returns_failed_attempt(self):
        """网络失败时返回 success=False，不抛异常。"""
        provider = PurePythonProvider()
        with patch("src.automation.sentinel._fetch_sentinel_challenge", return_value=None):
            attempt = provider.generate(flow="register")
        self.assertFalse(attempt.success)
        self.assertEqual(attempt.token, "")
        self.assertIn("failed to fetch", attempt.rationale)

    def test_challenge_missing_token_field_fails(self):
        provider = PurePythonProvider()
        with patch(
            "src.automation.sentinel._fetch_sentinel_challenge",
            return_value={"proofofwork": {"required": False}},
        ):
            attempt = provider.generate(flow="register")
        self.assertFalse(attempt.success)
        self.assertIn("missing", attempt.rationale.lower())

    def test_challenge_success_without_pow_required(self):
        provider = PurePythonProvider()
        with patch(
            "src.automation.sentinel._fetch_sentinel_challenge",
            return_value={
                "token": "challenge-c-value",
                "proofofwork": {"required": False},
            },
        ):
            attempt = provider.generate(flow="register", device_id="fixed-did")
        self.assertTrue(attempt.success)
        self.assertTrue(attempt.token)
        # token 是 JSON 字符串
        import json
        parsed = json.loads(attempt.token)
        self.assertEqual(parsed["c"], "challenge-c-value")
        self.assertEqual(parsed["id"], "fixed-did")
        self.assertEqual(parsed["flow"], "register")
        self.assertTrue(parsed["p"].startswith("gAAAAA"))

    def test_challenge_success_with_pow_required(self):
        provider = PurePythonProvider()
        with patch(
            "src.automation.sentinel._fetch_sentinel_challenge",
            return_value={
                "token": "c-val",
                "proofofwork": {"required": True, "seed": "abc", "difficulty": "0"},
            },
        ):
            attempt = provider.generate(flow="register")
        self.assertTrue(attempt.success)

    def test_exception_in_curl_cffi_returns_failed_attempt(self):
        # 模拟 curl_cffi.Session 构造抛异常
        provider = PurePythonProvider()
        with patch("curl_cffi.requests.Session", side_effect=RuntimeError("connection refused")):
            attempt = provider.generate(flow="register")
        self.assertFalse(attempt.success)
        self.assertIn("exception", attempt.rationale.lower())


if __name__ == "__main__":
    unittest.main()
