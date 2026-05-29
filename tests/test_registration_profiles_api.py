# -*- coding: utf-8 -*-
"""/api/registration-profiles REST 端点集成测试。

夹具复用 test_api.py 的 StaticPool + bootstrap users 模式。
覆盖：权限、CSRF、CRUD、set-default 互斥、binding 校验、revision 查询。
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
from src.db.models import ProviderConfig
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


class TestRegistrationProfilesAPI(unittest.TestCase):

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

        # seed 一组 ProviderConfig 让 binding 校验通过
        with get_session() as session:
            session.add_all([
                ProviderConfig(
                    provider_type="browser", provider_name="browser-default",
                    config={}, is_active=True,
                ),
                ProviderConfig(
                    provider_type="card", provider_name="card-default",
                    config={}, is_active=True,
                ),
                ProviderConfig(
                    provider_type="mail", provider_name="mail-cfworker-default",
                    config={}, is_active=True,
                ),
            ])
            session.commit()

    def tearDown(self):
        shutdown_workers(wait=True)

    # ── 客户端 helpers（与 test_api.py 同款）────────

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
            "username": username, "password": password, "next": "/",
        })
        self.assertEqual(response.status_code, 200)
        client.headers.update({"X-CSRF-Token": response.json()["csrf_token"]})
        return response

    def _admin_client(self) -> TestClient:
        client = self._new_client()
        self._login(client, "admin", "admin123456")
        return client

    def _viewer_client(self) -> TestClient:
        client = self._new_client()
        self._login(client, "viewer", "viewer123")
        return client

    # ── 测试用例 ──────────────────────────────────

    def test_list_requires_admin(self):
        client = self._viewer_client()
        response = client.get("/api/registration-profiles")
        self.assertEqual(response.status_code, 403)

    def test_create_requires_admin(self):
        client = self._viewer_client()
        response = client.post("/api/registration-profiles", json={
            "name": "viewer-attempt",
            "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        self.assertEqual(response.status_code, 403)

    def test_create_requires_csrf(self):
        """admin 已登录但故意移除 X-CSRF-Token，写操作应被 require_csrf 拒绝。"""
        client = self._admin_client()
        # 已登录，移除 CSRF header 模拟 token 缺失
        client.headers.pop("X-CSRF-Token", None)
        response = client.post("/api/registration-profiles", json={
            "name": "no-csrf",
            "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        # require_csrf 在 require_role 之前/之后都可能；只要 4xx 拒绝即可
        self.assertIn(response.status_code, (401, 403))

    def test_create_minimal(self):
        client = self._admin_client()
        response = client.post("/api/registration-profiles", json={
            "name": "email-test",
            "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "email-test")
        self.assertEqual(body["registration_kind"], "email")
        self.assertFalse(body["is_default"])
        self.assertTrue(body["is_active"])

    def test_create_with_invalid_binding_blocked(self):
        client = self._admin_client()
        response = client.post("/api/registration-profiles", json={
            "name": "bad",
            "registration_kind": "email",
            "provider_bindings": {"browser": "ghost-provider"},
        })
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "invalid_bindings")
        self.assertTrue(any("不存在" in err for err in detail["errors"]))

    def test_create_skip_binding_validation(self):
        """validate_bindings=False 时允许引用未创建的 provider（给 seed 模式留口子）。"""
        client = self._admin_client()
        response = client.post("/api/registration-profiles", json={
            "name": "loose",
            "registration_kind": "phone",
            "provider_bindings": {"sms": "sms-activate-future"},
            "validate_bindings": False,
        })
        self.assertEqual(response.status_code, 200)

    def test_create_duplicate_name(self):
        client = self._admin_client()
        payload = {
            "name": "dup",
            "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        }
        self.assertEqual(client.post("/api/registration-profiles", json=payload).status_code, 200)
        response = client.post("/api/registration-profiles", json=payload)
        self.assertEqual(response.status_code, 409)

    def test_create_invalid_kind(self):
        client = self._admin_client()
        response = client.post("/api/registration-profiles", json={
            "name": "bad-kind",
            "registration_kind": "webauthn",
            "provider_bindings": {"browser": "browser-default"},
        })
        self.assertEqual(response.status_code, 400)

    def test_get_not_found(self):
        client = self._admin_client()
        response = client.get("/api/registration-profiles/ghost")
        self.assertEqual(response.status_code, 404)

    def test_list_filters(self):
        client = self._admin_client()
        client.post("/api/registration-profiles", json={
            "name": "e1", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        client.post("/api/registration-profiles", json={
            "name": "p1", "registration_kind": "phone",
            "provider_bindings": {"browser": "browser-default"},
        })
        emails = client.get("/api/registration-profiles?registration_kind=email").json()
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["name"], "e1")

    def test_update_partial(self):
        client = self._admin_client()
        client.post("/api/registration-profiles", json={
            "name": "u1", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        response = client.put("/api/registration-profiles/u1", json={
            "description": "更新过的描述",
            "provider_bindings": {
                "browser": "browser-default",
                "card": "card-default",
            },
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["description"], "更新过的描述")
        self.assertEqual(response.json()["provider_bindings"]["card"], "card-default")

    def test_set_default_clears_others(self):
        client = self._admin_client()
        # 先建两条 email
        client.post("/api/registration-profiles", json={
            "name": "e1", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
            "is_default": True,
        })
        client.post("/api/registration-profiles", json={
            "name": "e2", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        # 把 e2 设为默认
        response = client.post("/api/registration-profiles/e2/set-default")
        self.assertEqual(response.status_code, 200)
        # e1 应已被清零
        e1 = client.get("/api/registration-profiles/e1").json()
        self.assertFalse(e1["is_default"])
        e2 = client.get("/api/registration-profiles/e2").json()
        self.assertTrue(e2["is_default"])

    def test_delete_default_blocked(self):
        client = self._admin_client()
        client.post("/api/registration-profiles", json={
            "name": "d1", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
            "is_default": True,
        })
        response = client.delete("/api/registration-profiles/d1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("默认组合", response.json()["detail"])

    def test_delete_non_default(self):
        client = self._admin_client()
        client.post("/api/registration-profiles", json={
            "name": "d2", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        response = client.delete("/api/registration-profiles/d2")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])

    def test_revisions_listed(self):
        client = self._admin_client()
        client.post("/api/registration-profiles", json={
            "name": "r1", "registration_kind": "email",
            "provider_bindings": {"browser": "browser-default"},
        })
        client.put("/api/registration-profiles/r1", json={
            "description": "改 1",
        })
        response = client.get("/api/registration-profiles/r1/revisions")
        self.assertEqual(response.status_code, 200)
        revs = response.json()
        # create + update = 2 条
        self.assertEqual(len(revs), 2)
        # 最新一条 actor 是 admin user id
        self.assertTrue(revs[0]["created_by"])


if __name__ == "__main__":
    unittest.main()
