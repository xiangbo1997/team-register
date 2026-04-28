# -*- coding: utf-8 -*-
"""服务层集成测试（ConfigService / AuthService / KnowledgeService / EventBroadcaster）"""

import asyncio
import os
import unittest
from pathlib import Path

import src.db.engine as engine_mod
from src.db.engine import init_db
from src.services.auth_service import AuthService
from src.services.config_service import ConfigService
from src.services.event_service import EventBroadcaster
from src.services.knowledge_service import KnowledgeService


def _reset_engine():
    engine_mod._engine = None


class TestConfigService(unittest.TestCase):
    """ConfigService 配置管理"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        from sqlmodel import SQLModel
        from src.db.engine import get_engine
        SQLModel.metadata.drop_all(get_engine())
        SQLModel.metadata.create_all(get_engine())
        self.svc = ConfigService(dotenv_path="/tmp/__nonexistent__.env")

    def test_get_config_defaults(self):
        cfg = self.svc.get_config()
        self.assertEqual(cfg.ads_api, "http://local.adspower.net:50325")

    def test_update_config_override(self):
        self.svc.update_config({"ads_api": "http://new:9999"})
        cfg = self.svc.get_config()
        self.assertEqual(cfg.ads_api, "http://new:9999")

    def test_update_ignores_unknown(self):
        self.svc.update_config({"nonexistent_field_xyz": "val"})
        cfg = self.svc.get_config()
        self.assertFalse(hasattr(cfg, "nonexistent_field_xyz"))

    def test_snapshot_desensitizes(self):
        self.svc.update_config({
            "sms_api_key": "sk-1234567890",
            "default_mail_provider": "mail-default",
        })
        self.svc._overrides["known_mail_accounts_json"] = '{"applemail":[{"email":"known@example.com","client_id":"cid","refresh_token":"rt"}]}'
        snap = self.svc.get_config_snapshot()
        self.assertTrue(snap["sms_api_key"].endswith("****"))
        self.assertNotEqual(snap["sms_api_key"], "sk-1234567890")
        self.assertTrue(snap["known_mail_accounts_json"].endswith("****"))

    def test_reload_base(self):
        self.svc.update_config({"ads_api": "http://changed"})
        self.svc.reload_base(dotenv_path="/tmp/__nonexistent__.env")
        cfg = self.svc.get_config()
        # override 仍然存在
        self.assertEqual(cfg.ads_api, "http://changed")

    def test_config_override_persists_across_service_instances(self):
        self.svc.update_config({
            "default_mail_provider": "mail-default",
            "payment_plan": "plus",
        })

        reloaded = ConfigService(dotenv_path="/tmp/__nonexistent__.env")
        cfg = reloaded.get_config()
        self.assertEqual(cfg.default_mail_provider, "mail-default")
        self.assertEqual(cfg.payment_plan, "plus")

    def test_provider_config_crud(self):
        pc = self.svc.save_provider_config("browser", "test-ads", {"url": "http://x"})
        self.assertEqual(pc.provider_type, "browser")
        self.assertTrue(pc.is_active)

        found = self.svc.get_provider_config("browser", "test-ads")
        self.assertIsNotNone(found)

        all_providers = self.svc.get_provider_configs()
        self.assertEqual(len(all_providers), 1)

        self.svc.save_provider_config("browser", "test-ads", {"url": "http://y"}, is_active=False)
        updated = self.svc.get_provider_config("browser", "test-ads")
        self.assertFalse(updated.is_active)
        self.assertEqual(updated.config["url"], "http://y")

        deleted = self.svc.delete_provider_config("browser", "test-ads")
        self.assertTrue(deleted)
        self.assertFalse(self.svc.delete_provider_config("browser", "test-ads"))

    def test_provider_revisions_and_restore_existing_snapshot(self):
        self.svc.save_provider_config("browser", "revision-demo", {"url": "http://v1"}, is_active=True)
        self.svc.save_provider_config("browser", "revision-demo", {"url": "http://v2"}, is_active=False)
        revisions = self.svc.list_provider_revisions("browser", "revision-demo")
        self.assertGreaterEqual(len(revisions), 2)

        restore_revision = revisions[0]
        restored = self.svc.rollback_provider_config(restore_revision.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.config["url"], "http://v1")
        self.assertTrue(restored.is_active)

    def test_provider_revisions_allow_restore_to_missing_state(self):
        self.svc.save_provider_config("browser", "delete-demo", {"url": "http://x"})
        self.svc.delete_provider_config("browser", "delete-demo")
        revisions = self.svc.list_provider_revisions("browser", "delete-demo")
        delete_state_revision = next(item for item in revisions if item.snapshot.get("exists") is False)
        restored = self.svc.rollback_provider_config(delete_state_revision.id)
        self.assertIsNone(restored)
        self.assertIsNone(self.svc.get_provider_config("browser", "delete-demo"))

    def test_mail_account_crud(self):
        account = self.svc.save_mail_account(
            label="Apple 一号",
            provider_name="applemail",
            email="alpha@example.com",
            client_id="cid-1",
            refresh_token="rt-1",
            extra={"account_id": "acct-1"},
        )
        self.assertTrue(account.id)

        found = self.svc.get_mail_account(account.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.email, "alpha@example.com")

        updated = self.svc.save_mail_account(
            account_id=account.id,
            label="Apple 二号",
            provider_name="applemail",
            email="beta@example.com",
            client_id="",
            refresh_token="",
            extra={"account_id": "acct-2"},
            is_active=False,
        )
        self.assertEqual(updated.label, "Apple 二号")
        self.assertEqual(updated.client_id, "cid-1")
        self.assertFalse(updated.is_active)

        redacted = self.svc.redact_mail_account(updated)
        self.assertTrue(redacted["client_id"].endswith("****"))

        deleted = self.svc.delete_mail_account(account.id)
        self.assertTrue(deleted)
        self.assertIsNone(self.svc.get_mail_account(account.id))


class TestAuthService(unittest.TestCase):
    """AuthService 登录、角色与密码校验。"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        from sqlmodel import SQLModel
        from src.db.engine import get_engine

        SQLModel.metadata.drop_all(get_engine())
        SQLModel.metadata.create_all(get_engine())
        self.svc = AuthService()

    def test_create_and_authenticate_user(self):
        user = self.svc.create_or_update_user("alice", "password123", role="admin")
        self.assertEqual(user.role, "admin")
        authed = self.svc.authenticate("alice", "password123")
        self.assertIsNotNone(authed)
        self.assertEqual(authed.username, "alice")

    def test_authenticate_rejects_wrong_password(self):
        self.svc.create_or_update_user("bob", "password123", role="viewer")
        self.assertIsNone(self.svc.authenticate("bob", "wrong-password"))

    def test_ensure_bootstrap_users(self):
        os.environ["ADMIN_USERNAME"] = "bootstrap-admin"
        os.environ["ADMIN_PASSWORD"] = "bootstrap-pass"
        self.svc.ensure_bootstrap_users()
        self.assertIsNotNone(self.svc.authenticate("bootstrap-admin", "bootstrap-pass"))
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)


class TestKnowledgeService(unittest.TestCase):
    """KnowledgeService 本地知识索引。"""

    def setUp(self):
        self.project_root = Path(__file__).resolve().parent.parent
        self.svc = KnowledgeService(project_root=self.project_root)

    def test_manual_html_and_index_version(self):
        self.assertTrue(self.svc.manual_html)
        self.assertTrue(self.svc.index_version)

    def test_search_returns_manual_and_repo_hits(self):
        result = self.svc.search("provider preview", limit=3)
        self.assertGreater(len(result["manual"]), 0)
        self.assertGreater(len(result["repo"]), 0)


class TestEventBroadcaster(unittest.TestCase):
    """EventBroadcaster 事件写入和广播"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        from sqlmodel import SQLModel
        from src.db.engine import get_engine
        SQLModel.metadata.drop_all(get_engine())
        SQLModel.metadata.create_all(get_engine())

    def test_emit_sync(self):
        bc = EventBroadcaster()
        event = bc.emit_sync("run-1", "state_change", state="AUTH", payload={"step": 1})
        self.assertIsNotNone(event.id)
        self.assertEqual(event.run_id, "run-1")
        self.assertEqual(event.event_type, "state_change")

    def test_emit_async(self):
        bc = EventBroadcaster()

        async def _test():
            event = await bc.emit("run-2", "action", state="HOME", payload={"click": "btn"})
            return event

        event = asyncio.get_event_loop().run_until_complete(_test())
        self.assertIsNotNone(event.id)

    def test_subscribe_receives_events(self):
        bc = EventBroadcaster()

        async def _test():
            received = []
            sub = bc.subscribe("run-3")

            async def _reader():
                async for evt in sub:
                    received.append(evt)
                    if len(received) >= 2:
                        break

            reader_task = asyncio.create_task(_reader())
            await asyncio.sleep(0.05)
            await bc.emit("run-3", "state_change", state="ENTRY")
            await bc.emit("run-3", "state_change", state="AUTH")
            await asyncio.wait_for(reader_task, timeout=2.0)
            return received

        events = asyncio.get_event_loop().run_until_complete(_test())
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["state"], "ENTRY")
        self.assertEqual(events[1]["state"], "AUTH")

    def test_subscribe_receives_sync_events(self):
        bc = EventBroadcaster()

        async def _test():
            received = []
            sub = bc.subscribe("run-4")

            async def _reader():
                async for evt in sub:
                    received.append(evt)
                    if len(received) >= 2:
                        break

            reader_task = asyncio.create_task(_reader())
            await asyncio.sleep(0.05)
            bc.emit_sync("run-4", "state_change", state="ENTRY")
            bc.emit_sync("run-4", "state_change", state="AUTH")
            await asyncio.wait_for(reader_task, timeout=2.0)
            return received

        events = asyncio.get_event_loop().run_until_complete(_test())
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["state"], "ENTRY")
        self.assertEqual(events[1]["state"], "AUTH")


if __name__ == "__main__":
    unittest.main()
