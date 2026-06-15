# -*- coding: utf-8 -*-
"""retry restart 脏号换号自愈测试。

覆盖 src/api/routes/tasks.py 的修复：
- managed provider（cfworker / outlook_email_plus 等号商随机分配号）的脏号在
  retry restart 时清空 run.email，让 worker 重新 claim-random 拿新号。
- credentialed provider（applemail 等用户绑定固定号）保持 email 不变。
- resume 模式不换号（续跑同号）。

判据核心 _is_managed_auto_allocated_mail 通过 ConfigService 解析 session_mode，
与任务创建 preflight / worker 运行时解析同一套语义。
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
from src.db.models import ProviderConfig, Run
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


class TestRetryEmailReuse(unittest.TestCase):
    """retry restart 脏号换号自愈。"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {
            key: os.environ.get(key)
            for key in [
                "DATABASE_URL", "SESSION_SECRET",
                "ADMIN_USERNAME", "ADMIN_PASSWORD",
                "OPERATOR_USERNAME", "OPERATOR_PASSWORD",
            ]
        }
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "admin123456"
        os.environ["OPERATOR_USERNAME"] = "operator"
        os.environ["OPERATOR_PASSWORD"] = "operator123"

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
        # 注册一个 managed mail provider 配置供判据解析
        with get_session() as s:
            s.add(ProviderConfig(
                provider_type="mail",
                provider_name="mail-outlook-default",
                config={"provider_name": "outlook_email_plus", "session_mode": "managed",
                        "config_name": "outlook-pool-default"},
                is_active=True,
            ))
            s.commit()

    def tearDown(self):
        shutdown_workers(wait=True)

    def _new_operator_client(self) -> TestClient:
        client = TestClient(self.app)
        client.headers.update({
            "Origin": "http://testserver",
            "Referer": "http://testserver/",
        })
        # 先 GET /api/auth/me 拿匿名 CSRF（CSRF 中间件对 POST 必校验）
        r = client.get("/api/auth/me")
        token = r.json().get("csrf_token", "")
        if token:
            client.headers.update({"X-CSRF-Token": token})
        r = client.post("/api/auth/login", json={
            "username": "operator", "password": "operator123", "next": "/",
        })
        assert r.status_code == 200, r.text
        token = r.json().get("csrf_token", "")
        if token:
            client.headers.update({"X-CSRF-Token": token})
        return client

    def _make_failed_run(self, *, email: str, mail_provider: str) -> str:
        with get_session() as s:
            run = Run(
                email=email,
                profile_id="prof-test",
                status="failed",
                phase="registration",
                mail_provider=mail_provider,
                error_reason="silent_failure_at_state=VERIFY_EMAIL",
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            return run.id

    # ── 判据单元测试 ──────────────────────────────────

    def test_judgment_managed_provider_is_replaceable(self):
        from src.api.routes.tasks import _is_managed_auto_allocated_mail
        self.assertTrue(_is_managed_auto_allocated_mail("mail-outlook-default"))

    def test_judgment_applemail_not_replaceable(self):
        from src.api.routes.tasks import _is_managed_auto_allocated_mail
        self.assertFalse(_is_managed_auto_allocated_mail("applemail"))

    def test_judgment_unknown_provider_not_replaceable(self):
        from src.api.routes.tasks import _is_managed_auto_allocated_mail
        self.assertFalse(_is_managed_auto_allocated_mail("no-such-config"))

    def test_judgment_empty_not_replaceable(self):
        from src.api.routes.tasks import _is_managed_auto_allocated_mail
        self.assertFalse(_is_managed_auto_allocated_mail(""))

    # ── retry 行为集成测试 ────────────────────────────

    def test_restart_clears_managed_dirty_email(self):
        """managed 脏号 restart → 不再是原脏号（被清空，worker 异步重新分配新号）。

        路由内同步将 run.email 清空；返回的 result 反映清空后的瞬时态。worker 随后
        异步 requeue 可能已重新 claim-random 填上新号，故断言"不等于原脏号"而非
        "恒为空"——两者都满足"换号"语义，且不受 worker 异步时序影响。
        """
        dirty = "DirtyName1225@outlook.com"
        run_id = self._make_failed_run(email=dirty, mail_provider="mail-outlook-default")
        client = self._new_operator_client()
        resp = client.post(f"/api/tasks/{run_id}/retry", json={"mode": "restart"})
        self.assertEqual(resp.status_code, 200, resp.text)
        # 路由返回体是清空后、worker 异步分配前的瞬时态：email 应为空。
        self.assertEqual(resp.json().get("email", ""), "", "retry 返回体应反映已清空脏号")
        # DB 最终态：要么仍空、要么已被 worker 换成新号——总之不再是原脏号。
        with get_session() as s:
            run = s.get(Run, run_id)
            self.assertNotEqual(run.email, dirty, "managed 脏号 restart 后不应继续复用原脏号")

    def test_restart_keeps_credentialed_email(self):
        """credentialed 固定号 restart → email 保留（不可换指定号）。"""
        run_id = self._make_failed_run(
            email="bound.user@outlook.com", mail_provider="applemail"
        )
        client = self._new_operator_client()
        resp = client.post(f"/api/tasks/{run_id}/retry", json={"mode": "restart"})
        self.assertEqual(resp.status_code, 200, resp.text)
        with get_session() as s:
            run = s.get(Run, run_id)
            self.assertEqual(run.email, "bound.user@outlook.com", "credentialed 固定号不应被换")

    def test_resume_keeps_email(self):
        """resume 模式 → 续跑同号，email 不变（即便是 managed）。"""
        run_id = self._make_failed_run(
            email="KeepMe99@outlook.com", mail_provider="mail-outlook-default"
        )
        client = self._new_operator_client()
        resp = client.post(f"/api/tasks/{run_id}/retry", json={"mode": "resume"})
        self.assertEqual(resp.status_code, 200, resp.text)
        with get_session() as s:
            run = s.get(Run, run_id)
            self.assertEqual(run.email, "KeepMe99@outlook.com", "resume 不应换号")


if __name__ == "__main__":
    unittest.main()
