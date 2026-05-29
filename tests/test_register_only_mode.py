# -*- coding: utf-8 -*-
"""纯注册任务模式测试（mode=register_only）。

覆盖：
  - CreateTaskRequest 的 mode 字段默认 "full"，接受 "register_only"，拒绝其他值
  - config_snapshot 持久化 task_mode
  - worker 的 _resolve_runtime_config 看到 task_mode=register_only 时
    把 config.enable_payment_flow 强制设为 False
  - _run_to_dict 透出 task_mode
"""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.db.engine as engine_mod
from src.api.routes.tasks import CreateTaskRequest, _run_to_dict
from src.db.models import Run


def _build_threadsafe_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class CreateTaskRequestModeTest(unittest.TestCase):
    """请求模型 mode 字段校验。"""

    def test_default_mode_is_full(self):
        req = CreateTaskRequest(password="x", profile_id="p")
        self.assertEqual(req.mode, "full")

    def test_register_only_accepted(self):
        req = CreateTaskRequest(password="x", profile_id="p", mode="register_only")
        self.assertEqual(req.mode, "register_only")

    def test_full_explicitly_accepted(self):
        req = CreateTaskRequest(password="x", profile_id="p", mode="full")
        self.assertEqual(req.mode, "full")

    def test_arbitrary_string_passes_pydantic(self):
        # mode 是 str，pydantic 不做枚举校验；
        # endpoint 内部会校验 + 抛 422，本测试不覆盖 endpoint 行为
        req = CreateTaskRequest(password="x", profile_id="p", mode="anything")
        self.assertEqual(req.mode, "anything")


class RunToDictTaskModeTest(unittest.TestCase):
    """_run_to_dict 透出 task_mode（默认 "full"）。"""

    def _make_run(self, snapshot: dict | None = None) -> Run:
        now = datetime.now(timezone.utc)
        return Run(
            id="abc" + "x" * 13,
            email="e@x.com",
            password="p",
            profile_id="prof",
            status="pending",
            phase="registration",
            config_snapshot=snapshot or {},
            created_at=now,
            updated_at=now,
        )

    def test_default_full_when_no_snapshot(self):
        run = self._make_run(snapshot={})
        self.assertEqual(_run_to_dict(run)["task_mode"], "full")

    def test_register_only_from_snapshot(self):
        run = self._make_run(snapshot={"task_mode": "register_only"})
        self.assertEqual(_run_to_dict(run)["task_mode"], "register_only")

    def test_unknown_value_passes_through(self):
        # _run_to_dict 不做校验（创建时已校验过），原样透出
        run = self._make_run(snapshot={"task_mode": "weird"})
        self.assertEqual(_run_to_dict(run)["task_mode"], "weird")


class WorkerResolveRuntimeConfigTest(unittest.TestCase):
    """worker._resolve_runtime_config 根据 task_mode 改 enable_payment_flow。"""

    def setUp(self):
        # 用 in-memory + StaticPool 替换 engine
        self.engine = _build_threadsafe_engine()
        self._original_engine = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original_engine

    def _seed_run(self, *, snapshot: dict, run_id: str = "test" + "x" * 12) -> Run:
        with Session(self.engine) as s:
            run = Run(
                id=run_id,
                email="e@x.com",
                password="pw",
                profile_id="p",
                status="pending",
                phase="registration",
                config_snapshot=snapshot,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            s.expunge(run)
            return run

    def _build_fake_config(self, *, enable_payment_flow=True):
        cfg = mock.MagicMock()
        cfg.enable_payment_flow = enable_payment_flow
        cfg.default_browser_provider = ""
        cfg.default_card_provider = ""
        cfg.default_mail_provider = ""
        cfg.default_mail_account_id = ""
        cfg.email_provider_name = ""
        cfg.proxy = ""
        return cfg

    def test_register_only_disables_payment_flow(self):
        from src.api import worker

        run = self._seed_run(snapshot={"task_mode": "register_only"})
        cfg = self._build_fake_config(enable_payment_flow=True)

        # mock get_config_service 返回带 svc.get_config() 的对象
        fake_svc = mock.MagicMock()
        fake_svc.get_config.return_value = cfg
        fake_svc.resolve_provider_config.return_value = None
        fake_svc.get_mail_account.return_value = None
        with mock.patch("src.api.deps.get_config_service", return_value=fake_svc):
            result = worker._resolve_runtime_config(run)

        self.assertFalse(result.enable_payment_flow,
                         "register_only 任务必须把 enable_payment_flow 强制设为 False")

    def test_full_mode_keeps_payment_flow_default(self):
        from src.api import worker

        run = self._seed_run(snapshot={"task_mode": "full"})
        cfg = self._build_fake_config(enable_payment_flow=True)
        fake_svc = mock.MagicMock()
        fake_svc.get_config.return_value = cfg
        fake_svc.resolve_provider_config.return_value = None
        fake_svc.get_mail_account.return_value = None
        with mock.patch("src.api.deps.get_config_service", return_value=fake_svc):
            result = worker._resolve_runtime_config(run)
        self.assertTrue(result.enable_payment_flow,
                        "full 模式应当保留原 enable_payment_flow")

    def test_missing_task_mode_defaults_to_full(self):
        """老任务（没有 task_mode 字段）应被当成 full 处理。"""
        from src.api import worker

        run = self._seed_run(snapshot={})  # 空 snapshot，没 task_mode
        cfg = self._build_fake_config(enable_payment_flow=True)
        fake_svc = mock.MagicMock()
        fake_svc.get_config.return_value = cfg
        fake_svc.resolve_provider_config.return_value = None
        fake_svc.get_mail_account.return_value = None
        with mock.patch("src.api.deps.get_config_service", return_value=fake_svc):
            result = worker._resolve_runtime_config(run)
        self.assertTrue(result.enable_payment_flow,
                        "缺 task_mode 应保留原 enable_payment_flow")


if __name__ == "__main__":
    unittest.main()
