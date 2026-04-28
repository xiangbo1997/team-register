# -*- coding: utf-8 -*-
"""验证 EncryptedString 在 Run/MailAccount 上的透明加解密与磁盘密文存储。"""

import os
import unittest
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from src.db import crypto
from src.db.models import MailAccount, Run


def _build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class TestRunEncryption(unittest.TestCase):
    def setUp(self) -> None:
        crypto.reset_for_tests()
        self._key = crypto.generate_key()
        self._env_patch = mock.patch.dict(os.environ, {"DB_ENCRYPTION_KEY": self._key})
        self._env_patch.start()
        self.engine = _build_engine()

    def tearDown(self) -> None:
        self._env_patch.stop()
        crypto.reset_for_tests()

    def test_run_password_and_card_key_are_encrypted_on_disk(self):
        with Session(self.engine) as s:
            run = Run(email="a@b.com", password="hunter2", card_key="CDK-XYZ")
            s.add(run)
            s.commit()
            run_id = run.id

        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT password, card_key FROM runs WHERE id = :id"),
                {"id": run_id},
            ).first()
            raw_password, raw_card_key = row
            self.assertTrue(raw_password.startswith("enc:v1:"), f"明文落盘: {raw_password}")
            self.assertTrue(raw_card_key.startswith("enc:v1:"), f"明文落盘: {raw_card_key}")
            self.assertNotIn("hunter2", raw_password)
            self.assertNotIn("CDK-XYZ", raw_card_key)

        with Session(self.engine) as s:
            loaded = s.get(Run, run_id)
            self.assertEqual(loaded.password, "hunter2")
            self.assertEqual(loaded.card_key, "CDK-XYZ")

    def test_mail_account_credentials_are_encrypted_on_disk(self):
        with Session(self.engine) as s:
            acct = MailAccount(
                email="u@mail.com",
                client_id="client-abc",
                refresh_token="refresh-token-long-value",
            )
            s.add(acct)
            s.commit()
            acct_id = acct.id

        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT client_id, refresh_token FROM mail_accounts WHERE id = :id"),
                {"id": acct_id},
            ).first()
            raw_client_id, raw_refresh = row
            self.assertTrue(raw_client_id.startswith("enc:v1:"))
            self.assertTrue(raw_refresh.startswith("enc:v1:"))
            self.assertNotIn("client-abc", raw_client_id)
            self.assertNotIn("refresh-token-long-value", raw_refresh)

        with Session(self.engine) as s:
            loaded = s.get(MailAccount, acct_id)
            self.assertEqual(loaded.client_id, "client-abc")
            self.assertEqual(loaded.refresh_token, "refresh-token-long-value")

    def test_legacy_plaintext_still_readable(self):
        # 模拟旧数据：直接 raw SQL 插入明文
        with self.engine.connect() as conn:
            conn.execute(
                text("INSERT INTO runs (id, email, password, card_key, status, phase, retry_mode, profile_id, browser_provider, card_provider, mail_provider, mail_account_id, is_card_warmed_up, warmup_account_id, warmup_pro_attempts, warmup_blocked_count, config_snapshot, created_at, updated_at) VALUES ('legacy1', 'x@y', 'legacy_plain_pw', '', 'pending', 'registration', 'restart', '', '', '', '', '', 0, '', 0, 0, '{}', '2026-04-24', '2026-04-24')")
            )
            conn.commit()

        with Session(self.engine) as s:
            loaded = s.get(Run, "legacy1")
            self.assertEqual(loaded.password, "legacy_plain_pw")


if __name__ == "__main__":
    unittest.main()
