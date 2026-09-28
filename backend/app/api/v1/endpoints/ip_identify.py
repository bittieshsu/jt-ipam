"""IP 詳細頁的「探測」（只有管理員能用）。

由負責該子網路的掃描代理對這個 IP 做非侵入式識別（見 services/ip_identify 與代理端
`_job_run_identify`），結果經工作佇列回來。沿用工具頁的代理探測佇列：代理只由內往外連，
後端不會主動連到任何主機。

- 目標固定是這筆 IP 記錄的位址，使用者不能指定別的目標
- 同一個 IP 同時只能有一個探測在排隊或執行
- 每次發起都寫稽核
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import detail_of, ui_detail
from app.models.address import IPAddress
from app.models.agent_probe_job import STATUS_DONE, STATUS_PENDING, STATUS_RUNNING, AgentProbeJob
from app.models.scan_agent import ScanAgent
from app.models.subnet import Subnet
from app.services import ip_identify
from app.services.agent_probe import ProbeJobError, create_job, expire_stale
from app.services.oui import vendor_for_mac

router = APIRouter(prefix="/addresses", tags=["addresses"], dependencies=[Depends(require_admin)])


def _ip_text(ip: IPAddress) -> str:
    return str(ip.ip).split("/", 1)[0]


def _jobs_of(ip_text: str):  # type: ignore[no-untyped-def]
    return select(AgentProbeJob).where(
        AgentProbeJob.kind == "identify",
        AgentProbeJob.params["targets"][0].astext == ip_text)


async def _ip_or_404(session: AsyncSession, address_id: uuid.UUID) -> IPAddress:
    ip = await session.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=404, detail="Address not found")
    return ip


async def _brief(session: AsyncSession, ip: IPAddress, job: AgentProbeJob,
                 mac_vendor: str | None) -> dict[str, Any]:
    """清單用的精簡版：不帶整包原始結果，但帶摘要（清單上就看得出是什麼）。"""
    agent = await session.get(ScanAgent, job.agent_id)
    out: dict[str, Any] = {
        "job_id": str(job.id), "status": job.status,
        "error": job.error, "error_code": None,
        "agent_name": agent.name if agent else None,
        "created_at": job.created_at, "claimed_at": job.claimed_at, "finished_at": job.finished_at,
        "summary": None,
    }
    # 舊版代理不認得這個探測種類（自動更新前）：講清楚是代理版本，而不是丟一句英文
    if job.error and job.error.startswith("unsupported probe"):
        out["error_code"] = "identify_agent_outdated"
    if job.status == STATUS_DONE and isinstance(job.result, dict):
        out["summary"] = ip_identify.summarize(job.result, mac_vendor=mac_vendor)
    return out


async def _job_out(session: AsyncSession, ip: IPAddress, job: AgentProbeJob) -> dict[str, Any]:
    out = await _brief(session, ip, job, await vendor_for_mac(session, ip.mac))
    out["result"] = job.result
    out["progress"] = job.progress
    out["changes"] = None
    if job.status == STATUS_DONE and isinstance(job.result, dict):
        # 跟上一次完成的探測比
        prev = (await session.execute(_jobs_of(_ip_text(ip)).where(
            AgentProbeJob.status == STATUS_DONE,
            AgentProbeJob.created_at < job.created_at,
        ).order_by(AgentProbeJob.created_at.desc()).limit(1))).scalars().first()
        if prev is not None and isinstance(prev.result, dict):
            out["changes"] = {"previous_job_id": str(prev.id), "previous_at": prev.created_at,
                              **ip_identify.changes_between(prev.result, job.result)}
    return out




@router.post("/{address_id}/identify", status_code=status.HTTP_202_ACCEPTED)
async def start_identify(
    address_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    ip = await _ip_or_404(session, address_id)
    ip_text = _ip_text(ip)
    subnet = await session.get(Subnet, ip.subnet_id)
    agent = await session.get(ScanAgent, subnet.scan_agent_id) if subnet and subnet.scan_agent_id else None
    if agent is None or not agent.enabled:
        raise HTTPException(409, detail=ui_detail(
            "identify_no_agent", "這個子網路沒有指定掃描代理，無法探測",
            subnet=str(subnet.cidr) if subnet else ""))

    await expire_stale(session)
    busy = (await session.execute(_jobs_of(ip_text).where(
        AgentProbeJob.status.in_((STATUS_PENDING, STATUS_RUNNING))).limit(1))).scalars().first()
    if busy is not None:
        raise HTTPException(409, detail=ui_detail(
            "identify_in_progress", "這個 IP 已經有探測在進行中", job_id=str(busy.id)))

    try:
        job = await create_job(session, agent_id=agent.id, kind="identify",
                               params={"targets": ip_text}, requested_by=user.id)
    except ProbeJobError as exc:
        raise HTTPException(400, detail=detail_of(exc, "probe_job_error")) from exc
    # 「作業」頁上的那一筆（完成時通知發起人），見 services/identify_tasks
    from app.services.identify_tasks import on_created
    await on_created(session, job=job, ip=ip, user=user, agent=agent)

    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="ip_address",
        object_id=str(ip.id),
        action="identify",
        diff={"ip": ip_text, "agent": agent.name, "job_id": str(job.id)},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return {"job_id": str(job.id), "agent_id": str(agent.id), "agent_name": agent.name,
            "status": job.status}


@router.get("/{address_id}/identify")
async def latest_identify(
    address_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """這個 IP 最近一次的探測（重新打開畫面時接著顯示）。沒有就回 job_id=null。"""
    ip = await _ip_or_404(session, address_id)
    await expire_stale(session)
    await session.commit()
    job = (await session.execute(_jobs_of(_ip_text(ip)).order_by(
        AgentProbeJob.created_at.desc()).limit(1))).scalars().first()
    if job is None:
        return {"job_id": None}
    return await _job_out(session, ip, job)


@router.get("/{address_id}/identify/history")
async def identify_history(
    address_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    """這個 IP 的歷次探測（新到舊）。每次的結果都保留，日後可以點回來看。"""
    ip = await _ip_or_404(session, address_id)
    await expire_stale(session)
    await session.commit()
    rows = (await session.execute(_jobs_of(_ip_text(ip)).order_by(
        AgentProbeJob.created_at.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    mv = await vendor_for_mac(session, ip.mac)
    return {"items": [await _brief(session, ip, j, mv) for j in rows]}


@router.get("/{address_id}/identify/{job_id}")
async def get_identify(
    address_id: uuid.UUID,
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    ip = await _ip_or_404(session, address_id)
    await expire_stale(session)
    await session.commit()
    job = (await session.execute(_jobs_of(_ip_text(ip)).where(AgentProbeJob.id == job_id))).scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="Probe not found")
    return await _job_out(session, ip, job)
