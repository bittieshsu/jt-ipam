"""憑證派送代理（cert-agents）。

兩種呼叫者：
- 管理面（admin，JWT）：CRUD + key 輪替 + 看各站台回報狀態。
- agent（X-Agent-Key 認證）：`check`（我負責的憑證有沒有新版）/ `bundle`（下載 crt/key/chain）/
  `report`（回報套用結果）。`bundle` 會回傳**私鑰明文**（即時解密）→ 強制 TLS、scope 限定、逐次稽核。

下載 `agent.py` / `installer.sh` 為純程式碼、無密鑰，公開可取（同掃描代理）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin, require_global_read
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.security import decrypt_secret
from app.models.certificate import CertAgent, Certificate, CertVersion
from app.schemas.base import Paginated
from app.schemas.certificate import (
    CertAgentCreate,
    CertAgentCreated,
    CertAgentRead,
    CertAgentUpdate,
)

router = APIRouter(prefix="/cert-agents", tags=["cert-agents"])

_AGENT_DIR = Path(__file__).resolve().parents[5] / "agent"
_AGENT_PY = _AGENT_DIR / "jt_ipam_cert_agent.py"


def _agent_sha() -> str:
    """目前 server 上派送代理程式的 sha256（給 agent 自動更新比對用）。"""
    try:
        return hashlib.sha256(_AGENT_PY.read_bytes()).hexdigest()
    except OSError:
        return ""


def _server_agent_version() -> str | None:
    """從 server 端 agent.py 解析 __version__，給 UI 標示「代理版本落後」。"""
    try:
        import re
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', _AGENT_PY.read_text(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def _key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_key() -> str:
    return secrets.token_urlsafe(32)


def _to_read(obj: CertAgent) -> CertAgentRead:
    m = CertAgentRead.model_validate(obj, from_attributes=True)
    m.has_key = bool(obj.enroll_key_hash)
    m.server_agent_version = _server_agent_version()
    return m


def _key_aad(certificate_id: uuid.UUID, fingerprint: str) -> bytes:
    return f"cert_version:{certificate_id}:{fingerprint}".encode()


def _scope_ids(agent: CertAgent) -> set[str]:
    return {str(x) for x in (agent.scope_cert_ids or [])}


async def _agent_from_key(session: AsyncSession, key: str | None) -> CertAgent:
    if not key:
        raise HTTPException(401, detail="missing agent key")
    obj = (await session.execute(
        select(CertAgent).where(CertAgent.enroll_key_hash == _key_hash(key))
    )).scalar_one_or_none()
    if obj is None or not obj.enabled:
        raise HTTPException(401, detail="invalid agent key")
    return obj


# ─────────────────── 下載（公開，無密鑰）───────────────────

@router.get("/installer.sh", include_in_schema=False)
async def download_installer() -> PlainTextResponse:
    p = _AGENT_DIR / "jt-ipam-cert-agent-installer.sh"
    if not p.exists():
        raise HTTPException(404, detail="installer not found")
    return PlainTextResponse(p.read_text(), media_type="text/x-shellscript")


@router.get("/agent.py", include_in_schema=False)
async def download_agent() -> PlainTextResponse:
    p = _AGENT_DIR / "jt_ipam_cert_agent.py"
    if not p.exists():
        raise HTTPException(404, detail="agent not found")
    return PlainTextResponse(p.read_text(), media_type="text/x-python")


# ─────────────────── 唯讀現況（global-read：admin 或唯讀檢視者）───────────────────

@router.get("/status", dependencies=[Depends(require_global_read)])
async def agents_status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """各代理的憑證派送現況（最後更新 / 有效日 / 到期日 / 剩餘天數 / 是否最新）。
    屬全域基礎設施檢視：非管理員但具萬用讀取（唯讀檢視者）亦可看;掛在「進階」。"""
    cur_rows = (await session.execute(
        select(Certificate, CertVersion)
        .join(CertVersion, CertVersion.certificate_id == Certificate.id)
        .where(CertVersion.is_current.is_(True))
    )).all()
    cur = {cert.name: ver for cert, ver in cur_rows}
    now = datetime.now(UTC)
    server_ver = _server_agent_version()
    agents = (await session.execute(select(CertAgent).order_by(CertAgent.name))).scalars().all()

    out: list[dict[str, Any]] = []
    for a in agents:
        deps: list[dict[str, Any]] = []
        for d in (a.reported or []):
            if not isinstance(d, dict):
                continue
            ver = cur.get(d.get("cert"))
            na = ver.not_after if ver else None
            deps.append({
                "cert": d.get("cert"), "profile": d.get("profile"), "status": d.get("status"),
                "applied_at": d.get("applied_at"), "dry_run": d.get("dry_run"),
                "reported_fingerprint": d.get("fingerprint"),
                "current_fingerprint": ver.fingerprint_sha256 if ver else None,
                "up_to_date": bool(ver and d.get("fingerprint") == ver.fingerprint_sha256),
                "not_before": ver.not_before.isoformat() if ver and ver.not_before else None,
                "not_after": na.isoformat() if na else None,
                "days_remaining": (na - now).days if na else None,
            })
        out.append({
            "agent": a.name, "enabled": a.enabled,
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            "last_source_ip": a.last_source_ip,
            "agent_version": a.agent_version, "server_agent_version": server_ver,
            "deployments": deps,
        })
    return {"agents": out}


# ─────────────────── 管理面（admin）───────────────────

@router.get("", response_model=Paginated[CertAgentRead], dependencies=[Depends(require_admin)])
async def list_agents(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=200),
) -> Paginated[CertAgentRead]:
    total = int((await session.execute(select(func.count()).select_from(CertAgent))).scalar_one())
    rows = (await session.execute(
        select(CertAgent).order_by(CertAgent.name).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return Paginated[CertAgentRead](
        items=[_to_read(r) for r in rows], total=total, page=page, page_size=page_size,
    )


@router.post("", response_model=CertAgentCreated, status_code=201,
             dependencies=[Depends(require_admin)])
async def create_agent(
    payload: CertAgentCreate,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CertAgentCreated:
    if (await session.execute(
        select(CertAgent).where(CertAgent.name == payload.name).limit(1)
    )).scalar_one_or_none() is not None:
        raise HTTPException(409, detail="Agent name already exists")
    raw_key = _new_key()
    obj = CertAgent(
        name=payload.name, description=payload.description, enabled=payload.enabled,
        enroll_key_hash=_key_hash(raw_key),
        scope_cert_ids=[str(c) for c in payload.scope_cert_ids],
    )
    session.add(obj)
    await session.flush()
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="cert_agent", object_id=str(obj.id), action="cert_agent_create",
        diff={"name": obj.name}, request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return CertAgentCreated(**_to_read(obj).model_dump(), enroll_key=raw_key)


@router.patch("/{agent_id}", response_model=CertAgentRead, dependencies=[Depends(require_admin)])
async def update_agent(
    agent_id: uuid.UUID,
    payload: CertAgentUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CertAgentRead:
    obj = await session.get(CertAgent, agent_id)
    if obj is None:
        raise HTTPException(404, detail="Not found")
    data = payload.model_dump(exclude_unset=True)
    if "scope_cert_ids" in data and data["scope_cert_ids"] is not None:
        data["scope_cert_ids"] = [str(c) for c in data["scope_cert_ids"]]
    for k, v in data.items():
        setattr(obj, k, v)
    await session.commit()
    await session.refresh(obj)  # commit 後 updated_at(onupdate)過期 → refresh 免 model_validate 同步 lazy IO 500
    return _to_read(obj)


@router.post("/{agent_id}/rotate-key", response_model=CertAgentCreated,
             dependencies=[Depends(require_admin)])
async def rotate_key(
    agent_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CertAgentCreated:
    obj = await session.get(CertAgent, agent_id)
    if obj is None:
        raise HTTPException(404, detail="Not found")
    raw_key = _new_key()
    obj.enroll_key_hash = _key_hash(raw_key)
    await session.commit()
    await session.refresh(obj)  # commit 後 updated_at(onupdate)過期 → refresh 免 model_validate 同步 lazy IO 500
    return CertAgentCreated(**_to_read(obj).model_dump(), enroll_key=raw_key)


@router.delete("/{agent_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_agent(
    agent_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    obj = await session.get(CertAgent, agent_id)
    if obj is None:
        raise HTTPException(404, detail="Not found")
    await session.delete(obj)
    await session.commit()


# ─────────────────── agent 協定（X-Agent-Key 認證）───────────────────

async def _current_versions_for_scope(session: AsyncSession, agent: CertAgent) -> list[dict[str, Any]]:
    scope = _scope_ids(agent)
    if not scope:
        return []
    rows = (await session.execute(
        select(Certificate, CertVersion)
        .join(CertVersion, CertVersion.certificate_id == Certificate.id)
        .where(CertVersion.is_current.is_(True), Certificate.id.in_([uuid.UUID(s) for s in scope]))
    )).all()
    return [{
        "cert": cert.name, "cert_id": str(cert.id),
        "fingerprint": ver.fingerprint_sha256,
        "not_after": ver.not_after.isoformat(),
        "domains": ver.domains or [],
    } for cert, ver in rows]


@router.get("/check")
async def agent_check(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_agent_key: Annotated[str | None, Header()] = None,
    x_agent_version: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """agent poll：回傳此 agent scope 內各憑證的「目前版本」指紋 + 到期日。
    agent 比對自己手上的指紋,不同就去 /bundle 拉。"""
    agent = await _agent_from_key(session, x_agent_key)
    agent.last_seen_at = datetime.now(UTC)
    agent.last_source_ip = request.client.host if request.client else None
    if x_agent_version:
        agent.agent_version = x_agent_version[:32]
    certs = await _current_versions_for_scope(session, agent)
    await session.commit()
    # agent_sha：server 上派送代理程式的 sha256；agent 比對自己不同就自我更新
    return {"certificates": certs, "agent_sha": _agent_sha()}


@router.get("/bundle")
async def agent_bundle(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    cert: Annotated[str, Query(description="憑證名稱或 id")],
    x_agent_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """agent 下載某憑證目前版本的 crt / key / chain（私鑰即時解密）。scope 限定 + 逐次稽核。"""
    agent = await _agent_from_key(session, x_agent_key)
    # 找憑證（依名稱或 id）
    obj = None
    try:
        obj = await session.get(Certificate, uuid.UUID(cert))
    except ValueError:
        obj = (await session.execute(
            select(Certificate).where(Certificate.name == cert).limit(1)
        )).scalar_one_or_none()
    if obj is None or str(obj.id) not in _scope_ids(agent):
        # 不在 scope 與不存在回相同 404，不洩漏存在性
        raise HTTPException(404, detail="certificate not found or not in agent scope")
    ver = (await session.execute(
        select(CertVersion).where(
            CertVersion.certificate_id == obj.id, CertVersion.is_current.is_(True)
        ).limit(1)
    )).scalar_one_or_none()
    if ver is None:
        raise HTTPException(404, detail="no current version")

    key_pem = decrypt_secret(ver.key_enc, ver.key_nonce,
                             aad=_key_aad(obj.id, ver.fingerprint_sha256)).decode("utf-8")
    await append_audit(
        session, actor_user_id=None,
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=f"cert-agent/{agent.name}",
        object_type="certificate", object_id=str(obj.id), action="cert_bundle_download",
        diff={"agent": agent.name, "fingerprint": ver.fingerprint_sha256},
        request_id=getattr(request.state, "request_id", None),
    )
    agent.last_seen_at = datetime.now(UTC)
    await session.commit()
    return {
        "cert": obj.name, "fingerprint": ver.fingerprint_sha256,
        "not_after": ver.not_after.isoformat(),
        "cert_pem": ver.cert_pem, "chain_pem": ver.chain_pem, "key_pem": key_pem,
    }


@router.post("/report")
async def agent_report(
    request: Request,
    body: dict[str, Any],
    session: Annotated[AsyncSession, Depends(get_session)],
    x_agent_key: Annotated[str | None, Header()] = None,
    x_agent_version: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """agent 回報各 deployment 的套用結果（給後台看站台健康度 / 飄移偵測）。"""
    agent = await _agent_from_key(session, x_agent_key)
    deployments = body.get("deployments")
    if not isinstance(deployments, list):
        raise HTTPException(400, detail="deployments must be a list")
    # 只存白名單欄位,避免塞任意巨物
    clean = [{
        k: d.get(k) for k in
        ("cert", "profile", "fingerprint", "not_after", "applied_at", "status", "message", "dry_run")
    } for d in deployments if isinstance(d, dict)][:200]
    agent.reported = clean
    agent.last_seen_at = datetime.now(UTC)
    agent.last_source_ip = request.client.host if request.client else None
    if x_agent_version:
        agent.agent_version = x_agent_version[:32]
    await session.commit()
    return {"ok": True, "received": len(clean)}
