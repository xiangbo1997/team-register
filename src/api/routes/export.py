# -*- coding: utf-8 -*-
"""
数据导出 API

GET /api/export/csv  — 导出成功账号
GET /api/stats       — 统计概览
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse
from sqlmodel import select, func

from src.api.security import require_authenticated_user
from src.db.engine import get_session
from src.db.models import Run, User

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/export/csv")
def export_csv(user: User = Depends(require_authenticated_user)):
    """导出成功账号为 CSV。"""
    with get_session() as session:
        runs = session.exec(
            select(Run).where(Run.status == "success").order_by(Run.created_at.desc())  # type: ignore
        ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Email", "Phase", "Created At"])
    for run in runs:
        writer.writerow([
            run.id,
            run.email,
            run.phase,
            run.created_at.isoformat() if run.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=accounts_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"},
    )


@router.get("/stats")
def get_stats(user: User = Depends(require_authenticated_user)):
    """统计概览。"""
    with get_session() as session:
        total = session.exec(select(func.count()).select_from(Run)).one()
        by_status = {}
        for status in ("pending", "running", "success", "failed", "cancelled"):
            count = session.exec(
                select(func.count()).select_from(Run).where(Run.status == status)
            ).one()
            by_status[status] = count

    return {
        "total": total,
        "by_status": by_status,
        "success_rate": f"{by_status['success'] / total * 100:.1f}%" if total > 0 else "0%",
    }
