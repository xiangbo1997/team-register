# -*- coding: utf-8 -*-
"""FastAPI 控制面 API 集成测试。"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.db.engine as engine_mod
from src.api import deps
from src.api.i18n import build_i18n_message_payload
from src.api.worker import shutdown_workers
from src.db.engine import get_session
from src.db.models import Run, RunEvent
from src.services.auth_service import AuthService


def _setup_test_engine():
    """创建使用 StaticPool 的 in-memory SQLite 引擎，确保所有连接共享同一数据库。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine_mod._engine = engine
    SQLModel.metadata.create_all(engine)
    return engine


class TestAPI(unittest.TestCase):
    """API 端点集成测试。"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: os.environ.get(key) for key in [
            "DATABASE_URL",
            "SESSION_SECRET",
            "ADMIN_USERNAME",
            "ADMIN_PASSWORD",
            "OPERATOR_USERNAME",
            "OPERATOR_PASSWORD",
            "VIEWER_USERNAME",
            "VIEWER_PASSWORD",
        ]}
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "admin123456"
        os.environ["OPERATOR_USERNAME"] = "operator"
        os.environ["OPERATOR_PASSWORD"] = "operator123"
        os.environ["VIEWER_USERNAME"] = "viewer"
        os.environ["VIEWER_PASSWORD"] = "viewer123"

        engine_mod._engine = None
        cls._clear_dep_caches()
        cls._engine = _setup_test_engine()

        auth_service = AuthService()
        auth_service.ensure_bootstrap_users()

        from src.api.app import app
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        engine_mod._engine = None
        cls._clear_dep_caches()
        for key, value in cls._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @classmethod
    def _clear_dep_caches(cls):
        deps.get_config_service.cache_clear()
        deps.get_event_broadcaster.cache_clear()
        deps.get_auth_service.cache_clear()
        deps.get_knowledge_service.cache_clear()
        deps.get_audit_service.cache_clear()
        deps.get_assistant_service.cache_clear()

    def setUp(self):
        SQLModel.metadata.drop_all(self._engine)
        SQLModel.metadata.create_all(self._engine)
        self._clear_dep_caches()
        AuthService().ensure_bootstrap_users()

    def tearDown(self):
        shutdown_workers(wait=True)

    def _new_client(self, *, with_csrf: bool = True) -> TestClient:
        client = TestClient(self.app)
        client.headers.update({
            "Origin": "http://testserver",
            "Referer": "http://testserver/",
        })
        if with_csrf:
            self._refresh_csrf(client)
        return client

    def _refresh_csrf(self, client: TestClient) -> str:
        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        token = response.json().get("csrf_token", "")
        self.assertTrue(token)
        client.headers.update({"X-CSRF-Token": token})
        return token

    def _login(self, client: TestClient, username: str, password: str):
        response = client.post("/api/auth/login", json={
            "username": username,
            "password": password,
            "next": "/",
        })
        self.assertEqual(response.status_code, 200)
        token = response.json().get("csrf_token", "")
        self.assertTrue(token)
        client.headers.update({"X-CSRF-Token": token})
        return response

    def _login_admin(self, client: TestClient):
        return self._login(client, "admin", "admin123456")

    def _login_operator(self, client: TestClient):
        return self._login(client, "operator", "operator123")

    def _login_viewer(self, client: TestClient):
        return self._login(client, "viewer", "viewer123")

    def _create_provider_as_admin(self, provider_name: str = "test-ads"):
        client = self._new_client()
        self._login_admin(client)
        response = client.put(f"/api/providers/browser/{provider_name}", json={
            "config": {"api_url": "http://localhost:50325", "api_key": "secret-token"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_health(self):
        client = self._new_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_auth_login_me_logout(self):
        client = self._new_client()

        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])

        response = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin123456",
            "next": "/config",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "admin")
        self.assertTrue(response.json().get("csrf_token"))
        client.headers.update({"X-CSRF-Token": response.json()["csrf_token"]})

        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])
        self.assertEqual(response.json()["user"]["username"], "admin")
        self.assertTrue(response.json().get("csrf_token"))

        response = client.post("/api/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        response = client.get("/api/auth/me")
        self.assertFalse(response.json()["authenticated"])

    def test_login_rejects_bad_credentials(self):
        client = self._new_client()
        response = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "wrong",
            "next": "/",
        })
        self.assertEqual(response.status_code, 401)

    def test_config_requires_auth_and_admin_for_updates(self):
        anon = self._new_client()
        self.assertEqual(anon.get("/api/config").status_code, 401)

        viewer = self._new_client()
        self._login_viewer(viewer)
        response = viewer.get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ads_api", response.json())

        response = viewer.put("/api/config", json={"updates": {"payment_plan": "team"}})
        self.assertEqual(response.status_code, 403)

        admin = self._new_client()
        self._login_admin(admin)
        response = admin.put("/api/config", json={"updates": {"payment_plan": "team"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment_plan"], "team")

        response = admin.post("/api/config/reload")
        self.assertEqual(response.status_code, 200)
        self.assertIn("payment_plan", response.json())

    def test_direct_config_update_also_hits_hidden_audit_chain(self):
        admin = self._new_client()
        self._login_admin(admin)

        response = admin.put("/api/config", json={"updates": {"payment_plan": "team"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("payment_plan"), "team")

        response = admin.put("/api/config", json={"updates": {}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("未提供", response.json()["detail"])

        response = admin.put("/api/config", json={"updates": {"llm_api_key": "secret-allowed-now"}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["llm_api_key"].endswith("****"))

    def test_tasks_permission_matrix(self):
        anon = self._new_client()
        self.assertEqual(anon.get("/api/tasks").status_code, 401)

        viewer = self._new_client()
        self._login_viewer(viewer)
        self.assertEqual(viewer.post("/api/tasks", json={
            "email": "viewer@test.com",
            "password": "pass123",
            "profile_id": "prof-viewer", "auto_start": False,
        }).status_code, 403)

        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "operator@test.com",
            "password": "pass123",
            "profile_id": "prof-1", "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["browser_provider"])
        self.assertTrue(response.json()["card_provider"])
        self.assertTrue(response.json()["mail_provider"])
        task_id = response.json()["id"]

        response = viewer.get("/api/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)

        response = viewer.get(f"/api/tasks/{task_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], task_id)

        response = operator.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")

        response = operator.post(f"/api/tasks/{task_id}/retry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

    def test_task_detail_returns_events_in_ascending_order(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "events@test.com",
            "password": "pass123",
            "profile_id": "prof-events",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.add(RunEvent(
                run_id=task_id,
                event_type="state_change",
                state="ENTRY",
                payload={"message": "进入入口页"},
                timestamp=now - timedelta(seconds=20),
            ))
            session.add(RunEvent(
                run_id=task_id,
                event_type="log",
                state="AUTH",
                payload={"message": "进入密码页"},
                timestamp=now - timedelta(seconds=10),
            ))
            session.commit()

        viewer = self._new_client()
        self._login_viewer(viewer)
        response = viewer.get(f"/api/tasks/{task_id}")
        self.assertEqual(response.status_code, 200)

        events = response.json()["events"]
        self.assertEqual(
            [item["payload"]["message"] for item in events],
            ["进入入口页", "进入密码页"],
        )
        self.assertEqual(
            [item["state"] for item in events],
            ["ENTRY", "AUTH"],
        )

    def test_retry_task_supports_resume_mode_and_preserves_failed_phase(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "resume@test.com",
            "password": "pass123",
            "profile_id": "prof-resume",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        with get_session() as session:
            run = session.get(Run, task_id)
            run.status = "failed"
            run.phase = "payment"
            run.error_reason = "payment_declined"
            session.add(run)
            session.commit()

        response = operator.post(f"/api/tasks/{task_id}/retry", json={"mode": "resume"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["phase"], "payment")
        self.assertEqual(response.json()["retry_mode"], "resume")

    def test_retry_task_invalid_mode_is_localized_in_english(self):
        operator = self._new_client()
        operator.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        self._login_operator(operator)

        response = operator.post("/api/tasks", json={
            "email": "retry-locale@test.com",
            "password": "pass123",
            "profile_id": "prof-retry-locale",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        with get_session() as session:
            run = session.get(Run, task_id)
            run.status = "failed"
            session.add(run)
            session.commit()

        response = operator.post(f"/api/tasks/{task_id}/retry", json={"mode": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Unsupported retry mode. Only resume / restart are supported",
        )

    def test_clear_task_events_deletes_existing_logs(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "clear-events@test.com",
            "password": "pass123",
            "profile_id": "prof-clear",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.add(RunEvent(
                run_id=task_id,
                event_type="log",
                state="ENTRY",
                payload={"message": "first"},
                timestamp=now - timedelta(seconds=2),
            ))
            session.add(RunEvent(
                run_id=task_id,
                event_type="error",
                state="AUTH",
                payload={"message": "second"},
                timestamp=now - timedelta(seconds=1),
            ))
            session.commit()

        response = operator.post(f"/api/tasks/{task_id}/events/clear")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["cleared"], 2)

        detail = operator.get(f"/api/tasks/{task_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["events"], [])

    def test_task_detail_localizes_event_payload_message_in_english(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "event-locale@test.com",
            "password": "pass123",
            "profile_id": "prof-event-locale",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        with get_session() as session:
            session.add(RunEvent(
                run_id=task_id,
                event_type="action",
                state="VERIFY_EMAIL",
                payload=build_i18n_message_payload(
                    "邮箱服务运行态预检失败",
                    "task_events.mail_runtime_preflight_failed",
                    action_id="mail_runtime_preflight",
                    result="failed",
                ),
                timestamp=datetime.now(timezone.utc),
            ))
            session.commit()

        viewer = self._new_client()
        viewer.headers.update({"Accept-Language": "en"})
        self._login_viewer(viewer)
        detail = viewer.get(f"/api/tasks/{task_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["events"][0]["payload"]["message"],
            "Mailbox runtime preflight failed",
        )

    def test_task_pages_only_render_manage_buttons_for_operator_roles(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "page-role@test.com",
            "password": "pass123",
            "profile_id": "prof-page-role",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        with get_session() as session:
            run = session.get(Run, task_id)
            run.status = "failed"
            session.add(run)
            session.commit()

        operator_detail = operator.get(f"/tasks/{task_id}")
        self.assertEqual(operator_detail.status_code, 200)
        self.assertIn("retryTask('resume')", operator_detail.text)
        self.assertIn("retryTask('restart')", operator_detail.text)

        viewer = self._new_client()
        self._login_viewer(viewer)
        viewer_detail = viewer.get(f"/tasks/{task_id}")
        self.assertEqual(viewer_detail.status_code, 200)
        self.assertNotIn("retryTask('resume')", viewer_detail.text)
        self.assertNotIn("retryTask('restart')", viewer_detail.text)

    def test_task_templates_show_toast_for_retry_cancel_errors(self):
        detail_template = Path("src/templates/pages/tasks/detail.html").read_text("utf-8")
        list_template = Path("src/templates/pages/tasks/list.html").read_text("utf-8")
        base_template = Path("src/templates/base.html").read_text("utf-8")

        self.assertIn("catch (error)", detail_template)
        self.assertIn("showToast(error.message", detail_template)
        self.assertIn("catch (error)", list_template)
        self.assertIn("showToast(error.message", list_template)
        self.assertIn("Content-Type", base_template)

    def test_provider_crud_is_admin_only_and_redacted(self):
        viewer = self._new_client()
        self._login_viewer(viewer)
        response = viewer.put("/api/providers/browser/test-ads", json={
            "config": {"api_key": "should-fail"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 403)

        self._create_provider_as_admin("test-ads")

        response = viewer.get("/api/providers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["config"]["api_key"], "[REDACTED]")

        response = viewer.get("/api/providers/browser/test-ads")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["api_key"], "[REDACTED]")

        admin = self._new_client()
        self._login_admin(admin)
        response = admin.delete("/api/providers/browser/test-ads")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_mail_account_crud_and_task_create_with_overrides(self):
        admin = self._new_client()
        self._login_admin(admin)

        response = admin.post("/api/mail-accounts", json={
            "label": "Apple 一号",
            "provider_name": "applemail",
            "email": "apple1@example.com",
            "client_id": "cid-1",
            "refresh_token": "rt-1",
            "extra": {"account_id": "acct-1"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)
        account_id = response.json()["id"]

        response = admin.get("/api/mail-accounts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertTrue(response.json()[0]["refresh_token"].endswith("****"))

        response = admin.post(f"/api/mail-accounts/{account_id}/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        response = admin.put("/api/providers/mail/luckmail-managed", json={
            "config": {"provider_name": "luckmail", "session_mode": "managed"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)

        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "apple1@example.com",
            "password": "pass123",
            "profile_id": "prof-provider",
            "card_key": "card-1",
            "browser_provider": "browser-default",
            "card_provider": "card-default",
            "mail_provider": "mail-default",
            "mail_account_id": account_id,
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mail_account_id"], account_id)
        self.assertEqual(body["mail_provider"], "mail-default")

        response = operator.post("/api/tasks", json={
            "email": "provider-task@test.com",
            "password": "pass123",
            "profile_id": "prof-provider-email-mismatch",
            "browser_provider": "browser-default",
            "card_provider": "card-default",
            "mail_provider": "mail-default",
            "mail_account_id": account_id,
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("任务邮箱", response.json()["detail"])

        response = operator.post("/api/tasks", json={
            "email": "provider-task-mismatch@test.com",
            "password": "pass123",
            "profile_id": "prof-provider-2",
            "browser_provider": "browser-default",
            "card_provider": "card-default",
            "mail_provider": "luckmail-managed",
            "mail_account_id": account_id,
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("不匹配", response.json()["detail"])

        response = admin.delete(f"/api/mail-accounts/{account_id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_direct_provider_write_also_hits_hidden_audit_chain(self):
        admin = self._new_client()
        self._login_admin(admin)

        response = admin.put("/api/providers/browser/audit-allow", json={
            "config": {"api_url": "http://127.0.0.1:50325", "api_key": "secret-token"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider_type"], "browser")

        response = admin.post("/api/providers/browser/audit-allow/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        response = admin.put("/api/providers/ftp/not-allowed", json={
            "config": {"endpoint": "ftp://example.test"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 403)
        self.assertIn("provider_type", response.json()["detail"])

        response = admin.post("/api/providers/ftp/not-allowed/test")
        self.assertEqual(response.status_code, 403)

    def test_provider_revisions_and_rollback(self):
        admin = self._new_client()
        self._login_admin(admin)

        response = admin.put("/api/providers/browser/revision-demo", json={
            "config": {"api_key": "secret-v1", "api_url": "http://v1"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)

        response = admin.put("/api/providers/browser/revision-demo", json={
            "config": {"api_key": "secret-v2", "api_url": "http://v2"},
            "is_active": False,
        })
        self.assertEqual(response.status_code, 200)

        response = admin.get("/api/providers/browser/revision-demo/revisions")
        self.assertEqual(response.status_code, 200)
        revisions = response.json()
        self.assertGreaterEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["snapshot"]["config"]["api_key"], "[REDACTED]")

        restore_target = revisions[0]["id"]
        response = admin.post(f"/api/providers/browser/revision-demo/revisions/{restore_target}/rollback")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["api_key"], "[REDACTED]")

    def test_stats_and_export_require_login(self):
        anon = self._new_client()
        self.assertEqual(anon.get("/api/stats").status_code, 401)
        self.assertEqual(anon.get("/api/export/csv").status_code, 401)

        operator = self._new_client()
        self._login_operator(operator)
        operator.post("/api/tasks", json={
            "email": "success@test.com",
            "password": "pass123",
            "profile_id": "prof-export", "auto_start": False,
        })

        viewer = self._new_client()
        self._login_viewer(viewer)
        response = viewer.get("/api/stats")
        self.assertEqual(response.status_code, 200)
        self.assertIn("total", response.json())

        response = viewer.get("/api/export/csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers.get("content-type", ""))

    def test_assistant_bootstrap_and_chat_return_manual_and_repo_citations(self):
        viewer = self._new_client()
        self._login_viewer(viewer)

        response = viewer.get("/api/assistant/bootstrap")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["assistant_enabled"])
        self.assertIn("manual_html", response.json())
        self.assertTrue(response.json().get("csrf_token"))

        response = viewer.post("/api/assistant/chat", json={
            "message": "如何登录控制台并 preview provider？",
            "intent_mode": "answer",
            "page_context": {"path": "/help", "title": "Help"},
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "answer")
        self.assertGreater(len(data["manual_citations"]), 0)
        self.assertGreater(len(data["repo_citations"]), 0)

    def test_assistant_preview_and_commit_flow_for_admin(self):
        admin = self._new_client()
        self._login_admin(admin)

        response = admin.post("/api/assistant/preview", json={
            "message": "preview provider",
            "draft_action": {
                "action_type": "upsert_provider",
                "payload": {
                    "provider_type": "browser",
                    "provider_name": "assistant-demo",
                    "config": {"api_url": "http://127.0.0.1:50325", "api_key": "secret-token"},
                    "is_active": False,
                },
            },
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "preview")
        self.assertTrue(data["can_commit"])
        self.assertTrue(data["preview"]["diff"])

        response = admin.post("/api/assistant/commit", json={"preview_id": data["preview_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        response = admin.get(f"/api/assistant/actions/{data['preview_id']}")
        self.assertEqual(response.status_code, 200)
        action = response.json()
        self.assertEqual(action["status"], "completed")
        self.assertEqual(action["result"]["commit"]["provider"]["config"]["api_key"], "[REDACTED]")

    def test_assistant_non_admin_action_request_is_denied(self):
        viewer = self._new_client()
        self._login_viewer(viewer)

        response = viewer.post("/api/assistant/preview", json={
            "message": "preview provider",
            "draft_action": {
                "action_type": "upsert_provider",
                "payload": {
                    "provider_type": "browser",
                    "provider_name": "viewer-demo",
                    "config": {"api_url": "http://127.0.0.1:50325"},
                    "is_active": False,
                },
            },
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "answer")
        self.assertFalse(response.json()["can_commit"])

    def test_card_activations_list_invalidate_endpoints(self):
        """GET /api/cards 列表 + POST /invalidate；非 admin 拒绝。"""
        admin = self._new_client()
        self._login_admin(admin)

        # 起初列表为空
        r = admin.get("/api/cards")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

        # 直接落库一条（绕过 X988 调用）
        from src.db.models import CardActivation
        from src.db.engine import get_session
        with get_session() as s:
            s.add(CardActivation(
                card_key="API_TEST_CDK",
                card_provider="x988card",
                card_number="4111000022223333",
                expiry_month="12",
                expiry_year="2099",
                cvv="789",
                name_on_card="API Tester",
                billing_address="addr",
                bin_country="US",
                sms_api="https://sms.example/q",
                phone="+1234567890",
            ))
            s.commit()

        # 列表应有 1 条 + 严格脱敏（卡号只露 last4 + bin_prefix）
        r = admin.get("/api/cards")
        self.assertEqual(r.status_code, 200)
        items = r.json()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["card_key"], "API_TEST_CDK")
        self.assertEqual(item["last4"], "3333")
        self.assertEqual(item["bin_prefix"], "411100")
        # CVV / sms_api 必须不出现
        self.assertNotIn("cvv", item)
        self.assertNotIn("sms_api", item)
        self.assertNotIn("card_number", item)
        self.assertEqual(item["status"], "active")

        # 详情
        r = admin.get("/api/cards/API_TEST_CDK")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["card_key"], "API_TEST_CDK")

        # invalidate
        r = admin.post("/api/cards/API_TEST_CDK/invalidate", json={"reason": "test_via_api"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_invalidated"])
        self.assertEqual(r.json()["status"], "invalidated")
        self.assertEqual(r.json()["invalidate_reason"], "test_via_api")

        # 默认列表不含已作废
        r = admin.get("/api/cards")
        self.assertEqual(r.json(), [])
        # 加 include_invalidated=true 才能看见
        r = admin.get("/api/cards?include_invalidated=true")
        self.assertEqual(len(r.json()), 1)

        # 操作员（非 admin）应被拒
        operator = self._new_client()
        self._login_operator(operator)
        r = operator.get("/api/cards")
        self.assertEqual(r.status_code, 403)

        # invalidate 不存在的卡 → 404
        r = admin.post("/api/cards/DOES_NOT_EXIST/invalidate", json={"reason": "x"})
        self.assertEqual(r.status_code, 404)

    def test_pro_warmup_create_only_requires_email(self):
        """新建 pro_warmup 账号仅强制必填 email；其它字段全部可选。

        历史曾强制 ``email + adspower_profile_id + password`` 三件套，假设所有
        pro_warmup 都走 ChatGPT 密码登录。但 OAuth-only 邮箱（applemail / outlook
        + Microsoft Graph）走 ``client_id + refresh_token`` 路径时，profile_id /
        password 反而留空。现在 UI 4 个凭据字段全可选，由运行时按场景再校验。
        """
        admin = self._new_client()
        self._login_admin(admin)

        # 1. 缺 email → 422（唯一硬必填）
        r = admin.post("/api/mail-accounts", json={
            "label": "warm-1", "provider_name": "applemail", "email": "",
            "extra": {"password": "p", "adspower_profile_id": "pf1"}, "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 422)
        self.assertIn("email", r.json()["detail"])

        # 2. 仅有 email，其它全空 → 200（UI 允许保存，运行时按情况校验）
        r = admin.post("/api/mail-accounts", json={
            "label": "warm-min", "provider_name": "applemail", "email": "warmin@x.c",
            "extra": {}, "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 200)

        # 3. 缺 password 但有 profile_id → 200（OAuth-only 场景）
        r = admin.post("/api/mail-accounts", json={
            "label": "warm-oauth", "provider_name": "applemail", "email": "warmoauth@x.c",
            "extra": {"adspower_profile_id": "pf-oauth"}, "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 200)

        # 4. 缺 profile_id 但有 password → 200（密码-only 场景，运行时会拦）
        r = admin.post("/api/mail-accounts", json={
            "label": "warm-pwd", "provider_name": "applemail", "email": "warmpwd@x.c",
            "extra": {"password": "p"}, "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 200)

        # 5. 完整 → 200，与历史完整路径行为一致
        r = admin.post("/api/mail-accounts", json={
            "label": "warm-1", "provider_name": "applemail", "email": "warm1@x.c",
            "extra": {"password": "p", "adspower_profile_id": "pf1"}, "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["role"], "pro_warmup")
        self.assertEqual(body["consecutive_failures"], 0)
        self.assertEqual(body["last_used_at"], "")
        self.assertEqual(body["cooldown_until"], "")

    def test_pro_warmup_update_allows_empty_password(self):
        """编辑 pro_warmup 账号时 extra.password 可以省略（service 保留旧密码）"""
        admin = self._new_client()
        self._login_admin(admin)

        # 先建一个完整账号
        r = admin.post("/api/mail-accounts", json={
            "label": "warm-x", "provider_name": "applemail", "email": "warmx@x.c",
            "extra": {"password": "old-pwd", "adspower_profile_id": "pf-old"},
            "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 200)
        account_id = r.json()["id"]

        # 编辑：只改 profile_id，不传 password —— 应 200
        r = admin.put(f"/api/mail-accounts/{account_id}", json={
            "label": "warm-x-renamed", "provider_name": "applemail", "email": "warmx@x.c",
            "extra": {"adspower_profile_id": "pf-new"},
            "role": "pro_warmup",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["label"], "warm-x-renamed")

    def test_public_page_routes_and_protected_redirects(self):
        client = self._new_client()

        self.assertEqual(client.get("/login").status_code, 200)
        self.assertEqual(client.get("/help").status_code, 200)
        self.assertEqual(client.get("/manual").status_code, 200)

        for path in ["/", "/tasks", "/tasks/create", "/config", "/providers", "/mail-accounts"]:
            response = client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertIn("/login", response.headers["location"])

        self._login_admin(client)
        for path in ["/", "/tasks", "/config", "/providers", "/mail-accounts"]:
            response = client.get(path)
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
