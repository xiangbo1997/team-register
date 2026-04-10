from __future__ import annotations

import os
import tempfile
import unittest

from unittest import mock


_DB_PATH = os.path.join(tempfile.gettempdir(), "any_auto_register_mailbox_service_test.db")
os.environ["MAILBOX_SERVICE_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from sqlmodel import Session, delete

from core.base_mailbox import MailboxAccount, MailboxServiceBackedMailbox
from services.mailbox_service import (
    MailboxSessionEventModel,
    MailboxSessionModel,
    init_mailbox_service_db,
    mailbox_service,
    mailbox_service_engine,
)


class _FakeMailbox:
    def __init__(self, *, code: str = "123456", error: Exception | None = None):
        self._code = code
        self._error = error
        self._removed = False
        self._accounts = [{"email": "demo@example.com"}]
        self._selected = {}
        self._token = "tok_demo"
        self._order_no = "ord_demo"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(
            email="demo@example.com",
            account_id="legacy-account-id",
            extra={"source": "fake"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        return {"m1", "m2"}

    def wait_for_code(self, account: MailboxAccount, **kwargs) -> str:
        if self._error:
            raise self._error
        return self._code

    def remove_used_account(self):
        self._removed = True


class MailboxServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_mailbox_service_db()

    def setUp(self):
        with Session(mailbox_service_engine) as session:
            session.exec(delete(MailboxSessionEventModel))
            session.exec(delete(MailboxSessionModel))
            session.commit()

    def test_service_session_lifecycle_and_cleanup(self):
        fake_mailbox = _FakeMailbox()
        with mock.patch("core.base_mailbox.create_local_mailbox", return_value=fake_mailbox):
            lease = mailbox_service.acquire_session(
                provider="laoudo",
                extra={},
                proxy=None,
                purpose="register",
            )
            self.assertEqual(lease.email, "demo@example.com")
            self.assertEqual(set(lease.before_ids), {"m1", "m2"})
            self.assertEqual(lease.provider_meta["mailbox_token"], "tok_demo")

            result = mailbox_service.poll_code(
                session_id=lease.session_id,
                lease_token=lease.lease_token,
                timeout_seconds=10,
                before_ids=set(lease.before_ids),
            )
            self.assertEqual(result.status, "ready")
            self.assertEqual(result.code, "123456")

            mailbox_service.complete_session(
                session_id=lease.session_id,
                lease_token=lease.lease_token,
                result="success",
            )
            self.assertTrue(fake_mailbox._removed)

    def test_service_backed_mailbox_keeps_legacy_call_shape(self):
        fake_mailbox = _FakeMailbox(code="654321")
        with mock.patch("core.base_mailbox.create_local_mailbox", return_value=fake_mailbox):
            mailbox = MailboxServiceBackedMailbox(provider="laoudo", extra={}, proxy=None)
            account = mailbox.get_email()
            before_ids = mailbox.get_current_ids(account)
            code = mailbox.wait_for_code(account, timeout=10, before_ids=before_ids)
            mailbox.complete_success()

            self.assertEqual(account.email, "demo@example.com")
            self.assertEqual(code, "654321")
            self.assertEqual(before_ids, {"m1", "m2"})
            self.assertTrue(fake_mailbox._removed)

    def test_invalid_grant_maps_to_runtime_error_in_adapter(self):
        fake_mailbox = _FakeMailbox(error=RuntimeError("invalid_grant"))
        with mock.patch("core.base_mailbox.create_local_mailbox", return_value=fake_mailbox):
            mailbox = MailboxServiceBackedMailbox(provider="laoudo", extra={}, proxy=None)
            account = mailbox.get_email()
            with self.assertRaises(RuntimeError):
                mailbox.wait_for_code(account, timeout=10, before_ids=set())


if __name__ == "__main__":
    unittest.main()
