"""工具探測工作的建立、領取與回報（後端側）。

這裡是整個功能的安全核心：它決定「代理會被要求做什麼」。因此所有驗證都集中在
`validate_params()`，代理端再獨立驗一次（不可只信後端 —— 後端被入侵時代理是最後一道閘）。
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_probe_job import (
    PROBE_KINDS,
    STATUS_DONE,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    AgentProbeJob,
)

# 沒被領走的工作多久作廢。代理離線時工作會堆積，上線後一次補跑一堆過期探測沒有意義，
# 而且會讓使用者以為是「剛剛那次」的結果。
JOB_TTL = timedelta(minutes=2)
# 領走但沒回報（代理當掉／被 kill）多久視為失敗，才不會永遠卡在 running
CLAIM_TTL = timedelta(minutes=5)
MAX_TARGETS = 64
MAX_PORTS = 64
MAX_PENDING_PER_AGENT = 20

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
                          r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")


class ProbeJobError(ValueError):
    """參數不合法（回 400，不建立工作）。"""


def _valid_target(t: str) -> str:
    """目標必須是 IP 或合法主機名稱。

    這是防注入的第一道：即使代理端一律用陣列傳參數、不經過 shell，仍然不接受
    奇怪的字串進到子行程的 argv —— 少一層可被玩的地方。
    """
    s = (t or "").strip()
    if not s or len(s) > 253:
        raise ProbeJobError(f"invalid target: {t!r}")
    try:
        ipaddress.ip_address(s)
        return s
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(s):
        raise ProbeJobError(f"invalid target: {t!r}")
    return s


def _valid_ports(raw: Any) -> list[int]:
    ports: list[int] = []
    items = raw if isinstance(raw, list) else str(raw or "").replace(",", " ").split()
    for p in items:
        try:
            n = int(p)
        except (TypeError, ValueError) as exc:
            raise ProbeJobError(f"invalid port: {p!r}") from exc
        if not 1 <= n <= 65535:
            raise ProbeJobError(f"port out of range: {n}")
        ports.append(n)
    if not ports:
        raise ProbeJobError("no ports given")
    if len(ports) > MAX_PORTS:
        raise ProbeJobError(f"too many ports (max {MAX_PORTS})")
    return ports


def validate_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """把使用者輸入正規化成代理能安全執行的形狀；不合法就拒絕建立工作。"""
    if kind not in PROBE_KINDS:
        raise ProbeJobError(f"unsupported probe: {kind!r}")

    raw_targets = params.get("targets") or params.get("target") or ""
    items = raw_targets if isinstance(raw_targets, list) else \
        [t for t in re.split(r"[\s,]+", str(raw_targets)) if t]
    if not items:
        raise ProbeJobError("no target given")
    if len(items) > MAX_TARGETS:
        raise ProbeJobError(f"too many targets (max {MAX_TARGETS})")
    targets = [_valid_target(t) for t in items]

    out: dict[str, Any] = {"targets": targets}
    if kind == "ping":
        out["count"] = max(1, min(int(params.get("count") or 3), 10))
        out["timeout"] = max(0.5, min(float(params.get("timeout") or 2.0), 10.0))
    elif kind == "tcp":
        out["ports"] = _valid_ports(params.get("ports"))
        out["timeout"] = max(0.2, min(float(params.get("timeout") or 1.5), 10.0))
    elif kind == "traceroute":
        if len(targets) != 1:
            raise ProbeJobError("traceroute takes exactly one target")
        out["max_hops"] = max(1, min(int(params.get("max_hops") or 20), 30))
    return out


async def create_job(
    session: AsyncSession, *, agent_id: uuid.UUID, kind: str,
    params: dict[str, Any], requested_by: uuid.UUID | None,
) -> AgentProbeJob:
    """建立一筆待辦。超過待辦上限就拒絕 —— 代理離線時使用者連按會堆積。"""
    clean = validate_params(kind, params)
    pending = list((await session.execute(
        select(AgentProbeJob.id).where(
            AgentProbeJob.agent_id == agent_id,
            AgentProbeJob.status == STATUS_PENDING,
            AgentProbeJob.expires_at > datetime.now(UTC),
        ).limit(MAX_PENDING_PER_AGENT + 1)
    )).scalars().all())
    if len(pending) >= MAX_PENDING_PER_AGENT:
        raise ProbeJobError("too many pending jobs for this agent")

    job = AgentProbeJob(
        agent_id=agent_id, kind=kind, params=clean, status=STATUS_PENDING,
        requested_by=requested_by, expires_at=datetime.now(UTC) + JOB_TTL,
    )
    session.add(job)
    await session.flush()
    return job


async def expire_stale(session: AsyncSession) -> int:
    """把過期的待辦與卡住的 running 收掉。每次領取／查詢時順手跑，不另開排程。"""
    now = datetime.now(UTC)
    r1 = await session.execute(
        update(AgentProbeJob)
        .where(AgentProbeJob.status == STATUS_PENDING, AgentProbeJob.expires_at <= now)
        .values(status=STATUS_EXPIRED, finished_at=now,
                error="沒有代理在時限內領取（代理可能離線）"))
    r2 = await session.execute(
        update(AgentProbeJob)
        .where(AgentProbeJob.status == STATUS_RUNNING,
               AgentProbeJob.claimed_at <= now - CLAIM_TTL)
        .values(status=STATUS_FAILED, finished_at=now, error="代理領取後未回報結果"))
    return int(r1.rowcount or 0) + int(r2.rowcount or 0)


async def claim_jobs(
    session: AsyncSession, *, agent_id: uuid.UUID, limit: int = 5,
) -> list[AgentProbeJob]:
    """代理領取待辦。`FOR UPDATE SKIP LOCKED` 讓同一代理的多個行程不會重複領。"""
    await expire_stale(session)
    now = datetime.now(UTC)
    rows = list((await session.execute(
        select(AgentProbeJob)
        .where(AgentProbeJob.agent_id == agent_id,
               AgentProbeJob.status == STATUS_PENDING,
               AgentProbeJob.expires_at > now)
        .order_by(AgentProbeJob.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all())
    for j in rows:
        j.status = STATUS_RUNNING
        j.claimed_at = now
    return rows


async def finish_job(
    session: AsyncSession, *, agent_id: uuid.UUID, job_id: uuid.UUID,
    result: Any = None, error: str | None = None,
) -> bool:
    """代理回報結果。**必須驗 agent_id**：代理只能結束自己領到的工作。"""
    job = (await session.execute(
        select(AgentProbeJob).where(
            AgentProbeJob.id == job_id, AgentProbeJob.agent_id == agent_id)
    )).scalars().first()
    if job is None or job.status not in (STATUS_RUNNING, STATUS_PENDING):
        return False
    job.status = STATUS_FAILED if error else STATUS_DONE
    job.result = result if isinstance(result, dict) else {"items": result}
    job.error = (error or "")[:2000] or None
    job.finished_at = datetime.now(UTC)
    return True
