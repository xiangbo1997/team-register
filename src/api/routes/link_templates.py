# -*- coding: utf-8 -*-
"""
链接模板 API（号池"生成链接"弹窗的表单快照）

GET    /api/link-templates                  — 列出全部
POST   /api/link-templates                  — 创建（name 唯一）
DELETE /api/link-templates/{template_id}    — 删除

权限：admin only
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.api.security import require_csrf, require_role
from src.db.models import User
from src.services.link_template_service import (
    create_template,
    delete_template,
    list_presets,
    list_templates,
)
from src.services.promo_eligibility_service import (
    PromoVerifyError,
    bulk_verify_all_templates,
    verify_link_template,
)
from src.services.promo_import_service import (
    DEFAULT_KNOWN_CODES_PATH,
    import_from_known_codes,
    import_from_uploaded,
)

# 上传导入文件大小上限（4 MB）：known_codes.json 通常 < 100KB，留足余量防滥用
_MAX_IMPORT_UPLOAD_BYTES = 4 * 1024 * 1024

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/link-templates", tags=["link-templates"])


class CreateTemplateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    plan: str = Field(default="team", description="team / plus / pro / pro_lite")
    seat_quantity: int = Field(default=1, ge=1, le=200)
    promo_code: str = Field(default="", max_length=80)
    promo_campaign_id: str = Field(default="", max_length=80)
    aimizy_country: str = Field(default="", max_length=8)
    aimizy_currency: str = Field(default="", max_length=8)
    workspace_name: str = Field(default="", max_length=120)
    return_mode: str = Field(default="long", description="long / app")
    proxy_id: Optional[int] = Field(default=None, description="可选关联代理 id")
    # P2 模板化扩展字段（2026-05-25）
    schema_version: Optional[str] = Field(
        default=None,
        max_length=20,
        description="单点覆盖 schema 版本（如 plus_v2），None 用 .env 全局默认",
    )
    url_locale: Optional[str] = Field(
        default=None,
        max_length=10,
        description="生成 URL 后注入 ?locale=xxx（en/ja/zh-CN 等），None 不注入",
    )
    extra_payload_json: Optional[dict] = Field(
        default=None,
        description="浅合并到 OpenAI payload 的字典；低代码扩展逃生口",
    )
    is_preset: bool = Field(
        default=False,
        description="True 时模板以快捷按钮形式呈现在号池弹窗",
    )
    sort_order: int = Field(
        default=0,
        ge=0,
        le=9999,
        description="预设按钮排序（小→大）",
    )
    # P6 暴露 checkout_ui_mode
    checkout_ui_mode: Optional[str] = Field(
        default=None,
        max_length=10,
        description="hosted / custom；仅 Plus schema 真生效",
    )


@router.get("")
def list_link_templates(
    user: User = Depends(require_role("admin")),
):
    """列出所有保存的链接模板（admin only）。"""
    return list_templates()


@router.get("/presets")
def list_link_template_presets(
    user: User = Depends(require_role("admin")),
):
    """列出预设模板（is_preset=True），按 sort_order 升序。

    号池弹窗前端用此 API 渲染快捷按钮（取代硬编码的 applyPreset）。
    """
    return list_presets()


@router.post("")
def create_link_template(
    body: CreateTemplateRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """创建模板。name 必须唯一。proxy_id 可选，指定时校验存在。"""
    try:
        return create_template(
            name=body.name,
            plan=body.plan,
            seat_quantity=body.seat_quantity,
            promo_code=body.promo_code,
            promo_campaign_id=body.promo_campaign_id,
            aimizy_country=body.aimizy_country,
            aimizy_currency=body.aimizy_currency,
            workspace_name=body.workspace_name,
            return_mode=body.return_mode,
            proxy_id=body.proxy_id,
            # P2 模板化扩展字段
            schema_version=body.schema_version,
            url_locale=body.url_locale,
            extra_payload_json=body.extra_payload_json,
            is_preset=body.is_preset,
            sort_order=body.sort_order,
            # P6 checkout_ui_mode
            checkout_ui_mode=body.checkout_ui_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{template_id}")
def delete_link_template(
    template_id: int,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """删除模板；不存在返回 404。"""
    if not delete_template(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"deleted": True, "id": template_id}


class VerifyEligibilityRequest(BaseModel):
    """可选指定借哪个账号的 token 调 ChatGPT API；为空时取最近 status=completed 的账号。"""
    run_id: Optional[str] = Field(default=None, max_length=64)


@router.post("/{template_id}/verify-eligibility")
def verify_link_template_eligibility(
    template_id: int,
    body: VerifyEligibilityRequest = VerifyEligibilityRequest(),
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """手动触发该模板 promo_code 的 eligibility 验证。

    流程：取模板 → 借账号 token → 按国家选代理 → 调 ChatGPT API → 回写 last_eligibility_*
    返回：{status, checked_at, metadata, used_run_id}
    """
    try:
        return verify_link_template(template_id, run_id=body.run_id)
    except PromoVerifyError as exc:
        # 业务前置错误（模板不存在/无 token/无 promo_code）映射为 400
        # client 端展示 exc.message；exc.code 给前端做分类（如"no_token"提示去登录）
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": exc.message},
        )


# ── 促销码专用便捷端点（promo_codes 页面调用）──────────────────────────


class CreatePromoRequest(BaseModel):
    """轻量"添加单个促销码"请求：只要 country + code，其他字段服务侧默认。"""
    country: str = Field(..., min_length=2, max_length=8)
    code: str = Field(..., min_length=1, max_length=80)
    aimizy_currency: str = Field(default="", max_length=8)
    seat_quantity: int = Field(default=2, ge=1, le=200)


@router.post("/promo")
def create_promo_template(
    body: CreatePromoRequest,
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """便捷创建 promo 模板：自动按 promo-{country}-{code} 命名 + plan=team + seat=2。

    用于 promo_codes 页面"添加单个促销码"对话框，不需要填 workspace_name 等无关字段。
    """
    country = body.country.strip().upper()
    code = body.code.strip()
    if not country or not code:
        raise HTTPException(status_code=400, detail="country 与 code 都不能为空")
    try:
        return create_template(
            name=f"promo-{country.lower()}-{code.lower()}",
            plan="team",
            seat_quantity=body.seat_quantity,
            promo_code=code,
            promo_campaign_id="",
            aimizy_country=country,
            aimizy_currency=body.aimizy_currency.strip().upper(),
            workspace_name="",
            return_mode="long",
            proxy_id=None,
        )
    except ValueError as exc:
        # name 已存在 / 字段超长 等 → 400
        raise HTTPException(status_code=400, detail=str(exc))


class ImportFromScannerRequest(BaseModel):
    """从扫描器 known_codes.json 导入。"""
    source_path: Optional[str] = Field(
        default=None,
        description=f"known_codes.json 绝对路径；为空时用默认 {DEFAULT_KNOWN_CODES_PATH}",
    )
    dry_run: bool = Field(default=False, description="True 时只返回计划不写库")


@router.post("/import-from-scanner")
def import_from_scanner(
    body: ImportFromScannerRequest = ImportFromScannerRequest(),
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """一键从 gpt-promo-scanner/known_codes.json 导入促销码到 LinkTemplate。

    幂等：按 LinkTemplate.name 去重，已存在的跳过；失败逐条记录不阻断。
    返回：{source, planned, skipped_existing, created, failed, dry_run, details}
    """
    from pathlib import Path as _Path
    src_path = _Path(body.source_path) if body.source_path else None
    try:
        return import_from_known_codes(source_path=src_path, dry_run=body.dry_run)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail={"code": "source_not_found", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_source", "message": str(exc)})
    except Exception as exc:
        # JSON 解析错误等
        logger.exception("import_from_scanner 失败")
        raise HTTPException(status_code=500, detail={"code": "import_failed", "message": str(exc)})


# 仅允许 JSON 文件（后端真正防线，前端 accept 可被绕过）
_ALLOWED_IMPORT_CONTENT_TYPES = frozenset({
    "application/json", "text/json", "application/octet-stream",  # 部分浏览器对 .json 用 octet-stream
    "text/plain",  # 有些系统给 .json 标 text/plain
})


@router.post("/import-upload")
async def import_upload(
    file: UploadFile = File(..., description="promo 导入模板 JSON 文件（见 /import-template）"),
    dry_run: bool = Form(default=False),
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """上传 promo 导入模板文件直接导入（不依赖服务器本地有该文件）。

    与 import-from-scanner 共用 _import_payload 核心逻辑（同源真相）。
    仅接受 .json 文件；格式见 GET /import-template 下载的模板。
    返回：{source, planned, skipped_existing, created, failed, dry_run, details}
    """
    # 文件类型校验：扩展名 + content-type 双重把关
    filename = (file.filename or "").lower()
    if not filename.endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="只支持 .json 文件，请下载模板填写后上传",
        )
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if ctype and ctype not in _ALLOWED_IMPORT_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"文件类型不支持（{ctype}）；请上传 JSON 文件",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > _MAX_IMPORT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大（>{_MAX_IMPORT_UPLOAD_BYTES // 1024 // 1024}MB）",
        )
    try:
        return import_from_uploaded(raw, dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("import_upload 失败")
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/import-template")
def import_template(
    user: User = Depends(require_role("admin")),
):
    """下载 promo 导入模板（带示例 + 字段说明）。

    模板结构与 import_from_uploaded 的解析器（_iter_valid / _iter_expired）
    同源：valid 按国家分组，expired 用 region 字段。运维下载填好后上传即可。
    """
    from fastapi.responses import Response

    template = {
        "_说明": "promo 码导入模板。删除所有 _开头的说明字段后上传，或保留也可（会被忽略）。",
        "_字段说明": {
            "valid": "有效码，按国家码分组（US/GB/CA/JP...），每国一个数组",
            "valid[].code": "必填，promo 码本身",
            "valid[].company": "可选，公司名（存入 import_note）",
            "valid[].discount_pct": "可选，折扣百分比（数字，如 50）",
            "valid[].duration_months": "可选，折扣月数（数字）",
            "valid[].price_usd": "可选，美元价（数字）",
            "valid[].price_local": "可选，本地货币价（字符串）",
            "expired": "过期码数组（可选），用 region 字段标国家",
            "expired[].code": "必填，promo 码",
            "expired[].region": "国家码（如 US）",
            "expired[].note": "可选，过期备注",
        },
        "valid": {
            "US": [
                {
                    "code": "exampleuscode",
                    "company": "Example Inc",
                    "discount_pct": 50,
                    "duration_months": 12,
                    "price_usd": 25,
                },
            ],
            "GB": [
                {
                    "code": "examplegbcode",
                    "company": "Example UK Ltd",
                    "discount_pct": 30,
                    "duration_months": 6,
                },
            ],
        },
        "expired": [
            {
                "code": "oldexpiredcode",
                "region": "US",
                "note": "2026-01 已过期，仅留档",
            },
        ],
    }
    body = json.dumps(template, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="promo_import_template.json"',
        },
    )


class BulkVerifyRequest(BaseModel):
    run_id: Optional[str] = Field(default=None, max_length=64)
    delay_sec: float = Field(default=0.5, ge=0, le=5, description="每条调用间隔秒数，防 Cloudflare 限流")
    stop_on_token_error: bool = Field(default=True, description="True 时遇 401 立即停止")


@router.post("/bulk-verify")
def bulk_verify_promo(
    body: BulkVerifyRequest = BulkVerifyRequest(),
    user: User = Depends(require_role("admin")),
    _csrf: None = Depends(require_csrf),
):
    """批量验证所有 promo_code 非空的模板。

    流程：取所有有 promo_code 的模板 → 借同一个 token → 逐条 verify（含 0.5s 间隔）
    返回：{total, verified, by_status, failed, used_run_id, stopped_early}
    """
    try:
        return bulk_verify_all_templates(
            run_id=body.run_id,
            delay_sec=body.delay_sec,
            stop_on_token_error=body.stop_on_token_error,
        )
    except PromoVerifyError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": exc.message},
        )
