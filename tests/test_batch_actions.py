# -*- coding: utf-8 -*-
"""批量操作 & 单删 endpoints 测试。

覆盖：
- DELETE /api/tasks/{id}: 成功删除 + RunEvent 级联 / running 状态 409
- POST /api/tasks/batch-actions: cancel / retry / delete 三种 action
  + skipped 行为（not_found / not_cancellable / not_retryable / running）
  + 无效 action / 空 task_ids 返回 422
"""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.db.engine as engine_mod
from src.api import deps
from src.api.worker import shutdown_workers
from src.db.engine import get_session
from src.db.models import Run, RunEvent
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


class TestBatchActions(unittest.TestCase):
    """批量动作端点测试。"""

    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: os.environ.get(key) for key in [
            "DATABASE_URL", "SESSION_SECRET",
            "ADMIN_USERNAME", "ADMIN_PASSWORD",
            "OPERATOR_USERNAME", "OPERATOR_PASSWORD",
        ]}
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

    def tearDown(self):
        shutdown_workers(wait=True)

    # ── helpers ──────────────────────────────────────

    def _new_operator_client(self) -> TestClient:
        client = TestClient(self.app)
        client.headers.update({
            "Origin": "http://testserver",
            "Referer": "http://testserver/",
        })
        # 先 GET /api/auth/me 拿到匿名 CSRF（CSRF 中间件需要）
        r = client.get("/api/auth/me")
        token = r.json().get("csrf_token", "")
        if token:
            client.headers.update({"X-CSRF-Token": token})
        # 登录 operator
        r = client.post("/api/auth/login", json={
            "username": "operator", "password": "operator123", "next": "/",
        })
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json().get("csrf_token", "")
        self.assertTrue(token)
        client.headers.update({"X-CSRF-Token": token})
        return client

    def _seed_run(self, status: str = "failed", run_id: str | None = None) -> str:
        """直接写一条 Run，返回 id。"""
        with get_session() as session:
            run = Run(
                email=f"{status}@example.com",
                password="pw",
                profile_id="profile-1",
                status=status,
                phase="registration",
            )
            if run_id:
                run.id = run_id
            session.add(run)
            session.commit()
            session.refresh(run)
            return run.id

    def _seed_event(self, run_id: str) -> None:
        with get_session() as session:
            session.add(RunEvent(
                run_id=run_id,
                event_type="state_change",
                state="ENTRY",
                timestamp=datetime.now(timezone.utc),
            ))
            session.commit()

    # ── DELETE /api/tasks/{id} ────────────────────────

    def test_delete_single_task_cascades_run_event(self):
        run_id = self._seed_run(status="failed")
        self._seed_event(run_id)
        client = self._new_operator_client()

        r = client.delete(f"/api/tasks/{run_id}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

        from sqlmodel import select
        with get_session() as session:
            self.assertIsNone(session.get(Run, run_id))
            events = session.exec(select(RunEvent).where(RunEvent.run_id == run_id)).all()
            self.assertEqual(len(events), 0)

    def test_delete_running_task_rejected(self):
        """运行中任务不可删（与上游 delete_task 一致：仅终态可删）。"""
        run_id = self._seed_run(status="running")
        client = self._new_operator_client()

        r = client.delete(f"/api/tasks/{run_id}")
        self.assertEqual(r.status_code, 400, r.text)

    def test_delete_pending_task_rejected(self):
        """pending 任务也不可删（非终态）。"""
        run_id = self._seed_run(status="pending")
        client = self._new_operator_client()
        r = client.delete(f"/api/tasks/{run_id}")
        self.assertEqual(r.status_code, 400, r.text)

    def test_delete_not_found_returns_404(self):
        client = self._new_operator_client()
        r = client.delete("/api/tasks/non-existent-id")
        self.assertEqual(r.status_code, 404)

    # ── POST /api/tasks/batch-actions ─────────────────

    def test_batch_cancel_mixed_status(self):
        running_id = self._seed_run(status="running")
        pending_id = self._seed_run(status="pending")
        success_id = self._seed_run(status="success")
        client = self._new_operator_client()

        with mock.patch("src.api.routes.tasks.request_task_cancel") as mock_cancel:
            r = client.post("/api/tasks/batch-actions", json={
                "action": "cancel",
                "task_ids": [running_id, pending_id, success_id],
            })

        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(set(data["succeeded"]), {running_id, pending_id})
        self.assertEqual(len(data["skipped"]), 1)
        self.assertEqual(data["skipped"][0]["id"], success_id)
        self.assertEqual(data["skipped"][0]["reason"], "not_cancellable")
        # request_task_cancel 应该只对 succeeded 调用
        self.assertEqual(mock_cancel.call_count, 2)

    def test_batch_delete_skips_non_terminal(self):
        """批量 delete 与单删一致：仅 success/failed/cancelled 可删，running/pending 跳过。"""
        failed_id = self._seed_run(status="failed")
        running_id = self._seed_run(status="running")
        pending_id = self._seed_run(status="pending")
        self._seed_event(failed_id)
        client = self._new_operator_client()

        r = client.post("/api/tasks/batch-actions", json={
            "action": "delete",
            "task_ids": [failed_id, running_id, pending_id],
        })
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["succeeded"], [failed_id])
        skipped_by_id = {item["id"]: item["reason"] for item in data["skipped"]}
        self.assertEqual(skipped_by_id[running_id], "running")
        self.assertEqual(skipped_by_id[pending_id], "not_terminal")

        # 确认 failed_id 已删，running/pending 仍在
        with get_session() as session:
            self.assertIsNone(session.get(Run, failed_id))
            self.assertIsNotNone(session.get(Run, running_id))
            self.assertIsNotNone(session.get(Run, pending_id))

    def test_batch_retry_calls_requeue_runs(self):
        failed_id = self._seed_run(status="failed")
        cancelled_id = self._seed_run(status="cancelled")
        success_id = self._seed_run(status="success")
        client = self._new_operator_client()

        with mock.patch("src.services.batch_register_service.requeue_runs",
                        return_value={"requeued": 2}) as mock_requeue, \
             mock.patch("src.api.worker._clear_cancel_requested") as mock_clear:
            r = client.post("/api/tasks/batch-actions", json={
                "action": "retry",
                "task_ids": [failed_id, cancelled_id, success_id],
            })

        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(set(data["succeeded"]), {failed_id, cancelled_id})
        self.assertEqual(data["skipped"][0]["id"], success_id)
        self.assertEqual(data["skipped"][0]["reason"], "not_retryable")
        # requeue_runs 应该被调用一次，参数是 succeeded 列表
        mock_requeue.assert_called_once()
        args = mock_requeue.call_args.args
        self.assertEqual(set(args[0]), {failed_id, cancelled_id})
        # _clear_cancel_requested 对每个 succeeded 调一次
        self.assertEqual(mock_clear.call_count, 2)

    def test_batch_action_invalid_action_returns_422(self):
        run_id = self._seed_run(status="failed")
        client = self._new_operator_client()
        r = client.post("/api/tasks/batch-actions", json={
            "action": "explode",
            "task_ids": [run_id],
        })
        self.assertEqual(r.status_code, 422, r.text)

    def test_batch_action_empty_task_ids_returns_422(self):
        client = self._new_operator_client()
        r = client.post("/api/tasks/batch-actions", json={
            "action": "delete",
            "task_ids": [],
        })
        self.assertEqual(r.status_code, 422)

    def test_batch_action_dedupes_task_ids(self):
        run_id = self._seed_run(status="failed")
        client = self._new_operator_client()
        r = client.post("/api/tasks/batch-actions", json={
            "action": "delete",
            "task_ids": [run_id, run_id, run_id],
        })
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # 去重后 total=1
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["succeeded"], [run_id])


if __name__ == "__main__":
    unittest.main()
