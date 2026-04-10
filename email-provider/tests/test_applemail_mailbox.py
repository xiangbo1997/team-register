import unittest
from unittest import mock

from core.base_mailbox import AppleMailMailbox, MailboxAccount


class AppleMailMailboxTests(unittest.TestCase):
    def _build_mailbox(self):
        mailbox = AppleMailMailbox.__new__(AppleMailMailbox)
        mailbox._proxy = None
        mailbox._accounts = [
            {
                "email": "demo@example.com",
                "password": "pw",
                "client_id": "cid",
                "refresh_token": "rt",
                "raw": "demo@example.com----pw----cid----rt",
            }
        ]
        mailbox._selected = mailbox._accounts[0]
        mailbox._log_fn = None
        return mailbox

    def test_fetch_latest_supports_dict_payload(self):
        mailbox = self._build_mailbox()

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "code": 200,
                    "success": True,
                    "data": {
                        "send": "noreply@tm.openai.com",
                        "subject": "Your ChatGPT code is 584863",
                        "text": "Your ChatGPT code is 584863",
                        "date": "2026-04-03T04:43:54Z",
                    },
                }

        session = mock.Mock()
        session.get.return_value = _Resp()
        mailbox._session = mock.Mock(return_value=session)

        msg = mailbox._fetch_latest(mailbox._accounts[0], "INBOX")

        self.assertEqual(msg["subject"], "Your ChatGPT code is 584863")
        self.assertEqual(mailbox._safe_extract(msg.get("text") or msg.get("subject")), "584863")

    def test_wait_for_code_supports_list_payload_and_picks_latest_mail(self):
        mailbox = self._build_mailbox()

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "code": 200,
                    "success": True,
                    "data": [
                        {
                            "send": "noreply@tm.openai.com",
                            "subject": "Your ChatGPT code is 111111",
                            "text": "Your ChatGPT code is 111111",
                            "date": "2026-04-03T04:43:54Z",
                        },
                        {
                            "send": "noreply@tm.openai.com",
                            "subject": "Your ChatGPT code is 584863",
                            "text": "Your ChatGPT code is 584863",
                            "date": "2026-04-03T04:44:54Z",
                        },
                    ],
                }

        session = mock.Mock()
        session.get.return_value = _Resp()
        mailbox._session = mock.Mock(return_value=session)

        with mock.patch("time.sleep", return_value=None):
            code = mailbox.wait_for_code(
                MailboxAccount(email="demo@example.com", account_id="demo@example.com"),
                timeout=1,
            )

        self.assertEqual(code, "584863")

    def test_fetch_latest_supports_top_level_list_payload(self):
        mailbox = self._build_mailbox()

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return [
                    {
                        "send": "noreply@tm.openai.com",
                        "subject": "Your ChatGPT code is 111111",
                        "text": "Your ChatGPT code is 111111",
                        "date": "2026-04-03T04:43:54Z",
                    },
                    {
                        "send": "noreply@tm.openai.com",
                        "subject": "Your ChatGPT code is 584863",
                        "text": "Your ChatGPT code is 584863",
                        "date": "2026-04-03T04:44:54Z",
                    },
                ]

        session = mock.Mock()
        session.get.return_value = _Resp()
        mailbox._session = mock.Mock(return_value=session)

        msg = mailbox._fetch_latest(mailbox._accounts[0], "INBOX")

        self.assertEqual(msg["subject"], "Your ChatGPT code is 584863")
        self.assertEqual(mailbox._safe_extract(msg.get("text") or msg.get("subject")), "584863")


if __name__ == "__main__":
    unittest.main()
