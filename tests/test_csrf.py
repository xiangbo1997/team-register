# -*- coding: utf-8 -*-
"""CSRF 防护集成测试。"""

import os
import unittest

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.db.engine as engine_mod
from src.api import deps
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


class TestCSRF(unittest.TestCase):
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

    def _new_client(self, *, with_csrf: bool = True) -> TestClient:
        client = TestClient(self.app)
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

    def _login(self, client: TestClient, username: str, password: str) -> str:
        response = client.post("/api/auth/login", json={
            "username": username,
            "password": password,
            "next": "/",
        })
        self.assertEqual(response.status_code, 200)
        token = response.json().get("csrf_token", "")
        self.assertTrue(token)
        client.headers.update({"X-CSRF-Token": token})
        return token

    def test_login_requires_csrf_token(self):
        client = self._new_client(with_csrf=False)

        denied = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin123456",
            "next": "/",
        })
        self.assertEqual(denied.status_code, 403)

        self._refresh_csrf(client)
        ok = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin123456",
            "next": "/",
        })
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json().get("csrf_token"))

    def test_state_change_requires_csrf_token(self):
        client = self._new_client()
        token = self._login(client, "admin", "admin123456")

        client.headers.pop("X-CSRF-Token", None)
        denied = client.post("/api/config/reload")
        self.assertEqual(denied.status_code, 403)

        ok = client.post("/api/config/reload", headers={"X-CSRF-Token": token})
        self.assertEqual(ok.status_code, 200)

    def test_cross_origin_rejected_even_with_token(self):
        client = self._new_client()
        token = self._login(client, "admin", "admin123456")

        denied = client.post(
            "/api/config/reload",
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_logout_rotates_token_and_me_returns_token(self):
        client = self._new_client()
        token_1 = self._login(client, "admin", "admin123456")

        me_before = client.get("/api/auth/me")
        self.assertEqual(me_before.status_code, 200)
        self.assertTrue(me_before.json()["authenticated"])
        self.assertEqual(me_before.json()["csrf_token"], token_1)

        logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": token_1})
        self.assertEqual(logout.status_code, 200)
        token_2 = logout.json().get("csrf_token", "")
        self.assertTrue(token_2)
        self.assertNotEqual(token_1, token_2)

        me_after = client.get("/api/auth/me")
        self.assertEqual(me_after.status_code, 200)
        self.assertFalse(me_after.json()["authenticated"])
        self.assertEqual(me_after.json()["csrf_token"], token_2)

    def test_assistant_chat_requires_csrf(self):
        client = self._new_client()
        token = self._login(client, "viewer", "viewer123")

        client.headers.pop("X-CSRF-Token", None)
        denied = client.post("/api/assistant/chat", json={
            "message": "hello",
            "intent_mode": "answer",
            "page_context": {"path": "/help", "title": "Help"},
        })
        self.assertEqual(denied.status_code, 403)

        ok = client.post(
            "/api/assistant/chat",
            headers={"X-CSRF-Token": token},
            json={
                "message": "hello",
                "intent_mode": "answer",
                "page_context": {"path": "/help", "title": "Help"},
            },
        )
        self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
