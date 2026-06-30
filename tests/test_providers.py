# -*- coding: utf-8 -*-
"""Provider 抽象层集成测试"""

import unittest
from unittest.mock import patch, MagicMock

from src.providers.browser import BrowserConnection, BrowserProvider, AdsPowerProvider
from src.providers.card import CardProvider, EfunCardProvider, NodeCardProvider
from src.providers.mail import (
    HttpMailProvider,
    MailProvider,
    MailRuntimeIncompatibleError,
    MailServiceError,
    MailSession,
)
from src.providers.registry import ProviderRegistry


class TestBrowserConnection(unittest.TestCase):
    """BrowserConnection 冻结数据类"""

    def test_frozen(self):
        conn = BrowserConnection(ws_url="ws://localhost:1234", proxy="http://p:8080")
        self.assertEqual(conn.ws_url, "ws://localhost:1234")
        with self.assertRaises(AttributeError):
            conn.ws_url = "other"


class TestAdsPowerProvider(unittest.TestCase):
    """AdsPower Provider 包装器"""

    def test_init(self):
        p = AdsPowerProvider(api_url="http://test:50325", api_key="key1")
        self.assertIsInstance(p, BrowserProvider)

    @patch("src.browser.get_browser_ws", return_value="ws://cdp:9222")
    def test_connect(self, mock_ws):
        p = AdsPowerProvider(api_url="http://test:50325")
        conn = p.connect("profile-1", proxy="socks5://proxy:1080")
        self.assertEqual(conn.ws_url, "ws://cdp:9222")
        self.assertEqual(conn.proxy, "socks5://proxy:1080")
        mock_ws.assert_called_once()

    @patch("src.browser.run_preflight_checks")
    def test_preflight(self, mock_pf):
        p = AdsPowerProvider(api_url="http://test:50325")
        p.preflight(target_url="https://example.com")
        mock_pf.assert_called_once()

    def test_disconnect_noop(self):
        p = AdsPowerProvider(api_url="http://test:50325")
        p.disconnect("profile-1")


class TestEfunCardProvider(unittest.TestCase):
    """EfunCard Provider"""

    @patch("src.efuncard.EfunCard")
    def test_get_card(self, mock_cls):
        mock_client = mock_cls.return_value
        mock_client.get_card.return_value = MagicMock(
            card_number="4111111111111111",
            expiry="12/28",
            cvc="123",
        )
        p = EfunCardProvider(token="test-token")
        card = p.get_card("CDK-001")
        self.assertIsNotNone(card)
        mock_client.get_card.assert_called_once_with("CDK-001")

    @patch("src.efuncard.EfunCard")
    def test_cancel_card(self, mock_cls):
        mock_client = mock_cls.return_value
        mock_client.cancel.return_value = True
        p = EfunCardProvider(token="test-token")
        self.assertTrue(p.cancel_card("CDK-001"))
        mock_client.cancel.assert_called_once_with("CDK-001")

    @patch("src.efuncard.EfunCard")
    def test_get_billing(self, mock_cls):
        from src.models import BillingInfo
        mock_client = mock_cls.return_value
        mock_billing = MagicMock(spec=BillingInfo)
        mock_client.billing.return_value = mock_billing
        p = EfunCardProvider(token="test-token")
        result = p.get_billing("CDK-001")
        self.assertIs(result, mock_billing)
        mock_client.billing.assert_called_once_with("CDK-001")


class TestNodeCardProvider(unittest.TestCase):
    """NodeCard Provider"""

    @patch("src.nodecard.NodeCard")
    def test_get_card(self, mock_cls):
        mock_client = mock_cls.return_value
        mock_client.get_card.return_value = MagicMock(card_number="5200000000000000")
        p = NodeCardProvider()
        card = p.get_card("CDK-002")
        self.assertIsNotNone(card)

    @patch("src.nodecard.NodeCard")
    def test_cancel_card_not_supported(self, mock_cls):
        p = NodeCardProvider()
        self.assertFalse(p.cancel_card("CDK-002"))

    @patch("src.nodecard.NodeCard")
    def test_get_billing_mapping(self, mock_cls):
        mock_client = mock_cls.return_value
        mock_client.query_transactions.return_value = [
            {
                "order_id": "tx1",
                "amount": -10.0,
                "currency": "USD",
                "merchant_name": "Test Merchant",
                "status": "completed",
                "create_time": "2024-01-01",
            }
        ]
        p = NodeCardProvider()
        billing = p.get_billing("CDK-002")
        self.assertIsNotNone(billing)
        self.assertEqual(len(billing.transactions), 1)
        self.assertEqual(billing.transactions[0].id, "tx1")
        self.assertEqual(billing.total_spent, -10.0)


class TestHttpMailProvider(unittest.TestCase):
    """HttpMailProvider HTTP 客户端"""

    def test_init_url_strip(self):
        p = HttpMailProvider(base_url="https://mail.test/")
        self.assertEqual(p._api_prefix, "https://mail.test/api/mailbox-service")

    @patch("src.providers.mail.requests.get")
    def test_ensure_runtime_ready_accepts_supported_credentialed_mode(self, mock_get):
        health_resp = MagicMock(status_code=200, json=lambda: {"ok": True})
        health_resp.raise_for_status = MagicMock()
        providers_resp = MagicMock(
            status_code=200,
            json=lambda: {
                "providers": [
                    {
                        "name": "applemail",
                        "supported_session_modes": ["managed", "credentialed"],
                    }
                ]
            },
        )
        providers_resp.raise_for_status = MagicMock()
        mock_get.side_effect = [health_resp, providers_resp]

        p = HttpMailProvider(base_url="https://mail.test")
        result = p.ensure_runtime_ready("applemail", session_mode="credentialed")

        self.assertIn("credentialed", result["supported_session_modes"])
        self.assertEqual(mock_get.call_count, 2)

    @patch("src.providers.mail.requests.get")
    def test_ensure_runtime_ready_rejects_missing_credentialed_support(self, mock_get):
        health_resp = MagicMock(status_code=200, json=lambda: {"ok": True})
        health_resp.raise_for_status = MagicMock()
        providers_resp = MagicMock(
            status_code=200,
            json=lambda: {
                "providers": [
                    {
                        "name": "applemail",
                        "supported_session_modes": ["managed"],
                    }
                ]
            },
        )
        providers_resp.raise_for_status = MagicMock()
        mock_get.side_effect = [health_resp, providers_resp]

        p = HttpMailProvider(base_url="https://mail.test")
        with self.assertRaises(MailRuntimeIncompatibleError) as captured:
            p.ensure_runtime_ready("applemail", session_mode="credentialed")

        self.assertIn("不支持 session_mode=credentialed", str(captured.exception))

    @patch("src.providers.mail.requests.post")
    def test_create_session(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "session_id": "sid-1",
                "lease_token": "lt-1",
                "email": "test@mail.com",
                "provider": "luckmail",
                "before_ids": ["msg-0"],
                "session_mode": "managed",
                "state": "leased",
            },
        )
        mock_post.return_value.raise_for_status = MagicMock()

        p = HttpMailProvider(base_url="https://mail.test")
        session = p.create_session("luckmail", purpose="register", session_mode="managed")
        self.assertEqual(session.session_id, "sid-1")
        self.assertEqual(session.email, "test@mail.com")
        self.assertEqual(session.before_ids, ["msg-0"])
        self.assertEqual(session.session_mode, "managed")
        mock_post.assert_called_once()
        self.assertIn("/managed-sessions", mock_post.call_args.args[0])

    @patch.object(HttpMailProvider, "ensure_runtime_ready")
    @patch("src.providers.mail.requests.post")
    def test_create_session_credentialed_uses_existing_account_endpoint(self, mock_post, mock_ensure_runtime_ready):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "session_id": "sid-2",
                "lease_token": "lt-2",
                "email": "known@mail.com",
                "provider": "applemail",
                "session_mode": "credentialed",
            },
        )
        mock_post.return_value.raise_for_status = MagicMock()
        mock_ensure_runtime_ready.return_value = {"supported_session_modes": ["credentialed", "managed"]}

        p = HttpMailProvider(base_url="https://mail.test")
        session = p.create_session(
            "applemail",
            session_mode="credentialed",
            email="known@mail.com",
            existing_account={"email": "known@mail.com", "credentials": {"client_id": "cid", "refresh_token": "rt"}},
        )

        self.assertEqual(session.session_mode, "credentialed")
        self.assertIn("/credentialed-sessions", mock_post.call_args.args[0])

    @patch("src.providers.mail.requests.post")
    def test_create_session_legacy_mode_uses_compat_endpoint(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "session_id": "sid-3",
                "lease_token": "lt-3",
                "email": "legacy@mail.com",
                "provider": "applemail",
            },
        )
        mock_post.return_value.raise_for_status = MagicMock()

        p = HttpMailProvider(base_url="https://mail.test")
        session = p.create_session("applemail", email="legacy@mail.com")

        self.assertEqual(session.session_mode, "managed")
        self.assertTrue(mock_post.call_args.args[0].endswith("/sessions"))

    @patch.object(HttpMailProvider, "ensure_runtime_ready")
    @patch("src.providers.mail.requests.post")
    def test_create_session_credentialed_404_no_longer_falls_back(self, mock_post, mock_ensure_runtime_ready):
        import requests as req

        not_found_resp = MagicMock(status_code=404)
        http_error = req.HTTPError("404 Not Found")
        http_error.response = not_found_resp
        not_found_resp.raise_for_status.side_effect = http_error
        mock_post.return_value = not_found_resp
        mock_ensure_runtime_ready.return_value = {
            "supported_session_modes": ["credentialed", "managed"],
        }

        p = HttpMailProvider(base_url="https://mail.test")
        with self.assertRaises(MailRuntimeIncompatibleError) as captured:
            p.create_session(
                "applemail",
                session_mode="credentialed",
                existing_account={"email": "fallback@mail.com"},
            )

        self.assertEqual(mock_post.call_count, 1)
        self.assertIn("/credentialed-sessions", mock_post.call_args.args[0])
        self.assertIn("缺少 /credentialed-sessions", str(captured.exception))
        self.assertIn("https://mail.test", str(captured.exception))

    @patch("time.sleep", new=lambda *_: None)
    @patch.object(HttpMailProvider, "ensure_runtime_ready")
    @patch("src.providers.mail.requests.post")
    def test_create_session_credentialed_500_surfaces_restart_hint(self, mock_post, mock_ensure_runtime_ready):
        """credentialed 5xx → MailRuntimeIncompatibleError + message 含运行态/重启提示。

        架构改动后 managed/credentialed 路径都走统一的 _raise_runtime_request_error。
        retry 5 次（_RETRY_MAX_ATTEMPTS）才到分类。time.sleep mock 掉避免阻塞。
        """
        import requests as req

        server_error_resp = MagicMock(status_code=500)
        http_error = req.HTTPError("500 Server Error")
        http_error.response = server_error_resp
        server_error_resp.raise_for_status.side_effect = http_error
        server_error_resp.json.side_effect = ValueError("no json")
        mock_post.return_value = server_error_resp
        mock_ensure_runtime_ready.return_value = {
            "supported_session_modes": ["credentialed", "managed"],
        }

        p = HttpMailProvider(base_url="https://mail.test")
        with self.assertRaises(MailRuntimeIncompatibleError) as captured:
            p.create_session(
                "applemail",
                session_mode="credentialed",
                existing_account={"email": "fatal@mail.com"},
            )

        # 统一兜底 message: "...email-provider 返回 500，当前运行态可能异常；请先确认 ... 已重启到最新版本后重试"
        self.assertIn("运行态可能异常", str(captured.exception))
        self.assertIn("https://mail.test", str(captured.exception))

    @patch("time.sleep", new=lambda *_: None)
    @patch("src.providers.mail.requests.post")
    def test_create_session_network_error_raises_runtime_incompat(self, mock_post):
        """网络错误（ConnectionError）→ retry 5 次后抛 MailRuntimeIncompatibleError。

        架构改动：之前 managed 路径会单独把 RequestException 包成 ConnectionError，
        现在统一走 _raise_runtime_request_error，无 status_code 的网络错误兜底成
        MailRuntimeIncompatibleError（"无法确认运行态"），而不是 ConnectionError。
        """
        import requests as req
        mock_post.side_effect = req.ConnectionError("refused")
        p = HttpMailProvider(base_url="https://mail.test")
        with self.assertRaises(MailRuntimeIncompatibleError) as captured:
            p.create_session("luckmail")
        self.assertIn("无法确认", str(captured.exception))
        self.assertIn("refused", str(captured.exception))

    @patch("src.providers.mail.requests.post")
    def test_poll_code_success(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "ready", "code": "123456"},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        p = HttpMailProvider(base_url="https://mail.test")
        session = MailSession(session_id="s1", lease_token="lt", email="a@b.com", provider="x")
        code = p.poll_code(session, keyword="OpenAI", timeout_seconds=60)
        self.assertEqual(code, "123456")

    @patch("src.providers.mail.requests.post")
    def test_poll_code_timeout(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "timeout", "error_code": "POLL_TIMEOUT", "message": "no mail"},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        p = HttpMailProvider(base_url="https://mail.test")
        session = MailSession(session_id="s1", lease_token="lt", email="a@b.com", provider="x")
        self.assertIsNone(p.poll_code(session))

    @patch("src.providers.mail.requests.post")
    def test_poll_code_provider_error_raises(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "failed", "error_code": "PROVIDER_ERROR", "message": "missing applemail config"},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        p = HttpMailProvider(base_url="https://mail.test")
        session = MailSession(session_id="s1", lease_token="lt", email="a@b.com", provider="x")
        with self.assertRaises(MailServiceError):
            p.poll_code(session)

    @patch("src.providers.mail.requests.post")
    def test_complete(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        p = HttpMailProvider(base_url="https://mail.test")
        session = MailSession(session_id="s1", lease_token="lt", email="a@b.com", provider="x")
        p.complete(session, result="success")
        mock_post.assert_called_once()


class TestProviderRegistry(unittest.TestCase):
    """ProviderRegistry 注册和查找"""

    def test_register_and_get(self):
        reg = ProviderRegistry()
        browser = MagicMock(spec=BrowserProvider)
        card = MagicMock(spec=CardProvider)
        mail = MagicMock(spec=MailProvider)

        reg.register_browser("ads", browser)
        reg.register_card("efun", card)
        reg.register_mail("http", mail)

        self.assertIs(reg.get_browser("ads"), browser)
        self.assertIs(reg.get_card("efun"), card)
        self.assertIs(reg.get_mail("http"), mail)

    def test_get_missing(self):
        reg = ProviderRegistry()
        self.assertIsNone(reg.get_browser("none"))

    def test_list(self):
        reg = ProviderRegistry()
        reg.register_browser("a", MagicMock(spec=BrowserProvider))
        reg.register_browser("b", MagicMock(spec=BrowserProvider))
        self.assertEqual(sorted(reg.list_browsers()), ["a", "b"])
        self.assertEqual(reg.list_cards(), [])


# ────────────────────────────────────────────────────
# X988CardProvider 缓存层（解决 X988 verify 一次性消耗问题）
# ────────────────────────────────────────────────────


class TestX988CardProviderCache(unittest.TestCase):
    """X988CardProvider 的两层缓存：L1 内存 + L2 DB。"""

    def setUp(self):
        from src.providers.card import X988CardProvider
        from src.models import CardInfo
        from src.db import crypto
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        import src.db.engine as db_engine

        crypto.reset_for_tests()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self._engine_patch = patch.object(db_engine, "_engine", self.engine)
        self._engine_patch.start()

        self.provider = X988CardProvider()
        self.fake_card = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2030",
            cvv="999",
            name_on_card="X",
            billing_address="",
            bin_country="US",
        )
        self.fake_meta = {"sms_api": "https://sms.example/q", "phone": "+1234567890"}

    def tearDown(self):
        self._engine_patch.stop()
        from src.db import crypto
        crypto.reset_for_tests()

    def _patch_client_get_card(self):
        """patch X988Card.get_card 模拟 verify 成功并写入 _last_meta。"""
        def side(card_key):
            self.provider._client._last_meta = dict(self.fake_meta)
            return self.fake_card
        return patch.object(self.provider._client, "get_card", side_effect=side)

    def test_l1_memory_cache_skips_underlying_client(self):
        """同一 provider 实例第二次 get_card 命中内存，不打底层 client。"""
        with self._patch_client_get_card() as m:
            r1 = self.provider.get_card("CDK_L1")
            r2 = self.provider.get_card("CDK_L1")
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(m.call_count, 1, "第二次应命中 L1 内存，不应再调底层")

    def test_l2_db_cache_across_provider_instances(self):
        """新 provider 实例第二次 get_card 命中 DB（不调底层 client）。"""
        with self._patch_client_get_card() as m1:
            self.provider.get_card("CDK_L2")
        self.assertEqual(m1.call_count, 1)

        from src.providers.card import X988CardProvider
        provider2 = X988CardProvider()
        with patch.object(provider2._client, "get_card") as m2:
            r = provider2.get_card("CDK_L2")
        self.assertIsNotNone(r)
        self.assertEqual(m2.call_count, 0, "L2 DB 命中，不应再调底层 verify")

    def test_db_hit_backfills_last_meta_for_3ds(self):
        """L2 DB 命中后必须把 sms_api 回填 _last_meta，否则 wait_for_3ds 拿不到。"""
        with self._patch_client_get_card():
            self.provider.get_card("CDK_META")

        from src.providers.card import X988CardProvider
        provider2 = X988CardProvider()
        with patch.object(provider2._client, "get_card"):
            provider2.get_card("CDK_META")

        self.assertEqual(provider2._client._last_meta.get("sms_api"), self.fake_meta["sms_api"])

    def test_wait_for_3ds_recovers_sms_api_from_db(self):
        """如果 _last_meta 被清空，wait_for_3ds 应从 DB 自动补 sms_api。"""
        with self._patch_client_get_card():
            self.provider.get_card("CDK_3DS")

        # 故意清空 _last_meta
        self.provider._client._last_meta = {}

        with patch.object(self.provider._client, "wait_for_3ds", return_value="123456") as m:
            code = self.provider.wait_for_3ds("CDK_3DS", timeout_sec=10)

        self.assertEqual(code, "123456")
        self.assertEqual(
            self.provider._client._last_meta.get("sms_api"),
            self.fake_meta["sms_api"],
            "wait_for_3ds 应从 DB 拿 sms_api 回填到 _last_meta",
        )
        m.assert_called_once_with("CDK_3DS", timeout_sec=10)


class TestSmsActivateProvider(unittest.TestCase):
    """SMS-Activate Provider 适配器测试。

    设计：mock 掉 requests.get 与 SMSManager，不发任何真实 HTTP；
    覆盖注册、校验、互斥约束、country_fallback 解析、价格降级、test_connection。
    """

    def _build_provider(self, **overrides):
        from src.providers.sms.sms_activate import SmsActivateProvider

        defaults = {
            "api_key": "test-key",
            "country": "6",
            "country_fallback": "",
            "max_price": "",
            "min_price": "",
            "operator": "any",
            "service": "dr",
            "max_retries": 30,
            "proxy": "",
        }
        defaults.update(overrides)
        with patch("src.providers.sms.sms_activate.SMSManager") as mock_mgr_cls:
            mock_mgr_cls.side_effect = lambda **kwargs: MagicMock(_kwargs=kwargs)
            provider = SmsActivateProvider(**defaults)
        return provider

    def test_registered(self):
        """sms_activate kind 应被 ProviderRegistry 发现并注册。"""
        from src.providers import get_registry

        reg = get_registry()
        reg.discover()
        self.assertIn(
            "sms_activate",
            reg.list_kinds("sms"),
            f"已注册 SMS kinds: {reg.list_kinds('sms')}",
        )
        meta = reg.get_meta("sms", "sms_activate")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.provider_type, "sms")
        self.assertEqual(meta.display_name, "SMS-Activate 接码平台")
        field_names = {f.name for f in meta.schema}
        for required in (
            "api_key", "country", "country_fallback",
            "max_price", "min_price", "operator",
            "service", "max_retries", "proxy",
        ):
            self.assertIn(required, field_names, f"schema 缺字段 {required}")

        # 代码型字段应声明 choices，供前端渲染下拉框
        by_name = {f.name: f for f in meta.schema}
        self.assertEqual(by_name["country"].choices, ("4", "6", "8", "10", "15", "16", "32", "33", "43", "52", "73", "78", "151", "182", "187"))
        self.assertEqual(by_name["service"].choices, ("dr", "go", "tg"))
        # to_dict（API 实际返回给前端的结构）也应透出 choices
        d = meta.to_dict()
        country_d = next(f for f in d["schema"] if f["name"] == "country")
        self.assertEqual(country_d["choices"], ["4", "6", "8", "10", "15", "16", "32", "33", "43", "52", "73", "78", "151", "182", "187"])

    def test_validate_required_api_key(self):
        """不传 api_key 应被 registry.build 拦下。"""
        from src.providers.registry import ProviderConfigInvalidError, get_registry

        reg = get_registry()
        reg.discover()
        with self.assertRaises(ProviderConfigInvalidError) as ctx:
            reg.build("sms", "sms_activate", {"country": "6"})
        self.assertIn("api_key", ctx.exception.missing_fields)

    def test_operator_max_price_mutex(self):
        """operator != 'any' 时同时传 max_price 应在 __init__ 抛 ValueError。"""
        from src.providers.sms.sms_activate import SmsActivateProvider

        with patch("src.providers.sms.sms_activate.SMSManager"):
            with self.assertRaises(ValueError) as ctx:
                SmsActivateProvider(
                    api_key="test-key",
                    country="6",
                    max_price="30",
                    operator="mts",
                )
        self.assertIn("互斥", str(ctx.exception))

    def test_country_fallback_parsing(self):
        """country_fallback 应支持 ';' 和 ',' 双分隔符且去重去主国家。"""
        provider = self._build_provider(country="6", country_fallback="22;12,6, 0")
        self.assertEqual(provider.country_chain, ["6", "22", "12", "0"])

    def test_country_fallback_empty(self):
        """无 fallback 时国家链只含主国家。"""
        provider = self._build_provider(country="6", country_fallback="")
        self.assertEqual(provider.country_chain, ["6"])

    def test_price_filter_downgrade_then_succeed(self):
        """主国家价格超 max_price → 降级到 fallback；fallback 价格符合 → 申号成功。"""
        from src.models import SMSOrder
        from src.providers.sms.sms_activate import SmsActivateProvider

        price_responses = {
            "6": MagicMock(json=lambda: {"6": {"dr": {"cost": 50.0, "count": 100}}}),
            "22": MagicMock(json=lambda: {"22": {"dr": {"cost": 20.0, "count": 50}}}),
        }

        def fake_requests_get(url, params=None, **kwargs):
            country = str(params.get("country", ""))
            action = params.get("action")
            if action == "getPrices":
                return price_responses[country]
            raise AssertionError(f"未预期的 action={action}")

        order_22 = SMSOrder(order_id="ORDER_22", phone_number="62812345")

        managers_created: dict = {}

        def make_mgr(**kwargs):
            mock = MagicMock()
            if kwargs["country"] == "22":
                mock.get_number = MagicMock(return_value=order_22)
            else:
                mock.get_number = MagicMock(return_value=None)
            managers_created[kwargs["country"]] = mock
            return mock

        with patch(
            "src.providers.sms.sms_activate.SMSManager", side_effect=make_mgr
        ), patch(
            "src.providers.sms.sms_activate.requests.get", side_effect=fake_requests_get
        ):
            provider = SmsActivateProvider(
                api_key="test-key",
                country="6",
                country_fallback="22",
                max_price="30",
            )
            result = provider.get_number(service="dr")

        self.assertIsNotNone(result)
        self.assertEqual(result.order_id, "ORDER_22")
        # 主国家被价格过滤跳过，不应调用其 get_number
        managers_created["6"].get_number.assert_not_called()
        managers_created["22"].get_number.assert_called_once_with(service="dr")

    def test_test_connection_success(self):
        """getBalance 返回 ACCESS_BALANCE:42.50 → ok=True, balance=42.5。"""
        with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
            mock_get.return_value = MagicMock(text="ACCESS_BALANCE:42.50")
            provider = self._build_provider()
            result = provider.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["balance"], 42.5)
        self.assertIn("42.50", result["message"])

    def test_test_connection_bad_key(self):
        """getBalance 返回 BAD_KEY → ok=False, balance=None。"""
        with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
            mock_get.return_value = MagicMock(text="BAD_KEY")
            provider = self._build_provider()
            result = provider.test_connection()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["balance"])
        self.assertIn("BAD_KEY", result["message"])

    def test_test_connection_network_error(self):
        """请求异常时应返回 ok=False 而不是抛异常（端点契约）。"""
        import requests as _requests

        with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
            mock_get.side_effect = _requests.ConnectionError("dns fail")
            provider = self._build_provider()
            result = provider.test_connection()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["balance"])
        self.assertIn("请求异常", result["message"])

    # ── acquire_priority 取号优先级（借鉴 GuJumpgate）──────────────

    def _build_priority_provider(self, priority, prices, order_per_country):
        """构造 provider 并 mock 查价 + 各国 get_number。

        Args:
            priority: acquire_priority 值
            prices: {country: cost} 查价响应
            order_per_country: {country: SMSOrder|None} 各国申号结果
        Returns:
            (provider, managers_dict)
        """
        from src.providers.sms.sms_activate import SmsActivateProvider

        managers: dict = {}

        def make_mgr(**kwargs):
            c = kwargs["country"]
            mock = MagicMock()
            mock.get_number = MagicMock(return_value=order_per_country.get(c))
            managers[c] = mock
            return mock

        def fake_get(url, params=None, **kwargs):
            c = str(params.get("country", ""))
            if params.get("action") == "getPrices":
                return MagicMock(json=lambda: {c: {"dr": {"cost": prices[c], "count": 9}}})
            raise AssertionError(f"未预期 action={params.get('action')}")

        with patch("src.providers.sms.sms_activate.SMSManager", side_effect=make_mgr), \
             patch("src.providers.sms.sms_activate.requests.get", side_effect=fake_get):
            provider = SmsActivateProvider(
                api_key="test-key",
                country="6",
                country_fallback="33;187",
                acquire_priority=priority,
            )
            order = provider.get_number(service="dr")
        return order, managers

    def test_acquire_priority_default_is_country_order(self):
        """默认 acquire_priority=country：按链顺序，首国有货即返回，不查价。"""
        from src.models import SMSOrder

        order_6 = SMSOrder(order_id="O6", phone_number="600")
        # country 模式无价格限制时不查价：fake_get 一旦被调用会 AssertionError
        from src.providers.sms.sms_activate import SmsActivateProvider
        managers: dict = {}

        def make_mgr(**kwargs):
            c = kwargs["country"]
            mock = MagicMock()
            mock.get_number = MagicMock(return_value=order_6 if c == "6" else None)
            managers[c] = mock
            return mock

        with patch("src.providers.sms.sms_activate.SMSManager", side_effect=make_mgr), \
             patch("src.providers.sms.sms_activate.requests.get",
                   side_effect=AssertionError("country 模式不应查价")):
            provider = SmsActivateProvider(
                api_key="k", country="6", country_fallback="33;187",
            )
            order = provider.get_number(service="dr")
        self.assertEqual(order.order_id, "O6")

    def test_acquire_priority_price_low_picks_cheapest_first(self):
        """price_low：先查全链价，最低价国家先申号。"""
        from src.models import SMSOrder

        # 价格：6=50, 33=10(最低), 187=30 → 应先申 33
        order, managers = self._build_priority_provider(
            "price_low",
            prices={"6": 50.0, "33": 10.0, "187": 30.0},
            order_per_country={"33": SMSOrder(order_id="O33", phone_number="3300")},
        )
        self.assertEqual(order.order_id, "O33")
        managers["33"].get_number.assert_called_once()
        # 更贵的 6 不应在 33 之前被申号
        managers["6"].get_number.assert_not_called()

    def test_acquire_priority_price_high_picks_most_expensive_first(self):
        """price_high：最高价国家先申号（高价号成功率高）。"""
        from src.models import SMSOrder

        order, managers = self._build_priority_provider(
            "price_high",
            prices={"6": 50.0, "33": 10.0, "187": 30.0},
            order_per_country={"6": SMSOrder(order_id="O6", phone_number="600")},
        )
        self.assertEqual(order.order_id, "O6")
        managers["6"].get_number.assert_called_once()
        managers["33"].get_number.assert_not_called()

    def test_invalid_acquire_priority_falls_back_to_country(self):
        """非法 acquire_priority 回退 country（不查价）。"""
        provider = self._build_provider(acquire_priority="garbage")
        self.assertEqual(provider._acquire_priority, "country")

    # ── 号码复用（setStatus=3）─────────────────────────────────

    def test_request_additional_sms_delegates_to_manager(self):
        """request_additional_sms 应转调首个 manager 的 request_retry。"""
        provider = self._build_provider(country="6", country_fallback="33")
        first_mgr = next(iter(provider._managers.values()))
        first_mgr.request_retry = MagicMock(return_value=True)
        ok = provider.request_additional_sms("ORDER_X")
        self.assertTrue(ok)
        first_mgr.request_retry.assert_called_once_with("ORDER_X")


class TestHeroSmsProvider(unittest.TestCase):
    """HeroSMS Provider 适配器测试。

    HeroSMS 子类化 SmsActivateProvider，仅覆写 BASE_URL/CURRENCY_LABEL。
    test_connection/_query_current_price 方法体仍在 sms_activate 模块，
    所以 patch target 一律用 ``src.providers.sms.sms_activate.*``。
    """

    HERO_URL = "https://hero-sms.com/stubs/handler_api.php"

    def _build_provider(self, **overrides):
        from src.providers.sms.hero_sms import HeroSmsProvider

        defaults = {
            "api_key": "test-key",
            "country": "6",
            "country_fallback": "",
            "max_price": "",
            "min_price": "",
            "operator": "any",
            "service": "dr",
            "max_retries": 30,
            "proxy": "",
        }
        defaults.update(overrides)
        with patch("src.providers.sms.sms_activate.SMSManager") as mock_mgr_cls:
            mock_mgr_cls.side_effect = lambda **kwargs: MagicMock(_kwargs=kwargs)
            provider = HeroSmsProvider(**defaults)
        return provider

    def test_registered(self):
        """hero_sms kind 应被注册，且不影响父类 sms_activate。"""
        from src.providers import get_registry

        reg = get_registry()
        reg.discover()
        kinds = reg.list_kinds("sms")
        self.assertIn("hero_sms", kinds, f"已注册 SMS kinds: {kinds}")
        # 子类装饰器不能覆盖父类注册
        self.assertIn("sms_activate", kinds, f"父类丢失: {kinds}")

        meta = reg.get_meta("sms", "hero_sms")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.provider_type, "sms")
        self.assertEqual(meta.display_name, "HeroSMS 接码平台")
        field_names = {f.name for f in meta.schema}
        for required in (
            "api_key", "country", "country_fallback",
            "max_price", "min_price", "operator",
            "service", "max_retries", "proxy",
        ):
            self.assertIn(required, field_names, f"schema 缺字段 {required}")

        by_name = {f.name: f for f in meta.schema}
        self.assertEqual(by_name["country"].choices, ("4", "6", "8", "10", "15", "16", "32", "33", "43", "52", "73", "78", "151", "182", "187"))
        self.assertEqual(by_name["service"].choices, ("dr", "go", "tg"))
        d = meta.to_dict()
        country_d = next(f for f in d["schema"] if f["name"] == "country")
        self.assertEqual(country_d["choices"], ["4", "6", "8", "10", "15", "16", "32", "33", "43", "52", "73", "78", "151", "182", "187"])

    def test_base_url_in_sms_manager(self):
        """每个国家的 SMSManager 应以 hero-sms 端点构造（与 sms_activate 的核心区别）。"""
        from src.providers.sms.hero_sms import HeroSmsProvider

        with patch("src.providers.sms.sms_activate.SMSManager") as mock_mgr_cls:
            mock_mgr_cls.side_effect = lambda **kwargs: MagicMock(_kwargs=kwargs)
            HeroSmsProvider(api_key="k", country="6")
        # 至少一次构造，且 api_url 指向 hero
        self.assertTrue(mock_mgr_cls.called)
        for call in mock_mgr_cls.call_args_list:
            self.assertEqual(call.kwargs.get("api_url"), self.HERO_URL)

    def test_base_url_in_test_connection(self):
        """test_connection 应把 getBalance 请求打到 hero-sms 端点。"""
        with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
            mock_get.return_value = MagicMock(text="ACCESS_BALANCE:10.0")
            provider = self._build_provider()
            provider.test_connection()
        self.assertEqual(mock_get.call_args.args[0], self.HERO_URL)

    def test_test_connection_success_usd(self):
        """getBalance 成功 → 余额文案用 USD（CURRENCY_LABEL 覆写生效）。"""
        with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
            mock_get.return_value = MagicMock(text="ACCESS_BALANCE:42.50")
            provider = self._build_provider()
            result = provider.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["balance"], 42.5)
        self.assertIn("42.50", result["message"])
        self.assertIn("USD", result["message"])

    def test_test_connection_bad_key(self):
        """getBalance 返回 BAD_KEY → ok=False。"""
        with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
            mock_get.return_value = MagicMock(text="BAD_KEY")
            provider = self._build_provider()
            result = provider.test_connection()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["balance"])
        self.assertIn("BAD_KEY", result["message"])


class TestSmsActivateCompatProviders(unittest.TestCase):
    """SMS-Activate 协议兼容族新增 provider 测试（grizzly / smsbower / sms-verification-number）。

    这三家与 HeroSMS 同款：子类化 SmsActivateProvider，仅覆写 BASE_URL / CURRENCY_LABEL，
    schema 复用 build_sms_activate_schema 工厂。test_connection / _query_current_price 方法体
    仍在 sms_activate 模块，patch target 一律用 ``src.providers.sms.sms_activate.*``。

    参数表：(kind, 模块路径, 类名, 预期 BASE_URL, 预期 CURRENCY_LABEL, 预期 display_name)
    """

    CASES = (
        (
            "grizzly_sms",
            "src.providers.sms.grizzly_sms",
            "GrizzlySmsProvider",
            "https://api.grizzlysms.com/stubs/handler_api.php",
            "RUB",
            "GrizzlySMS 接码平台",
        ),
        (
            "sms_bower",
            "src.providers.sms.sms_bower",
            "SmsBowerProvider",
            "https://smsbower.page/stubs/handler_api.php",
            "RUB",
            "SMSBower 接码平台",
        ),
        (
            "sms_verification_number",
            "src.providers.sms.sms_verification_number",
            "SmsVerificationNumberProvider",
            "https://sms-verification-number.com/stubs/handler_api",
            "USD",
            "SMS-Verification-Number 接码平台",
        ),
    )

    def _load_class(self, module_path: str, class_name: str):
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, class_name)

    def _build_provider(self, cls, **overrides):
        defaults = {
            "api_key": "test-key",
            "country": "6",
            "country_fallback": "",
            "max_price": "",
            "min_price": "",
            "operator": "any",
            "service": "dr",
            "max_retries": 30,
            "proxy": "",
        }
        defaults.update(overrides)
        with patch("src.providers.sms.sms_activate.SMSManager") as mock_mgr_cls:
            mock_mgr_cls.side_effect = lambda **kwargs: MagicMock(_kwargs=kwargs)
            provider = cls(**defaults)
        return provider

    def test_registered(self):
        """三家 kind 均应注册，且不影响父类 sms_activate 注册。"""
        from src.providers import get_registry

        reg = get_registry()
        reg.discover()
        kinds = reg.list_kinds("sms")
        self.assertIn("sms_activate", kinds, f"父类丢失: {kinds}")
        for kind, _module, _cls, _url, _cur, display_name in self.CASES:
            with self.subTest(kind=kind):
                self.assertIn(kind, kinds, f"已注册 SMS kinds: {kinds}")
                meta = reg.get_meta("sms", kind)
                self.assertIsNotNone(meta)
                self.assertEqual(meta.provider_type, "sms")
                self.assertEqual(meta.display_name, display_name)

    def test_schema_from_factory(self):
        """schema 应来自 build_sms_activate_schema 工厂，9 字段齐全且 choices 一致。"""
        from src.providers import get_registry

        reg = get_registry()
        reg.discover()
        for kind, _module, _cls, _url, _cur, _display in self.CASES:
            with self.subTest(kind=kind):
                meta = reg.get_meta("sms", kind)
                by_name = {f.name: f for f in meta.schema}
                for required in (
                    "api_key", "country", "country_fallback",
                    "max_price", "min_price", "operator",
                    "service", "max_retries", "proxy",
                ):
                    self.assertIn(required, by_name, f"{kind} schema 缺字段 {required}")
                # country / service 的 choices 与父类工厂一致
                self.assertEqual(by_name["country"].choices, ("4", "6", "8", "10", "15", "16", "32", "33", "43", "52", "73", "78", "151", "182", "187"))
                self.assertEqual(by_name["service"].choices, ("dr", "go", "tg"))

    def test_base_url_in_sms_manager(self):
        """每个国家的 SMSManager 应以各自兼容端点构造（与 sms_activate 的核心区别）。"""
        for kind, module_path, class_name, expected_url, _cur, _display in self.CASES:
            with self.subTest(kind=kind):
                cls = self._load_class(module_path, class_name)
                self.assertEqual(cls.BASE_URL, expected_url)
                with patch("src.providers.sms.sms_activate.SMSManager") as mock_mgr_cls:
                    mock_mgr_cls.side_effect = lambda **kwargs: MagicMock(_kwargs=kwargs)
                    cls(api_key="k", country="6")
                self.assertTrue(mock_mgr_cls.called)
                for call in mock_mgr_cls.call_args_list:
                    self.assertEqual(call.kwargs.get("api_url"), expected_url)

    def test_test_connection_currency_and_endpoint(self):
        """test_connection 应打到各自端点，余额文案用各自 CURRENCY_LABEL。"""
        for kind, module_path, class_name, expected_url, expected_cur, _display in self.CASES:
            with self.subTest(kind=kind):
                cls = self._load_class(module_path, class_name)
                with patch("src.providers.sms.sms_activate.requests.get") as mock_get:
                    mock_get.return_value = MagicMock(text="ACCESS_BALANCE:42.50")
                    provider = self._build_provider(cls)
                    result = provider.test_connection()
                self.assertEqual(mock_get.call_args.args[0], expected_url)
                self.assertTrue(result["ok"])
                self.assertEqual(result["balance"], 42.5)
                self.assertIn(expected_cur, result["message"])


class TestFiveSimProvider(unittest.TestCase):
    """5sim Provider 测试（原生 JSON API，不走 SMSManager）。

    5sim 直接继承 SmsProvider ABC、自带 requests + JSON 实现，
    patch target 用 ``src.providers.sms.five_sim.requests``。
    """

    def _build(self, **overrides):
        from src.providers.sms.five_sim import FiveSimProvider

        defaults = {
            "api_key": "jwt-token",
            "country": "vietnam",
            "country_fallback": "",
            "product": "openai",
            "operator": "any",
            "max_price": "",
            "max_retries": 30,
            "proxy": "",
        }
        defaults.update(overrides)
        return FiveSimProvider(**defaults)

    def test_registered(self):
        """five_sim kind 应注册，且不是 SmsActivateProvider 子类（异构判据）。"""
        from src.providers import get_registry
        from src.providers.sms.five_sim import FiveSimProvider
        from src.providers.sms.sms_activate import SmsActivateProvider

        reg = get_registry()
        reg.discover()
        self.assertIn("five_sim", reg.list_kinds("sms"))
        meta = reg.get_meta("sms", "five_sim")
        self.assertEqual(meta.display_name, "5sim 接码平台")
        # 关键：five_sim 不继承 SmsActivateProvider → 走异构路径
        self.assertFalse(issubclass(FiveSimProvider, SmsActivateProvider))

    def test_validate_required_api_key(self):
        from src.providers.sms.five_sim import FiveSimProvider

        with self.assertRaises(ValueError):
            FiveSimProvider(api_key="")

    def test_country_fallback_parsing(self):
        provider = self._build(country="vietnam", country_fallback="indonesia;england, usa")
        self.assertEqual(provider.country_chain, ["vietnam", "indonesia", "england", "usa"])

    def test_get_number_success(self):
        """申号成功：解析 {id, phone} → SMSOrder。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req:
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(
                status_code=200, text="{}",
                json=lambda: {"id": 12345, "phone": "+84123456789"},
            )
            provider = self._build()
            order = provider.get_number()
        self.assertIsNotNone(order)
        self.assertEqual(order.order_id, "12345")
        self.assertEqual(order.phone_number, "+84123456789")

    def test_get_number_no_stock_then_fallback(self):
        """主国家无号（200 但无 phone）→ 降级到 fallback 国家成功。"""
        responses = [
            MagicMock(status_code=200, text="{}", json=lambda: {"id": "", "phone": ""}),          # vietnam 无号
            MagicMock(status_code=200, text="{}", json=lambda: {"id": "999", "phone": "+44999"}),  # england 有号
        ]
        with patch("src.providers.sms.five_sim.requests") as mock_req:
            mock_req.RequestException = Exception
            mock_req.get.side_effect = responses
            provider = self._build(country="vietnam", country_fallback="england")
            order = provider.get_number()
        self.assertIsNotNone(order)
        self.assertEqual(order.phone_number, "+44999")

    def test_get_number_price_filter_downgrade(self):
        """max_price 生效：主国家超价跳过，备选国家符合则申号。"""
        # 调用序列：vietnam getPrices(贵) → england getPrices(便宜) → england buy
        responses = [
            MagicMock(status_code=200, json=lambda: {"vietnam": {"openai": {"any": {"cost": 50.0, "count": 5}}}}),
            MagicMock(status_code=200, json=lambda: {"england": {"openai": {"any": {"cost": 10.0, "count": 5}}}}),
            MagicMock(status_code=200, text="{}", json=lambda: {"id": "777", "phone": "+44777"}),
        ]
        with patch("src.providers.sms.five_sim.requests") as mock_req:
            mock_req.RequestException = Exception
            mock_req.get.side_effect = responses
            provider = self._build(country="vietnam", country_fallback="england", max_price="20")
            order = provider.get_number()
        self.assertIsNotNone(order)
        self.assertEqual(order.phone_number, "+44777")

    def test_get_number_transient_empty_body_then_retry_success(self):
        """瞬时空 body（200 但非 JSON）→ 退避重试 → 第二次成功。

        复现线上故障：5sim 批量并发期间偶发返回空 body，旧逻辑直接放弃，
        新逻辑识别为瞬时抖动并重试。
        """
        def _empty_json():
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        responses = [
            MagicMock(status_code=200, text="", json=_empty_json),                          # 瞬时空 body
            MagicMock(status_code=200, text="{}", json=lambda: {"id": "555", "phone": "+84555"}),  # 重试成功
        ]
        with patch("src.providers.sms.five_sim.requests") as mock_req, \
             patch("src.providers.sms.five_sim.time.sleep") as mock_sleep:
            mock_req.RequestException = Exception
            mock_req.get.side_effect = responses
            provider = self._build()
            order = provider.get_number()
        self.assertIsNotNone(order)
        self.assertEqual(order.phone_number, "+84555")
        mock_sleep.assert_called_once()  # 退避了一次

    def test_get_number_server_5xx_retries_then_gives_up(self):
        """持续 5xx → 重试到上限后返回 None（4 次尝试）。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req, \
             patch("src.providers.sms.five_sim.time.sleep"):
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(status_code=503, text="service unavailable")
            provider = self._build()  # 单国家 vietnam
            order = provider.get_number()
        self.assertIsNone(order)
        self.assertEqual(mock_req.get.call_count, 4)  # 首次 + 3 次重试

    def test_get_number_fatal_4xx_no_retry(self):
        """业务层 4xx（401 鉴权失败）→ fail-fast，不重试。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req, \
             patch("src.providers.sms.five_sim.time.sleep") as mock_sleep:
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(status_code=401, text="unauthorized")
            provider = self._build()
            order = provider.get_number()
        self.assertIsNone(order)
        self.assertEqual(mock_req.get.call_count, 1)  # 不重试
        mock_sleep.assert_not_called()

    def test_get_number_network_exception_retries(self):
        """网络层异常（连接中断）→ 退避重试。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req, \
             patch("src.providers.sms.five_sim.time.sleep"):
            mock_req.RequestException = Exception
            mock_req.get.side_effect = [
                Exception("Connection reset"),
                MagicMock(status_code=200, text="{}", json=lambda: {"id": "111", "phone": "+84111"}),
            ]
            provider = self._build()
            order = provider.get_number()
        self.assertIsNotNone(order)
        self.assertEqual(order.phone_number, "+84111")

    def test_get_code_from_sms_array(self):
        """取码：从 sms[] 数组最新一条抠出 4-8 位验证码。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req:
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "RECEIVED", "sms": [{"code": "123456", "text": "your code 123456"}]}
            )
            provider = self._build()
            code = provider.get_code("12345", max_retries=1)
        self.assertEqual(code, "123456")

    def test_get_code_terminal_status_stops(self):
        """订单进入终态（BANNED）→ 立即返回 None，不空等。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req, \
             patch("src.providers.sms.five_sim.time.sleep"):
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "BANNED", "sms": []}
            )
            provider = self._build()
            code = provider.get_code("12345", max_retries=5)
        self.assertIsNone(code)
        # 终态命中即停：只调用一次，没有重试到 5 次
        self.assertEqual(mock_req.get.call_count, 1)

    def test_test_connection_success(self):
        """profile 返回 balance → ok=True，余额文案 RUB。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req:
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(
                status_code=200, json=lambda: {"balance": 88.5}
            )
            provider = self._build()
            result = provider.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["balance"], 88.5)
        self.assertIn("88.50", result["message"])
        self.assertIn("RUB", result["message"])

    def test_test_connection_unauthorized(self):
        """401 → ok=False（token 无效）。"""
        with patch("src.providers.sms.five_sim.requests") as mock_req:
            mock_req.RequestException = Exception
            mock_req.get.return_value = MagicMock(status_code=401, text="unauthorized")
            provider = self._build()
            result = provider.test_connection()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["balance"])

    def test_duck_type_signature_matches_sms_manager(self):
        """5sim 的 get_number/get_code 签名应与 SMSManager 鸭子兼容（顶替前提）。"""
        import inspect
        from src.providers.sms.five_sim import FiveSimProvider
        from src.sms import SMSManager

        for method in ("get_number", "get_code"):
            five_params = list(inspect.signature(getattr(FiveSimProvider, method)).parameters)
            mgr_params = list(inspect.signature(getattr(SMSManager, method)).parameters)
            self.assertEqual(
                five_params, mgr_params,
                f"{method} 签名不匹配：five_sim={five_params} vs SMSManager={mgr_params}",
            )


if __name__ == "__main__":
    unittest.main()
