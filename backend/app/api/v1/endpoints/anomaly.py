"""異常偵測 endpoint：trigger run + read latest report。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.services.anomaly import run_detection

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.post("/scan", dependencies=[Depends(require_admin)])
async def scan(
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """執行所有偵測規則。Phase 2 的排程版（Celery beat）會週期觸發此邏輯。"""
    report = await run_detection(session, notify_admins=True)
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="anomaly",
        object_id=None,
        action="scan",
        diff={
            "ip_conflicts": len(report.ip_conflicts),
            "mac_drifts": len(report.mac_drifts),
            "ghost_ips": len(report.ghost_ips),
            "unauthorized_ips": len(report.unauthorized_ips),
            "rogue_dhcp": len(report.rogue_dhcp),
            "external_exposure": len(report.external_exposure),
            "dangling_dns": len(report.dangling_dns),
            "duplicate_ip_records": len(report.duplicate_ip_records),
            "suspicious_changes": len(report.suspicious_changes),
        },
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return report.to_dict()

@router.post("/triage", dependencies=[Depends(require_admin)])
async def triage(
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """對單一 IP 產 AI 鑑識卡（未授權 IP 的判讀）。

    admin 限定：跟 /scan 同級 —— 它會呼叫 LLM（成本），且判讀對象多半是異常清單
    裡的項目。證據彙整走 get_ip_history（RBAC 同規），LLM 拿到的是定界後的快照。
    """
    import ipaddress as _ipaddr

    from fastapi import HTTPException

    from app.services.ip_triage import triage_ip

    ip = str(payload.get("ip") or "").strip()
    try:
        _ipaddr.ip_address(ip)
    except ValueError:
        raise HTTPException(422, detail="請提供合法的 IP") from None
    try:
        result = await triage_ip(session, user, ip)
    except Exception as exc:
        # LLM 沒開／連不上要回可讀訊息，不是 500（跟 AI chat 同一課）
        raise HTTPException(502, detail=f"AI 判讀失敗：{exc}") from exc
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="anomaly", object_id=None, action="triage", diff={"ip": ip})
    return result



@router.get("/fw-rule-changes", dependencies=[Depends(require_admin)])
async def fw_rule_changes(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    """防火牆規則異動歷史（哨兵快照，admin 限定 —— 規則內容屬純管理資料）。

    通知只給摘要；這裡回完整 diff，讓「細節到快照裡看」是真的做得到的事。
    """
    from sqlalchemy import select as _select

    from app.models.fw_snapshot import FwRuleSnapshot

    limit = max(1, min(int(limit or 50), 200))
    rows = (await session.execute(
        _select(FwRuleSnapshot).order_by(FwRuleSnapshot.taken_at.desc()).limit(limit)
    )).scalars().all()
    return {"items": [{
        "id": str(r.id), "source_type": r.source_type, "instance_name": r.instance_name,
        "taken_at": r.taken_at.isoformat(), "rule_count": r.rule_count,
        "is_baseline": r.diff is None,
        "diff": r.diff,
    } for r in rows]}
