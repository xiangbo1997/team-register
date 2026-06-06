# -*- coding: utf-8 -*-
"""
任务管理 API

POST   /api/tasks           — 创建注册任务
GET    /api/tasks            — 任务列表
GET    /api/tasks/{id}       — 任务详情
POST   /api/tasks/{id}/retry — 重试任务（仅 failed/cancelled）
POST   /api/tasks/{id}/clone — 克隆已结束任务为新 Run（任何状态，源 Run 保留）
POST   /api/tasks/{id}/cancel — 取消任务
DELETE /api/tasks/{id}        — 硬删除任务（仅终态，级联 RunEvent + Checkpoint）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import delete, select, func

from src.api.deps import get_config_service, get_event_broadcaster
from src.api.i18n import _ as tr, localize_event_data
from src.api.security import require_authenticated_user, require_csrf, require_role
from src.api.worker import get_worker_status, request_task_cancel, submit_task
from src.db.engine import get_session
from src.db.models import Checkpoint, Run, RunEvent, User
from src.services.config_service import ConfigService
from src.services.event_service import EventBroadcaster

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ── 请求模型 ──────────────────────────────────────


class CreateTaskRequest(BaseModel):
    # 邮箱可选：留空时由后端 mail provider 自动分配（仅 cfworker 等 managed-only 类型可用）
    email: Optional[str] = ""
    # 密码可选：留空时由后端自动生成强密码（grok/openai 注册流程在底层 _gen_password 生成）
    password: str = ""
    profile_id: str
    card_key: str = ""
    browser_provider: str = ""
    card_provider: str = ""
    mail_provider: str = ""
    mail_account_id: str = ""
    auto_start: bool = True  # 是否创建后自动执行
    # 任务模式：
    #   "full"          (默认) — 完整跑 Phase 1 注册 + Phase 2 提取 token + Phase 3 支付绑卡
    #   "register_only"        — 只跑 Phase 1+2，绑卡留给"普号池"后续手动操作
    mode: str = "full"
    # 注册类型鉴别（feat/mode-phone-registration 2026-05-27）
    #   "email" (默认) — 邮箱注册路径；空邮箱 + cfworker/outlook provider 时后端静默生成
    #   "phone"        — 手机号注册路径；空手机号时后端通过 SMS-Activate 自动申领
    registration_kind: str = "email"
    phone_number: Optional[str] = ""
    sms_country: Optional[str] = ""
    # 注册方式 × 供应商组合（feat/registration-profile 2026-05-27）
    #   registration_profile_name: 指定具体组合名（如 "email-default"）；
    #     留空时按 registration_kind 取默认组合
    #   provider_overrides: 任务级 provider 临时覆盖，
    #     如 {"card": "card-nodecard"} 表示本次任务用 nodecard 代替组合里的卡商
    registration_profile_name: Optional[str] = ""
    provider_overrides: Optional[dict[str, str]] = None


class RetryTaskRequest(BaseModel):
    mode: str = "restart"


class BatchRegisterRequest(BaseModel):
    """批量注册请求。

    profile_id / profile_ids 二选一或同传（同传会合并去重）。
    profile_ids 长度 = 并发数（每个 profile 一个并发槽）。
    """
    count: int = Field(..., ge=1, le=100, description="账号数量 1-100")
    profile_id: str = Field(default="", description="单 profile（兼容老入参；与 profile_ids 至少填一个）")
    profile_ids: list[str] = Field(
        default_factory=list,
        description="多 profile 并发槽（推荐）；每行/项一个 AdsPower profile，长度 = 并发数",
    )
    password: str = Field(default="", description="所有号共用密码；留空则每号自动生成强密码")
    interval_min_sec: float = Field(default=30.0, ge=0, description="profile 内部串行的抖动下限（秒）")
    interval_max_sec: float = Field(default=90.0, ge=0, description="profile 内部串行的抖动上限（秒）")
    mode: str = Field(default="register_only", description="register_only / full")
    browser_provider: str = ""
    card_provider: str = ""
    mail_provider: str = ""
    mail_account_id: str = ""
    gender: Optional[str] = Field(default=None, description="m / f / None（混合）")


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
    # 校验 mode（默认 full；register_only 跳过 Phase 3 让账号进普号池）
    task_mode = str(body.mode or "full").strip().lower()
    if task_mode not in ("full", "register_only"):
        raise HTTPException(
            status_code=422,
            detail=f"invalid mode: {body.mode!r}（只接受 'full' 或 'register_only'）",
        )

    # 注册类型鉴别（feat/mode-phone-registration 2026-05-27 / feat/grok-register）
    #   email — OpenAI 邮箱注册；phone — OpenAI 手机号注册；grok — Grok(x.ai) 邮箱注册
    registration_kind = str(body.registration_kind or "email").strip().lower()
    if registration_kind not in ("email", "phone", "grok"):
        raise HTTPException(
            status_code=422,
            detail=f"invalid registration_kind: {body.registration_kind!r}（只接受 'email' / 'phone' / 'grok'）",
        )
    # platform 派生：grok kind → grok 平台；其余（email/phone）→ openai 平台。
    # worker._execute_task_inner 按 Run.platform 分叉到 grok_runtime vs main.run_task。
    platform = "grok" if registration_kind == "grok" else "openai"

    config = svc.get_config()

    # provider_overrides dict 优先（2026-05-27 合并 PROVIDER 配置 UI 后，前端只传 dict）；
    # 扁平字段（browser_provider/card_provider/mail_provider）保留为向后兼容路径（API client / curl）。
    # 三级回退：provider_overrides[slot] > body.{slot}_provider > config.default_{slot}_provider
    overrides_input = body.provider_overrides if isinstance(body.provider_overrides, dict) else {}

    def _pick_provider(slot: str, flat_value: str, default_value: str) -> str:
        override = str(overrides_input.get(slot, "") or "").strip() if overrides_input else ""
        if override:
            return override
        return str(flat_value or default_value or "").strip()

    browser_provider = _pick_provider("browser", body.browser_provider, config.default_browser_provider)
    card_provider = _pick_provider("card", body.card_provider, config.default_card_provider)
    # 手机号模式仍然落 default mail_provider（OpenAI 手机号注册流程也会要求邮箱验证；UI 不暴露邮箱字段
    # 是因为后端会自动分配 cfworker 临时邮箱）。账号 / 留空校验全部跳过。
    if registration_kind == "phone":
        # phone 模式：override 也允许指定 mail（与 email 模式一致），但前端目前不发；空就回退到 default
        mail_provider = _pick_provider("mail", "", config.default_mail_provider)
        mail_account_id = ""
        mail_profile = svc.resolve_provider_config("mail", mail_provider, default_name=config.default_mail_provider) if mail_provider else None
        session_mode = str(((mail_profile.config if mail_profile else {}) or {}).get("session_mode") or "managed").strip().lower()
    elif registration_kind == "grok":
        # grok 模式：无卡 / 无 SMS，只需 mail（Grok 注册必须真实邮箱收码）。
        # card_provider 仍解析但 worker 不会构建/使用（grok 路径跳过 card_api）。
        mail_provider = _pick_provider("mail", body.mail_provider, config.default_mail_provider)
        mail_account_id = str(body.mail_account_id or config.default_mail_account_id).strip()
        mail_profile = svc.resolve_provider_config("mail", mail_provider, default_name=config.default_mail_provider)
        session_mode = str(((mail_profile.config if mail_profile else {}) or {}).get("session_mode") or "managed").strip().lower()
    else:
        mail_provider = _pick_provider("mail", body.mail_provider, config.default_mail_provider)
        mail_account_id = str(body.mail_account_id or config.default_mail_account_id).strip()
        mail_profile = svc.resolve_provider_config("mail", mail_provider, default_name=config.default_mail_provider)
        session_mode = str(((mail_profile.config if mail_profile else {}) or {}).get("session_mode") or "managed").strip().lower()

    # 邮箱 / grok 模式 + 空邮箱：仅 cfworker / outlook_email_plus 等可自动生成的 provider 放行；
    # 其它 provider 直接 422 让前端阻止。grok 与 email 共用同一白名单逻辑（所选 mail provider
    # 支持自动生成时邮箱可留空，否则必填）——见用户反馈：下面选了 cfworker 时上面邮箱不该强制必填。
    if registration_kind in ("email", "grok") and not str(body.email or "").strip():
        allowed_blank_providers = {"cfworker", "outlook_email_plus"}
        provider_name_for_blank = str(((mail_profile.config if mail_profile else {}) or {}).get("provider_name") or "").strip().lower()
        if provider_name_for_blank not in allowed_blank_providers:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "EMAIL_REQUIRED_FOR_PROVIDER",
                    "i18n_key": "tasks.create.email_required_for_provider",
                    "message": "当前邮箱供应商不支持留空，需手动填写邮箱",
                    "provider_name": provider_name_for_blank or "(unknown)",
                },
            )

    # L2 fail-fast — 任务创建前 preflight：managed 模式下必须 mail provider 配置完整。
    # 仅当 mail_profile 真实存在时才校验 — 缺 ProviderConfig 时由运行时 _resolve_runtime_config
    # 兜底（.env 凭据）+ L3 ensure_runtime_ready 把关，本层不重复拦截。
    # 详见 docs/architecture/mail-provider-contract.md
    if registration_kind == "email" and session_mode == "managed" and mail_profile is not None:
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

    if registration_kind == "email" and mail_account_id:
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

    # 把 task_mode 合并到 config_snapshot —— worker 启动时读这个字段决定要不要跑 Phase 3
    snapshot = dict(svc.get_config_snapshot() or {})
    snapshot["task_mode"] = task_mode
    snapshot["registration_kind"] = registration_kind
    snapshot["platform"] = platform
    if registration_kind == "phone":
        snapshot["requested_phone"] = str(body.phone_number or "").strip()
        # sms_country 优先用前端传值，否则沿用 .env 默认（不写也行，worker 兜底）
        sms_country_override = str(body.sms_country or "").strip()
        if sms_country_override:
            snapshot["sms_country"] = sms_country_override

    # 注册方式 × 供应商组合（feat/registration-profile 2026-05-27）：
    # worker._resolve_runtime_config 会读 snapshot.registration_profile_name +
    # provider_overrides 决定 provider 解析路径
    profile_name = str(body.registration_profile_name or "").strip()
    if profile_name:
        snapshot["registration_profile_name"] = profile_name
    overrides = body.provider_overrides or {}
    if isinstance(overrides, dict) and overrides:
        # 过滤空 k/v；不做存在性校验（worker 解析时会自动回退到默认）
        clean_overrides = {
            str(k).strip(): str(v).strip()
            for k, v in overrides.items()
            if str(k).strip() and str(v).strip()
        }
        if clean_overrides:
            snapshot["provider_overrides"] = clean_overrides

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
            phone_number=str(body.phone_number or "").strip() if registration_kind == "phone" else "",
            platform=platform,
            status="pending",
            phase="registration",
            retry_mode="restart",
            config_snapshot=snapshot,
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
    platform: Optional[str] = Query(None, description="openai / grok（其余值=不过滤）"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_authenticated_user),
):
    """查询任务列表（支持状态 / 平台筛选和分页）。

    platform 宽松处理：仅 openai / grok 生效，其余值（all/空/非法）静默不过滤，
    与 status 筛选的容错风格一致。
    """
    with get_session() as session:
        stmt = select(Run)
        count_stmt = select(func.count()).select_from(Run)

        if status:
            stmt = stmt.where(Run.status == status)
            count_stmt = count_stmt.where(Run.status == status)

        if platform in ("openai", "grok"):
            stmt = stmt.where(Run.platform == platform)
            count_stmt = count_stmt.where(Run.platform == platform)

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

    # 清理残留的 cancel 标志 —— 这是 cancel→retry 路径的关键 bug 点：
    # cancel 时 task 还在 pending（没进 _execute_task），_cancel_requests.add 后
    # 不会触发 _on_task_done 的清理（_on_task_done 只在线程池任务结束时跑）。
    # retry 不清这个 set，新 _execute_task 启动后会在 _is_cancel_requested 检查
    # 处立刻 return，表现为"submit 成功但任务静默退出，UI 永远 pending"。
    from src.api.worker import _clear_cancel_requested
    _clear_cancel_requested(task_id)

    # 重新提交到 Worker —— 走 batch dispatcher 的 per-profile 串行队列
    # 不直接 submit_task：批量 retry 下 ThreadPoolExecutor 内部排队对 AdsPower
    # profile 物理约束（同 profile 不能并发开浏览器）不可见，会出现"submit 成功
    # 但永远不轮到自己跑"的静默丢弃。requeue_runs 按 Run.profile_id 分桶后，
    # 同 profile 内严格串行、profile 间并行，单 run 也走同路径。
    from src.services.batch_register_service import requeue_runs
    result["worker_submitted"] = requeue_runs([task_id], broadcaster).get("requeued", 0) > 0
    return result


@router.post("/{task_id}/clone")
def clone_task(
    request: Request,
    task_id: str,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """把已结束的任务克隆成一条全新 Run（新 ID + 新 created_at），复用源 config_snapshot 重新走完整流程。

    与 /retry 的区别：源 Run 完全保留（成功历史 / 失败现场 / token 不被覆盖），
    新 Run 进入 per-profile 串行队列（复用 requeue_runs，遵守 AdsPower 物理约束）。
    """
    with get_session() as session:
        src = session.get(Run, task_id)
        if not src:
            raise HTTPException(status_code=404, detail=tr(request, "api.task_not_found"))

        # 复制业务字段（password / card_key 是 EncryptedString：读出来是明文，写回去自动加密）
        new_run = Run(
            email=src.email,
            password=src.password,
            profile_id=src.profile_id,
            card_key=src.card_key,
            browser_provider=src.browser_provider,
            card_provider=src.card_provider,
            mail_provider=src.mail_provider,
            mail_account_id=src.mail_account_id,
            config_snapshot=dict(src.config_snapshot or {}),
            # 重置：不继承源行的运行时副作用
            status="pending",
            phase="registration",
            retry_mode="restart",
            error_reason=None,
            openai_tokens={},
            account_tier="registered",
            is_card_warmed_up=False,
            warmup_pro_attempts=0,
            warmup_blocked_count=0,
            decline_attempts=0,
            ip_address="",
            ip_country="",
            card_bin="",
            phone_number="",
            sms_order_id="",
            warmup_account_id="",
        )
        session.add(new_run)
        session.commit()
        session.refresh(new_run)
        result = _run_to_dict(new_run)

    # 走 batch dispatcher 的 per-profile 串行队列（同 retry 路径，避免 AdsPower 并发冲突）
    from src.services.batch_register_service import requeue_runs
    result["worker_submitted"] = requeue_runs([new_run.id], broadcaster).get("requeued", 0) > 0
    result["cloned_from"] = task_id
    return result


@router.delete("/{task_id}")
def delete_task(
    request: Request,
    task_id: str,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
):
    """硬删除任务（级联 RunEvent + Checkpoint）。仅终态（success/failed/cancelled）可删，运行中任务必须先 cancel。"""
    with get_session() as session:
        run = session.get(Run, task_id)
        if not run:
            raise HTTPException(status_code=404, detail=tr(request, "api.task_not_found"))
        if run.status not in ("success", "failed", "cancelled"):
            raise HTTPException(
                status_code=400,
                detail=tr(request, "api.task_not_deletable", status=run.status),
            )
        session.exec(delete(RunEvent).where(RunEvent.run_id == task_id))
        session.exec(delete(Checkpoint).where(Checkpoint.run_id == task_id))
        session.delete(run)
        session.commit()
    return {"ok": True, "deleted": task_id}


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


class BatchActionRequest(BaseModel):
    """批量动作请求。

    action 限定为 cancel / retry / delete；task_ids 1-200 个。
    """
    action: str = Field(..., description="cancel / retry / delete")
    task_ids: list[str] = Field(..., min_length=1, max_length=200)


@router.post("/batch-actions")
def batch_actions(
    request: Request,
    body: BatchActionRequest,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """批量执行 cancel / retry / delete。

    返回 {succeeded: [...], skipped: [{id, reason}], total: N}。
    跳过原因：not_found / not_cancellable / not_retryable / running。
    """
    action = body.action.strip().lower()
    if action not in ("cancel", "retry", "delete"):
        raise HTTPException(status_code=422, detail=f"invalid action: {body.action!r}")

    # 去重保序
    seen: set[str] = set()
    task_ids: list[str] = []
    for tid in body.task_ids:
        tid = str(tid).strip()
        if tid and tid not in seen:
            seen.add(tid)
            task_ids.append(tid)
    if not task_ids:
        raise HTTPException(status_code=422, detail="task_ids 不能为空")

    succeeded: list[str] = []
    skipped: list[dict] = []

    with get_session() as session:
        for tid in task_ids:
            run = session.get(Run, tid)
            if not run:
                skipped.append({"id": tid, "reason": "not_found"})
                continue

            if action == "cancel":
                if run.status in ("success", "completed", "cancelled"):
                    skipped.append({"id": tid, "reason": "not_cancellable"})
                    continue
                run.status = "cancelled"
                run.updated_at = datetime.now(timezone.utc)
                session.add(run)
                succeeded.append(tid)

            elif action == "delete":
                # 与单删 endpoint 对齐：仅终态（success/failed/cancelled）可删，级联 Checkpoint
                if run.status not in ("success", "failed", "cancelled"):
                    reason = "running" if run.status == "running" else "not_terminal"
                    skipped.append({"id": tid, "reason": reason})
                    continue
                session.exec(delete(RunEvent).where(RunEvent.run_id == tid))
                session.exec(delete(Checkpoint).where(Checkpoint.run_id == tid))
                session.delete(run)
                succeeded.append(tid)

            elif action == "retry":
                if run.status not in ("failed", "cancelled"):
                    skipped.append({"id": tid, "reason": "not_retryable"})
                    continue
                succeeded.append(tid)

        session.commit()

    # 副作用（事务外，避免锁竞争）
    if action == "cancel":
        for tid in succeeded:
            request_task_cancel(tid, broadcaster)
    elif action == "retry" and succeeded:
        from src.api.worker import _clear_cancel_requested
        from src.services.batch_register_service import requeue_runs
        for tid in succeeded:
            _clear_cancel_requested(tid)
        requeue_runs(succeeded, broadcaster)

    return {"succeeded": succeeded, "skipped": skipped, "total": len(task_ids)}


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


# ── 批量注册端点 ─────────────────────────────────────


@router.post("/batch")
def create_batch(
    body: BatchRegisterRequest,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
    broadcaster: EventBroadcaster = Depends(get_event_broadcaster),
):
    """批量起 N 个注册任务（多 profile 并发，每个 profile 内部按抖动间隔串行）。"""
    from src.services.batch_register_service import start_batch
    try:
        return start_batch(
            count=body.count,
            profile_id=body.profile_id,
            profile_ids=body.profile_ids,
            password=body.password,
            interval_min_sec=body.interval_min_sec,
            interval_max_sec=body.interval_max_sec,
            mode=body.mode,
            browser_provider=body.browser_provider,
            card_provider=body.card_provider,
            mail_provider=body.mail_provider,
            mail_account_id=body.mail_account_id,
            gender=body.gender,
            broadcaster=broadcaster,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/batch/{batch_id}")
def get_batch(
    batch_id: str,
    user: User = Depends(require_authenticated_user),
):
    """查批量任务状态（前端轮询用）。"""
    from src.services.batch_register_service import get_batch_status
    st = get_batch_status(batch_id)
    if st is None:
        raise HTTPException(status_code=404, detail="batch_id 不存在")
    return st


@router.get("/batch")
def list_batches(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_authenticated_user),
):
    """列出近期批量任务（process-local，重启即丢）。"""
    from src.services.batch_register_service import list_recent_batches
    return list_recent_batches(limit=limit)


@router.post("/batch/{batch_id}/cancel")
def cancel_batch_endpoint(
    batch_id: str,
    user: User = Depends(require_role("operator")),
    _csrf: None = Depends(require_csrf),
):
    """取消批量任务（已 submit 的不撤回，仅停后续 submit）。"""
    from src.services.batch_register_service import cancel_batch
    if not cancel_batch(batch_id):
        raise HTTPException(status_code=404, detail="batch_id 不存在")
    return {"ok": True, "batch_id": batch_id, "message": "已请求取消"}


# ── 工具 ──────────────────────────────────────────


def _run_to_dict(run: Run) -> dict:
    snapshot = dict(run.config_snapshot or {})
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
        "task_mode": str(snapshot.get("task_mode") or "full"),
        "registration_kind": str(snapshot.get("registration_kind") or "email"),
        # 注册平台（openai=GPT / grok），供前端区分两类任务；legacy 行回退 openai
        "platform": run.platform or "openai",
        "phone_number": run.phone_number,
        "sms_order_id": run.sms_order_id,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "updated_at": run.updated_at.isoformat() if run.updated_at else "",
    }
