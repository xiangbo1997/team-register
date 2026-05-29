# -*- coding: utf-8 -*-
"""worker._resolve_runtime_config 走 RegistrationProfile 的集成测试。

覆盖优先级路径：
  1. Run 字段（最高）
  2. config_snapshot.provider_overrides
  3. RegistrationProfile.provider_bindings
  4. AppConfig.default_*_provider（最低，兼容护栏）
"""

import os
import unittest
from unittest.mock import patch

from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.api.worker import _resolve_runtime_config
from src.db.engine import get_engine, get_session, init_db
from src.db.models import Run
from src.services.config_service import ConfigService
from src.services.registration_profile_service import RegistrationProfileService


def _reset_engine():
    engine_mod._engine = None


class TestWorkerWithProfiles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _reset_engine()
        os.environ["DATABASE_URL"] = "sqlite://"
        init_db()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATABASE_URL", None)
        _reset_engine()

    def setUp(self):
        SQLModel.metadata.drop_all(get_engine())
        SQLModel.metadata.create_all(get_engine())

        # 预置三组 provider 凭据，对应 default / alt 命名
        self.cfg_svc = ConfigService(dotenv_path="/tmp/__nonexistent__.env")
        self.cfg_svc.save_provider_config(
            "browser", "browser-default",
            {"driver": "adspower", "ads_api": "http://ads.default", "ads_api_key": "key-default"},
        )
        self.cfg_svc.save_provider_config(
            "browser", "browser-alt",
            {"driver": "adspower", "ads_api": "http://ads.alt", "ads_api_key": "key-alt"},
        )
        self.cfg_svc.save_provider_config(
            "card", "card-default",
            {"driver": "efuncard", "efuncard_token": "token-default"},
        )
        self.cfg_svc.save_provider_config(
            "card", "card-nodecard",
            {"driver": "nodecard", "nodecard_api_url": "https://node.alt", "nodecard_merchant_id": 99, "nodecard_platform_id": 88},
        )
        self.cfg_svc.save_provider_config(
            "mail", "mail-default",
            {"provider_name": "applemail", "session_mode": "managed"},
        )

        self.profile_svc = RegistrationProfileService(base_config=self.cfg_svc.get_config())

    def _create_run(self, *, config_snapshot=None, **fields) -> str:
        defaults = {
            "email": "test@example.com",
            "password": "pass",
            "profile_id": "prof-1",
            "browser_provider": "",
            "card_provider": "",
            "mail_provider": "",
            "config_snapshot": config_snapshot or {},
        }
        defaults.update(fields)
        with get_session() as session:
            run = Run(**defaults)
            session.add(run)
            session.commit()
            session.refresh(run)
            return run.id

    def _get_run(self, run_id: str) -> Run:
        with get_session() as session:
            return session.get(Run, run_id)

    # ── Path 1：Run 字段优先（不变） ────────────────────

    def test_run_field_wins_over_profile_binding(self):
        """Run.browser_provider 直接指定时，profile 的 binding 不应生效。"""
        self.profile_svc.create(
            name="email-default", registration_kind="email",
            provider_bindings={"browser": "browser-alt"},
            is_default=True,
        )
        run_id = self._create_run(
            browser_provider="browser-default",  # Run 字段显式
            config_snapshot={"registration_kind": "email"},
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        # Run 字段赢：ads_api 应该是 default 不是 alt
        self.assertEqual(cfg.ads_api, "http://ads.default")

    # ── Path 2：provider_overrides 在 profile 上面 ──────

    def test_overrides_beat_profile_binding(self):
        """config_snapshot.provider_overrides 优先于 profile bindings。"""
        self.profile_svc.create(
            name="email-default", registration_kind="email",
            provider_bindings={"browser": "browser-default"},
            is_default=True,
        )
        run_id = self._create_run(
            config_snapshot={
                "registration_kind": "email",
                "provider_overrides": {"browser": "browser-alt"},
            },
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        # overrides 赢
        self.assertEqual(cfg.ads_api, "http://ads.alt")
        self.assertEqual(cfg.ads_api_key, "key-alt")

    # ── Path 3：profile bindings 生效 ───────────────────

    def test_profile_binding_resolves_card_provider(self):
        """profile 指定 card=card-nodecard 时，cfg.card_provider 应为 nodecard。"""
        self.profile_svc.create(
            name="email-default", registration_kind="email",
            provider_bindings={"card": "card-nodecard"},
            is_default=True,
        )
        run_id = self._create_run(
            config_snapshot={"registration_kind": "email"},
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        self.assertEqual(cfg.card_provider, "nodecard")
        self.assertEqual(cfg.nodecard_api_url, "https://node.alt")
        self.assertEqual(cfg.nodecard_merchant_id, 99)

    def test_named_profile_used_when_specified(self):
        """config_snapshot.registration_profile_name 指定具体 profile 时使用之。"""
        self.profile_svc.create(
            name="email-default", registration_kind="email",
            provider_bindings={"browser": "browser-default"},
            is_default=True,
        )
        self.profile_svc.create(
            name="email-special", registration_kind="email",
            provider_bindings={"browser": "browser-alt"},
        )
        run_id = self._create_run(
            config_snapshot={
                "registration_kind": "email",
                "registration_profile_name": "email-special",
            },
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        self.assertEqual(cfg.ads_api, "http://ads.alt")  # email-special 赢

    def test_unknown_profile_name_falls_back_to_default(self):
        """指定不存在的 profile_name 时回退到该 kind 的默认 profile。"""
        self.profile_svc.create(
            name="email-default", registration_kind="email",
            provider_bindings={"browser": "browser-alt"},
            is_default=True,
        )
        run_id = self._create_run(
            config_snapshot={
                "registration_kind": "email",
                "registration_profile_name": "ghost-profile",
            },
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        # 回退到 email-default
        self.assertEqual(cfg.ads_api, "http://ads.alt")

    def test_phone_kind_uses_phone_default(self):
        """registration_kind=phone 应取 phone 类型的默认 profile。"""
        self.profile_svc.create(
            name="phone-default", registration_kind="phone",
            provider_bindings={"browser": "browser-alt"},
            is_default=True,
        )
        # 同时建一个 email-default 作为干扰
        self.profile_svc.create(
            name="email-default", registration_kind="email",
            provider_bindings={"browser": "browser-default"},
            is_default=True,
        )
        run_id = self._create_run(
            config_snapshot={"registration_kind": "phone"},
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        # 应取 phone-default → browser-alt
        self.assertEqual(cfg.ads_api, "http://ads.alt")

    # ── Path 4：完全没有 profile 时回退到 AppConfig.default ──

    def test_no_profile_falls_back_to_appconfig_default(self):
        """没创建任何 profile 时，老 fallback 行为不变（用 AppConfig.default_*）。"""
        # 不创建任何 RegistrationProfile
        run_id = self._create_run(
            config_snapshot={"registration_kind": "email"},
        )
        run = self._get_run(run_id)
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=self.profile_svc):
            cfg = _resolve_runtime_config(run)
        # AppConfig.default_browser_provider = "browser-default" → browser-default 配置生效
        self.assertEqual(cfg.ads_api, "http://ads.default")

    def test_profile_service_failure_does_not_block(self):
        """profile 服务抛错时不应阻塞任务，老 fallback 接管。"""
        run_id = self._create_run(
            config_snapshot={"registration_kind": "email"},
        )
        run = self._get_run(run_id)

        broken_svc = unittest_mock_object_raising("get_profile 故障")
        with patch("src.api.deps.get_config_service", return_value=self.cfg_svc), \
             patch("src.api.deps.get_registration_profile_service", return_value=broken_svc):
            cfg = _resolve_runtime_config(run)
        # 异常被吞，老路径生效
        self.assertEqual(cfg.ads_api, "http://ads.default")


def unittest_mock_object_raising(msg: str):
    """生成一个 service 对象，调用 get_profile / get_default 都抛异常。"""
    from unittest.mock import MagicMock
    obj = MagicMock()
    obj.get_profile.side_effect = RuntimeError(msg)
    obj.get_default.side_effect = RuntimeError(msg)
    return obj


if __name__ == "__main__":
    unittest.main()
