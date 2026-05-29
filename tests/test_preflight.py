# -*- coding: utf-8 -*-
"""Preflight 集成模块测试。"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from src.orchestration.preflight import (
    PreflightReport,
    _resolve_min_score,
    _resolve_mode,
    check_coherence,
    fetch_exit_ip,
    fetch_exit_ip_from_page,
    run_preflight,
    write_run_exit_ip,
)


def _cfg(**kw):
    defaults = dict(
        sms_country="187",  # US in our mapping
        billing_country="US",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _fake_fingerprint(verdict="pass", score=95):
    return SimpleNamespace(verdict=verdict, score=score)


class TestModeResolution(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.dict(os.environ, {}, clear=False)
        self._patch.start()
        os.environ.pop("PREFLIGHT_MODE", None)

    def tearDown(self):
        self._patch.stop()

    def test_mode_env_wins(self):
        with mock.patch.dict(os.environ, {"PREFLIGHT_MODE": "block"}):
            self.assertEqual(_resolve_mode(_cfg()), "block")

    def test_mode_config_fallback(self):
        self.assertEqual(_resolve_mode(_cfg(preflight_mode="off")), "off")

    def test_mode_default_warn(self):
        self.assertEqual(_resolve_mode(_cfg()), "warn")

    def test_mode_invalid_falls_back_to_warn(self):
        self.assertEqual(_resolve_mode(_cfg(preflight_mode="garbage")), "warn")


class TestMinScoreResolution(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FINGERPRINT_MIN_SCORE", None)

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"FINGERPRINT_MIN_SCORE": "95"}):
            self.assertEqual(_resolve_min_score(_cfg()), 95)

    def test_config_fallback(self):
        self.assertEqual(_resolve_min_score(_cfg(fingerprint_min_score=70)), 70)

    def test_default(self):
        self.assertEqual(_resolve_min_score(_cfg()), 80)

    def test_invalid_value_fallback(self):
        with mock.patch.dict(os.environ, {"FINGERPRINT_MIN_SCORE": "notanumber"}):
            self.assertEqual(_resolve_min_score(_cfg()), 80)


class TestCheckCoherence(unittest.TestCase):
    def test_all_match(self):
        rep = check_coherence(
            config=_cfg(sms_country="187", billing_country="US"),
            card_bin_country="US",
            proxy_country="US",
        )
        self.assertTrue(rep.ok)
        self.assertEqual(rep.severity, "ok")

    def test_card_mismatch_blocks(self):
        rep = check_coherence(
            config=_cfg(sms_country="187", billing_country="US"),
            card_bin_country="HK",
            proxy_country="US",
        )
        # 1 field deviates → warn per Fintech agent's semantics
        self.assertFalse(rep.ok)
        self.assertIn(rep.severity, ("warn", "block"))


class TestRunPreflight(unittest.TestCase):
    def setUp(self):
        for k in ("PREFLIGHT_MODE", "FINGERPRINT_MIN_SCORE"):
            os.environ.pop(k, None)

    def test_mode_off_short_circuits(self):
        emitted = []
        rep = run_preflight(
            config=_cfg(preflight_mode="off"),
            card_bin_country="",
            proxy_country="",
            page=None,
            emit_event=lambda t, p: emitted.append((t, p)),
        )
        self.assertTrue(rep.ok)
        self.assertEqual(rep.mode, "off")
        self.assertFalse(rep.should_abort)
        # off 模式完全跳过，不发事件
        self.assertEqual(emitted, [])

    def test_warn_mode_never_aborts(self):
        rep = run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="",  # empty → block severity in coherence
            proxy_country="",
            page=None,
        )
        self.assertFalse(rep.should_abort)  # warn 永远不 abort
        self.assertEqual(rep.mode, "warn")
        self.assertIn("block", rep.coherence.severity)  # 一致性已经是 block 级

    def test_block_mode_aborts_on_coherence(self):
        rep = run_preflight(
            config=_cfg(preflight_mode="block"),
            card_bin_country="",
            proxy_country="",
            page=None,
        )
        self.assertTrue(rep.should_abort)
        self.assertFalse(rep.ok)

    def test_fingerprint_fail_aborts_in_block(self):
        fake_scorer = mock.Mock(return_value=_fake_fingerprint(verdict="fail", score=20))
        rep = run_preflight(
            config=_cfg(preflight_mode="block"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=fake_scorer,
        )
        self.assertTrue(rep.should_abort)
        self.assertEqual(rep.fingerprint.verdict, "fail")
        fake_scorer.assert_called_once()

    def test_fingerprint_pass_in_block(self):
        fake_scorer = mock.Mock(return_value=_fake_fingerprint(verdict="pass", score=95))
        rep = run_preflight(
            config=_cfg(preflight_mode="block"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=fake_scorer,
        )
        self.assertFalse(rep.should_abort)
        self.assertTrue(rep.ok)

    def test_event_emission_shape(self):
        captured = []
        fake_scorer = mock.Mock(return_value=_fake_fingerprint(verdict="warn", score=75))
        run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=fake_scorer,
            emit_event=lambda t, p: captured.append((t, p)),
        )
        self.assertEqual(len(captured), 1)
        event_type, payload = captured[0]
        self.assertEqual(event_type, "preflight")
        self.assertEqual(payload["mode"], "warn")
        self.assertEqual(payload["fingerprint_verdict"], "warn")
        self.assertEqual(payload["fingerprint_score"], 75)
        self.assertFalse(payload["will_abort"])

    def test_fingerprint_scorer_exception_degrades_gracefully(self):
        def bad_scorer(*args, **kwargs):
            raise RuntimeError("boom")

        rep = run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="US",
            proxy_country="US",
            page=mock.Mock(),
            scorer=bad_scorer,
        )
        # 异常 → fingerprint 为 None，coherence 为 ok → 整体 ok
        self.assertIsNone(rep.fingerprint)
        self.assertTrue(rep.ok)

    def test_event_emit_failure_does_not_crash(self):
        def bad_emit(t, p):
            raise RuntimeError("emit blew up")

        rep = run_preflight(
            config=_cfg(preflight_mode="warn"),
            card_bin_country="US",
            proxy_country="US",
            page=None,
            emit_event=bad_emit,
        )
        # 事件失败不影响主流程，依然返回结果
        self.assertIsNotNone(rep)


class TestPreflightReportImmutable(unittest.TestCase):
    def test_report_is_frozen(self):
        rep = PreflightReport(ok=True, mode="warn")
        with self.assertRaises(Exception):
            rep.ok = False  # type: ignore[misc]


class _FakeResponse:
    """模拟 httpx.Response 的最小接口，避免真实网络。"""

    def __init__(self, *, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """模拟 httpx.Client，捕获 proxy 参数便于断言。"""

    def __init__(self, *, response, captured_proxy_holder):
        self._response = response
        self._captured = captured_proxy_holder

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url):
        self._captured["url"] = url
        return self._response


def _make_factory(*, response, captured: dict):
    def factory(proxy=None, timeout=5.0):
        captured["proxy"] = proxy
        captured["timeout"] = timeout
        return _FakeClient(response=response, captured_proxy_holder=captured)

    return factory


class TestFetchExitIP(unittest.TestCase):
    """fetch_exit_ip：账号池"创建时 IP"抓取的纯单元测试，全程 mock 网络。"""

    def test_success_returns_ip_and_country(self):
        captured: dict = {}
        factory = _make_factory(
            response=_FakeResponse(status_code=200, payload={"ip": "203.0.113.42", "country": "US"}),
            captured=captured,
        )
        ip, country = fetch_exit_ip(proxy=None, _client_factory=factory)
        self.assertEqual(ip, "203.0.113.42")
        self.assertEqual(country, "US")
        # 确认调用了 ipinfo
        self.assertEqual(captured["url"], "https://ipinfo.io/json")

    def test_country_is_uppercased(self):
        """ipinfo 返回的 country 通常已大写，但严格走 upper() 防御。"""
        factory = _make_factory(
            response=_FakeResponse(status_code=200, payload={"ip": "1.1.1.1", "country": "sg"}),
            captured={},
        )
        ip, country = fetch_exit_ip(_client_factory=factory)
        self.assertEqual(country, "SG")
        self.assertEqual(ip, "1.1.1.1")

    def test_non_200_status_degrades_to_empty(self):
        factory = _make_factory(
            response=_FakeResponse(status_code=503, payload={}),
            captured={},
        )
        ip, country = fetch_exit_ip(_client_factory=factory)
        self.assertEqual((ip, country), ("", ""))

    def test_missing_fields_returns_empty_strings(self):
        """payload 完全为空 → 不抛异常，返回 ('', '')。"""
        factory = _make_factory(
            response=_FakeResponse(status_code=200, payload={}),
            captured={},
        )
        ip, country = fetch_exit_ip(_client_factory=factory)
        self.assertEqual((ip, country), ("", ""))

    def test_factory_exception_is_swallowed(self):
        """factory 自身抛异常（网络断/代理坏）必须降级，不能让主流程崩。"""
        def boom_factory(proxy=None, timeout=5.0):
            raise RuntimeError("network down")

        ip, country = fetch_exit_ip(proxy="http://bad:9999", _client_factory=boom_factory)
        self.assertEqual((ip, country), ("", ""))

    def test_proxy_is_passed_to_client(self):
        """代理 URL 必须透传给 httpx Client（决定出口 IP 是否走代理）。"""
        captured: dict = {}
        factory = _make_factory(
            response=_FakeResponse(status_code=200, payload={"ip": "9.9.9.9", "country": "JP"}),
            captured=captured,
        )
        fetch_exit_ip(proxy="http://user:pass@proxy.example:8080", _client_factory=factory)
        self.assertEqual(captured["proxy"], "http://user:pass@proxy.example:8080")


# ---------------------------------------------------------------------------
# fetch_exit_ip_from_page —— 浏览器内抓 IP（修复"获取 IP 一直失败"bug）
# ---------------------------------------------------------------------------


class _FakeTmpPage:
    """模拟 Playwright 临时 tab。"""

    def __init__(self, *, body_text: str = "{}", goto_exc: Exception | None = None):
        self._body = body_text
        self._goto_exc = goto_exc
        self.closed = False
        self.goto_calls: list[tuple] = []

    def goto(self, url, *, timeout=None, wait_until=None):
        self.goto_calls.append((url, timeout, wait_until))
        if self._goto_exc is not None:
            raise self._goto_exc

    def evaluate(self, _script):
        return self._body

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, *, tmp: _FakeTmpPage | None = None, new_page_exc: Exception | None = None):
        self._tmp = tmp
        self._new_page_exc = new_page_exc
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        if self._new_page_exc is not None:
            raise self._new_page_exc
        return self._tmp


class _FakePage:
    def __init__(self, *, context: _FakeContext):
        self.context = context


class TestFetchExitIPFromPage(unittest.TestCase):
    """浏览器内查询 ipinfo.io —— 拿到 AdsPower 注入的住宅代理实际出口 IP。"""

    def test_success_parses_ip_and_country(self):
        tmp = _FakeTmpPage(body_text='{"ip": "203.0.113.42", "country": "US"}')
        page = _FakePage(context=_FakeContext(tmp=tmp))
        ip, country = fetch_exit_ip_from_page(page)
        self.assertEqual(ip, "203.0.113.42")
        self.assertEqual(country, "US")
        # 抓完必须关闭临时 tab，避免泄漏
        self.assertTrue(tmp.closed)
        # 调用了正确的 URL + 超时
        self.assertEqual(len(tmp.goto_calls), 1)
        self.assertEqual(tmp.goto_calls[0][0], "https://ipinfo.io/json")

    def test_country_is_uppercased(self):
        """ipinfo 一般已返回大写，但 _parse_ipinfo_payload 仍走 upper() 防御。"""
        tmp = _FakeTmpPage(body_text='{"ip": "1.1.1.1", "country": "sg"}')
        page = _FakePage(context=_FakeContext(tmp=tmp))
        _, country = fetch_exit_ip_from_page(page)
        self.assertEqual(country, "SG")

    def test_goto_timeout_degrades_to_empty_and_closes_tmp(self):
        """超时（用户报告的"一直失败"现场）→ 不抛异常 + 关闭临时 tab。"""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        tmp = _FakeTmpPage(goto_exc=PlaywrightTimeout("Timeout 8000ms exceeded"))
        page = _FakePage(context=_FakeContext(tmp=tmp))
        ip, country = fetch_exit_ip_from_page(page, timeout_ms=8000)
        self.assertEqual((ip, country), ("", ""))
        self.assertTrue(tmp.closed)

    def test_new_page_exception_returns_empty(self):
        """连开 tab 都失败也不能抛 —— close 不应被调用（没东西要关）。"""
        page = _FakePage(context=_FakeContext(new_page_exc=RuntimeError("browser crashed")))
        ip, country = fetch_exit_ip_from_page(page)
        self.assertEqual((ip, country), ("", ""))

    def test_invalid_json_body_degrades_to_empty(self):
        """body 不是合法 JSON（被 WAF 拦截后返回 HTML）→ 降级为空。"""
        tmp = _FakeTmpPage(body_text="<html>cloudflare block</html>")
        page = _FakePage(context=_FakeContext(tmp=tmp))
        ip, country = fetch_exit_ip_from_page(page)
        self.assertEqual((ip, country), ("", ""))
        self.assertTrue(tmp.closed)


# ---------------------------------------------------------------------------
# write_run_exit_ip —— DB 落库，给 worker / main / orchestrator 三方复用
# ---------------------------------------------------------------------------


class TestWriteRunExitIP(unittest.TestCase):
    """write_run_exit_ip：跨调用方共享的 Run.ip_address 写入器。"""

    def setUp(self):
        # In-memory SQLite，避免污染开发库。
        from sqlmodel import Session, SQLModel, create_engine
        from sqlalchemy.pool import StaticPool
        from src.db import models as db_models  # noqa: F401  -- 触发 SQLModel 注册

        self._engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self._engine)
        self._Session = Session

    def _seed_run(self, run_id: str = "run-xyz"):
        from src.db.models import Run
        with self._Session(self._engine) as s:
            run = Run(id=run_id, email="t@example.com", ip_address=None, ip_country=None)
            s.add(run)
            s.commit()
        return run_id

    def _read_run(self, run_id: str):
        from src.db.models import Run
        with self._Session(self._engine) as s:
            return s.get(Run, run_id)

    def _make_session_ctx(self):
        """构造模拟 get_session() 上下文管理器，返回 in-memory session。"""
        engine = self._engine
        Session = self._Session

        class _Ctx:
            def __enter__(self_inner):
                self_inner._s = Session(engine)
                return self_inner._s

            def __exit__(self_inner, *a):
                self_inner._s.close()
                return False

        return _Ctx

    def test_writes_ip_and_country(self):
        run_id = self._seed_run()
        ctx_cls = self._make_session_ctx()
        with mock.patch("src.db.engine.get_session", lambda: ctx_cls()):
            write_run_exit_ip(run_id, "203.0.113.7", "US")
        run = self._read_run(run_id)
        self.assertEqual(run.ip_address, "203.0.113.7")
        self.assertEqual(run.ip_country, "US")

    def test_truncates_long_ip_and_country(self):
        """ip 超 45 字节 + country 超 8 字节 → 严格截断（DB 列长约束）。"""
        run_id = self._seed_run("run-trunc")
        ctx_cls = self._make_session_ctx()
        long_ip = "x" * 80
        long_country = "abcdefghij"  # 10 chars
        with mock.patch("src.db.engine.get_session", lambda: ctx_cls()):
            write_run_exit_ip(run_id, long_ip, long_country)
        run = self._read_run(run_id)
        self.assertEqual(len(run.ip_address), 45)
        self.assertEqual(len(run.ip_country), 8)
        self.assertEqual(run.ip_country, "ABCDEFGH")  # upper + 截断

    def test_missing_run_id_silently_skips(self):
        """空/None run_id → 直接返回，不报错。"""
        # 不打 patch（不应该访问 DB）
        write_run_exit_ip(None, "1.1.1.1", "US")
        write_run_exit_ip("", "1.1.1.1", "US")
        # 不抛异常即通过

    def test_empty_ip_and_country_silently_skips(self):
        """ip+country 都为空 → 不去 DB 写值（行保持初始空状态）。"""
        run_id = self._seed_run("run-empty")
        write_run_exit_ip(run_id, "", "")
        run = self._read_run(run_id)
        # DB 默认值即"未填"（None 或空串均视为未写入）
        self.assertFalse(run.ip_address)
        self.assertFalse(run.ip_country)

    def test_nonexistent_run_id_silently_skips(self):
        """DB 里没这个 Run → 静默 return，不抛。"""
        ctx_cls = self._make_session_ctx()
        with mock.patch("src.db.engine.get_session", lambda: ctx_cls()):
            write_run_exit_ip("nonexistent-run-id-xyz", "1.1.1.1", "US")
        # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
