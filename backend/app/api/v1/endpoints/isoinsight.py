"""ISOinsight 整合 endpoints。

- 來源管理、測試連線、預覽、立即同步、同步記錄：**管理員**（比照其他整合頁）
- 來源租約查詢（`/leases`）：依既有 RBAC —— 只看得到自己可見子網路內的租約；配對不到子網路的
  租約不屬於任何網路範圍，只有管理員看得到

秘密：密碼寫入後不回傳（只有 has_password）；Cookie／Token 從不存；稽核的 diff 只記「密碼已更換」。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.sqlin import in_values
from app.core.ui_error import ui_detail
from app.models.customer import Customer
from app.models.isoinsight import IsoInsightLease, IsoInsightSource, IsoInsightSyncRun
from app.models.subnet import Subnet
from app.schemas.base import Paginated
from app.schemas.isoinsight import (
    IsoInsightCreate,
    IsoInsightLeaseRead,
    IsoInsightRead,
    IsoInsightRunRead,
    IsoInsightTestRequest,
    IsoInsightUpdate,
)
from app.services.background_tasks import spawn_task
from app.services.isoinsight import config as cfg
from app.services.isoinsight import job
from app.services.isoinsight.errors import IsoError
from app.services.isoinsight.queries import query_leases

router = APIRouter(prefix="/isoinsight", tags=["isoinsight"])
_admin = [Depends(require_admin)]

#: 改了就要重新測試（不沿用舊的成功標記）：連線與認證
_CONN_FIELDS = frozenset({"base_url", "login_path", "login_method", "post_format", "username", "username_param",
                          "password_param", "auth_mode", "token_path", "token_header", "token_prefix", "lease_path",
                          "verify_tls"})
#: 改了就要重新預覽：連線＋解讀與配對（時區、範圍、租戶、是否新增 IP）
_PREVIEW_FIELDS = _CONN_FIELDS | {"source_timezone", "scope_subnet_ids", "customer_id", "create_ips", "max_rows",
                                  "max_response_mib"}
#: 不影響進行中工作結果的欄位（其餘改了都要讓進行中的工作放棄提交）
_NO_VERSION_BUMP = frozenset({"name", "description", "product_version", "schedule_enabled",
                              "sync_interval_seconds", "enabled"})


def _meta(request: Request) -> dict[str, Any]:
    return {"actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


def _read(src: IsoInsightSource) -> IsoInsightRead:
    out = IsoInsightRead.model_validate(src)
    out.has_password = bool(src.password_enc)
    out.scope_subnet_ids = list(src.scope_subnet_ids or [])
    out.running = bool(src.running_since and src.running_since > datetime.now(UTC) - job.LOCK_STALE)
    return out


async def _or_404(session: AsyncSession, sid: uuid.UUID) -> IsoInsightSource:
    src = await session.get(IsoInsightSource, sid)
    if src is None:
        raise HTTPException(404, detail="Not found")
    return src


async def _check_scope(session: AsyncSession, subnet_ids: list[uuid.UUID], customer_id: uuid.UUID | None) -> None:
    """允許的子網路必選、必須存在，而且全部屬於設定的租戶（不可跨租戶自動配對）。"""
    if not subnet_ids:
        raise HTTPException(422, detail=ui_detail("isoinsight_scope_empty", "至少要選一個允許同步的子網路"))
    if customer_id is not None and await session.get(Customer, customer_id) is None:
        raise HTTPException(422, detail=ui_detail("isoinsight_customer_unknown", "找不到這個客戶"))
    rows = (await session.execute(select(Subnet.id, Subnet.cidr, Subnet.customer_id)
                                  .where(in_values(Subnet.id, subnet_ids)))).all()
    if len(rows) != len(set(subnet_ids)):
        raise HTTPException(422, detail=ui_detail("isoinsight_scope_unknown", "有選取的子網路不存在"))
    foreign = [str(cidr) for _sid, cidr, cust in rows if cust != customer_id]
    if foreign:
        raise HTTPException(422, detail=ui_detail(
            "isoinsight_scope_mixed_tenant", "允許的子網路必須都屬於所選的客戶（不可跨租戶配對）",
            subnets=", ".join(sorted(foreign)[:5])))


def _job_http_error(exc: IsoError) -> HTTPException:
    if exc.spec_code == "SYNC_ALREADY_RUNNING":
        return HTTPException(409, detail=ui_detail("isoinsight_sync_already_running", "這個來源已經有工作在執行"))
    return HTTPException(422, detail=ui_detail("isoinsight_config_invalid", "來源設定不完整", reason=str(exc)))


# ── 來源 ─────────────────────────────────────────────────────────────────────

@router.get("/sources", response_model=Paginated[IsoInsightRead], dependencies=_admin)
async def list_sources(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=500),
) -> Paginated[IsoInsightRead]:
    rows = list((await session.execute(select(IsoInsightSource).order_by(IsoInsightSource.name)
                                       .offset((page - 1) * page_size).limit(page_size))).scalars().all())
    total = int(await session.scalar(select(func.count()).select_from(IsoInsightSource)) or 0)
    return Paginated[IsoInsightRead](items=[_read(r) for r in rows], total=total, page=page, page_size=page_size)


@router.get("/sources/{source_id}", response_model=IsoInsightRead, dependencies=_admin)
async def get_source(source_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> IsoInsightRead:
    return _read(await _or_404(session, source_id))


@router.post("/sources", response_model=IsoInsightRead, status_code=status.HTTP_201_CREATED, dependencies=_admin)
async def create_source(
    payload: IsoInsightCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IsoInsightRead:
    if not payload.timezone_confirmed:
        raise HTTPException(422, detail=ui_detail(
            "isoinsight_timezone_unconfirmed", "儲存前請確認來源時區（時間會依這個時區換算）"))
    await _check_scope(session, payload.scope_subnet_ids, payload.customer_id)
    data = payload.model_dump(exclude={"password", "timezone_confirmed"})
    src = IsoInsightSource(**data, password_enc=b"", password_nonce=b"")
    session.add(src)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail=ui_detail("isoinsight_name_taken", "名稱已存在")) from exc
    src.password_enc, src.password_nonce = cfg.encrypt_password(src.id, payload.password)
    await append_audit(session, actor_user_id=str(user.id), object_type="isoinsight_source",
                       object_id=str(src.id), action="create",
                       diff={"name": src.name, "base_url": src.base_url, "login_method": src.login_method,
                             "post_format": src.post_format, "auth_mode": src.auth_mode,
                             "verify_tls": src.verify_tls, "source_timezone": src.source_timezone,
                             "scope_subnets": len(src.scope_subnet_ids or []), "create_ips": src.create_ips},
                       **_meta(request))
    await session.commit()
    await session.refresh(src)
    return _read(src)


@router.patch("/sources/{source_id}", response_model=IsoInsightRead, dependencies=_admin)
async def update_source(
    source_id: uuid.UUID, payload: IsoInsightUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IsoInsightRead:
    src = await _or_404(session, source_id)
    data = payload.model_dump(exclude_unset=True)
    new_password = data.pop("password", None) or None       # 留空＝保留原密碼
    tz_ok = data.pop("timezone_confirmed", None)
    if "source_timezone" in data and data["source_timezone"] != src.source_timezone and not tz_ok:
        raise HTTPException(422, detail=ui_detail(
            "isoinsight_timezone_unconfirmed", "儲存前請確認來源時區（時間會依這個時區換算）"))
    for k in ("name", "base_url", "login_path", "login_method", "post_format", "username", "username_param",
              "password_param", "auth_mode", "token_header", "token_prefix", "lease_path", "verify_tls",
              "source_timezone", "scope_subnet_ids", "create_ips", "enabled", "schedule_enabled",
              "sync_interval_seconds", "connect_timeout_seconds", "request_timeout_seconds", "job_timeout_seconds",
              "max_response_mib", "max_rows"):
        if k in data and data[k] is None:
            data.pop(k)                                    # 不可清空的欄位：送 null 當作沒改
    changed = {k: v for k, v in data.items() if getattr(src, k) != v}
    auth_mode = changed.get("auth_mode", src.auth_mode)
    token_path = changed.get("token_path", src.token_path) if "token_path" in data else src.token_path
    if auth_mode == "token" and not token_path:
        raise HTTPException(422, detail=ui_detail("isoinsight_token_path_required", "Token 模式需要設定 Token 欄位路徑"))
    if "scope_subnet_ids" in changed or "customer_id" in changed:
        await _check_scope(session, list(changed.get("scope_subnet_ids", src.scope_subnet_ids) or []),
                           changed.get("customer_id", src.customer_id) if "customer_id" in data else src.customer_id)
    conn = bool(new_password) or bool(_CONN_FIELDS & set(changed))
    needs_preview = conn or bool(_PREVIEW_FIELDS & set(changed))
    # 排程開關隨時可以切（使用者 2026-10-08：預設開啟）；沒有目前這組設定的成功預覽時，排程輪到也不同步（job.run_due）
    for k, v in changed.items():
        setattr(src, k, v)
    if new_password:
        src.password_enc, src.password_nonce = cfg.encrypt_password(src.id, new_password)
    if conn:
        src.last_test_ok_at = None
        src.auth_hold = False              # 帳密或連線設定改過：解除登入失敗造成的暫停
    if needs_preview:
        src.preview_ok_at = None
    if new_password or (set(changed) - _NO_VERSION_BUMP):
        src.config_version = (src.config_version or 1) + 1
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail=ui_detail("isoinsight_name_taken", "名稱已存在")) from exc
    diff: dict[str, Any] = {k: (str(v) if isinstance(v, uuid.UUID) else
                                ([str(x) for x in v] if isinstance(v, list) else v)) for k, v in changed.items()}
    if new_password:
        diff["password_changed"] = True
    if needs_preview:
        diff["requires_preview"] = True
    await append_audit(session, actor_user_id=str(user.id), object_type="isoinsight_source",
                       object_id=str(src.id), action="update", diff=diff, **_meta(request))
    await session.commit()
    await session.refresh(src)
    return _read(src)


@router.delete("/sources/{source_id}", status_code=204, dependencies=_admin)
async def delete_source(
    source_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    src = await _or_404(session, source_id)
    leases = int(await session.scalar(select(func.count()).select_from(IsoInsightLease).where(
        IsoInsightLease.source_id == src.id)) or 0)
    # 先記再刪（刪完名稱就沒了）；正式 IP 不刪，它寫進共用表的租約旗標與主機名稱收回
    await append_audit(session, actor_user_id=str(user.id), object_type="isoinsight_source",
                       object_id=str(source_id), action="delete",
                       diff={"name": src.name, "base_url": src.base_url, "lease_observations": leases,
                             "last_commit_at": src.last_commit_at.isoformat() if src.last_commit_at else None},
                       **_meta(request))
    from app.services.integration_cleanup import forget_instance
    await forget_instance(session, source="isoinsight", source_id=src.id)
    await session.delete(src)
    await session.commit()


# ── 測試連線／預覽／同步 ────────────────────────────────────────────────────

@router.post("/sources/{source_id}/test", dependencies=_admin)
async def run_source_test(
    source_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: IsoInsightTestRequest | None = None,
) -> dict[str, Any]:
    """分階段測試（登入 → 讀租約 → 結構驗證）。不寫任何 IP／租約資料。"""
    await _or_404(session, source_id)
    anonymous = bool(payload and payload.anonymous_check)
    try:
        out = await job.run_test(source_id, actor_user_id=user.id, anonymous=anonymous)
    except IsoError as exc:
        raise _job_http_error(exc) from exc
    await append_audit(session, actor_user_id=str(user.id), object_type="isoinsight_source",
                       object_id=str(source_id), action="test",
                       diff={"ok": out.get("ok"), "error_code": out.get("error_code"),
                             "anonymous_check": anonymous}, **_meta(request))
    await session.commit()
    return out


@router.post("/sources/{source_id}/preview", dependencies=_admin)
async def preview_source(
    source_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """預覽租約與子網路配對結果（尚未套用）。成功一次之後才可以開排程。"""
    await _or_404(session, source_id)
    try:
        out = await job.run_preview(source_id, actor_user_id=user.id)
    except IsoError as exc:
        raise _job_http_error(exc) from exc
    await append_audit(session, actor_user_id=str(user.id), object_type="isoinsight_source",
                       object_id=str(source_id), action="preview",
                       diff={"ok": out.get("ok"), "error_code": out.get("error_code"),
                             "counts": out.get("counts")}, **_meta(request))
    await session.commit()
    return out


@router.post("/sources/{source_id}/sync", dependencies=_admin)
async def sync_source(
    source_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """非同步：立刻回 task_id，同步在背景跑（作業頁看得到）。手動與排程共用同一把鎖。"""
    src = await _or_404(session, source_id)
    if src.running_since and src.running_since > datetime.now(UTC) - job.LOCK_STALE:
        raise HTTPException(409, detail=ui_detail("isoinsight_sync_already_running", "這個來源已經有工作在執行"))
    if not src.enabled:
        raise HTTPException(422, detail=ui_detail("isoinsight_source_disabled", "這個來源已停用"))
    actor_user_id, name, meta = user.id, src.name, _meta(request)
    await append_audit(session, actor_user_id=str(actor_user_id), object_type="isoinsight_source",
                       object_id=str(source_id), action="sync", diff={"trigger": "manual"}, **meta)
    await session.commit()

    async def _runner(_sess: AsyncSession, task: Any) -> dict[str, Any]:
        summary = await job.run_sync(source_id, trigger="manual", actor_user_id=actor_user_id, task_id=task.id)
        if summary.get("result") == "failed":
            # 作業頁顯示失敗＋原因（訊息已遮蔽，不含帳密、Cookie、Token）
            raise RuntimeError(f"{summary.get('error_code')}: {summary.get('error') or ''}".strip())
        return summary

    task = await spawn_task(session=session, kind="isoinsight.sync", target_type="isoinsight_source",
                            target_id=source_id, target_label=name, actor_user_id=actor_user_id, runner=_runner)
    return {"task_id": str(task.id), "status": task.status, "queued_at": task.queued_at.isoformat()}


@router.get("/sources/{source_id}/runs", response_model=Paginated[IsoInsightRunRead], dependencies=_admin)
async def list_runs(
    source_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)],
    kind: Literal["sync", "test", "preview"] | None = None,
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=200),
) -> Paginated[IsoInsightRunRead]:
    await _or_404(session, source_id)
    stmt = select(IsoInsightSyncRun).where(IsoInsightSyncRun.source_id == source_id)
    if kind:
        stmt = stmt.where(IsoInsightSyncRun.kind == kind)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.execute(stmt.order_by(IsoInsightSyncRun.started_at.desc())
                                  .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return Paginated[IsoInsightRunRead](items=[IsoInsightRunRead.model_validate(r) for r in rows], total=total,
                                        page=page, page_size=page_size)


# ── 來源租約（依 RBAC）─────────────────────────────────────────────────────

@router.get("/leases")
async def list_leases(
    user: CurrentUser, session: Annotated[AsyncSession, Depends(get_session)],
    source_id: uuid.UUID | None = None,
    ip: Annotated[str | None, Query(max_length=64)] = None,
    mac: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
    subnet_id: uuid.UUID | None = None,
    state: Literal["active", "expired", "not_started", "invalid_period", "unknown"] | None = None,
    match_status: Literal["matched", "ambiguous", "no_subnet"] | None = None,
    observed_since: datetime | None = None,
    observed_until: datetime | None = None,
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    out = await query_leases(session, user=user, source_id=source_id, ip=ip, mac=mac, q=q,
                             subnet_ids=[subnet_id] if subnet_id else None, state=state, match_status=match_status,
                             observed_since=observed_since, observed_until=observed_until,
                             offset=(page - 1) * page_size, limit=page_size)
    out["items"] = [IsoInsightLeaseRead(**i).model_dump(mode="json") for i in out["items"]]
    out["page"], out["page_size"] = page, page_size
    return out
