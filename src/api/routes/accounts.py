# -*- coding: utf-8 -*-
"""
账号池管理 API

GET    /api/accounts?tier=registered&limit=50  — 列出指定段位的号
GET    /api/accounts/{run_id}                  — 单号详情
POST   /api/accounts/{run_id}/bind-link        — 生成绑卡 checkout 链接
POST   /api/accounts/{run_id}/promote          — 晋级（plus / team）
POST   /api/accounts/{run_id}/abandon          — 标记放弃

权限：admin only
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.api.security import require_csrf, require_role
from src.db.models import User
from src.services.account_pool_service import (
    FMT_CPA_JSON,
    FMT_CREDENTIALS_CSV,
    TIER_REGISTERED,
    abandon as svc_abandon,
    assign_card as svc_assign_card,
    export_pool as svc_export_pool,
    generate_bind_link as svc_bind_link,
    generate_link as svc_generate_link,
    generate_link_standalone as svc_generate_link_standalone,
    get_account_detail,
    import_pool as svc_import_pool,
    list_pool,
    promote as svc_promote,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class BindLinkRequest(BaseModel):
    card_key: str = Field(..., min_length=1, max_length=64)
    plan: str = Field(default="team", description="team / plus")


class GenerateLinkRequest(BaseModel):
    plan: str = Field(default="team", description="team / plus / pro / pro_lite")
    return_mode: str = Field(default="long", description="long / app")
    seat_quantity: int = Field(default=1, ge=1, le=200)
    promo_code: str = Field(default="", max_length=80)
    promo_campaign_id: str = Field(default="", max_length=80)
    aimizy_country: str = Field(default="", max_length=8)
    aimizy_currency: str = Field(default="", max_length=8)
    workspace_name: str = Field(default="", max_length=120)
    proxy_id: Optional[int] = Field(default=None, description="代理池 id；None = 用 .env 默认 PROXY")
    # P4 模板化扩展字段（2026-05-25）
    schema_version: Optional[str] = Field(default=None, max_length=20, description="单点覆盖 schema 版本")
    url_locale: Optional[str] = Field(default=None, max_length=10, description="URL 注入 ?locale=xxx")
    extra_payload: Optional[dict] = Field(default=None, description="浅合并到 OpenAI payload")
    # P6 暴露 checkout_ui_mode（hosted=pay.openai.com 长链 / custom=chatgpt.com 站内 checkout）
    checkout_ui_mode: str = Field(default="hosted", description="hosted / custom；仅 Plus 真生效")


class CheckoutLinkStandaloneRequest(BaseModel):
    """无 run_id 版生成请求：用户在独立页面手动喂 access_token。

    字段命名与 GenerateLinkRequest 保持一致，方便共用前端 form 逻辑；
    额外加 ``access_token`` 必填字段，由前端 password textarea 收集。
    """
    access_token: str = Field(..., min_length=1, description="ChatGPT API access token（不入库）")
    plan: str = Field(default="team", description="team / plus / pro / pro_lite")
    return_mode: str = Field(default="long", description="long / app")
    seat_quantity: int = Field(default=1, ge=1, le=200)
    promo_code: str = Field(default="", max_length=80)
    promo_campaign_id: str = Field(default="", max_length=80)
    aimizy_country: str = Field(default="", max_length=8)
    aimizy_currency: str = Field(default="", max_length=8)
    workspace_name: str = Field(default="", max_length=120)
    proxy_id: Optional[int] = Field(default=None, description="代理池 id；None = 用 .env 默认 PROXY")
    schema_version: Optional[str] = Field(default=None, max_length=20)
    url_locale: Optional[str] = Field(default=None, max_length=10)
    extra_payload: Optional[dict] = Field(default=None)
    checkout_ui_mode: str = Field(default="hosted", description="hosted / custom")


class AssignCardRequest(BaseModel):
    card_key: str = Field(..., min_length=1, max_length=64)
    note: str = Field(default="", max_length=200)


class PromoteRequest(BaseModel):
    tier: str = Field(..., description="plus / team")


class AbandonRequest(BaseModel):
    reason: str = "manual"


@router.get("")
def list_accounts(
    tier: str = Query(default=TIER_REGISTERED, description="registered / plus / team / abandoned"),
    platform: Optional[str] = Query(default=None, description="openai / grok / all（不传=全部平台）"),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_role("admin")),
):
    """列出指定段位的账号，可选按注册平台（openai/grok）过滤。"""
    try:
        return list_pool(tier=tier, platform=platform, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/export")
def export_accounts(
    tier: str = Query(default=TIER_REGISTERED, description="registered / plus / team / abandoned"),
    fmt: str = Query(default=FMT_CREDENTIALS_CSV, description="credentials_csv / cpa_json"),
    run_ids: Optional[str] = Query(
        default=None,
        description="可选：逗号分隔的 run_id 列表（仅导出选中行）；不传则导出整个 tier",
    ),
    platform: Optional[str] = Query(default=None, description="openai / grok / all（不传=全部平台）"),
    user: User = Depends(require_role("admin")),
):
    """导出指定段位的号池（CSV / JSON 下载）。

    支持两种模式：
    - 不传 run_ids → 导出该 tier 下所有 status=success 的号
    - 传 run_ids='id1,id2,...' → 仅导出选中行（用于"导出选中"功能）
    """
    ids_list: Optional[list[str]] = None
    if run_ids is not None:
        ids_list = [s.strip() for s in run_ids.split(",") if s.strip()]
    try:
        content, content_type, filename, skipped = svc_export_pool(
            tier, fmt, run_ids=ids_list, platform=platform
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # cpa_json 格式可能因 JWT 解析失败跳过部分 Run；通过 header 暴露跳过信息
    # 前端可读取 X-Export-Skipped-Count 决定是否提示运维去查日志
    response_headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if skipped:
        response_headers["X-Export-Skipped-Count"] = str(len(skipped))
        # 把跳过明细打日志（不进 response body，避免污染下游解析）
        logger.warning(
            "号池导出跳过 %d 条 (fmt=%s tier=%s)：%s",
            len(skipped), fmt, tier,
            ", ".join(f"{s['email']}({s['reason']})" for s in skipped[:5]),
        )
    return Response(
        content=content,
        media_type=content_type,
        headers=response_headers,
    )


@router.post("/import")
async def import_accounts(
    file: UploadFile = File(...),
    fmt: str = Form(default=FMT_CREDENTIALS_CSV, description="credentials_csv / cpa_json"),
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """导入号池（补号池语义：外部账号入库为 registered）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        result = svc_import_pool(raw, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("号池导入失败 fmt=%s", fmt)
        raise HTTPException(status_code=500, detail=f"内部错误: {exc}")
    return result


@router.post("/checkout-link")
def generate_checkout_link_standalone(
    body: CheckoutLinkStandaloneRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """脱离号池直接生成 checkout 链接 —— 用户在独立页 ``/checkout-link`` 手动喂 access_token。

    与 ``POST /{run_id}/generate-link`` 的差异：
      - 不绑 run_id，不查 Run 表，不写 RunEvent
      - access_token 直接由请求体提供（**不入库**，每次请求都要带）
      - 返回结构相同：``{"link": str, "plan": str, "return_mode": str}``

    路由顺序关键：必须放在 ``GET /{run_id}`` 之前，否则 "checkout-link" 会被
    当作 run_id 路径参数匹配。FastAPI 按声明顺序匹配静态路径优于动态路径，
    本端点声明在 ``@router.get("/{run_id}")`` 之上即可确保正确路由。
    """
    proxy_url: Optional[str] = None
    if body.proxy_id is not None:
        from src.services.proxy_service import get_proxy

        proxy = get_proxy(int(body.proxy_id), with_url=True)
        if proxy is None:
            raise HTTPException(status_code=400, detail=f"代理不存在: id={body.proxy_id}")
        if not proxy.get("is_active"):
            raise HTTPException(status_code=400, detail=f"代理已停用: {proxy.get('label')}")
        proxy_url = proxy.get("url") or None

    try:
        return svc_generate_link_standalone(
            body.access_token,
            plan=body.plan,
            return_mode=body.return_mode,
            seat_quantity=body.seat_quantity,
            promo_code=body.promo_code,
            promo_campaign_id=body.promo_campaign_id,
            aimizy_country=body.aimizy_country,
            aimizy_currency=body.aimizy_currency,
            workspace_name=body.workspace_name,
            proxy=proxy_url,
            schema_version=body.schema_version,
            url_locale=body.url_locale,
            extra_payload=body.extra_payload,
            checkout_ui_mode=body.checkout_ui_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("standalone checkout 链接生成失败")
        raise HTTPException(status_code=500, detail=f"内部错误: {exc}")


@router.get("/{run_id}")
def get_account(
    run_id: str,
    user: User = Depends(require_role("admin")),
):
    """单号详情。"""
    detail = get_account_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return detail


@router.post("/{run_id}/generate-link")
def generate_checkout_link_endpoint(
    run_id: str,
    body: GenerateLinkRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """生成 hosted checkout 链接（无卡）。

    支持 team / plus / pro / pro_lite + 长链/短链 + URL promo code 拼接 +
    payload promo campaign 覆盖 + 按 proxy_id 选 IP 出口。
    Pro 系列实验性，失败时看返回 detail。
    """
    # 解析 proxy_id → 真实代理 URL（None = 走 .env 默认）
    proxy_url: Optional[str] = None
    if body.proxy_id is not None:
        from src.services.proxy_service import get_proxy

        proxy = get_proxy(int(body.proxy_id), with_url=True)
        if proxy is None:
            raise HTTPException(status_code=400, detail=f"代理不存在: id={body.proxy_id}")
        if not proxy.get("is_active"):
            raise HTTPException(status_code=400, detail=f"代理已停用: {proxy.get('label')}")
        proxy_url = proxy.get("url") or None

    try:
        return svc_generate_link(
            run_id,
            plan=body.plan,
            return_mode=body.return_mode,
            seat_quantity=body.seat_quantity,
            promo_code=body.promo_code,
            promo_campaign_id=body.promo_campaign_id,
            aimizy_country=body.aimizy_country,
            aimizy_currency=body.aimizy_currency,
            workspace_name=body.workspace_name,
            proxy=proxy_url,
            # P4 模板化扩展字段
            schema_version=body.schema_version,
            url_locale=body.url_locale,
            extra_payload=body.extra_payload,
            # P6 checkout_ui_mode
            checkout_ui_mode=body.checkout_ui_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("生成 checkout 链接失败 run=%s", run_id[:12])
        raise HTTPException(status_code=500, detail=f"内部错误: {exc}")


@router.post("/{run_id}/assign-card")
def assign_card_endpoint(
    run_id: str,
    body: AssignCardRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """登记"这个号准备用这张卡"——仅留痕，不调 ChatGPT API。"""
    try:
        return svc_assign_card(run_id, body.card_key, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{run_id}/bind-link")
def generate_bind_link(
    run_id: str,
    body: BindLinkRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """[Deprecated] 旧的"选卡 + 生成链接"一体化端点。

    保留兼容老调用方。新逻辑走 generate-link / assign-card 两个端点。
    """
    try:
        return svc_bind_link(run_id, card_key=body.card_key, plan=body.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("生成绑卡链接失败 run=%s", run_id[:12])
        raise HTTPException(status_code=500, detail=f"内部错误: {exc}")


@router.post("/{run_id}/promote")
def promote_account(
    run_id: str,
    body: PromoteRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """绑卡成功后晋级到 plus / team 池子。"""
    try:
        return svc_promote(run_id, body.tier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{run_id}/abandon")
def abandon_account(
    run_id: str,
    body: Optional[AbandonRequest] = None,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """绑卡失败 → 标记放弃。"""
    try:
        return svc_abandon(run_id, (body.reason if body else "manual") or "manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
