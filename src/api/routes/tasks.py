# -*- coding: utf-8 -*-
"""
任务管理 API

POST   /api/tasks           — 创建注册任务
GET    /api/tasks            — 任务列表
GET    /api/tasks/{id}       — 任务详情
POST   /api/tasks/{id}/retry — 重试任务
POST   /api/tasks/{id}/cancel — 取消任务
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import delete, select, func

from src.api.deps import get_config_service, get_event_broadcaster
from src.api.i18n import _ as tr, localize_event_data
from src.api.security import require_authenticated_user, require_csrf, require_role
from src.api.worker import get_worker_status, request_task_cancel, submit_task
from src.db.engine import get_session
from src.db.models import Run, RunEvent, User
from src.services.config_service import ConfigService
from src.services.event_service import EventBroadcaster

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ── 请求模型 ──────────────────────────────────────


class CreateTaskRequest(BaseModel):
    # 邮箱可选：留空时由后端 mail provider 自动分配（仅 cfworker 等 managed-only 类型可用）
    email: Optional[str] = ""
    password: str
    profile_id: str
    card_key: str = ""
    browser_provider: str = ""
    card_provider: str = ""
    mail_provider: str = ""
    mail_account_id: str = ""
    auto_start: bool = True  # 是否创建后自动执行


class RetryTaskRequest(BaseModel):
    mode: str = "restart"


# ── 端点 ──────────────────────────────────────────


@router.post("")
def create_task(
    request: Request,
    body: CreateTaskRequest,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
    svc: ConfigService = Depends(get_config_service),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """创建新的注册任务并自动提交到 Worker 执行。"""
    config = svc.get_config()
    browser_provider = str(body.browser_provider or config.default_browser_provider).strip()
    card_provider = str(body.card_provider or config.default_card_provider).strip()
    mail_provider = str(body.mail_provider or config.default_mail_provider).strip()
    mail_account_id = str(body.mail_account_id or config.default_mail_account_id).strip()

    mail_profile = svc.resolve_provider_config("mail", mail_provider, default_name=config.default_mail_provider)
    session_mode = str(((mail_profile.config if mail_profile else {}) or {}).get("session_mode") or "managed").strip().lower()

    # L2 fail-fast — 任务创建前 preflight：managed 模式下必须 mail provider 配置完整。
    # 仅当 mail_profile 真实存在时才校验 — 缺 ProviderConfig 时由运行时 _resolve_runtime_config
    # 兜底（.env 凭据）+ L3 ensure_runtime_ready 把关，本层不重复拦截。
    # 详见 docs/architecture/mail-provider-contract.md
    if session_mode == "managed" and mail_profile is not None:
        from src.api.routes.config import _validate_mail_provider_config
        mail_payload = dict(mail_profile.config or {})
        normalized_provider = str(mail_payload.get("provider_name") or "").strip().lower()
        # 只在 ProviderConfig 显式声明了白名单 provider（cfworker / skymail）才校验
        if normalized_provider:
            try:
                _validate_mail_provider_config(normalized_provider, mail_payload)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"code": "PROVIDER_NOT_CONFIGURED", "message": str(exc.detail)}
                raise HTTPException(
                    status_code=422,
                    detail={
                        **detail,
                        "context": "task_create_preflight",
                        "hint": "请在 /providers 把对应 mail provider 的必填字段补齐后再创建任务",
                    },
                ) from exc

    if mail_account_id:
        account = svc.get_mail_account(mail_account_id)
        if account is None or not account.is_active:
            raise HTTPException(status_code=400, detail=tr(request, "api.mail_account_unavailable"))
        # role 隔离：pro_warmup 账号是垫脚石，不应被任务级 mail 选中（消除 Ambiguity #2）
        if str(getattr(account, "role", "regular") or "regular").lower() == "pro_warmup":
            raise HTTPException(
                status_code=400,
                detail="role=pro_warmup 的账号是卡预热垫脚石，不能用作任务的注册邮箱凭据，请改选 role=regular 的账号",
            )
        expected_provider_name = str(((mail_profile.config if mail_profile else {}) or {}).get("provider_name") or "").strip().lower()
        if expected_provider_name and account.provider_name != expected_provider_name:
            raise HTTPException(status_code=400, detail=tr(request, "api.task_mail_provider_mismatch"))
        # 一个邮箱必须绑定它自己的 client_id / refresh_token，创建任务时就拦截错绑。
        # 当 email 留空（自动分配模式）时，跳过这个校验 —— mail-account 在 credentialed 模式才有意义，
        # cfworker 等 managed-only provider 不会用到 mail_account_id。
        if body.email and str(account.email or "").strip().lower() != str(body.email or "").strip().lower():
            raise HTTPException(
                status_code=400,
                detail=tr(request, "api.task_mail_account_email_mismatch"),
            )
    elif body.email and str(config.default_mail_account_id or "").strip():
        # 用户没显式选 mail_account_id，但有 default_mail_account_id 兜底 → 校验 email 是否匹配（消除 Ambiguity #4）
        # credentialed 模式下，默认账号会被 worker 用作凭据。如果 email 和默认账号邮箱不一致，
        # worker 启动后才报错"任务邮箱与所选邮箱账号不一致"，对用户体验差 —— 在创建时就拦截。
        default_account = svc.get_mail_account(str(config.default_mail_account_id).strip())
        if (
            default_account is not None
            and default_account.is_active
            and session_mode == "credentialed"
            and str(default_account.email or "").strip().lower() != str(body.email or "").strip().lower()
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "任务 email 与默认 mail_account 邮箱不一致。"
                    "credentialed 模式下，请要么显式选择 mail_account_id，要么改 email 与默认账号一致，"
                    "或在 /config 改 default_mail_account_id。"
                ),
            )

    with get_session() as session:
        run = Run(
            email=str(body.email or "").strip(),
            password=body.password,
            profile_id=body.profile_id,
            card_key=body.card_key,
            browser_provider=browser_provider,
            card_provider=card_provider,
            mail_provider=mail_provider,
            mail_account_id=mail_account_id,
            status="pending",
            phase="registration",
            retry_mode="restart",
            config_snapshot=svc.get_config_snapshot(),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        result = _run_to_dict(run)

    # 自动提交到后台 Worker 执行
    if body.auto_start:
        submitted = submit_task(run.id, broadcaster)
        result["worker_submitted"] = submitted
    else:
        result["worker_submitted"] = False
    return result


@router.get("")
def list_tasks(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_authenticated_user),
):
    """查询任务列表（支持状态筛选和分页）。"""
    with get_session() as session:
        stmt = select(Run)
        count_stmt = select(func.count()).select_from(Run)

        if status:
            stmt = stmt.where(Run.status == status)
            count_stmt = count_stmt.where(Run.status == status)

        total = session.exec(count_stmt).one()
        runs = session.exec(
            stmt.order_by(Run.created_at.desc())  # type: ignore
            .offset((page - 1) * limit)
            .limit(limit)
        ).all()

        return {
            "items": [_run_to_dict(r) for r in runs],
            "total": total,
            "page": page,
            "limit": limit,
        }


@router.get("/{task_id}")
def get_task(request: Request, task_id: str, user: User = Depends(require_authenticated_user)):
    """获取任务详情。"""
    with get_session() as session:
        run = session.get(Run, task_id)
        if not run:
            raise HTTPException(status_code=404, detail=tr(request, "api.task_not_found"))

        # 附带最近事件
        events_stmt = (
            select(RunEvent)
            .where(RunEvent.run_id == task_id)
            .order_by(RunEvent.timestamp.asc())  # type: ignore
            .limit(50)
        )
        events = session.exec(events_stmt).all()

        result = _run_to_dict(run)
        locale = getattr(request.state, "locale", "zh-CN")
        result["events"] = [
            localize_event_data(locale, {
                "id": e.id,
                "event_type": e.event_type,
                "state": e.state,
                "payload": e.payload,
                "timestamp": e.timestamp.isoformat() if e.timestamp else "",
            })
            for e in events
        ]
        return result


@router.post("/{task_id}/retry")
def retry_task(
    request: Request,
    task_id: str,
    body: RetryTaskRequest | None = None,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """重试失败的任务（重置状态并重新提交到 Worker）。"""
    retry_mode = str((body.mode if body else "restart") or "restart").strip().lower()
    if retry_mode not in {"resume", "restart"}:
        raise HTTPException(status_code=400, detail=tr(request, "api.task_invalid_retry_mode"))

    with get_session() as session:
        run = session.get(Run, task_id)
        if not run:
            raise HTTPException(status_code=404, detail=tr(request, "api.task_not_found"))
        if run.status not in ("failed", "cancelled"):
            raise HTTPException(status_code=400, detail=tr(request, "api.task_not_retryable", status=run.status))

        run.status = "pending"
        run.error_reason = None
        run.retry_mode = retry_mode
        if retry_mode == "restart":
            run.phase = "registration"
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        session.refresh(run)
        result = _run_to_dict(run)

    # 重新提交到 Worker
    submitted = submit_task(task_id, broadcaster)
    result["worker_submitted"] = submitted
    return result


@router.post("/{task_id}/cancel")
def cancel_task(
    request: Request,
    task_id: str,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """取消任务。"""
    with get_session() as session:
        run = session.get(Run, task_id)
        if not run:
            raise HTTPException(status_code=404, detail=tr(request, "api.task_not_found"))
        if run.status in ("success", "completed", "cancelled"):
            raise HTTPException(status_code=400, detail=tr(request, "api.task_not_cancellable"))

        run.status = "cancelled"
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        session.refresh(run)
        result = _run_to_dict(run)

    request_task_cancel(task_id, broadcaster)
    return result


@router.post("/{task_id}/events/clear")
def clear_task_events(
    request: Request,
    task_id: str,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
):
    """清空指定任务的历史事件日志。"""
    with get_session() as session:
        run = session.get(Run, task_id)
        if not run:
            raise HTTPException(status_code=404, detail=tr(request, "api.task_not_found"))

        events = session.exec(select(RunEvent).where(RunEvent.run_id == task_id)).all()
        cleared = len(events)
        if cleared:
            session.exec(delete(RunEvent).where(RunEvent.run_id == task_id))
            session.commit()
    return {"ok": True, "cleared": cleared}


@router.get("/worker/status")
def worker_status(user: User = Depends(require_authenticated_user)):
    """获取 Worker 线程池状态。"""
    return get_worker_status()


# ── 工具 ──────────────────────────────────────────


def _run_to_dict(run: Run) -> dict:
    return {
        "id": run.id,
        "email": run.email,
        "status": run.status,
        "phase": run.phase,
        "retry_mode": run.retry_mode,
        "profile_id": run.profile_id,
        "card_key": run.card_key,
        "browser_provider": run.browser_provider,
        "card_provider": run.card_provider,
        "mail_provider": run.mail_provider,
        "mail_account_id": run.mail_account_id,
        "is_card_warmed_up": run.is_card_warmed_up,
        "warmup_account_id": run.warmup_account_id,
        "error_reason": run.error_reason,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "updated_at": run.updated_at.isoformat() if run.updated_at else "",
    }
