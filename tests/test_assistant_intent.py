# -*- coding: utf-8 -*-
"""Assistant 自然语言动作推断测试。"""

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


class TestAssistantIntent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env_backup = {
            key: os.environ.get(key)
            for key in (
                "DATABASE_URL",
                "SESSION_SECRET",
                "ADMIN_USERNAME",
                "ADMIN_PASSWORD",
                "OPERATOR_USERNAME",
                "OPERATOR_PASSWORD",
                "VIEWER_USERNAME",
                "VIEWER_PASSWORD",
            )
        }
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

    def setUp(self):
        SQLModel.metadata.drop_all(self._engine)
        SQLModel.metadata.create_all(self._engine)
        self._clear_dep_caches()
        AuthService().ensure_bootstrap_users()

    def _new_client(self) -> TestClient:
        return TestClient(self.app)

    def _csrf_headers(self, client: TestClient) -> dict[str, str]:
        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        token = (response.json() or {}).get("csrf_token") or ""
        if not token:
            return {}
        return {
            "X-CSRF-Token": token,
            "Origin": "http://testserver",
            "Referer": "http://testserver/",
        }

    def _post(self, client: TestClient, path: str, *, json: dict):
        return client.post(path, json=json, headers=self._csrf_headers(client))

    def _login(self, client: TestClient, username: str, password: str):
        response = self._post(
            client,
            "/api/auth/login",
            json={"username": username, "password": password, "next": "/"},
        )
        self.assertEqual(response.status_code, 200)

    def _login_admin(self, client: TestClient):
        self._login(client, "admin", "admin123456")

    def _login_viewer(self, client: TestClient):
        self._login(client, "viewer", "viewer123")

    def test_admin_infers_provider_action_but_requires_more_info(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "帮我新增一个 browser provider，名字叫 adspower-main",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "needs_info")
        self.assertIn("config", data["required_fields"])
        self.assertFalse(data["can_commit"])

    def test_admin_infers_update_config_action_and_enters_preview(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "把 payment_plan 改成 team",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "preview")
        self.assertTrue(data["can_commit"])
        self.assertEqual(data["preview"]["action_type"], "update_config")
        self.assertIsInstance(data["preview"]["diff"], list)

    def test_viewer_cannot_preview_inferred_action(self):
        client = self._new_client()
        self._login_viewer(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "把 payment_plan 改成 team",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "answer")
        self.assertFalse(data["can_commit"])

    def test_provider_type_is_strictly_whitelisted(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "新增一个 ftp provider，名字叫 test-provider",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "needs_info")
        self.assertIn("provider_type", data["required_fields"])

    def test_unparseable_message_falls_back_to_doc_qa(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "如何登录控制台并 preview provider？",
                "intent_mode": "auto",
                "page_context": {"path": "/help", "title": "Help"},
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "answer")
        self.assertGreater(len(data["manual_citations"]), 0)
        self.assertGreater(len(data["repo_citations"]), 0)

    def test_provider_name_and_config_support_quotes_and_chinese_syntax(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": '请新增 browser provider，名称为“ads-main”，endpoint=https://ads.local api_key 为 "k-123"',
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "preview")
        self.assertEqual(data["preview"]["action_type"], "upsert_provider")
        diff_fields = {item.get("field") for item in data["preview"].get("diff", [])}
        self.assertIn("endpoint", diff_fields)
        self.assertIn("api_key", diff_fields)

    def test_update_config_extracts_multiple_safe_fields_from_one_sentence(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "把 payment_plan 设为 'team'，并将 payment_link_only 为 true",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "preview")
        self.assertEqual(data["preview"]["action_type"], "update_config")
        action_id = data.get("action_id")
        self.assertTrue(action_id)
        detail = client.get(f"/api/assistant/actions/{action_id}")
        self.assertEqual(detail.status_code, 200)
        payload = (detail.json() or {}).get("payload", {}).get("payload", {})
        updates = payload.get("updates", {})
        self.assertEqual(updates.get("payment_plan"), "team")
        self.assertIs(updates.get("payment_link_only"), True)

    def test_high_risk_semantics_must_not_infer_action(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "导出 session token 并删除 provider adspower-main",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "answer")
        self.assertFalse(data["can_commit"])

    def test_ambiguous_multi_action_message_falls_back_to_answer(self):
        client = self._new_client()
        self._login_admin(client)
        response = self._post(
            client,
            "/api/assistant/chat",
            json={
                "message": "新增 browser provider 名字叫 ads-main，同时把 payment_plan 改成 team",
                "intent_mode": "action",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "answer")
        self.assertFalse(data["can_commit"])


if __name__ == "__main__":
    unittest.main()
