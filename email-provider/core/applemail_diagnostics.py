from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .proxy_utils import build_requests_proxy_config


@dataclass
class MailDiagnosticEntry:
    mailbox: str
    date: str
    subject: str
    sender: str
    preview: str
    raw: dict


class AppleMailDiagnosticClient:
    """通用的小苹果邮箱诊断客户端，仅用于检查收信接口行为。"""

    def __init__(
        self,
        client_id: str,
        refresh_token: str,
        email: str,
        proxy: str | None = None,
        api_base: str = "https://www.appleemail.top",
        log_fn: Optional[Callable[[str], None]] = None,
        session_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._client_id = client_id
        self._refresh_token = refresh_token
        self._email = email
        self._proxy = proxy
        self._api_base = api_base.rstrip("/")
        self._log = log_fn or (lambda _msg: None)
        self._session_factory = session_factory

    def _session(self):
        if self._session_factory:
            return self._session_factory()
        from curl_cffi import requests as cffi_requests

        proxies = build_requests_proxy_config(self._proxy)
        return cffi_requests.Session(proxies=proxies, impersonate="chrome")

    def _request_json(self, endpoint: str, params: dict) -> object:
        session = self._session()
        resp = session.get(f"{self._api_base}{endpoint}", params=params, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"{endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _base_params(self) -> dict[str, str]:
        return {
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "email": self._email,
        }

    @staticmethod
    def _coerce_items(data: object) -> list[dict]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
            if isinstance(inner, dict):
                return [inner]
            return [data]
        return []

    @staticmethod
    def _match_filters(
        item: MailDiagnosticEntry,
        subject_filter: str | None,
        sender_filter: str | None,
        content_filter: str | None,
    ) -> bool:
        subject = item.subject.lower()
        sender = item.sender.lower()
        preview = item.preview.lower()
        if subject_filter and subject_filter.lower() not in subject:
            return False
        if sender_filter and sender_filter.lower() not in sender:
            return False
        if content_filter and content_filter.lower() not in preview:
            return False
        return True

    @staticmethod
    def _parse_iso_datetime(value: str | None) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _to_entry(mailbox: str, item: dict) -> MailDiagnosticEntry:
        sender = ""
        from_data = item.get("from")
        if isinstance(from_data, dict):
            email_addr = from_data.get("emailAddress")
            if isinstance(email_addr, dict):
                sender = str(email_addr.get("address") or "")
            else:
                sender = str(from_data.get("address") or "")
        sender = sender or str(item.get("from_addr") or item.get("sender") or item.get("send") or "")

        preview = item.get("bodyPreview") or item.get("text") or item.get("content") or item.get("body") or ""
        if isinstance(preview, dict):
            preview = preview.get("content") or ""

        return MailDiagnosticEntry(
            mailbox=mailbox,
            date=str(item.get("date") or item.get("receivedDateTime") or item.get("sentDateTime") or ""),
            subject=str(item.get("subject") or ""),
            sender=sender,
            preview=str(preview),
            raw=item,
        )

    def fetch_latest(self, mailbox: str) -> list[MailDiagnosticEntry]:
        payload = self._request_json(
            "/api/mail-new",
            {
                **self._base_params(),
                "mailbox": mailbox,
                "response_type": "json",
            },
        )
        items = [self._to_entry(mailbox, item) for item in self._coerce_items(payload)]
        self._log(f"[AppleMailDiagnostics] {mailbox} latest={len(items)}")
        return items

    def fetch_all(self, mailbox: str) -> list[MailDiagnosticEntry]:
        payload = self._request_json(
            "/api/mail-all",
            {
                **self._base_params(),
                "mailbox": mailbox,
            },
        )
        items = [self._to_entry(mailbox, item) for item in self._coerce_items(payload)]
        self._log(f"[AppleMailDiagnostics] {mailbox} all={len(items)}")
        return items

    def inspect_mailboxes(
        self,
        mailboxes: Iterable[str] = ("INBOX", "Junk"),
        mode: str = "latest",
        subject_filter: str | None = None,
        sender_filter: str | None = None,
        content_filter: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> list[MailDiagnosticEntry]:
        fetcher = self.fetch_all if mode == "all" else self.fetch_latest
        results: list[MailDiagnosticEntry] = []
        after_dt = self._parse_iso_datetime(after)
        before_dt = self._parse_iso_datetime(before)
        for mailbox in mailboxes:
            try:
                entries = fetcher(mailbox)
            except Exception as exc:
                self._log(f"[AppleMailDiagnostics] {mailbox} failed: {exc}")
                continue
            for entry in entries:
                if not self._match_filters(entry, subject_filter, sender_filter, content_filter):
                    continue
                entry_dt = self._parse_iso_datetime(entry.date)
                if after_dt and (entry_dt is None or entry_dt < after_dt):
                    continue
                if before_dt and (entry_dt is None or entry_dt > before_dt):
                    continue
                results.append(entry)
        return results
