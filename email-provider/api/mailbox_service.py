from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.mailbox_service import MailboxServiceError, mailbox_service


router = APIRouter(prefix="/mailbox-service", tags=["mailbox-service"])


class CreateMailboxSessionRequest(BaseModel):
    provider: str
    purpose: str = "generic"
    proxy: str | None = None
    extra: dict = Field(default_factory=dict)
    email: str | None = None
    account_id: str | None = None
    account_extra: dict | None = None
    lease_seconds: int = 900


class PollMailboxCodeRequest(BaseModel):
    lease_token: str
    timeout_seconds: int = 120
    keyword: str = ""
    code_pattern: str | None = None
    otp_sent_at: float | None = None
    exclude_codes: list[str] = Field(default_factory=list)
    before_ids: list[str] = Field(default_factory=list)


class CompleteMailboxSessionRequest(BaseModel):
    lease_token: str
    result: str
    reason: str = ""


class ValidateMailboxProviderRequest(BaseModel):
    extra: dict = Field(default_factory=dict)
    proxy: str | None = None


def _raise_http(exc: MailboxServiceError):
    raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message})


@router.get("/health")
def mailbox_service_health():
    return mailbox_service.health()


@router.get("/providers")
def list_mailbox_service_providers():
    return {"providers": mailbox_service.list_providers()}


@router.post("/providers/{provider}/validate-config")
def validate_mailbox_service_provider(provider: str, body: ValidateMailboxProviderRequest):
    try:
        result = mailbox_service.validate_provider_config(
            provider=provider,
            extra=body.extra,
            proxy=body.proxy,
        )
    except MailboxServiceError as exc:
        _raise_http(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PROVIDER_CONFIG", "message": str(exc)})
    return result


@router.post("/sessions")
def create_mailbox_session(body: CreateMailboxSessionRequest):
    try:
        account_override = None
        if body.email:
            from core.base_mailbox import MailboxAccount

            account_override = MailboxAccount(
                email=body.email,
                account_id=body.account_id or "",
                extra=body.account_extra,
            )
        lease = mailbox_service.acquire_session(
            provider=body.provider,
            extra=body.extra,
            proxy=body.proxy,
            purpose=body.purpose,
            account_override=account_override,
            lease_seconds=body.lease_seconds,
        )
    except MailboxServiceError as exc:
        _raise_http(exc)
    return {
        "session_id": lease.session_id,
        "lease_token": lease.lease_token,
        "provider": lease.provider,
        "email": lease.email,
        "account_id": lease.account_id,
        "state": lease.state,
        "expires_at": lease.expires_at,
        "before_ids": lease.before_ids,
        "provider_meta": lease.provider_meta,
    }


@router.get("/sessions/{session_id}")
def get_mailbox_session(session_id: str):
    try:
        lease = mailbox_service.get_session(session_id)
    except MailboxServiceError as exc:
        _raise_http(exc)
    return {
        "session_id": lease.session_id,
        "provider": lease.provider,
        "email": lease.email,
        "account_id": lease.account_id,
        "state": lease.state,
        "expires_at": lease.expires_at,
        "before_ids": lease.before_ids,
        "provider_meta": lease.provider_meta,
    }


@router.post("/sessions/{session_id}/poll-code")
def poll_mailbox_code(session_id: str, body: PollMailboxCodeRequest):
    try:
        result = mailbox_service.poll_code(
            session_id=session_id,
            lease_token=body.lease_token,
            timeout_seconds=body.timeout_seconds,
            keyword=body.keyword,
            code_pattern=body.code_pattern,
            otp_sent_at=body.otp_sent_at,
            exclude_codes=set(body.exclude_codes),
            before_ids=set(body.before_ids),
        )
    except MailboxServiceError as exc:
        _raise_http(exc)
    return {
        "status": result.status,
        "code": result.code,
        "message": result.message,
        "matched_mailbox": result.matched_mailbox,
        "error_code": result.error_code,
    }


@router.post("/sessions/{session_id}/complete")
def complete_mailbox_session(session_id: str, body: CompleteMailboxSessionRequest):
    try:
        lease = mailbox_service.complete_session(
            session_id=session_id,
            lease_token=body.lease_token,
            result=body.result,
            reason=body.reason,
        )
    except MailboxServiceError as exc:
        _raise_http(exc)
    return {
        "session_id": lease.session_id,
        "provider": lease.provider,
        "email": lease.email,
        "state": lease.state,
        "expires_at": lease.expires_at,
    }
