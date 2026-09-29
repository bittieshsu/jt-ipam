"""IP「探測」在「作業」頁的那一筆，以及完成時通知發起人。

使用者問（2026-09-28）：「探測中會出現在作業嗎？完成時會通知嗎？」—— 原本都不會：探測走的是
掃描代理的工作佇列（agent_probe_jobs），不在作業表裡，完成了也沒有人知道，除非自己回去看。

做法：發起時登記一筆 background_tasks（kind＝ip.identify，summary.job_id 對回工作），
代理領走／回報進度／完成／逾時都同步更新它；完成或失敗時通知發起人（通知矩陣 identify.done）。
作業列只是**鏡像**：真正的狀態仍在 agent_probe_jobs，這裡出錯不可以影響探測本身。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_probe_job import STATUS_DONE, AgentProbeJob
from app.models.background_task import BackgroundTask

KIND = "ip.identify"
EVENT = "identify.done"
# 代理只回報階段（names → scan），沒有百分比：用階段估個大概，讓作業頁的進度條會動
_STAGE_PROGRESS = {"names": 30, "scan": 60}


async def _task(session: AsyncSession, job_id: uuid.UUID) -> BackgroundTask | None:
    return (await session.execute(select(BackgroundTask).where(
        BackgroundTask.kind == KIND,
        BackgroundTask.summary["job_id"].astext == str(job_id)))).scalars().first()


async def on_created(session: AsyncSession, *, job: AgentProbeJob, ip_text: str, ip: Any | None,
                     user: Any, agent: Any) -> None:
    """`ip` 是那筆 IP 記錄；以位址探測 IPAM 沒有記錄的位址時是 None（作業列就只掛位址）。"""
    hostname = getattr(ip, "hostname", None)
    label = f"{ip_text}（{hostname}）" if hostname else ip_text
    session.add(BackgroundTask(
        kind=KIND, status="pending", trigger="manual",
        target_type="ip_address" if ip is not None else "ip",
        target_id=ip.id if ip is not None else None,
        target_label=label, actor_user_id=user.id, progress=0,
        summary={"job_id": str(job.id), "agent": agent.name, "ip": ip_text}))


async def on_claimed(session: AsyncSession, jobs: list[AgentProbeJob]) -> None:
    now = datetime.now(UTC)
    for j in jobs:
        if j.kind != "identify":
            continue
        t = await _task(session, j.id)
        if t is not None and t.status == "pending":
            t.status, t.started_at, t.progress = "running", now, 10


async def on_progress(session: AsyncSession, job: AgentProbeJob) -> None:
    if job.kind != "identify":
        return
    t = await _task(session, job.id)
    stage = (job.progress or {}).get("stage")
    if t is not None and stage in _STAGE_PROGRESS:
        t.progress = max(t.progress or 0, _STAGE_PROGRESS[stage])


async def on_finished(session: AsyncSession, job: AgentProbeJob) -> None:
    """完成或失敗：更新作業列、通知發起人。"""
    if job.kind != "identify":
        return
    t = await _task(session, job.id)
    if t is None:
        return
    now = datetime.now(UTC)
    ok = job.status == STATUS_DONE
    summary = dict(t.summary or {})
    if ok and isinstance(job.result, dict):
        from app.services.ip_identify import summarize
        from app.services.recog import get_matcher
        s = summarize(job.result, recog=await get_matcher(session))
        summary.update({"device_type": s["device_type"], "os": s["os"],
                        "ports": len(s["services"])})
    t.summary = summary
    t.status = "succeeded" if ok else "failed"
    t.progress = 100 if ok else t.progress
    t.error = None if ok else (job.error or "")[:2000] or None
    t.finished_at = now
    t.started_at = t.started_at or job.claimed_at or now
    if t.actor_user_id:
        await _notify(session, t, job, ok, summary)


async def on_expired(session: AsyncSession, jobs: list[AgentProbeJob]) -> None:
    """沒人領走而作廢、或領走後沒回報：作業列跟著失敗，也通知一聲（不然會一直等下去）。"""
    for j in jobs:
        await on_finished(session, j)


async def _notify(session: AsyncSession, t: BackgroundTask, job: AgentProbeJob, ok: bool,
                  summary: dict[str, Any]) -> None:
    from app.services.notification import push_notification
    from app.services.system_config import get_notification_matrix
    ch = (await get_notification_matrix(session)).get(EVENT, {"in_app": True})
    if not ch.get("in_app"):
        return
    ip_text = summary.get("ip") or ""
    # 沒有 IP 記錄（以位址探測）→ 連回以位址的探測頁
    link = (f"/addresses/{t.target_id}/identify?job={job.id}" if t.target_id
            else f"/identify/ip/{ip_text}?job={job.id}")
    if ok:
        dtype = summary.get("device_type") or "unknown"
        await push_notification(
            session, user_id=t.actor_user_id, severity="info", link=link,
            title=f"探測完成：{ip_text}", body=f"找到 {summary.get('ports', 0)} 個開放的連接埠。",
            object_type="ip_address" if t.target_id else None, object_id=t.target_id,
            title_key="notif.identify_done", body_key="notif.identify_done_body",
            params={"ip": ip_text, "type_key": f"identify.type.{dtype}",
                    "ports": summary.get("ports", 0), "os": summary.get("os") or "—"})
    else:
        await push_notification(
            session, user_id=t.actor_user_id, severity="warning", link=link,
            title=f"探測失敗：{ip_text}", body=t.error or "",
            object_type="ip_address" if t.target_id else None, object_id=t.target_id,
            title_key="notif.identify_failed", body_key="notif.identify_failed_body",
            params={"ip": ip_text, "error": t.error or ""})
