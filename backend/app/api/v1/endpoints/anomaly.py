"""異常偵測 endpoint：trigger run + read latest report。"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin, require_global_read
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
            "fw_rule_rot": len(report.fw_rule_rot),
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
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="triage", diff={"ip": ip})
    return result



@router.get("/fw-rule-changes", dependencies=[Depends(require_admin)])
async def fw_rule_changes(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    """防火牆規則異動歷史（異動偵測快照，admin 限定 —— 規則內容屬純管理資料）。

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
        "ack": None if r.ack_at is None else {
            "at": r.ack_at.isoformat(), "note": r.ack_note or "",
        },
    } for r in rows]}

@router.post("/fw-rule-changes/{snapshot_id}/analyze", dependencies=[Depends(require_admin)])
async def fw_rule_change_analyze(
    snapshot_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """對一筆規則異動產 AI 解讀卡（admin 限定，按需觸發）。

    偵測與告警永遠是確定性的；這裡是解讀層 —— 帶上目標位址的全系統整合證據
    （IPAM／ARP／Wazuh／DNS／NAT 曝露／虛擬化／管理單位）讓模型判讀。
    """
    from fastapi import HTTPException

    from app.models.fw_snapshot import FwRuleSnapshot
    from app.services.fw_review import analyze_change

    snap = await session.get(FwRuleSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, detail="找不到這筆快照")
    if not snap.diff:
        raise HTTPException(422, detail="初次快照是比對基準，沒有異動可以解讀")
    try:
        result = await analyze_change(session, user, snap)
    except Exception as exc:
        raise HTTPException(502, detail=f"AI 解讀失敗：{exc}") from exc
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="fw_analyze",
        diff={"snapshot": str(snapshot_id)})
    return result

@router.post("/fw-rule-changes/{snapshot_id}/ack", dependencies=[Depends(require_admin)])
async def fw_rule_change_ack(
    snapshot_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """認領一筆規則異動：這是已知變更＋說明（合規證據鏈）。

    沒被認領的異動累積起來就是稽核報表：「本月 N 筆防火牆變更，M 筆無人說明」。
    """
    from datetime import UTC, datetime

    from fastapi import HTTPException

    from app.models.fw_snapshot import FwRuleSnapshot

    snap = await session.get(FwRuleSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, detail="找不到這筆快照")
    if snap.diff is None:
        raise HTTPException(422, detail="初次快照是比對基準，不需要認領")
    snap.ack_by = user.id
    snap.ack_at = datetime.now(UTC)
    snap.ack_note = str(payload.get("note") or "")[:500]
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="fw_ack",
        diff={"snapshot": str(snapshot_id), "note": snap.ack_note})
    await session.commit()
    return {"ok": True, "at": snap.ack_at.isoformat()}

@router.get("/attack-surface")
async def get_attack_surface(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    _gr: Annotated[None, Depends(require_global_read)],
) -> dict[str, Any]:
    """對外開放服務清單（require_global_read：稽核員這類萬用唯讀帳號正是它的受眾）。

    只列明確可判定的對外開口；目的是別名／any／網段的規則不展開猜測 ——
    稽核拿去簽名的清單不能有猜的成分。
    """
    from app.services.fw_lookup import attack_surface

    items = await attack_surface(session)
    return {"items": items,
            "note": "僅列明確可判定的對外開口（NAT 轉發與目的為單一 IP 的 WAN 放行）；"
                    "目的為別名／any／網段的規則未展開；FortiGate 待各廠牌逐一驗證後納入。"}

