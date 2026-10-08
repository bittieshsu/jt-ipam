"""報告排版（PDF）。前端把組好的報告大綱送來，這裡只負責排版成 PDF 檔。

不查任何資料：內容在前端組大綱時已依權限過濾，所以只要求登入；
排版吃 CPU 與記憶體，另有每人速率限制與同時份數上限（services/report_pdf）。
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from app.api.v1.dependencies import CurrentUser
from app.core.rate_limit import check_rate_limit
from app.core.ui_error import detail_of
from app.schemas.reports import ReportIn
from app.services import report_pdf

router = APIRouter(prefix="/reports", tags=["reports"])

_STATUS = {"report_pdf_no_font": 503, "report_pdf_busy": 429}


def _disposition(name: str) -> str:
    """下載檔名：ASCII 退路＋RFC 5987 的 UTF-8 檔名（中文檔名在各瀏覽器都正確）。"""
    base = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", name).strip(" .") or "report"
    base = base[:120]
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "report"
    return f"attachment; filename=\"{ascii_name}.pdf\"; filename*=UTF-8''{quote(base + '.pdf')}"


@router.post("/pdf", response_class=Response,
             responses={200: {"content": {"application/pdf": {}}, "description": "PDF 檔"}})
async def render_report_pdf(payload: ReportIn, user: CurrentUser) -> Response:
    await check_rate_limit(bucket=f"rl:report_pdf:user:{user.id}", rate="30/minute")
    try:
        data = await report_pdf.render_pdf(payload)
    except report_pdf.ReportPdfError as exc:
        raise HTTPException(_STATUS.get(exc.code or "", 500), detail=detail_of(exc, "report_pdf_failed")) from exc
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": _disposition(payload.filename),
                             "Cache-Control": "no-store"})
