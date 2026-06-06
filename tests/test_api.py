# -*- coding: utf-8 -*-
"""FastAPI 控制面 API 集成测试。"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.db.engine as engine_mod
from src.api import deps
from src.api.i18n import build_i18n_message_payload
from src.api.worker import shutdown_workers
from src.db.engine import get_session
from src.db.models import Checkpoint, Run, RunEvent
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
        deps.get_registration_profile_service.cache_clear()

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

    def test_create_task_phone_kind_blank_phone_persists_registration_kind(self):
        """Mode B（手机号注册）：空 phone_number 仍可创建，registration_kind 落到 snapshot + dict 输出。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",  # phone 模式应允许 email 留空
            "password": "pass123",
            "profile_id": "prof-phone-1",
            "auto_start": False,
            "phone_number": "",  # 留空，后端不申领（auto_start=False 不进 worker）
            "sms_country": "187",
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["registration_kind"], "phone")
        self.assertEqual(data["phone_number"], "")
        # mail_provider 仍落默认（后端会用 default 兜底，UI 不暴露邮箱字段但落库仍记）
        self.assertTrue(data["mail_provider"])

    # ── 平台（GPT / Grok）筛选 ─────────────────────────────
    def _seed_platform_runs(self):
        """直接落库一个 openai + 一个 grok 成功号，account_tier=registered。"""
        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.add(Run(
                id="gpt" + "a" * 13, email="gpt-acc@x.com", password="pw",
                profile_id="p1", status="success", phase="token_extraction",
                account_tier="registered", platform="openai",
                created_at=now, updated_at=now,
            ))
            session.add(Run(
                id="grk" + "a" * 13, email="grok-acc@x.com", password="pw",
                profile_id="p2", status="success", phase="grok_done",
                account_tier="registered", platform="grok",
                created_at=now, updated_at=now,
            ))
            session.commit()

    def test_accounts_platform_filter_returns_only_grok(self):
        admin = self._new_client()
        self._login_admin(admin)
        self._seed_platform_runs()
        resp = admin.get("/api/accounts?tier=registered&platform=grok")
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()
        self.assertEqual([r["email"] for r in rows], ["grok-acc@x.com"])
        self.assertEqual(rows[0]["platform"], "grok")

    def test_accounts_no_platform_returns_all_with_platform_field(self):
        admin = self._new_client()
        self._login_admin(admin)
        self._seed_platform_runs()
        resp = admin.get("/api/accounts?tier=registered")
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()
        self.assertEqual({r["email"] for r in rows}, {"gpt-acc@x.com", "grok-acc@x.com"})
        self.assertTrue(all("platform" in r for r in rows))

    def test_accounts_invalid_platform_returns_422(self):
        admin = self._new_client()
        self._login_admin(admin)
        resp = admin.get("/api/accounts?tier=registered&platform=meta")
        self.assertEqual(resp.status_code, 422)

    def test_accounts_export_platform_filter_only_grok(self):
        admin = self._new_client()
        self._login_admin(admin)
        self._seed_platform_runs()
        resp = admin.get("/api/accounts/export?tier=registered&platform=grok&fmt=full_json")
        self.assertEqual(resp.status_code, 200, resp.text)
        # 单账号导出 → JSON object，仅含 grok
        body = resp.json()
        rows = body if isinstance(body, list) else [body]
        self.assertEqual([r["email"] for r in rows], ["grok-acc@x.com"])

    def test_tasks_platform_filter_returns_only_grok(self):
        viewer = self._new_client()
        self._login_viewer(viewer)
        self._seed_platform_runs()
        resp = viewer.get("/api/tasks?platform=grok")
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        self.assertEqual([t["email"] for t in items], ["grok-acc@x.com"])
        self.assertTrue(all("platform" in t for t in items))

    def test_tasks_invalid_platform_lenient_returns_all(self):
        viewer = self._new_client()
        self._login_viewer(viewer)
        self._seed_platform_runs()
        resp = viewer.get("/api/tasks?platform=bogus")
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        self.assertEqual({t["email"] for t in items}, {"gpt-acc@x.com", "grok-acc@x.com"})

    def test_create_task_phone_kind_with_explicit_phone_persists_to_run(self):
        """Mode B：用户手填 phone_number 时直接落到 Run.phone_number（不走 SMS 申领）。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-phone-2",
            "auto_start": False,
            "phone_number": "+14155551212",
            "sms_country": "187",
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["registration_kind"], "phone")
        self.assertEqual(data["phone_number"], "+14155551212")

    def test_create_task_email_kind_blank_email_with_non_cfworker_provider_returns_422(self):
        """Mode A：邮箱留空 + 非 cfworker/outlook provider → 422 EMAIL_REQUIRED_FOR_PROVIDER。"""
        operator = self._new_client()
        self._login_operator(operator)
        # bootstrap 默认 mail-default provider_name=applemail（credentialed）→ 不在白名单
        response = operator.post("/api/tasks", json={
            "registration_kind": "email",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-email-blank",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["code"], "EMAIL_REQUIRED_FOR_PROVIDER")

    def test_create_task_persists_registration_profile_name_in_snapshot(self):
        """新字段 registration_profile_name 落到 config_snapshot，worker 后续可读。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-with-profile",
            "auto_start": False,
            "registration_profile_name": "phone-default",
        })
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["id"]
        with get_session() as session:
            run = session.get(Run, run_id)
        self.assertEqual(
            run.config_snapshot.get("registration_profile_name"),
            "phone-default",
        )

    def test_create_task_persists_provider_overrides_in_snapshot(self):
        """provider_overrides 落到 snapshot；空值被过滤。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-with-overrides",
            "auto_start": False,
            "provider_overrides": {
                "card": "card-nodecard",
                "browser": "",  # 应被过滤
                "  ": "noise",  # 应被过滤
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["id"]
        with get_session() as session:
            run = session.get(Run, run_id)
        overrides = run.config_snapshot.get("provider_overrides", {})
        self.assertEqual(overrides, {"card": "card-nodecard"})

    def test_create_task_provider_overrides_dict_takes_precedence_over_flat_fields(self):
        """2026-05-27 合并 UI 后契约：provider_overrides[slot] 优先于 body.{slot}_provider 写到 Run.{slot}_provider。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-dict-precedence",
            "auto_start": False,
            # 扁平字段填一个值
            "browser_provider": "browser-flat-value",
            "card_provider": "card-flat-value",
            # provider_overrides 填另一个值（应被优先采用）
            "provider_overrides": {
                "browser": "browser-from-overrides",
                "card": "card-from-overrides",
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["browser_provider"], "browser-from-overrides")
        self.assertEqual(data["card_provider"], "card-from-overrides")

    def test_create_task_provider_overrides_empty_slot_falls_back_to_flat(self):
        """provider_overrides 中槽位为空字符串时，回退到扁平字段（再回退到 config 默认）。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-empty-slot-fallback",
            "auto_start": False,
            "browser_provider": "browser-from-flat",
            "provider_overrides": {
                "browser": "",  # 空 → 回退到扁平字段
                "card": "card-from-overrides",
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["browser_provider"], "browser-from-flat")
        self.assertEqual(data["card_provider"], "card-from-overrides")

    def test_create_task_without_profile_fields_omits_snapshot_keys(self):
        """不传新字段时 snapshot 不应有 registration_profile_name / provider_overrides 键。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "phone",
            "email": "",
            "password": "pass123",
            "profile_id": "prof-no-overrides",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["id"]
        with get_session() as session:
            run = session.get(Run, run_id)
        self.assertNotIn("registration_profile_name", run.config_snapshot)
        self.assertNotIn("provider_overrides", run.config_snapshot)

    def test_create_task_email_kind_invalid_registration_kind_returns_422(self):
        """非法 registration_kind → 422。"""
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "registration_kind": "telegram",  # 非法
            "email": "x@y.com",
            "password": "pass123",
            "profile_id": "prof-invalid-kind",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("registration_kind", response.json()["detail"].lower() if isinstance(response.json()["detail"], str) else str(response.json()["detail"]))

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

    # ── clone_task / delete_task ──────────────────────────────────────────

    def _create_succeeded_task(self, operator: TestClient, *, email: str, profile_id: str) -> str:
        """创建一条任务并人工置为 success，返回 task_id。"""
        response = operator.post("/api/tasks", json={
            "email": email,
            "password": "pass123",
            "profile_id": profile_id,
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]
        with get_session() as session:
            run = session.get(Run, task_id)
            run.status = "success"
            run.phase = "payment"
            run.openai_tokens = {"access_token": "ey-xxx", "refresh_token": "rt-yyy"}
            run.config_snapshot = {"identity": {"email_local": "foo"}, "marker": "src-snapshot"}
            session.add(run)
            session.commit()
        return task_id

    def test_clone_task_creates_new_run_with_pending_status_and_copies_snapshot(self):
        operator = self._new_client()
        self._login_operator(operator)
        src_id = self._create_succeeded_task(operator, email="clone-src@test.com", profile_id="prof-clone")

        with mock.patch("src.services.batch_register_service.requeue_runs", return_value={"requeued": 1}) as mocked:
            response = operator.post(f"/api/tasks/{src_id}/clone")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertNotEqual(body["id"], src_id)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["phase"], "registration")
        self.assertEqual(body["email"], "clone-src@test.com")
        self.assertEqual(body["profile_id"], "prof-clone")
        self.assertEqual(body["cloned_from"], src_id)
        self.assertTrue(body["worker_submitted"])
        mocked.assert_called_once()

        with get_session() as session:
            new_run = session.get(Run, body["id"])
            self.assertEqual(new_run.config_snapshot.get("marker"), "src-snapshot")
            self.assertEqual(new_run.openai_tokens, {})  # 重置
            self.assertEqual(new_run.error_reason, None)
            self.assertEqual(new_run.account_tier, "registered")

    def test_clone_task_leaves_source_run_unchanged(self):
        operator = self._new_client()
        self._login_operator(operator)
        src_id = self._create_succeeded_task(operator, email="clone-untouched@test.com", profile_id="prof-untouched")

        with mock.patch("src.services.batch_register_service.requeue_runs", return_value={"requeued": 1}):
            response = operator.post(f"/api/tasks/{src_id}/clone")
        self.assertEqual(response.status_code, 200)

        with get_session() as session:
            src = session.get(Run, src_id)
            self.assertEqual(src.status, "success")
            self.assertEqual(src.phase, "payment")
            self.assertEqual(src.openai_tokens, {"access_token": "ey-xxx", "refresh_token": "rt-yyy"})

    def test_clone_task_not_found_returns_404(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks/no-such-id/clone")
        self.assertEqual(response.status_code, 404)

    def test_clone_task_without_csrf_is_rejected(self):
        operator = self._new_client()
        self._login_operator(operator)
        src_id = self._create_succeeded_task(operator, email="clone-csrf@test.com", profile_id="prof-csrf")
        operator.headers.pop("X-CSRF-Token", None)
        response = operator.post(f"/api/tasks/{src_id}/clone")
        self.assertIn(response.status_code, (401, 403))

    def test_delete_task_removes_run_events_and_checkpoints(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "delete-me@test.com",
            "password": "pass123",
            "profile_id": "prof-del",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        with get_session() as session:
            run = session.get(Run, task_id)
            run.status = "failed"
            session.add(run)
            session.add(RunEvent(run_id=task_id, event_type="log", state="ENTRY", payload={"m": "x"}))
            session.add(Checkpoint(run_id=task_id, phase="registration", state="AUTH", resumable_data={}))
            session.commit()

        response = operator.delete(f"/api/tasks/{task_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "deleted": task_id})

        from sqlmodel import select as _select
        with get_session() as session:
            self.assertIsNone(session.get(Run, task_id))
            self.assertEqual(list(session.exec(_select(RunEvent).where(RunEvent.run_id == task_id))), [])
            self.assertEqual(list(session.exec(_select(Checkpoint).where(Checkpoint.run_id == task_id))), [])

    def test_delete_task_running_returns_400(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "no-del-running@test.com",
            "password": "pass123",
            "profile_id": "prof-running",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]
        with get_session() as session:
            run = session.get(Run, task_id)
            run.status = "running"
            session.add(run)
            session.commit()
        response = operator.delete(f"/api/tasks/{task_id}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("running", response.json()["detail"])

    def test_delete_task_pending_returns_400(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.post("/api/tasks", json={
            "email": "no-del-pending@test.com",
            "password": "pass123",
            "profile_id": "prof-pending",
            "auto_start": False,
        })
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]
        response = operator.delete(f"/api/tasks/{task_id}")
        self.assertEqual(response.status_code, 400)

    def test_delete_task_not_found_returns_404(self):
        operator = self._new_client()
        self._login_operator(operator)
        response = operator.delete("/api/tasks/no-such-id")
        self.assertEqual(response.status_code, 404)

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

    def test_sms_provider_upsert_requires_api_key(self):
        """SMS provider 创建时必填 api_key —— L1 fail-fast 在入库前拦下。"""
        admin = self._new_client()
        self._login_admin(admin)
        # 空 api_key → 422
        response = admin.put("/api/providers/sms/sms-default", json={
            "config": {"country": "0"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 422)
        detail = response.json().get("detail") or {}
        self.assertEqual(detail.get("code"), "PROVIDER_NOT_CONFIGURED")
        self.assertIn("api_key", detail.get("missing_fields") or [])

        # 合法 api_key → 200
        response = admin.put("/api/providers/sms/sms-default", json={
            "config": {"api_key": "valid-key-12345", "country": "0"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)

    def test_llm_provider_upsert_requires_all_fields(self):
        """LLM provider 必填 base_url + api_key + model。"""
        admin = self._new_client()
        self._login_admin(admin)
        response = admin.put("/api/providers/llm/llm-default", json={
            "config": {"base_url": "https://api.openai.com"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 422)
        detail = response.json().get("detail") or {}
        missing = detail.get("missing_fields") or []
        self.assertIn("api_key", missing)
        self.assertIn("model", missing)

    def test_captcha_provider_upsert_validates_kind_and_token(self):
        """Captcha kind=nocaptcha 时必须有 user_token；非法 kind 直接拒绝。"""
        admin = self._new_client()
        self._login_admin(admin)

        # 非法 kind → 422
        response = admin.put("/api/providers/captcha/captcha-default", json={
            "config": {"kind": "invalid"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 422)

        # kind=nocaptcha 缺 user_token → 422
        response = admin.put("/api/providers/captcha/captcha-default", json={
            "config": {"kind": "nocaptcha"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 422)
        detail = response.json().get("detail") or {}
        self.assertIn("user_token", detail.get("missing_fields") or [])

        # kind=noop 不需要 token → 200
        response = admin.put("/api/providers/captcha/captcha-default", json={
            "config": {"kind": "noop"},
            "is_active": True,
        })
        self.assertEqual(response.status_code, 200)

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

    def test_card_pool_add_card_success_duplicate_and_static_routes(self):
        """POST /api/cards 入池成功；/pool /ready 不应被 /{card_key} 吞掉。"""
        admin = self._new_client()
        self._login_admin(admin)

        from src.models import CardInfo

        class StubCardApi:
            def __init__(self):
                self.calls = []

            def get_card(self, card_key):
                self.calls.append(card_key)
                return CardInfo(
                    card_number="4242424242424242",
                    expiry_month="12",
                    expiry_year="2030",
                    cvv="123",
                    last_four="4242",
                    name_on_card="John Doe",
                    status="active",
                    created_at="",
                    billing_address="123 Main St",
                    bin_country="US",
                )

        stub = StubCardApi()
        with mock.patch("src.api.routes.cards._build_card_api", return_value=stub):
            r = admin.post("/api/cards", json={
                "card_key": "POOL_ADD_1",
                "card_provider": "efuncard",
                "target_warmup_count": 0,
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["card_key"], "POOL_ADD_1")
        self.assertEqual(body["last4"], "4242")
        self.assertEqual(body["bin_prefix"], "424242")
        self.assertNotIn("card_number", body)
        self.assertNotIn("cvv", body)
        self.assertNotIn("sms_api", body)

        with mock.patch("src.api.routes.cards._build_card_api", return_value=stub):
            dup = admin.post("/api/cards", json={
                "card_key": "POOL_ADD_1",
                "card_provider": "efuncard",
                "target_warmup_count": 0,
            })
        self.assertEqual(dup.status_code, 409)

        pool = admin.get("/api/cards/pool")
        self.assertEqual(pool.status_code, 200)
        self.assertEqual(pool.json()[0]["card_key"], "POOL_ADD_1")
        ready = admin.get("/api/cards/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()[0]["card_key"], "POOL_ADD_1")

    def test_card_pool_add_card_auth_csrf_and_sanitized_errors(self):
        admin = self._new_client()
        self._login_admin(admin)

        anonymous = self._new_client()
        r = anonymous.post("/api/cards", json={
            "card_key": "AUTH_1",
            "card_provider": "efuncard",
            "target_warmup_count": 0,
        })
        self.assertEqual(r.status_code, 401)

        operator = self._new_client()
        self._login_operator(operator)
        r = operator.post("/api/cards", json={
            "card_key": "AUTH_2",
            "card_provider": "efuncard",
            "target_warmup_count": 0,
        })
        self.assertEqual(r.status_code, 403)

        admin_no_csrf = self._new_client()
        self._login_admin(admin_no_csrf)
        admin_no_csrf.headers.pop("X-CSRF-Token", None)
        r = admin_no_csrf.post("/api/cards", json={
            "card_key": "AUTH_3",
            "card_provider": "efuncard",
            "target_warmup_count": 0,
        })
        self.assertEqual(r.status_code, 403)

        class FailingCardApi:
            def get_card(self, card_key):
                raise RuntimeError(
                    "bad 4242424242424242 cvv=123 sms_api=https://sms.example/secret proxy=http://proxy"
                )

        with mock.patch("src.api.routes.cards._build_card_api", return_value=FailingCardApi()):
            r = admin.post("/api/cards", json={
                "card_key": "ERR_1",
                "card_provider": "efuncard",
                "target_warmup_count": 0,
            })
        self.assertEqual(r.status_code, 502)
        detail = r.json()["detail"]
        self.assertEqual(detail, "卡商接口错误")
        for secret in ("4242424242424242", "123", "sms_api", "proxy", "https://sms.example"):
            self.assertNotIn(secret, detail)

    def test_card_api_constructor_arguments_match_clients(self):
        from src.api.routes.cards import _build_card_api
        from src.efuncard import EfunCard
        from src.nodecard import NodeCard
        from src.x988card import X988Card

        with mock.patch.dict(os.environ, {
            "EFUNCARD_TOKEN": "test-token",
            "NODECARD_API_URL": "https://node.example",
            "NODECARD_MERCHANT_ID": "123",
            "NODECARD_PLATFORM_ID": "456",
            "X988CARD_API_BASE": "https://x988.example",
            "X988CARD_REQUEST_TIMEOUT": "9",
        }, clear=False):
            self.assertIsInstance(_build_card_api("efuncard"), EfunCard)
            node = _build_card_api("nodecard")
            self.assertIsInstance(node, NodeCard)
            self.assertEqual(node._base_url, "https://node.example")
            self.assertEqual(node._merchant_dict_id, 123)
            self.assertEqual(node._platform_id, 456)
            x988 = _build_card_api("x988card")
            self.assertIsInstance(x988, X988Card)
            self.assertEqual(x988._base_url, "https://x988.example")
            self.assertEqual(x988._request_timeout, 9)

    # ── 合成卡 A2 + A3 测试（审计落表 + 姓名注入 + feedback 闭环）─────

    def test_synthetic_visa_generate_returns_audit_id_and_persists_record(self):
        """A2: POST /api/cards/synthetic-visa 应落 SyntheticCardAudit 记录"""
        from src.db.engine import get_session
        from src.db.models import SyntheticCardAudit
        from sqlmodel import select

        admin = self._new_client()
        self._login_admin(admin)
        r = admin.post("/api/cards/synthetic-visa", json={})
        self.assertEqual(r.status_code, 200, r.text)
        payload = r.json()
        # 返回 audit_id
        self.assertIn("audit_id", payload)
        self.assertTrue(payload["audit_id"], "audit_id 必须非空")
        audit_id = payload["audit_id"]
        # DB 落了一条 pending 记录
        with get_session() as s:
            audit = s.get(SyntheticCardAudit, audit_id)
            self.assertIsNotNone(audit)
            self.assertEqual(audit.feedback_status, "pending")
            self.assertIn(audit.bin_prefix, ("4147", "4100"))
            self.assertEqual(len(audit.last_four), 4)
            # 不存完整卡号
            self.assertEqual(len(audit.last_four), 4)
            self.assertEqual(audit.created_by, "admin")

    def test_synthetic_visa_with_name_override_uses_account_name(self):
        """A3: 传 first_name/last_name 时持卡人姓名必须是注入值"""
        admin = self._new_client()
        self._login_admin(admin)
        r = admin.post("/api/cards/synthetic-visa", json={
            "first_name": "John",
            "last_name": "Doe",
        })
        self.assertEqual(r.status_code, 200, r.text)
        payload = r.json()
        self.assertEqual(payload["first_name"], "John")
        self.assertEqual(payload["last_name"], "Doe")
        self.assertEqual(payload["full_name"], "John Doe")

    def test_synthetic_visa_rejects_unsafe_names(self):
        """A3: 注入姓名做白名单校验，拒绝特殊字符 / SQL 注入"""
        admin = self._new_client()
        self._login_admin(admin)
        for bad in ["John<script>", "Doe; DROP TABLE", "John1", "'OR'1'='1"]:
            r = admin.post("/api/cards/synthetic-visa", json={
                "first_name": bad,
                "last_name": "Doe",
            })
            self.assertEqual(r.status_code, 400, f"应拒绝 {bad!r}: {r.text}")

    def test_synthetic_visa_feedback_success_and_stats(self):
        """A2: 反馈 success 后 stats 端点正确聚合"""
        admin = self._new_client()
        self._login_admin(admin)
        # 生成 3 张卡
        ids = []
        for _ in range(3):
            r = admin.post("/api/cards/synthetic-visa", json={})
            ids.append(r.json()["audit_id"])
        # 反馈：2 success / 1 declined
        r = admin.post(f"/api/cards/synthetic-visa/{ids[0]}/feedback", json={"status": "success"})
        self.assertEqual(r.status_code, 200)
        r = admin.post(f"/api/cards/synthetic-visa/{ids[1]}/feedback", json={"status": "success"})
        self.assertEqual(r.status_code, 200)
        r = admin.post(f"/api/cards/synthetic-visa/{ids[2]}/feedback", json={
            "status": "declined", "decline_code": "do_not_honor",
        })
        self.assertEqual(r.status_code, 200)
        # 查 stats
        r = admin.get("/api/cards/synthetic-visa/stats")
        self.assertEqual(r.status_code, 200, r.text)
        stats = r.json()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["success"], 2)
        self.assertEqual(stats["declined"], 1)
        self.assertEqual(stats["pending"], 0)

    def test_synthetic_visa_feedback_rejects_duplicate(self):
        """A2: 同一审计记录已反馈后再反馈应 409"""
        admin = self._new_client()
        self._login_admin(admin)
        r = admin.post("/api/cards/synthetic-visa", json={})
        audit_id = r.json()["audit_id"]
        r = admin.post(f"/api/cards/synthetic-visa/{audit_id}/feedback", json={"status": "success"})
        self.assertEqual(r.status_code, 200)
        # 重复反馈
        r = admin.post(f"/api/cards/synthetic-visa/{audit_id}/feedback", json={"status": "declined"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_synthetic_visa_feedback_rejects_invalid_status(self):
        admin = self._new_client()
        self._login_admin(admin)
        r = admin.post("/api/cards/synthetic-visa", json={})
        audit_id = r.json()["audit_id"]
        r = admin.post(f"/api/cards/synthetic-visa/{audit_id}/feedback", json={"status": "unknown"})
        self.assertEqual(r.status_code, 400)

    def test_synthetic_visa_feedback_404_for_missing_audit(self):
        admin = self._new_client()
        self._login_admin(admin)
        r = admin.post("/api/cards/synthetic-visa/does-not-exist/feedback", json={"status": "success"})
        self.assertEqual(r.status_code, 404)

    def test_synthetic_visa_endpoints_require_admin(self):
        """A2/A3: 非 admin 角色全部拒绝"""
        operator = self._new_client()
        self._login_operator(operator)
        for path in [
            "/api/cards/synthetic-visa",
            "/api/cards/synthetic-visa/some-id/feedback",
        ]:
            r = operator.post(path, json={"status": "success"})
            self.assertEqual(r.status_code, 403, f"{path} 非 admin 应拒绝")
        r = operator.get("/api/cards/synthetic-visa/stats")
        self.assertEqual(r.status_code, 403)

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

    def test_account_pool_export_returns_full_json_with_credentials(self):
        """admin 调用 /api/accounts/export?fmt=full_json → 200 + JSON 含 password + OAuth。

        注：从这一版起 CSV 已从导出菜单移除（CSV 表达力不够嵌套结构），
        换成 full_json，字段集等价于「账号凭证组 + 令牌组」。
        """
        import json as _json
        from src.db.models import MailAccount

        # 造一条 registered + 关联 MailAccount
        with get_session() as s:
            s.add(Run(
                id="r" + "1" * 31, email="exp@x.com", password="secret-pw",
                status="success", phase="token_extraction",
                account_tier="registered", mail_provider="applemail",
            ))
            s.add(MailAccount(
                label="exp", provider_name="applemail", email="exp@x.com",
                client_id="cid-zz", refresh_token="rt-zz",
            ))
            s.commit()

        client = self._new_client()
        self._login_admin(client)
        response = client.get("/api/accounts/export?tier=registered&fmt=full_json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        # 单账号 → JSON object（不是数组），文件名 codex-{email}.json
        self.assertIn("codex-exp@x.com", response.headers["content-disposition"])
        item = _json.loads(response.text)
        self.assertIsInstance(item, dict)
        self.assertEqual(item["email"], "exp@x.com")
        self.assertEqual(item["password"], "secret-pw")
        self.assertEqual(item["client_id"], "cid-zz")
        self.assertEqual(item["refresh_token"], "rt-zz")  # OAuth refresh

        # CSV 已从导出菜单移除 → fmt=credentials_csv 应被 422 拒绝
        response_csv = client.get("/api/accounts/export?tier=registered&fmt=credentials_csv")
        self.assertEqual(response_csv.status_code, 422)

        # viewer 不能导出（admin only）
        client_v = self._new_client()
        self._login_viewer(client_v)
        resp = client_v.get("/api/accounts/export?tier=registered&fmt=full_json")
        self.assertEqual(resp.status_code, 403)

    def test_account_pool_import_creates_registered_runs(self):
        """admin 上传 CSV → 新 Run(status=success, tier=registered) + 跳过重复邮箱。"""
        # 已存在的号
        with get_session() as s:
            s.add(Run(
                id="d" * 32, email="dup@x.com", password="x",
                status="success", account_tier="registered", phase="token_extraction",
            ))
            s.commit()

        csv_content = (
            "email,password,mail_provider,client_id,refresh_token,account_tier,created_at\n"
            "dup@x.com,xx,applemail,c,r,registered,\n"
            "fresh@x.com,pw,applemail,c2,r2,registered,\n"
        )

        client = self._new_client()
        self._login_admin(client)
        response = client.post(
            "/api/accounts/import",
            files={"file": ("pool.csv", csv_content, "text/csv")},
            data={"fmt": "credentials_csv"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["imported"], 1)
        self.assertEqual(data["skipped"], 1)

        with get_session() as s:
            emails = sorted(r.email for r in s.query(Run).all())
            self.assertIn("fresh@x.com", emails)
            self.assertIn("dup@x.com", emails)

        # viewer 无权
        client_v = self._new_client()
        self._login_viewer(client_v)
        resp = client_v.post(
            "/api/accounts/import",
            files={"file": ("p.csv", "email\nx@y.com\n", "text/csv")},
            data={"fmt": "credentials_csv"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_generate_link_endpoint_admin_only_and_pro_supported(self):
        """新 generate-link 端点：admin 可调 + Pro plan 透传。"""
        with get_session() as s:
            s.add(Run(
                id="L" + "1" * 31, email="link@x.com", password="x",
                status="success", phase="token_extraction",
                account_tier="registered",
                config_snapshot={"access_token": "tok-link"},
            ))
            s.commit()

        # viewer 没权限
        cv = self._new_client()
        self._login_viewer(cv)
        r = cv.post("/api/accounts/L" + "1" * 31 + "/generate-link",
                    json={"plan": "team", "return_mode": "long"})
        self.assertEqual(r.status_code, 403)

        # admin 调 + Pro plan 透传
        client = self._new_client()
        self._login_admin(client)
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/pro"),
        ) as mock_gen:
            resp = client.post(
                "/api/accounts/L" + "1" * 31 + "/generate-link",
                json={
                    "plan": "pro",
                    "return_mode": "long",
                    "promo_code": "datroaiuk",
                    "seat_quantity": 1,
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["link"], "https://checkout.example/pro")
        self.assertEqual(body["plan"], "pro")
        # 验证 PaymentLinkGenerator 收到的参数
        kwargs = mock_gen.call_args.kwargs
        self.assertEqual(kwargs["plan_type"], "pro")
        self.assertEqual(kwargs["promo_code"], "datroaiuk")

    def test_checkout_link_standalone_admin_can_generate(self):
        """POST /api/accounts/checkout-link：admin 提供 access_token → 返回 link。"""
        client = self._new_client()
        self._login_admin(client)
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/standalone"),
        ) as mock_gen:
            resp = client.post("/api/accounts/checkout-link", json={
                "access_token": "user-supplied-tok",
                "plan": "team",
                "return_mode": "long",
                "seat_quantity": 2,
                "promo_code": "datroaiuk",
            })
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["link"], "https://checkout.example/standalone")
        self.assertEqual(body["plan"], "team")
        self.assertEqual(body["return_mode"], "long")
        # 验证 PaymentLinkGenerator 收到的 access_token 来自前端而非 DB
        positional_args = mock_gen.call_args.args
        self.assertEqual(positional_args[0], "user-supplied-tok")
        kwargs = mock_gen.call_args.kwargs
        self.assertEqual(kwargs["plan_type"], "team")
        self.assertEqual(kwargs["promo_code"], "datroaiuk")
        self.assertEqual(kwargs["seat_quantity"], 2)

    def test_checkout_link_standalone_missing_token_rejected(self):
        """POST /api/accounts/checkout-link：access_token 为空 → 422 (Pydantic min_length)。"""
        client = self._new_client()
        self._login_admin(client)
        resp = client.post("/api/accounts/checkout-link", json={
            "access_token": "",
            "plan": "team",
        })
        # Pydantic min_length=1 校验失败返回 422
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_checkout_link_standalone_invalid_plan(self):
        """POST /api/accounts/checkout-link：plan='garbage' → 400（service 层 ValueError）。"""
        client = self._new_client()
        self._login_admin(client)
        resp = client.post("/api/accounts/checkout-link", json={
            "access_token": "tok-x",
            "plan": "garbage_plan",
            "return_mode": "long",
        })
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("plan", resp.json()["detail"].lower())

    def test_checkout_link_standalone_payment_gen_failure(self):
        """PaymentLinkGenerator 返回 (False, error_msg) → 端点返回 400。"""
        client = self._new_client()
        self._login_admin(client)
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(False, "OpenAI API 返回 401"),
        ):
            resp = client.post("/api/accounts/checkout-link", json={
                "access_token": "bad-tok",
                "plan": "team",
                "return_mode": "long",
            })
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("401", resp.json()["detail"])

    def test_checkout_link_standalone_viewer_forbidden(self):
        """viewer 角色访问 → 403。"""
        client = self._new_client()
        self._login_viewer(client)
        resp = client.post("/api/accounts/checkout-link", json={
            "access_token": "tok-x",
            "plan": "team",
        })
        self.assertEqual(resp.status_code, 403)

    def test_checkout_link_standalone_unauthenticated(self):
        """未登录 → 401。"""
        client = self._new_client(with_csrf=False)
        resp = client.post("/api/accounts/checkout-link", json={
            "access_token": "tok-x",
            "plan": "team",
        })
        self.assertIn(resp.status_code, (401, 403))

    def test_checkout_link_page_route_renders(self):
        """GET /checkout-link：admin 登录后页面能渲染（200 + 含标题）。"""
        client = self._new_client()
        self._login_admin(client)
        resp = client.get("/checkout-link")
        self.assertEqual(resp.status_code, 200, resp.text)
        # 模板渲染产物应含独立页关键标识
        self.assertIn("生成 Checkout", resp.text)
        # access_token 输入应存在
        self.assertIn("Access Token", resp.text)
        # 应走 standalone endpoint
        self.assertIn("/api/accounts/checkout-link", resp.text)

    def test_checkout_link_page_inlines_country_data_and_i18n(self):
        """checkout_link 页面应引用 country data 脚本 + 内联 i18n 翻译表 + 含 combobox 工厂。"""
        client = self._new_client()
        self._login_admin(client)
        resp = client.get("/checkout-link")
        self.assertEqual(resp.status_code, 200)
        # 必须引用 data 脚本（含 60 国 + 联动数据）
        self.assertIn("/static/js/checkout-link-data.js", resp.text)
        # 必须内联 _translations 让 Alpine 能读到本地化
        self.assertIn("window.I18N", resp.text)
        # combobox 应使用 inline x-data（避免 $root 拿不到父组件方法）
        self.assertIn("countryButtonLabel", resp.text)
        self.assertIn("currencyButtonLabel", resp.text)
        # 联动方法应存在
        self.assertIn("applyCountryToCurrency", resp.text)

    def test_checkout_link_data_js_served_with_60_countries(self):
        """/static/js/checkout-link-data.js 应作为静态资源可访问 + 含 60 国数据 + 联动 map。"""
        client = self._new_client()
        self._login_admin(client)
        resp = client.get("/static/js/checkout-link-data.js")
        self.assertEqual(resp.status_code, 200, resp.text)
        # 关键结构应存在
        self.assertIn("CheckoutLinkData", resp.text)
        self.assertIn("countryToCurrency", resp.text)
        # 抽几个关键国家校验数据完整
        for code in ("US", "JP", "DE", "BR", "AE", "ZA"):
            self.assertIn(f"code: '{code}'", resp.text)

    def test_link_templates_crud_admin_only(self):
        """链接模板 CRUD 全流程：list 空 → create → list 含一条 → delete → list 空。"""
        # viewer 无权 list
        cv = self._new_client()
        self._login_viewer(cv)
        r = cv.get("/api/link-templates")
        self.assertEqual(r.status_code, 403)

        client = self._new_client()
        self._login_admin(client)

        # 初始为空
        r = client.get("/api/link-templates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

        # 创建
        r = client.post("/api/link-templates", json={
            "name": "UK promo datroaiuk",
            "plan": "team",
            "seat_quantity": 2,
            "promo_code": "datroaiuk",
            "aimizy_country": "GB",
            "aimizy_currency": "GBP",
        })
        self.assertEqual(r.status_code, 200, r.text)
        tpl = r.json()
        self.assertEqual(tpl["name"], "UK promo datroaiuk")
        self.assertEqual(tpl["plan"], "team")
        self.assertEqual(tpl["promo_code"], "datroaiuk")
        template_id = tpl["id"]

        # 同名再建 → 400
        r = client.post("/api/link-templates", json={"name": "UK promo datroaiuk", "plan": "plus"})
        self.assertEqual(r.status_code, 400)

        # 列表含一条
        r = client.get("/api/link-templates")
        self.assertEqual(len(r.json()), 1)

        # 删除
        r = client.delete(f"/api/link-templates/{template_id}")
        self.assertEqual(r.status_code, 200)

        # 删不存在 → 404
        r = client.delete(f"/api/link-templates/{template_id}")
        self.assertEqual(r.status_code, 404)

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
