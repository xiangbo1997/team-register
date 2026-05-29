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


if __name__ == "__main__":
    unittest.main()
