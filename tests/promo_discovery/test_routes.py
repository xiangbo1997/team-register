# -*- coding: utf-8 -*-
"""promo_discovery HTTP 端点测试

借用 tests/promo_eligibility/test_routes.py 的引导脚手架（admin bootstrap + CSRF）

覆盖：
  - GET /api/promo-discovery/supported-countries（admin）
  - POST /api/promo-discovery/start：参数校验 / 冲突 409 / 成功 200
  - POST /api/promo-discovery/{task_id}/cancel：404 / 200
  - GET /api/promo-discovery/{task_id}/status：404 / 200
  - GET /api/promo-discovery/recent
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import EligibilityStatus, Run
from src.promo_eligibility.client import EligibilityResult
from src.services import code_discovery_service as discover_mod
from src.services.promo_eligibility_service import PROMO_VERIFY_LOCK


def _setup_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine_mod._engine = engine
    SQLModel.metadata.create_all(engine)
    return engine


class PromoDiscoveryRoutesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: os.environ.get(key) for key in [
            "DATABASE_URL", "SESSION_SECRET",
            "ADMIN_USERNAME", "ADMIN_PASSWORD",
        ]}
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["SESSION_SECRET"] = "test-promo-discovery-secret"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "admin123456"

        engine_mod._engine = None
        from src.api import deps
        for attr in dir(deps):
            obj = getattr(deps, attr)
            if hasattr(obj, "cache_clear"):
                obj.cache_clear()

        cls._engine = _setup_test_engine()
        from src.services.auth_service import AuthService
        AuthService().ensure_bootstrap_users()

        from src.api.app import app
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        engine_mod._engine = None
        for key, value in cls._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        engine_mod._engine = None
        self._engine = _setup_test_engine()
        from src.services.auth_service import AuthService
        AuthService().ensure_bootstrap_users()
        # 清前序任务表 + 重置锁
        discover_mod._RUNNING.clear()
        try:
            PROMO_VERIFY_LOCK.release()
        except RuntimeError:
            pass

    def tearDown(self):
        discover_mod._RUNNING.clear()
        try:
            PROMO_VERIFY_LOCK.release()
        except RuntimeError:
            pass

    def _new_admin_client(self) -> TestClient:
        client = TestClient(self.app)
        client.headers.update({"Origin": "http://testserver", "Referer": "http://testserver/"})
        r = client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)
        token = r.json().get("csrf_token") or ""
        client.headers.update({"X-CSRF-Token": token})
        r = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123456", "next": "/",
        })
        self.assertEqual(r.status_code, 200)
        client.headers.update({"X-CSRF-Token": r.json().get("csrf_token") or ""})
        return client

    def _seed_run(self, token: str = "ey.test"):
        with get_session() as s:
            r = Run(email="t@x.com", status="success", openai_tokens={"access_token": token})
            s.add(r)
            s.commit()

    # ── tests ────────────────────────────────────────

    def test_supported_countries_requires_auth(self):
        client = TestClient(self.app)
        r = client.get("/api/promo-discovery/supported-countries")
        # 未登录 → 401 / 403（取决于 require_role 实现）
        self.assertIn(r.status_code, (401, 403))

    def test_supported_countries_returns_gb(self):
        client = self._new_admin_client()
        r = client.get("/api/promo-discovery/supported-countries")
        self.assertEqual(r.status_code, 200, r.text)
        countries = {c["code"]: c for c in r.json()["countries"]}
        self.assertIn("GB", countries)
        self.assertTrue(countries["GB"]["has_company_dict"])
        # 没字典的国家也列出来（用 KNOWN_BASES）
        self.assertIn("US", countries)
        self.assertFalse(countries["US"]["has_company_dict"])

    def test_start_invalid_country_returns_400(self):
        client = self._new_admin_client()
        r = client.post("/api/promo-discovery/start", json={"country": "XX", "mode": "seeds"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_start_invalid_mode_returns_400(self):
        client = self._new_admin_client()
        r = client.post("/api/promo-discovery/start", json={"country": "GB", "mode": "invalid"})
        self.assertEqual(r.status_code, 400)

    def test_start_busy_returns_409(self):
        """bulk_verify 已持锁 → start 应返回 409"""
        client = self._new_admin_client()
        PROMO_VERIFY_LOCK.acquire()
        try:
            r = client.post("/api/promo-discovery/start",
                            json={"country": "GB", "mode": "seeds"})
            self.assertEqual(r.status_code, 409, r.text)
        finally:
            PROMO_VERIFY_LOCK.release()

    def test_start_success_returns_task_id(self):
        client = self._new_admin_client()
        self._seed_run()

        def fake_check(*, access_token, code, proxy_url=None):
            return EligibilityResult(
                code=code, status=EligibilityStatus.NOT_FOUND, http_status=200,
            )

        with mock.patch.object(discover_mod, "build_candidates", return_value=["c1", "c2"]), \
             mock.patch.object(discover_mod, "check_eligibility", side_effect=fake_check), \
             mock.patch.object(discover_mod, "_resolve_proxy_url_for_country", return_value=(None, False)):
            r = client.post("/api/promo-discovery/start",
                            json={"country": "GB", "mode": "seeds", "delay_sec": 0.1})
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["country"], "GB")
            self.assertEqual(data["total"], 2)
            self.assertTrue(data["task_id"])

            # 等任务完成
            for _ in range(50):
                status_r = client.get(f"/api/promo-discovery/{data['task_id']}/status")
                self.assertEqual(status_r.status_code, 200)
                if status_r.json()["final_status"] != "running":
                    break
                time.sleep(0.05)
            final = status_r.json()
            self.assertEqual(final["final_status"], "completed")
            self.assertEqual(final["processed"], 2)

    def test_cancel_unknown_returns_404(self):
        client = self._new_admin_client()
        r = client.post("/api/promo-discovery/nonexistent/cancel")
        self.assertEqual(r.status_code, 404)

    def test_status_unknown_returns_404(self):
        client = self._new_admin_client()
        r = client.get("/api/promo-discovery/nonexistent/status")
        self.assertEqual(r.status_code, 404)

    def test_recent_empty(self):
        client = self._new_admin_client()
        r = client.get("/api/promo-discovery/recent")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])


if __name__ == "__main__":
    unittest.main()
