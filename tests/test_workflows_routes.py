# -*- coding: utf-8 -*-
"""/api/workflows REST 端点集成测试（夹具复用 test_registration_profiles_api.py 模式）。

覆盖：list 脱敏 / toggle / delete / 非 admin 403 / stats。
"""

import os
import unittest

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.db.engine as engine_mod
from src.api import deps
from src.api.worker import shutdown_workers
from src.db.engine import get_session
from src.db.models import LearnedWorkflow
from src.services.auth_service import AuthService


def _setup_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine_mod._engine = engine
    SQLModel.metadata.create_all(engine)
    return engine


class TestWorkflowsAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {
            key: os.environ.get(key) for key in [
                "DATABASE_URL", "SESSION_SECRET",
                "ADMIN_USERNAME", "ADMIN_PASSWORD",
                "VIEWER_USERNAME", "VIEWER_PASSWORD",
            ]
        }
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["SESSION_SECRET"] = "test-secret"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "admin123456"
        os.environ["VIEWER_USERNAME"] = "viewer"
        os.environ["VIEWER_PASSWORD"] = "viewer123"

        engine_mod._engine = None
        cls._clear_dep_caches()
        cls._engine = _setup_test_engine()
        AuthService().ensure_bootstrap_users()

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
        deps.get_registration_profile_service.cache_clear()

    def setUp(self):
        SQLModel.metadata.drop_all(self._engine)
        SQLModel.metadata.create_all(self._engine)
        self._clear_dep_caches()
        AuthService().ensure_bootstrap_users()
        # seed 两条经验：一条高成功率含 fill 值（验证脱敏），一条低成功率
        with get_session() as session:
            session.add_all([
                LearnedWorkflow(
                    platform="openai", state="AUTH", location="auth.openai.com/login",
                    signal_signature={"has_password_input": True}, signature_hash="h1",
                    action_id="submit_password", source="llm",
                    success_count=5, fail_count=0, is_enabled=True,
                    last_action_meta={"value": "SuperSecret123!", "idx": 2},
                ),
                LearnedWorkflow(
                    platform="openai", state="ABOUT_YOU", location="auth.openai.com/about-you",
                    signal_signature={}, signature_hash="h2",
                    action_id="fill_about_you", source="experience",
                    success_count=1, fail_count=4, is_enabled=True,
                ),
            ])
            session.commit()

    def tearDown(self):
        shutdown_workers(wait=True)

    def _new_client(self) -> TestClient:
        client = TestClient(self.app)
        client.headers.update({"Origin": "http://testserver", "Referer": "http://testserver/"})
        self._refresh_csrf(client)
        return client

    def _refresh_csrf(self, client) -> str:
        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        token = response.json().get("csrf_token", "")
        self.assertTrue(token)
        client.headers.update({"X-CSRF-Token": token})
        return token

    def _login(self, client, username, password):
        response = client.post("/api/auth/login", json={
            "username": username, "password": password, "next": "/",
        })
        self.assertEqual(response.status_code, 200)
        client.headers.update({"X-CSRF-Token": response.json()["csrf_token"]})
        return response

    def _admin_client(self):
        client = self._new_client()
        self._login(client, "admin", "admin123456")
        return client

    def _viewer_client(self):
        client = self._new_client()
        self._login(client, "viewer", "viewer123")
        return client

    # ── 测试 ──────────────────────────────────────

    def test_list_requires_admin(self):
        client = self._viewer_client()
        self.assertEqual(client.get("/api/workflows").status_code, 403)

    def test_list_returns_and_redacts(self):
        client = self._admin_client()
        rows = client.get("/api/workflows").json()
        self.assertEqual(len(rows), 2)
        # 找含 fill 值的那条，确认 value 被脱敏（不应原样泄漏密码）
        auth_row = next(r for r in rows if r["state"] == "AUTH")
        meta_str = str(auth_row["last_action_meta"])
        self.assertNotIn("SuperSecret123!", meta_str)
        # 低成功率行标 is_low_rate
        about_row = next(r for r in rows if r["state"] == "ABOUT_YOU")
        self.assertTrue(about_row["is_low_rate"])  # 1/5=0.2 < 0.5
        self.assertFalse(auth_row["is_low_rate"])  # 5/5=1.0

    def test_stats(self):
        client = self._admin_client()
        stats = client.get("/api/workflows/stats").json()
        self.assertEqual(stats["workflow_count"], 2)
        self.assertEqual(stats["total_success"], 6)
        self.assertEqual(stats["total_fail"], 4)

    def test_toggle(self):
        client = self._admin_client()
        rows = client.get("/api/workflows").json()
        wid = rows[0]["id"]
        resp = client.post(f"/api/workflows/{wid}/toggle", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])
        # 验证 enabled=false 过滤能查到它
        disabled = client.get("/api/workflows?enabled=false").json()
        self.assertTrue(any(r["id"] == wid for r in disabled))

    def test_toggle_requires_csrf(self):
        client = self._admin_client()
        rows = client.get("/api/workflows").json()
        wid = rows[0]["id"]
        client.headers.pop("X-CSRF-Token", None)
        resp = client.post(f"/api/workflows/{wid}/toggle", json={"enabled": False})
        self.assertIn(resp.status_code, (401, 403))

    def test_delete(self):
        client = self._admin_client()
        rows = client.get("/api/workflows").json()
        wid = rows[0]["id"]
        resp = client.delete(f"/api/workflows/{wid}")
        self.assertEqual(resp.status_code, 200)
        remaining = client.get("/api/workflows").json()
        self.assertFalse(any(r["id"] == wid for r in remaining))

    def test_delete_nonexistent_404(self):
        client = self._admin_client()
        self.assertEqual(client.delete("/api/workflows/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
