from __future__ import annotations

import json
import os
import secrets
import uuid

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import Field, SQLModel, Session, create_engine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str, default: Any) -> Any:
    try:
        loaded = json.loads(value or "")
    except Exception:
        return default
    return loaded if loaded is not None else default


def _create_mailbox_service_engine():
    database_url = os.getenv("MAILBOX_SERVICE_DATABASE_URL", "sqlite:///mailbox_service.db")
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


mailbox_service_engine = _create_mailbox_service_engine()


class MailboxSessionModel(SQLModel, table=True):
    __tablename__ = "mailbox_service_sessions"

    session_id: str = Field(primary_key=True)
    lease_token: str
    provider: str = Field(index=True)
    email: str = Field(index=True)
    account_id: str = ""
    purpose: str = ""
    state: str = Field(default="leased", index=True)
    result: str = ""
    proxy: str = ""
    config_json: str = "{}"
    account_extra_json: str = "{}"
    before_ids_json: str = "[]"
    provider_meta_json: str = "{}"
    error_code: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(default_factory=lambda: _utcnow() + timedelta(minutes=15))
    completed_at: Optional[datetime] = None


class MailboxSessionEventModel(SQLModel, table=True):
    __tablename__ = "mailbox_service_session_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True)
    event_type: str = Field(index=True)
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


@dataclass
class MailboxLease:
    session_id: str
    lease_token: str
    provider: str
    email: str
    account_id: str = ""
    state: str = "leased"
    expires_at: datetime = field(default_factory=lambda: _utcnow() + timedelta(minutes=15))
    before_ids: list[str] = field(default_factory=list)
    provider_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MailboxPollResult:
    status: str
    code: str = ""
    message: str = ""
    matched_mailbox: str = ""
    error_code: str = ""


class MailboxServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MailboxService:
    SUPPORTED_PROVIDERS = (
        "laoudo",
        "tempmail_lol",
        "skymail",
        "duckmail",
        "freemail",
        "moemail",
        "maliapi",
        "cfworker",
        "luckmail",
        "qqemail",
        "applemail",
    )

    def init_db(self) -> None:
        MailboxSessionModel.__table__.create(mailbox_service_engine, checkfirst=True)
        MailboxSessionEventModel.__table__.create(mailbox_service_engine, checkfirst=True)

    def list_providers(self) -> list[dict[str, Any]]:
        return [{"name": name, "mode": "legacy_adapter"} for name in self.SUPPORTED_PROVIDERS]

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "providers": list(self.SUPPORTED_PROVIDERS),
            "database_url": os.getenv("MAILBOX_SERVICE_DATABASE_URL", "sqlite:///mailbox_service.db"),
        }

    def validate_provider(self, provider: str) -> None:
        if provider not in self.SUPPORTED_PROVIDERS:
            raise MailboxServiceError("UNSUPPORTED_PROVIDER", f"不支持的邮箱 provider: {provider}")

    def validate_provider_config(
        self,
        *,
        provider: str,
        extra: Optional[dict[str, Any]] = None,
        proxy: Optional[str] = None,
    ) -> dict[str, Any]:
        self.validate_provider(provider)
        self._create_local_mailbox(provider=provider, extra=dict(extra or {}), proxy=proxy)
        return {"ok": True, "provider": provider}

    def acquire_session(
        self,
        *,
        provider: str,
        extra: Optional[dict[str, Any]] = None,
        proxy: Optional[str] = None,
        purpose: str = "generic",
        account_override: Any = None,
        lease_seconds: int = 900,
    ) -> MailboxLease:
        self.validate_provider(provider)
        extra = dict(extra or {})
        mailbox = self._create_local_mailbox(provider=provider, extra=extra, proxy=proxy)

        if account_override is None:
            account = mailbox.get_email()
            try:
                before_ids = sorted(str(item) for item in (mailbox.get_current_ids(account) or set()) if str(item))
            except Exception:
                before_ids = []
        else:
            from core.base_mailbox import MailboxAccount

            if isinstance(account_override, MailboxAccount):
                account = account_override
            else:
                account = MailboxAccount(
                    email=str(getattr(account_override, "email", "") or ""),
                    account_id=str(getattr(account_override, "account_id", "") or ""),
                    extra=getattr(account_override, "extra", None),
                )
            self._prepare_known_account(mailbox, account)
            try:
                before_ids = sorted(str(item) for item in (mailbox.get_current_ids(account) or set()) if str(item))
            except Exception:
                before_ids = []

        provider_meta = self._extract_provider_meta(mailbox=mailbox, account=account)
        expires_at = _utcnow() + timedelta(seconds=max(30, int(lease_seconds or 900)))
        lease = MailboxLease(
            session_id=uuid.uuid4().hex,
            lease_token=secrets.token_urlsafe(24),
            provider=provider,
            email=account.email,
            account_id=str(account.account_id or ""),
            state="leased",
            expires_at=expires_at,
            before_ids=before_ids,
            provider_meta=provider_meta,
        )
        with Session(mailbox_service_engine) as session:
            model = MailboxSessionModel(
                session_id=lease.session_id,
                lease_token=lease.lease_token,
                provider=provider,
                email=lease.email,
                account_id=lease.account_id,
                purpose=purpose,
                state=lease.state,
                proxy=str(proxy or ""),
                config_json=_json_dumps(extra),
                account_extra_json=_json_dumps(account.extra or {}),
                before_ids_json=_json_dumps(before_ids),
                provider_meta_json=_json_dumps(provider_meta),
                expires_at=expires_at,
            )
            session.add(model)
            session.commit()
        self._record_event(lease.session_id, "created", {"email": lease.email, "provider": provider})
        return lease

    def get_session(self, session_id: str) -> MailboxLease:
        with Session(mailbox_service_engine) as session:
            model = session.get(MailboxSessionModel, session_id)
            if not model:
                raise MailboxServiceError("SESSION_NOT_FOUND", f"未找到会话: {session_id}")
            self._expire_if_needed(session, model)
            return self._to_lease(model)

    def poll_code(
        self,
        *,
        session_id: str,
        lease_token: str,
        timeout_seconds: int = 120,
        keyword: str = "",
        code_pattern: str | None = None,
        otp_sent_at: float | None = None,
        exclude_codes: Optional[set[str]] = None,
        before_ids: Optional[set[str]] = None,
    ) -> MailboxPollResult:
        with Session(mailbox_service_engine) as session:
            model = self._get_session_model(session, session_id, lease_token)
            model.state = "polling"
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()

        model = self.get_session_model(session_id, lease_token)
        mailbox, account, snapshot_ids = self._hydrate_runtime(model)
        used_before_ids = set(before_ids or snapshot_ids)
        try:
            code = mailbox.wait_for_code(
                account,
                keyword=keyword,
                timeout=int(timeout_seconds or 120),
                before_ids=used_before_ids,
                code_pattern=code_pattern,
                otp_sent_at=otp_sent_at,
                exclude_codes=exclude_codes or set(),
            )
        except Exception as exc:
            error_code = self._map_error_code(exc)
            message = str(exc)
            with Session(mailbox_service_engine) as session:
                model = self._get_session_model(session, session_id, lease_token)
                model.state = "failed"
                model.error_code = error_code
                model.error_message = message
                model.updated_at = _utcnow()
                session.add(model)
                session.commit()
            self._record_event(session_id, "poll_failed", {"error_code": error_code, "message": message})
            return MailboxPollResult(status="failed", message=message, error_code=error_code)

        with Session(mailbox_service_engine) as session:
            model = self._get_session_model(session, session_id, lease_token)
            model.state = "code_ready"
            model.error_code = ""
            model.error_message = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
        self._record_event(session_id, "code_ready", {"code": "***"})
        return MailboxPollResult(status="ready", code=str(code or ""))

    def complete_session(
        self,
        *,
        session_id: str,
        lease_token: str,
        result: str,
        reason: str = "",
    ) -> MailboxLease:
        with Session(mailbox_service_engine) as session:
            model = self._get_session_model(session, session_id, lease_token)
            if model.completed_at:
                return self._to_lease(model)

        if str(result or "").lower() == "success":
            try:
                mailbox, account, _ = self._hydrate_runtime(self.get_session_model(session_id, lease_token))
                self._prepare_selected_account(mailbox, account.email)
                if hasattr(mailbox, "remove_used_account"):
                    mailbox.remove_used_account()
            except Exception as exc:
                self._record_event(session_id, "cleanup_failed", {"message": str(exc)})

        with Session(mailbox_service_engine) as session:
            model = self._get_session_model(session, session_id, lease_token)
            model.result = str(result or "").strip().lower()
            model.state = "completed"
            model.completed_at = _utcnow()
            model.updated_at = _utcnow()
            if reason:
                model.error_message = reason
            session.add(model)
            session.commit()
            lease = self._to_lease(model)
        self._record_event(session_id, "completed", {"result": result, "reason": reason})
        return lease

    def get_session_model(self, session_id: str, lease_token: str) -> MailboxSessionModel:
        with Session(mailbox_service_engine) as session:
            model = self._get_session_model(session, session_id, lease_token)
            session.expunge(model)
            return model

    def _hydrate_runtime(self, model: MailboxSessionModel):
        from core.base_mailbox import MailboxAccount

        extra = _json_loads(model.config_json, {})
        account_extra = _json_loads(model.account_extra_json, {})
        before_ids = set(_json_loads(model.before_ids_json, []))
        mailbox = self._create_local_mailbox(provider=model.provider, extra=extra, proxy=model.proxy or None)
        account = MailboxAccount(
            email=model.email,
            account_id=model.account_id or "",
            extra=account_extra or None,
        )
        return mailbox, account, before_ids

    def _create_local_mailbox(self, *, provider: str, extra: dict[str, Any], proxy: Optional[str]):
        from core.base_mailbox import create_local_mailbox

        return create_local_mailbox(provider=provider, extra=extra, proxy=proxy)

    def _extract_provider_meta(self, *, mailbox, account) -> dict[str, Any]:
        provider_meta: dict[str, Any] = {}
        if isinstance(getattr(account, "extra", None), dict):
            provider_meta.update(account.extra)

        for source_attr, target_key in (
            ("_token", "mailbox_token"),
            ("_order_no", "mailbox_order_no"),
            ("_email", "allocated_email"),
        ):
            value = getattr(mailbox, source_attr, "")
            if value:
                provider_meta[target_key] = str(value)
        if account.account_id:
            provider_meta.setdefault("mailbox_token", str(account.account_id))
        provider_meta.setdefault("source_email", str(account.email or ""))
        return provider_meta

    def _prepare_selected_account(self, mailbox, email: str) -> None:
        accounts = getattr(mailbox, "_accounts", None)
        if not isinstance(accounts, list):
            return
        target_email = str(email or "").strip().lower()
        for item in accounts:
            if str((item or {}).get("email", "")).strip().lower() == target_email:
                try:
                    mailbox._selected = item
                except Exception:
                    pass
                return

    def _prepare_known_account(self, mailbox, account) -> None:
        self._prepare_selected_account(mailbox, account.email)

        account_id = str(account.account_id or "").strip()
        if account_id and hasattr(mailbox, "_token"):
            try:
                mailbox._token = account_id
            except Exception:
                pass
        if account.email and hasattr(mailbox, "_email"):
            try:
                mailbox._email = account.email
            except Exception:
                pass

        selected = getattr(mailbox, "_selected", None)
        if selected and hasattr(mailbox, "_clear_mailbox"):
            try:
                mailbox._clear_mailbox(selected)
            except Exception:
                pass

    def _record_event(self, session_id: str, event_type: str, detail: dict[str, Any]) -> None:
        with Session(mailbox_service_engine) as session:
            session.add(
                MailboxSessionEventModel(
                    session_id=session_id,
                    event_type=event_type,
                    detail_json=_json_dumps(detail),
                )
            )
            session.commit()

    def _expire_if_needed(self, session: Session, model: MailboxSessionModel) -> None:
        if model.completed_at:
            return
        if _ensure_utc(model.expires_at) > _utcnow():
            return
        model.state = "expired"
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()
        raise MailboxServiceError("LEASE_EXPIRED", f"邮箱会话已过期: {model.session_id}")

    def _get_session_model(self, session: Session, session_id: str, lease_token: str) -> MailboxSessionModel:
        model = session.get(MailboxSessionModel, session_id)
        if not model:
            raise MailboxServiceError("SESSION_NOT_FOUND", f"未找到会话: {session_id}")
        if model.lease_token != lease_token:
            raise MailboxServiceError("INVALID_LEASE", "邮箱会话租约无效")
        self._expire_if_needed(session, model)
        return model

    def _to_lease(self, model: MailboxSessionModel) -> MailboxLease:
        return MailboxLease(
            session_id=model.session_id,
            lease_token=model.lease_token,
            provider=model.provider,
            email=model.email,
            account_id=model.account_id or "",
            state=model.state,
            expires_at=_ensure_utc(model.expires_at),
            before_ids=list(_json_loads(model.before_ids_json, [])),
            provider_meta=dict(_json_loads(model.provider_meta_json, {})),
        )

    def _map_error_code(self, exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        if "invalid_grant" in text or "aadsts70000" in text:
            return "INVALID_CREDENTIAL"
        if "lease" in text and "expire" in text:
            return "LEASE_EXPIRED"
        if "超时" in text or "timed out" in text or isinstance(exc, TimeoutError):
            return "CODE_TIMEOUT"
        if "429" in text or "rate limit" in text:
            return "RATE_LIMITED"
        if "500" in text or "502" in text or "503" in text or "504" in text:
            return "UPSTREAM_5XX"
        return "PROVIDER_ERROR"


mailbox_service = MailboxService()


def init_mailbox_service_db() -> None:
    mailbox_service.init_db()
