"""外部 DNS 解析（使用者 2026-10-19 定案；規格 docs/SPEC_EXTERNAL_DNS_zh-TW.md）。

  GET  /api/v1/system/dns-resolvers          設定（管理員）
  PUT  /api/v1/system/dns-resolvers          寫入設定（管理員，留稽核）
  POST /api/v1/system/dns-resolvers/test     對一個解析器查一個名稱（管理員）
  GET  /api/v1/anomalies/attack-surface/external-dns       每個 FQDN 的外部答案與判定（全域讀取）
  POST /api/v1/anomalies/attack-surface/external-dns/run   立即查一輪（管理員，背景作業）
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin, require_global_read
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import ui_detail
from app.schemas.base import StrictModel
from app.services import external_dns as svc

admin_router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(require_admin)])
router = APIRouter(prefix="/anomalies/attack-surface/external-dns", tags=["anomalies"])

TASK_KIND = "external_dns.run"


class ResolverIn(StrictModel):
    name: Annotated[str, Field(max_length=60)] = ""
    address: Annotated[str, Field(max_length=64)]
    port: Annotated[int, Field(ge=1, le=65535)] = 53
    enabled: bool = True


class DnsResolversIn(StrictModel):
    resolvers: Annotated[list[ResolverIn], Field(max_length=svc.MAX_RESOLVERS)] = []
    public_domains: Annotated[list[Annotated[str, Field(max_length=253)]], Field(max_length=svc.MAX_DOMAINS)] = []
    public_ranges: Annotated[list[Annotated[str, Field(max_length=64)]], Field(max_length=svc.MAX_RANGES)] = []
    query_authoritative: bool = True
    interval_hours: Annotated[int, Field(ge=1, le=168)] = 6
    notify_on_change: bool = True


@admin_router.get("/dns-resolvers")
async def get_dns_resolvers(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    return {**await svc.get_settings(session), "state": await svc.get_state(session)}


@admin_router.put("/dns-resolvers")
async def put_dns_resolvers(payload: DnsResolversIn, user: CurrentUser, request: Request,
                            session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    raw = payload.model_dump()
    saved = await svc.save_settings(session, raw, user_id=user.id)
    dropped = {k: len(raw[k]) - len(saved[k]) for k in ("resolvers", "public_domains", "public_ranges")
               if len(raw[k]) != len(saved[k])}
    if dropped:   # 不合格的項目不要靜靜丟掉：讓管理員知道哪一類有幾筆沒存
        await session.rollback()
        raise HTTPException(422, detail=ui_detail("ext_dns_invalid_entries", f"有不合格的項目：{dropped}",
                                                  **{k: str(v) for k, v in dropped.items()}))
    await append_audit(
        session, actor_user_id=str(user.id), actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"), request_id=getattr(request.state, "request_id", None),
        object_type="system", object_id=None, action="update",
        diff={"target": "dns_resolvers", "resolvers": [r["address"] for r in saved["resolvers"]],
              "public_domains": saved["public_domains"], "public_ranges": saved["public_ranges"],
              "query_authoritative": saved["query_authoritative"], "interval_hours": saved["interval_hours"]})
    await session.commit()
    return {**saved, "state": await svc.get_state(session)}


class ResolverTestIn(StrictModel):
    address: Annotated[str, Field(max_length=64)]
    port: Annotated[int, Field(ge=1, le=65535)] = 53
    name: Annotated[str, Field(max_length=253)] = ""


@admin_router.post("/dns-resolvers/test")
async def test_dns_resolver(payload: ResolverTestIn,
                            session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """對一個解析器查一個名稱：沒給名稱就查第一個對外網域。"""
    import ipaddress
    try:
        addr = str(ipaddress.ip_address(payload.address.strip()))
    except ValueError as exc:
        raise HTTPException(422, detail=ui_detail("ext_dns_bad_address", "不是有效的 IP 位址",
                                                  value=payload.address)) from exc
    name = payload.name.strip().rstrip(".") or next(iter((await svc.get_settings(session))["public_domains"]), "")
    if not name:
        raise HTTPException(422, detail=ui_detail("ext_dns_need_name", "請先設定對外網域，或指定要查的名稱"))
    ans = await svc.query_resolver(name, addr, payload.port, addr)
    return {"name": name, **ans.as_dict()}


@router.get("")
async def get_external_dns(session: Annotated[AsyncSession, Depends(get_session)],
                           _gr: Annotated[None, Depends(require_global_read)]) -> dict[str, Any]:
    return await svc.view(session)


@router.post("/run", dependencies=[Depends(require_admin)])
async def run_external_dns(user: CurrentUser, request: Request,
                           session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """立即查一輪（背景作業）。同時只能有一輪。"""
    from app.models.background_task import BackgroundTask
    from app.services.background_tasks import spawn_task

    running = await session.scalar(select(BackgroundTask.id).where(
        BackgroundTask.kind == TASK_KIND, BackgroundTask.status.in_(("pending", "running"))).limit(1))  # bounded: 兩個狀態
    if running:
        raise HTTPException(409, detail=ui_detail("ext_dns_running", "外部 DNS 查詢正在執行"))
    if not svc.configured(await svc.get_settings(session)):
        raise HTTPException(422, detail=ui_detail("ext_dns_not_configured", "尚未設定外部解析器與對外網域"))
    actor = str(user.id)
    actor_ip = request.client.host if request.client else None
    actor_ua = request.headers.get("user-agent")
    request_id = getattr(request.state, "request_id", None)

    async def _runner(sess: AsyncSession, _task: Any) -> dict[str, Any]:
        summary = await svc.run_round(sess)
        await append_audit(sess, actor_user_id=actor, actor_ip=actor_ip, actor_user_agent=actor_ua,
                           request_id=request_id, object_type="system", object_id=None,
                           action="external_dns_run", diff=summary)
        await sess.commit()
        return summary

    task = await spawn_task(session=session, kind=TASK_KIND, target_type="system", target_label="external-dns",
                            actor_user_id=user.id, runner=_runner)
    return {"task_id": str(task.id), "status": task.status}
