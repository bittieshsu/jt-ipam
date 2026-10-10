"""前端產生的匯出檔，由瀏覽器回報一筆稽核。

表格匯出（CSV／XLSX／ODS／ODT／Markdown／TXT／PDF）與報告匯出（DOCX／ODT／XLSX）都在瀏覽器裡用
已經取得、而且已依權限過濾過的資料組成檔案，伺服器看不到「有人把這份清單存下來了」。這裡讓前端在
存檔時回報一筆：哪個畫面、什麼格式、幾列。

這是**瀏覽器自己回報的**：改過的前端或直接呼叫 API 的人可以不回報（他們本來就拿得到同樣的資料）。
它補的是「一般使用者把清單匯出帶走」這件事的可追溯性，不是防止資料外流的控制；伺服器端產生的
匯出（子網路 CSV、報告 PDF、系統匯出、憑證私鑰、IP 變更評估）各自在伺服器端留稽核。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.rate_limit import check_rate_limit
from app.schemas.base import StrictModel

router = APIRouter(prefix="/export-events", tags=["audit"])

#: 副檔名（csv、xlsx、svg、drawio…）；只收短英數，不是任意字串
ExportFormat = Annotated[str, Field(pattern=r"^[a-z0-9]{1,8}$")]


class ExportEventIn(StrictModel):
    #: 匯出的來源（畫面／表格的識別，例如 "devices"、"rack-R01"）
    source: Annotated[str, Field(min_length=1, max_length=120)]
    format: ExportFormat
    rows: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    filename: Annotated[str, Field(max_length=200)] = ""


@router.post("", status_code=204)
async def report_export(
    payload: ExportEventIn,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    # 每人限流：這是任何登入帳號都能呼叫的寫入，不能拿來洗稽核記錄
    await check_rate_limit(bucket=f"rl:export_event:user:{user.id}", rate="120/minute")
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="export", object_id=None, action="export_client",
        diff={"source": payload.source, "format": payload.format, "rows": payload.rows,
              "filename": payload.filename, "reported_by": "browser"},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
