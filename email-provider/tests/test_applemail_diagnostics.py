import unittest

from core.applemail_diagnostics import AppleMailDiagnosticClient
from scripts.applemail_diagnose import entry_to_dict, parse_iso8601


class AppleMailDiagnosticsTests(unittest.TestCase):
    def test_inspect_mailboxes_checks_inbox_and_junk_and_applies_filters(self):
        payloads = {
            "/api/mail-new:INBOX": {
                "success": True,
                "data": {
                    "date": "2026-04-02T17:10:00Z",
                    "subject": "Welcome",
                    "from": {"emailAddress": {"address": "noreply@example.com"}},
                    "text": "plain message",
                },
            },
            "/api/mail-new:Junk": {
                "success": True,
                "data": {
                    "date": "2026-04-02T17:14:00Z",
                    "subject": "OpenAI verification",
                    "send": "noreply@openai.com",
                    "text": "verification code message",
                },
            },
        }

        class _Resp:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload
                self.text = ""

            def json(self):
                return self._payload

        class _Session:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, timeout=30):
                mailbox = params.get("mailbox", "")
                endpoint = url.split(".top", 1)[1]
                self.calls.append((endpoint, mailbox))
                return _Resp(payloads[f"{endpoint}:{mailbox}"])

        session = _Session()
        client = AppleMailDiagnosticClient(
            client_id="cid",
            refresh_token="rt",
            email="demo@example.com",
            session_factory=lambda: session,
        )

        entries = client.inspect_mailboxes(sender_filter="openai.com")

        self.assertEqual(session.calls, [("/api/mail-new", "INBOX"), ("/api/mail-new", "Junk")])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].mailbox, "Junk")
        self.assertEqual(entries[0].sender, "noreply@openai.com")
        self.assertEqual(entries[0].date, "2026-04-02T17:14:00Z")

    def test_inspect_mailboxes_applies_time_window_filters(self):
        payloads = {
            "/api/mail-all:INBOX": {
                "success": True,
                "data": [
                    {
                        "date": "2026-04-02T17:10:00Z",
                        "subject": "Too early",
                        "send": "noreply@openai.com",
                        "text": "old code",
                    }
                ],
            },
            "/api/mail-all:Junk": {
                "success": True,
                "data": [
                    {
                        "date": "2026-04-02T17:14:00Z",
                        "subject": "In window",
                        "send": "noreply@openai.com",
                        "text": "new code",
                    }
                ],
            },
        }

        class _Resp:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload
                self.text = ""

            def json(self):
                return self._payload

        class _Session:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, timeout=30):
                mailbox = params.get("mailbox", "")
                endpoint = url.split(".top", 1)[1]
                self.calls.append((endpoint, mailbox))
                return _Resp(payloads[f"{endpoint}:{mailbox}"])

        session = _Session()
        client = AppleMailDiagnosticClient(
            client_id="cid",
            refresh_token="rt",
            email="demo@example.com",
            session_factory=lambda: session,
        )

        entries = client.inspect_mailboxes(
            mode="all",
            after="2026-04-02T17:13:42Z",
            before="2026-04-02T17:15:15Z",
        )

        self.assertEqual(session.calls, [("/api/mail-all", "INBOX"), ("/api/mail-all", "Junk")])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].mailbox, "Junk")
        self.assertEqual(entries[0].date, "2026-04-02T17:14:00Z")

    def test_entry_to_dict_and_iso8601_parser(self):
        self.assertEqual(parse_iso8601("2026-04-02T17:13:42"), "2026-04-02T17:13:42+00:00")
        self.assertEqual(parse_iso8601("2026-04-02T17:13:42Z"), "2026-04-02T17:13:42+00:00")

        class _Entry:
            mailbox = "INBOX"
            date = "2026-04-02T17:14:00Z"
            sender = "noreply@openai.com"
            subject = "Code"
            preview = "preview text"

        self.assertEqual(
            entry_to_dict(_Entry()),
            {
                "mailbox": "INBOX",
                "date": "2026-04-02T17:14:00Z",
                "sender": "noreply@openai.com",
                "subject": "Code",
                "preview": "preview text",
            },
        )


if __name__ == "__main__":
    unittest.main()
