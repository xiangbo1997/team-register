# -*- coding: utf-8 -*-
"""promo 相关 HTTP 端点集成测试

通过 FastAPI TestClient 覆盖：
  - POST /api/link-templates/{id}/verify-eligibility
  - POST /api/link-templates/promo            （便捷创建）
  - POST /api/link-templates/import-from-scanner
  - POST /api/link-templates/bulk-verify

重点验证：
  - 鉴权链（未登录 401，非 admin 403）
  - CSRF 校验（缺 X-CSRF-Token → 拒绝）
  - Pydantic schema 校验（缺字段 422、字段长度 422）
  - 业务错误映射（PromoVerifyError → 400 + 结构化 detail）
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import EligibilityStatus, LinkTemplate, Run
from src.promo_eligibility.client import EligibilityResult


_SAMPLE_KNOWN_CODES = {
    "valid": {
        "US": [
            {"code": "talentgeniusus", "company": "TalentGenius", "price_usd": 25,
             "discount_pct": 50},
        ],
    },
    "expired": [],
}


def _setup_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine_mod._engine = engine
    SQLModel.metadata.create_all(engine)
    return engine


class PromoRoutesTests(unittest.TestCase):
    """promo HTTP 端点鉴权 + 业务错误映射"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: os.environ.get(key) for key in [
            "DATABASE_URL", "SESSION_SECRET",
            "ADMIN_USERNAME", "ADMIN_PASSWORD",
            "VIEWER_USERNAME", "VIEWER_PASSWORD",
        ]}
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["SESSION_SECRET"] = "test-promo-routes-secret"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "admin123456"
        os.environ["VIEWER_USERNAME"] = "viewer"
        os.environ["VIEWER_PASSWORD"] = "viewer123"

        engine_mod._engine = None
        # 清依赖缓存（参考 test_api.py 的做法）
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
        # 每条测试用新的 in-memory DB，避免互相污染
        # 但保留 env，因为 TestClient.app 依赖 env-loaded auth_service
        engine_mod._engine = None  # 强制重建
        self._engine = _setup_test_engine()
        # 新 DB 没 admin 用户 —— 重新 bootstrap 才能登录
        from src.services.auth_service import AuthService
        AuthService().ensure_bootstrap_users()

    # ── helpers ────────────────────────────────────────

    def _new_client(self, *, login_admin: bool = True) -> TestClient:
        client = TestClient(self.app)
        client.headers.update({"Origin": "http://testserver", "Referer": "http://testserver/"})
        # GET /api/auth/me 拿初始 CSRF token + session cookie
        r = client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200, f"auth/me failed: {r.text}")
        token = r.json().get("csrf_token") or ""
        self.assertTrue(token)
        client.headers.update({"X-CSRF-Token": token})
        if login_admin:
            r = client.post("/api/auth/login", json={
                "username": "admin", "password": "admin123456", "next": "/",
            })
            self.assertEqual(r.status_code, 200, f"login failed: {r.text}")
            client.headers.update({"X-CSRF-Token": r.json().get("csrf_token") or ""})
        return client

    def _make_run_with_token(self, *, email: str = "test@x.com", access_token: str = "ey.GOOD"):
        run = Run(email=email, status="success", openai_tokens={"access_token": access_token})
        with get_session() as session:
            session.add(run)
            session.commit()
            session.refresh(run)
            return run.id

    def _make_template(self, *, name: str, promo_code: str = "talentgeniusus", country: str = "US"):
        tpl = LinkTemplate(
            name=name, plan="team", promo_code=promo_code,
            aimizy_country=country, aimizy_currency="USD",
        )
        with get_session() as session:
            session.add(tpl)
            session.commit()
            session.refresh(tpl)
            return tpl.id

    # ── 鉴权 ───────────────────────────────────────────

    def test_verify_eligibility_requires_login(self):
        """未登录调 verify-eligibility → 401"""
        client = self._new_client(login_admin=False)
        r = client.post("/api/link-templates/1/verify-eligibility", json={})
        self.assertEqual(r.status_code, 401)

    def test_create_promo_requires_login(self):
        client = self._new_client(login_admin=False)
        r = client.post("/api/link-templates/promo", json={"country": "US", "code": "abc"})
        self.assertEqual(r.status_code, 401)

    def test_import_from_scanner_requires_login(self):
        client = self._new_client(login_admin=False)
        r = client.post("/api/link-templates/import-from-scanner", json={})
        self.assertEqual(r.status_code, 401)

    def test_bulk_verify_requires_login(self):
        client = self._new_client(login_admin=False)
        r = client.post("/api/link-templates/bulk-verify", json={})
        self.assertEqual(r.status_code, 401)

    # ── verify-eligibility 业务错误映射 ────────────────

    def test_verify_eligibility_template_not_found_maps_400(self):
        """模板不存在 → PromoVerifyError(template_not_found) → 400"""
        client = self._new_client()
        r = client.post("/api/link-templates/99999/verify-eligibility", json={})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "template_not_found")
        self.assertIn("99999", detail["message"])

    def test_verify_eligibility_no_account_maps_400(self):
        """有模板但没 status='success' 账号 → 400 no_account"""
        tpl_id = self._make_template(name="t1")
        client = self._new_client()
        r = client.post(f"/api/link-templates/{tpl_id}/verify-eligibility", json={})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertEqual(detail["code"], "no_account")
        self.assertIn("success", detail["message"])

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_verify_eligibility_success_returns_status(self, mock_check):
        """主路径：返回 eligible 状态 + checked_at + metadata"""
        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
            metadata_raw={"metadata": {"discount": {"value": 25}}},
        )
        tpl_id = self._make_template(name="t-ok")
        self._make_run_with_token()

        client = self._new_client()
        r = client.post(f"/api/link-templates/{tpl_id}/verify-eligibility", json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "eligible")
        self.assertEqual(body["promo_code"], "talentgeniusus")
        self.assertIn("checked_at", body)
        self.assertIn("metadata", body)

    def test_verify_eligibility_missing_csrf_rejected(self):
        """缺 X-CSRF-Token → 403（CSRF middleware 拒绝）"""
        client = TestClient(self.app)
        client.headers.update({"Origin": "http://testserver"})
        # 先拿 cookie 但故意不设置 X-CSRF-Token header
        client.get("/api/auth/me")
        # admin login 也不带 csrf header → 应被拒
        r = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123456",
        })
        # CSRF 失败应该是 401/403；至少不是 200
        self.assertNotEqual(r.status_code, 200)

    # ── POST /promo 便捷创建 ───────────────────────────

    def test_create_promo_basic_flow(self):
        client = self._new_client()
        r = client.post("/api/link-templates/promo", json={
            "country": "us",  # lowercase 应自动转大写
            "code": "newcode123",
            "aimizy_currency": "usd",
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["name"], "promo-us-newcode123")
        self.assertEqual(body["aimizy_country"], "US")
        self.assertEqual(body["aimizy_currency"], "USD")
        self.assertEqual(body["seat_quantity"], 2)
        self.assertEqual(body["plan"], "team")

    def test_create_promo_duplicate_returns_400(self):
        """重复创建同 name → 400"""
        self._make_template(name="promo-us-dupcode", promo_code="dupcode")
        client = self._new_client()
        r = client.post("/api/link-templates/promo", json={
            "country": "US", "code": "dupcode",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("已存在", str(r.json()["detail"]))

    def test_create_promo_missing_required_field_422(self):
        """缺 country 字段 → 422（Pydantic 校验）"""
        client = self._new_client()
        r = client.post("/api/link-templates/promo", json={"code": "abc"})
        self.assertEqual(r.status_code, 422)

    def test_create_promo_short_country_422(self):
        """country 长度 < 2 → 422"""
        client = self._new_client()
        r = client.post("/api/link-templates/promo", json={"country": "U", "code": "abc"})
        self.assertEqual(r.status_code, 422)

    # ── POST /import-from-scanner ──────────────────────

    def test_import_from_scanner_missing_source_returns_400(self):
        """指定不存在的 source_path → 400 source_not_found"""
        client = self._new_client()
        r = client.post("/api/link-templates/import-from-scanner", json={
            "source_path": "/nonexistent/path.json",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "source_not_found")

    def test_import_from_scanner_dry_run_returns_plan(self):
        """dry_run=True 时返回计划但不写库"""
        tmp_dir = tempfile.mkdtemp()
        src = Path(tmp_dir) / "known.json"
        src.write_text(json.dumps(_SAMPLE_KNOWN_CODES), encoding="utf-8")
        try:
            client = self._new_client()
            r = client.post("/api/link-templates/import-from-scanner", json={
                "source_path": str(src), "dry_run": True,
            })
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["dry_run"])
            self.assertEqual(body["planned"], 1)
            self.assertEqual(body["created"], 0)
            # DB 没写
            with get_session() as session:
                from sqlmodel import select
                rows = list(session.exec(select(LinkTemplate)).all())
            self.assertEqual(len(rows), 0)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_import_from_scanner_apply_writes_records(self):
        """dry_run=False 时真正写库"""
        tmp_dir = tempfile.mkdtemp()
        src = Path(tmp_dir) / "known.json"
        src.write_text(json.dumps(_SAMPLE_KNOWN_CODES), encoding="utf-8")
        try:
            client = self._new_client()
            r = client.post("/api/link-templates/import-from-scanner", json={
                "source_path": str(src), "dry_run": False,
            })
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertFalse(body["dry_run"])
            self.assertEqual(body["created"], 1)
            with get_session() as session:
                from sqlmodel import select
                rows = list(session.exec(select(LinkTemplate)).all())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].name, "promo-us-talentgeniusus")
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── POST /bulk-verify ──────────────────────────────

    def test_bulk_verify_no_templates_returns_400(self):
        """DB 没任何 promo 模板 → 400 no_promo_templates"""
        # 即使有 token 也没用，因为没模板
        self._make_run_with_token()
        client = self._new_client()
        r = client.post("/api/link-templates/bulk-verify", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "no_promo_templates")

    def test_bulk_verify_no_token_returns_400(self):
        """有模板但没 completed 账号 → 400 no_account（token 在前置检查阶段就缺）"""
        self._make_template(name="t-bulk-1")
        client = self._new_client()
        r = client.post("/api/link-templates/bulk-verify", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "no_account")

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_bulk_verify_aggregates_status(self, mock_check):
        """主路径：批量验证 2 条 → 返回聚合统计"""
        # 2 条模板，1 个 eligible 1 个 exists
        self._make_template(name="t-bulk-a", promo_code="codeA")
        self._make_template(name="t-bulk-b", promo_code="codeB", country="GB")
        self._make_run_with_token()

        mock_check.side_effect = [
            EligibilityResult(code="codeA", status=EligibilityStatus.ELIGIBLE),
            EligibilityResult(code="codeB", status=EligibilityStatus.EXISTS, reason_code="user_not_eligible"),
        ]

        client = self._new_client()
        # delay_sec=0 避免测试拖时间
        r = client.post("/api/link-templates/bulk-verify", json={"delay_sec": 0})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["verified"], 2)
        self.assertEqual(body["by_status"], {"eligible": 1, "exists": 1})
        self.assertFalse(body["stopped_early"])


if __name__ == "__main__":
    unittest.main()
