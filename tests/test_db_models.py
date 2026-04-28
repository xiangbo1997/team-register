# -*- coding: utf-8 -*-
"""数据库模型 + 引擎集成测试"""

import os
import unittest
from datetime import datetime, timezone

from sqlmodel import Session, select

import src.db.engine as engine_mod
from src.db.engine import get_engine, init_db, get_session
from src.db.models import (
    AppSetting,
    AssistantActionLog,
    Checkpoint,
    MailAccount,
    ProviderConfig,
    ProviderConfigRevision,
    Run,
    RunEvent,
    User,
)


def _reset_engine():
    """重置引擎单例以使用测试数据库"""
    engine_mod._engine = None


class TestDBModels(unittest.TestCase):
    """数据库模型 CRUD 测试"""

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        cls.engine = init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        from sqlmodel import SQLModel
        SQLModel.metadata.drop_all(self.engine)
        SQLModel.metadata.create_all(self.engine)

    def test_create_run(self):
        with get_session() as session:
            run = Run(
                id="aabbccdd11223344",
                email="test@example.com",
                password="pass123",
                profile_id="prof-1",
                browser_provider="browser-default",
                card_provider="card-default",
                mail_provider="mail-default",
                mail_account_id="mail-acct-1",
                config_snapshot={"ads_api": "http://test"},
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            self.assertEqual(run.status, "pending")
            self.assertEqual(run.retry_mode, "restart")
            self.assertEqual(run.email, "test@example.com")
            self.assertEqual(run.mail_account_id, "mail-acct-1")
            self.assertIsNotNone(run.created_at)

    def test_run_update_status(self):
        with get_session() as session:
            run = Run(id="run-update-01", email="u@t.com", password="p",
                      profile_id="p1", config_snapshot={})
            session.add(run)
            session.commit()

            run.status = "running"
            run.phase = "registration"
            session.add(run)
            session.commit()
            session.refresh(run)
            self.assertEqual(run.status, "running")

    def test_create_run_event(self):
        with get_session() as session:
            run = Run(id="run-evt-01", email="e@t.com", password="p",
                      profile_id="p1", config_snapshot={})
            session.add(run)
            session.commit()

            event = RunEvent(
                run_id="run-evt-01",
                event_type="state_change",
                state="AUTH",
                payload={"url": "https://auth.openai.com"},
                timestamp=datetime.now(timezone.utc),
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            self.assertIsNotNone(event.id)
            self.assertEqual(event.event_type, "state_change")

    def test_create_checkpoint(self):
        with get_session() as session:
            run = Run(id="run-ckpt-01", email="c@t.com", password="p",
                      profile_id="p1", config_snapshot={})
            session.add(run)
            session.commit()

            ckpt = Checkpoint(
                run_id="run-ckpt-01",
                phase="registration",
                state="VERIFY_EMAIL",
                resumable_data={"session_cookie": "abc"},
            )
            session.add(ckpt)
            session.commit()
            session.refresh(ckpt)
            self.assertEqual(ckpt.phase, "registration")

    def test_provider_config_crud(self):
        with get_session() as session:
            pc = ProviderConfig(
                provider_type="browser",
                provider_name="adspower-main",
                config={"api_url": "http://localhost:50325"},
                is_active=True,
            )
            session.add(pc)
            session.commit()

            found = session.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_type == "browser",
                    ProviderConfig.provider_name == "adspower-main",
                )
            ).first()
            self.assertIsNotNone(found)
            self.assertTrue(found.is_active)
            self.assertEqual(found.config["api_url"], "http://localhost:50325")

            session.delete(found)
            session.commit()
            gone = session.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_name == "adspower-main",
                )
            ).first()
            self.assertIsNone(gone)

    def test_run_events_relationship(self):
        """同一 run_id 下可以有多个事件"""
        with get_session() as session:
            run = Run(id="run-multi-evt", email="m@t.com", password="p",
                      profile_id="p1", config_snapshot={})
            session.add(run)
            session.commit()

            for i, evt_type in enumerate(["state_change", "action", "error"]):
                session.add(RunEvent(
                    run_id="run-multi-evt",
                    event_type=evt_type,
                    state=f"STATE_{i}",
                    payload={},
                    timestamp=datetime.now(timezone.utc),
                ))
            session.commit()

            events = list(session.exec(
                select(RunEvent).where(RunEvent.run_id == "run-multi-evt")
            ).all())
            self.assertEqual(len(events), 3)

    def test_user_model(self):
        with get_session() as session:
            user = User(
                username="alice",
                password_hash="salt$digest",
                role="admin",
                is_active=True,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            self.assertEqual(user.username, "alice")
            self.assertEqual(user.role, "admin")

    def test_assistant_action_log(self):
        with get_session() as session:
            log = AssistantActionLog(
                user_id="user-1",
                mode="action",
                intent="preview provider",
                action_type="upsert_provider",
                status="preview_ready",
                title="Preview provider",
                payload={"payload": {"provider_name": "demo"}},
                audit={"preview": {"status": "ALLOW"}},
                result={"preview": {"summary": "ok"}},
            )
            session.add(log)
            session.commit()
            session.refresh(log)
            self.assertEqual(log.status, "preview_ready")
            self.assertEqual(log.audit["preview"]["status"], "ALLOW")

    def test_provider_revision_model(self):
        with get_session() as session:
            revision = ProviderConfigRevision(
                provider_type="browser",
                provider_name="demo",
                snapshot={"exists": True, "config": {"api_key": "[REDACTED]"}},
                action_log_id="action-1",
                created_by="user-1",
            )
            session.add(revision)
            session.commit()
            session.refresh(revision)
            self.assertIsNotNone(revision.id)
            self.assertEqual(revision.snapshot["exists"], True)

    def test_mail_account_and_app_setting_models(self):
        with get_session() as session:
            account = MailAccount(
                label="Apple A",
                provider_name="applemail",
                email="alpha@example.com",
                client_id="cid-1",
                refresh_token="rt-1",
                extra={"account_id": "acct-1"},
            )
            setting = AppSetting(key="default_mail_provider", value="mail-default")
            session.add(account)
            session.add(setting)
            session.commit()

            found_account = session.exec(select(MailAccount).where(MailAccount.email == "alpha@example.com")).first()
            found_setting = session.get(AppSetting, "default_mail_provider")
            self.assertIsNotNone(found_account)
            self.assertEqual(found_account.provider_name, "applemail")
            self.assertEqual(found_setting.value, "mail-default")


if __name__ == "__main__":
    unittest.main()
