"""SNMP 收集 endpoints（admin only，0217）。

管理 → 外部系統整合 → SNMP：收集目標、憑證、收集記錄三個頁籤。
- 憑證的機密（社群字串、v3 密碼）只寫不讀：回應只有 has_*；更新時留空＝不變
- 收集目標＝既有裝置（每台一筆）；收集者 auto／agent／server，判定不出來時回明確的錯誤代碼
- 測試與立即收集走掃描代理的工作通道（agent_probe_jobs）：伺服器沒有辦法主動連代理，代理領走工作才知道要做
- 所有設定異動與測試都留稽核（object_type snmp_credential／snmp_target；diff 不含機密，只記「哪些機密改了」）
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.sqlin import in_values
from app.core.ui_error import ui_detail
from app.models.snmp import SnmpCredential, SnmpNeighbor, SnmpRun, SnmpTarget
from app.schemas.snmp import (
    SnmpCredentialCreate,
    SnmpCredentialRead,
    SnmpCredentialUpdate,
    SnmpTargetBulk,
    SnmpTargetCreate,
    SnmpTargetUpdate,
    SnmpTestIn,
)
from app.services import snmp as svc

router = APIRouter(prefix="/snmp", tags=["snmp"], dependencies=[Depends(require_admin)])

_SECRETS = ("community", "auth_passphrase", "priv_passphrase")


async def _audit(session: AsyncSession, user: Any, request: Request, *, object_type: str,
                 object_id: Any, action: str, diff: dict[str, Any] | None) -> None:
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type=object_type, object_id=str(object_id), action=action, diff=diff,
        request_id=getattr(request.state, "request_id", None),
    )


def _cfg_error(exc: svc.SnmpConfigError) -> HTTPException:
    return HTTPException(422, detail=ui_detail(exc.code, str(exc), **exc.params))


# ═════════════════════════════ 憑證 ═════════════════════════════
async def _cred_or_404(session: AsyncSession, cred_id: uuid.UUID) -> SnmpCredential:
    obj = await session.get(SnmpCredential, cred_id)
    if obj is None:
        raise HTTPException(404, detail=ui_detail("snmp_credential_not_found", "找不到這組 SNMP 憑證"))
    return obj


async def _cred_reads(session: AsyncSession, creds: list[SnmpCredential]) -> list[SnmpCredentialRead]:
    from app.models.customer import Customer
    ids = [c.id for c in creds]
    counts = dict((await session.execute(
        select(SnmpTarget.credential_id, func.count()).where(in_values(SnmpTarget.credential_id, ids))
        .group_by(SnmpTarget.credential_id))).all()) if ids else {}
    cust_ids = {c.customer_id for c in creds if c.customer_id}
    names = dict((await session.execute(select(Customer.id, Customer.name).where(
        in_values(Customer.id, cust_ids)))).all()) if cust_ids else {}
    out: list[SnmpCredentialRead] = []
    for c in creds:
        m = SnmpCredentialRead.model_validate(c)
        m.has_community = svc.has_secret(c, "community")
        m.has_auth_passphrase = svc.has_secret(c, "auth_passphrase")
        m.has_priv_passphrase = svc.has_secret(c, "priv_passphrase")
        m.target_count = int(counts.get(c.id, 0))
        m.customer_name = names.get(c.customer_id) if c.customer_id else None
        out.append(m)
    return out


async def _check_customer(session: AsyncSession, customer_id: uuid.UUID | None) -> None:
    if customer_id is None:
        return
    from app.models.customer import Customer
    if await session.get(Customer, customer_id) is None:
        raise HTTPException(422, detail=ui_detail("snmp_customer_not_found", "指定的單位不存在"))


async def _clear_other_defaults(session: AsyncSession, keep: uuid.UUID) -> None:
    await session.execute(update(SnmpCredential).where(
        SnmpCredential.is_default.is_(True), SnmpCredential.id != keep).values(is_default=False))


@router.get("/credentials")
async def list_credentials(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    creds = list((await session.execute(select(SnmpCredential).order_by(
        SnmpCredential.is_default.desc(), SnmpCredential.name))).scalars().all())
    items = await _cred_reads(session, creds)
    return {"items": [i.model_dump(mode="json") for i in items], "total": len(items)}


@router.get("/credentials/{cred_id}", response_model=SnmpCredentialRead)
async def get_credential(cred_id: uuid.UUID,
                         session: Annotated[AsyncSession, Depends(get_session)]) -> SnmpCredentialRead:
    return (await _cred_reads(session, [await _cred_or_404(session, cred_id)]))[0]


@router.post("/credentials", response_model=SnmpCredentialRead, status_code=status.HTTP_201_CREATED)
async def create_credential(
    payload: SnmpCredentialCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SnmpCredentialRead:
    data = payload.model_dump(exclude=set(_SECRETS))
    secrets = {k: getattr(payload, k) or None for k in _SECRETS}
    await _check_customer(session, data.get("customer_id"))
    obj = SnmpCredential(id=uuid.uuid4(), **data)     # 先給 id：機密的 AAD 綁 id
    try:
        svc.validate_credential(obj, secrets)
    except svc.SnmpConfigError as exc:
        raise _cfg_error(exc) from exc
    changed = svc.apply_secrets(obj, secrets)
    if obj.is_default:
        await _clear_other_defaults(session, obj.id)
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail=ui_detail("snmp_credential_name_taken", "已經有同名的 SNMP 憑證",
                                                  name=obj.name)) from exc
    await _audit(session, user, request, object_type="snmp_credential", object_id=obj.id, action="create",
                 diff={"name": obj.name, "version": obj.version, "is_default": obj.is_default,
                       "secrets_set": changed})
    await session.commit()
    await session.refresh(obj)
    return (await _cred_reads(session, [obj]))[0]


@router.patch("/credentials/{cred_id}", response_model=SnmpCredentialRead)
async def update_credential(
    cred_id: uuid.UUID, payload: SnmpCredentialUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SnmpCredentialRead:
    obj = await _cred_or_404(session, cred_id)
    data = payload.model_dump(exclude_unset=True)
    secrets = {k: (data.pop(k, None) or None) for k in _SECRETS}
    if "customer_id" in data:
        await _check_customer(session, data["customer_id"])
        if data["customer_id"] is not None:
            # 改成限定某個單位：已經指派給別的單位裝置的收集目標會違反限制 → 先講清楚是哪些
            from app.models.device import Device
            bad = (await session.execute(
                select(Device.id, Device.name).join(SnmpTarget, SnmpTarget.device_id == Device.id)
                .where(SnmpTarget.credential_id == obj.id,
                       or_(Device.customer_id.is_(None), Device.customer_id != data["customer_id"]))
                .limit(50))).all()
            if bad:
                raise HTTPException(409, detail=ui_detail(
                    "snmp_credential_scope_conflict", "這組憑證已指派給其他單位的裝置，不能改成只限這個單位",
                    count=len(bad), devices=[{"device_id": str(d), "device_name": n} for d, n in bad]))
    for k, v in data.items():
        if k in ("name", "version", "timeout_s", "retries", "is_default") and v is None:
            continue
        setattr(obj, k, v)
    try:
        svc.validate_credential(obj, secrets)
    except svc.SnmpConfigError as exc:
        raise _cfg_error(exc) from exc
    changed = svc.apply_secrets(obj, secrets)
    if obj.is_default:
        await _clear_other_defaults(session, obj.id)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail=ui_detail("snmp_credential_name_taken", "已經有同名的 SNMP 憑證",
                                                  name=obj.name)) from exc
    await _audit(session, user, request, object_type="snmp_credential", object_id=obj.id, action="update",
                 diff={**{k: str(v) for k, v in data.items()}, "secrets_set": changed})
    await session.commit()
    await session.refresh(obj)
    return (await _cred_reads(session, [obj]))[0]


@router.delete("/credentials/{cred_id}", status_code=204)
async def delete_credential(
    cred_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    obj = await _cred_or_404(session, cred_id)
    from app.models.device import Device
    used = (await session.execute(
        select(SnmpTarget.id, SnmpTarget.device_id, Device.name).join(Device, Device.id == SnmpTarget.device_id)
        .where(SnmpTarget.credential_id == obj.id).order_by(Device.name).limit(50))).all()
    if used:
        total = int(await session.scalar(select(func.count()).select_from(SnmpTarget)
                                         .where(SnmpTarget.credential_id == obj.id)) or 0)
        raise HTTPException(409, detail=ui_detail(
            "snmp_credential_in_use", "這組憑證還有收集目標在用：請先改用其他憑證或刪除這些收集目標",
            count=total, targets=[{"target_id": str(t), "device_id": str(d), "device_name": n} for t, d, n in used]))
    name = obj.name
    await session.delete(obj)
    await _audit(session, user, request, object_type="snmp_credential", object_id=cred_id, action="delete",
                 diff={"name": name})
    await session.commit()


# ═════════════════════════════ 收集目標 ═════════════════════════════
async def _target_or_404(session: AsyncSession, target_id: uuid.UUID) -> SnmpTarget:
    obj = await session.get(SnmpTarget, target_id)
    if obj is None:
        raise HTTPException(404, detail=ui_detail("snmp_target_not_found", "找不到這個 SNMP 收集目標"))
    return obj


async def _target_reads(session: AsyncSession, targets: list[SnmpTarget]) -> list[dict[str, Any]]:
    from app.models.device import Device
    if not targets:
        return []
    res = await svc.resolve_many(session, [svc.want_of(t) for t in targets])
    dev = {i: (n, c) for i, n, c in (await session.execute(select(Device.id, Device.name, Device.customer_id).where(
        in_values(Device.id, {t.device_id for t in targets})))).all()}
    creds = {i: (n, v) for i, n, v in (await session.execute(
        select(SnmpCredential.id, SnmpCredential.name, SnmpCredential.version).where(
            in_values(SnmpCredential.id, {t.credential_id for t in targets})))).all()}
    out: list[dict[str, Any]] = []
    for t in targets:
        r = res[t.id]
        dname, dcust = dev.get(t.device_id, (None, None))
        cname, cver = creds.get(t.credential_id, (None, None))
        out.append({
            "id": str(t.id), "device_id": str(t.device_id), "device_name": dname,
            "customer_id": str(dcust) if dcust else None,
            "credential_id": str(t.credential_id), "credential_name": cname, "credential_version": cver,
            "ip_override": str(t.ip_override) if t.ip_override else None,
            "mgmt_ip": r.ip, "port": t.port,
            "collect_arp": t.collect_arp, "collect_fdb": t.collect_fdb,
            "collect_neighbors": t.collect_neighbors, "collect_interfaces": t.collect_interfaces,
            "intervals": t.intervals, "effective_intervals": svc.effective_intervals(t),
            "collector": t.collector, "agent_id": str(t.agent_id) if t.agent_id else None,
            "effective_agent_id": str(r.agent_id) if r.agent_id else None,
            "effective_agent_name": r.agent_name,
            "collector_subnet_id": str(r.subnet_id) if r.subnet_id else None,
            "collector_subnet_cidr": r.subnet_cidr,
            "collector_error": r.error_detail(),
            "enabled": t.enabled, "auto_create_ips": t.auto_create_ips, "description": t.description,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
            "last_ok_at": t.last_ok_at.isoformat() if t.last_ok_at else None,
            "last_error": t.last_error, "last_error_code": t.last_error_code,
            "last_summary": t.last_summary,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        })
    return out


async def _pick_credential(session: AsyncSession, cred_id: uuid.UUID | None) -> SnmpCredential:
    if cred_id is None:
        cred = (await session.execute(select(SnmpCredential).where(SnmpCredential.is_default.is_(True)))).scalars().first()
        if cred is None:
            raise HTTPException(422, detail=ui_detail(
                "snmp_no_default_credential", "沒有指定憑證，也還沒有設定預設憑證：請先選一組憑證"))
        return cred
    cred = await session.get(SnmpCredential, cred_id)
    if cred is None:
        raise HTTPException(422, detail=ui_detail("snmp_credential_not_found", "找不到這組 SNMP 憑證"))
    return cred


def _check_scope(cred: SnmpCredential, device: Any) -> None:
    """限定單位的憑證只能給那個單位的裝置。"""
    if cred.customer_id is not None and device.customer_id != cred.customer_id:
        raise HTTPException(422, detail=ui_detail(
            "snmp_credential_scope", "這組憑證限定給另一個單位的裝置使用", credential=cred.name, device=device.name))


async def _check_collector(session: AsyncSession, collector: str | None, agent_id: uuid.UUID | None) -> None:
    if collector != "agent":
        return
    if agent_id is None:
        raise HTTPException(422, detail=ui_detail("snmp_agent_required", "收集者選「指定代理」時要選一台掃描代理"))
    from app.models.scan_agent import ScanAgent
    if await session.get(ScanAgent, agent_id) is None:
        raise HTTPException(422, detail=ui_detail("snmp_agent_not_found", "指定的掃描代理不存在"))


async def _device_or_404(session: AsyncSession, device_id: uuid.UUID) -> Any:
    from app.models.device import Device
    dev = await session.get(Device, device_id)
    if dev is None:
        raise HTTPException(404, detail=ui_detail("snmp_device_not_found", "找不到這台裝置"))
    return dev


async def _device_for_ip(session: AsyncSession, user: Any, request: Request,
                         ip_id: uuid.UUID) -> tuple[Any, str | None]:
    """子網路頁「對閘道啟用 SNMP 收集」：這筆 IP 的裝置；沒有就以它建一台（類型 router、主要 IP＝它、單位跟子網路）。
    回傳（裝置, 管理位址覆寫）：裝置原本的主要 IP 不是這筆時，管理位址要指定成這筆。"""
    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.subnet import Subnet
    ip = await session.get(IPAddress, ip_id)
    if ip is None:
        raise HTTPException(404, detail=ui_detail("snmp_ip_not_found", "找不到這筆 IP"))
    addr = str(ip.ip).split("/")[0]
    if ip.device_id is not None:
        dev = await _device_or_404(session, ip.device_id)
        return dev, (None if dev.primary_ip_id == ip.id else addr)
    sub = await session.get(Subnet, ip.subnet_id)
    dev = Device(id=uuid.uuid4(), name=(ip.hostname or addr)[:200], type="router", primary_ip_id=ip.id,
                 customer_id=getattr(sub, "customer_id", None))
    session.add(dev)
    await session.flush()
    ip.device_id = dev.id
    await _audit(session, user, request, object_type="device", object_id=dev.id, action="create",
                 diff={"name": dev.name, "primary_ip": addr, "via": "snmp_target"})
    return dev, None


@router.get("/targets")
async def list_targets(
    session: Annotated[AsyncSession, Depends(get_session)],
    device_id: uuid.UUID | None = None,
    credential_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = Query(None, description="只列這台代理（依實際判定的收集者）"),
    q: Annotated[str | None, Query(max_length=128)] = None,
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    from app.models.device import Device
    stmt = select(SnmpTarget).join(Device, Device.id == SnmpTarget.device_id)
    if device_id:
        stmt = stmt.where(SnmpTarget.device_id == device_id)
    if credential_id:
        stmt = stmt.where(SnmpTarget.credential_id == credential_id)
    if q:
        stmt = stmt.where(Device.name.ilike(f"%{q}%"))
    if agent_id:
        # 收集者要判定過才知道（auto 看子網路），先全部撈出來再篩：收集目標是網路設備，量不大
        all_rows = list((await session.execute(stmt.order_by(Device.name))).scalars().all())
        res = await svc.resolve_many(session, [svc.want_of(t) for t in all_rows])
        rows = [t for t in all_rows if res[t.id].agent_id == agent_id]
        total = len(rows)
        rows = rows[(page - 1) * page_size: page * page_size]
    else:
        total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = list((await session.execute(stmt.order_by(Device.name)
                                           .offset((page - 1) * page_size).limit(page_size))).scalars().all())
    return {"items": await _target_reads(session, rows), "total": total, "page": page, "page_size": page_size}


@router.get("/targets/{target_id}")
async def get_target(target_id: uuid.UUID,
                     session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    return (await _target_reads(session, [await _target_or_404(session, target_id)]))[0]


@router.post("/targets", status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: SnmpTargetCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if payload.ip_address_id is not None:
        dev, override = await _device_for_ip(session, user, request, payload.ip_address_id)
        if override and not payload.ip_override:
            payload.ip_override = override
    else:
        dev = await _device_or_404(session, payload.device_id)  # type: ignore[arg-type]
    existing = (await session.execute(select(SnmpTarget.id).where(
        SnmpTarget.device_id == dev.id))).scalar()
    if existing:
        raise HTTPException(409, detail=ui_detail("snmp_target_exists", "這台裝置已經有 SNMP 收集目標",
                                                  target_id=str(existing)))
    cred = await _pick_credential(session, payload.credential_id)
    _check_scope(cred, dev)
    await _check_collector(session, payload.collector, payload.agent_id)
    data = payload.model_dump(exclude={"credential_id", "ip_address_id", "device_id"})
    obj = SnmpTarget(**data, device_id=dev.id, credential_id=cred.id)
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail=ui_detail("snmp_target_exists", "這台裝置已經有 SNMP 收集目標")) from exc
    await _audit(session, user, request, object_type="snmp_target", object_id=obj.id, action="create",
                 diff={"device": dev.name, "credential": cred.name, "collector": obj.collector,
                       "ip_override": obj.ip_override, "sections": list(svc.effective_intervals(obj))})
    await session.commit()
    await session.refresh(obj)
    return (await _target_reads(session, [obj]))[0]


@router.post("/targets/bulk")
async def bulk_targets(
    payload: SnmpTargetBulk, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """裝置清單的「批次指派 SNMP 憑證」：沒有收集目標的裝置新建（用給的或預設的收集項目）、已有的改憑證。
    違反單位限制的裝置略過並列出原因，其餘照做。"""
    from app.models.device import Device
    cred = await _pick_credential(session, payload.credential_id)
    await _check_collector(session, payload.collector, payload.agent_id)
    devs = list((await session.execute(select(Device).where(in_values(Device.id, payload.device_ids)))).scalars().all())
    found = {d.id for d in devs}
    # 每一筆略過的原因都是一個可翻譯的錯誤（code／params／message），畫面逐台列出
    skipped = [{"device_id": str(i), **ui_detail("snmp_device_not_found", "找不到這台裝置")}
               for i in payload.device_ids if i not in found]
    have = {t.device_id: t for t in (await session.execute(select(SnmpTarget).where(
        in_values(SnmpTarget.device_id, found)))).scalars().all()} if found else {}
    fields = {k: v for k, v in payload.model_dump(exclude={"device_ids", "credential_id"}).items() if v is not None}
    created = updated = 0
    for d in devs:
        if cred.customer_id is not None and d.customer_id != cred.customer_id:
            skipped.append({"device_id": str(d.id), "device_name": d.name, **ui_detail(
                "snmp_credential_scope", "這組憑證限定給另一個單位的裝置使用", credential=cred.name, device=d.name)})
            continue
        t = have.get(d.id)
        if t is None:
            # 沒指定的收集項目依裝置類型（跟單筆表單同一套規則），有指定的照指定
            from app.services.snmp import default_collect_flags
            session.add(SnmpTarget(device_id=d.id, credential_id=cred.id,
                                   **{**default_collect_flags(d.type), **fields}))
            created += 1
        else:
            t.credential_id = cred.id
            for k, v in fields.items():
                setattr(t, k, v)
            updated += 1
    await session.flush()
    await _audit(session, user, request, object_type="snmp_credential", object_id=cred.id, action="bulk_assign",
                 diff={"credential": cred.name, "created": created, "updated": updated, "skipped": len(skipped),
                       "fields": {k: str(v) for k, v in fields.items()}})
    await session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


@router.patch("/targets/{target_id}")
async def update_target(
    target_id: uuid.UUID, payload: SnmpTargetUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    obj = await _target_or_404(session, target_id)
    data = payload.model_dump(exclude_unset=True)
    if "credential_id" in data:
        cred = await _pick_credential(session, data["credential_id"])
        _check_scope(cred, await _device_or_404(session, obj.device_id))
        data["credential_id"] = cred.id
    collector = data.get("collector", obj.collector)
    agent_id = data["agent_id"] if "agent_id" in data else obj.agent_id
    await _check_collector(session, collector, agent_id)
    for k, v in data.items():
        if v is None and k in ("port", "collector", "enabled", "auto_create_ips", "collect_arp", "collect_fdb",
                               "collect_neighbors", "collect_interfaces"):
            continue
        setattr(obj, k, v)
    await _audit(session, user, request, object_type="snmp_target", object_id=obj.id, action="update",
                 diff={k: str(v) for k, v in data.items()})
    await session.commit()
    await session.refresh(obj)
    return (await _target_reads(session, [obj]))[0]


@router.delete("/targets/{target_id}", status_code=204)
async def delete_target(
    target_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """刪除收集目標：FDB、鄰居、收集記錄跟著刪（外鍵 CASCADE）；寫進裝置連接埠的介面沒接線的刪、接了線的留著。"""
    from app.services.integration_cleanup import forget_instance
    obj = await _target_or_404(session, target_id)
    await forget_instance(session, source="snmp", source_id=obj.id)
    await session.delete(obj)
    await _audit(session, user, request, object_type="snmp_target", object_id=target_id, action="delete",
                 diff={"device_id": str(obj.device_id)})
    await session.commit()


# ─────────────────── 測試／立即收集 ───────────────────
async def _start_job(session: AsyncSession, user: Any, request: Request, target: SnmpTarget, kind: str,
                     credential_ids: list[uuid.UUID] | None) -> dict[str, Any]:
    from app.services.agent_probe import ProbeJobError
    from app.services.snmp_collect import expire_stuck_runs

    res = await svc.resolve(session, target)
    if not res.ok:
        raise HTTPException(409, detail=res.error_detail())
    names: list[str] = []
    if credential_ids:
        found = dict((await session.execute(select(SnmpCredential.id, SnmpCredential.name).where(
            in_values(SnmpCredential.id, credential_ids)))).all())
        missing = [str(c) for c in credential_ids if c not in found]
        if missing:
            raise HTTPException(422, detail=ui_detail("snmp_credential_not_found", "找不到這組 SNMP 憑證",
                                                      ids=missing))
        names = [found[c] for c in credential_ids]
    await expire_stuck_runs(session)
    try:
        job = await svc.create_job(session, agent_id=res.agent_id, kind=kind, target_id=target.id,  # type: ignore[arg-type]
                                   credential_ids=credential_ids, requested_by=user.id)
    except ProbeJobError as exc:
        raise HTTPException(409, detail=ui_detail("snmp_job_busy", "這台掃描代理有太多待辦工作，請稍後再試",
                                                  agent=res.agent_name, reason=str(exc))) from exc
    run = SnmpRun(target_id=target.id, agent_id=res.agent_id, requested_by=user.id, job_id=job.id,
                  kind="test" if kind == "snmp_test" else "collect", trigger="manual", result="running")
    session.add(run)
    await session.flush()
    await _audit(session, user, request, object_type="snmp_target", object_id=target.id,
                 action="test" if kind == "snmp_test" else "collect",
                 diff={"agent": res.agent_name, "ip": res.ip, "credentials": names or None})
    await session.commit()
    return {"job_id": str(job.id), "run_id": str(run.id), "agent_id": str(res.agent_id),
            "agent_name": res.agent_name, "ip": res.ip, "expires_at": job.expires_at.isoformat()}


@router.post("/targets/{target_id}/test", status_code=202)
async def start_test(
    target_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: SnmpTestIn | None = None,
) -> dict[str, Any]:
    """測試：由負責的掃描代理讀 sysName／sysDescr／sysObjectID／sysUpTime，再逐區段看設備有沒有回應、有幾列。
    `credential_ids` 可以一次試幾組憑證（最多 5 組，都對這一台設備；不會對網段每台主機試）。"""
    target = await _target_or_404(session, target_id)
    return await _start_job(session, user, request, target, "snmp_test",
                            payload.credential_ids if payload and payload.credential_ids else None)


@router.post("/targets/{target_id}/collect", status_code=202)
async def collect_target(
    target_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """立即收集：代理馬上把這台的所有收集項目跑一次（不等排程）並回報。"""
    target = await _target_or_404(session, target_id)
    if not target.enabled:
        raise HTTPException(409, detail=ui_detail("snmp_target_disabled", "這個收集目標已停用"))
    return await _start_job(session, user, request, target, "snmp_collect", None)


@router.get("/jobs/{job_id}")
async def job_status(job_id: uuid.UUID,
                     session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """測試／立即收集的進度與結果（前端輪詢）。結果不含任何機密。"""
    from app.models.agent_probe_job import AgentProbeJob
    from app.services.agent_probe import expire_stale
    await expire_stale(session)
    await session.commit()
    job = await session.get(AgentProbeJob, job_id)
    if job is None or job.kind not in svc.SNMP_JOB_KINDS:
        raise HTTPException(404, detail="Not found")
    run = (await session.execute(select(SnmpRun).where(SnmpRun.job_id == job.id))).scalars().first()
    return {
        "job_id": str(job.id), "kind": job.kind, "status": job.status, "error": job.error,
        "result": job.result if job.kind == "snmp_test" else None,
        "run": _run_read(run, {}) if run is not None else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _run_read(r: SnmpRun, agents: dict[Any, str]) -> dict[str, Any]:
    return {
        "id": str(r.id), "target_id": str(r.target_id), "kind": r.kind, "trigger": r.trigger,
        "result": r.result, "agent_id": str(r.agent_id) if r.agent_id else None,
        "agent_name": agents.get(r.agent_id), "job_id": str(r.job_id) if r.job_id else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms, "sections": r.sections, "info": r.info,
        "error_code": r.error_code, "error": r.error,
    }


@router.get("/targets/{target_id}/runs")
async def list_runs(
    target_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(50, ge=1, le=300),
    kind: str | None = Query(None, pattern="^(test|collect)$"),
) -> dict[str, Any]:
    from app.models.scan_agent import ScanAgent
    from app.services.snmp_collect import expire_stuck_runs
    await _target_or_404(session, target_id)
    await expire_stuck_runs(session)
    await session.commit()
    stmt = select(SnmpRun).where(SnmpRun.target_id == target_id)
    if kind:
        stmt = stmt.where(SnmpRun.kind == kind)
    rows = list((await session.execute(stmt.order_by(SnmpRun.started_at.desc()).limit(limit))).scalars().all())
    agents = dict((await session.execute(select(ScanAgent.id, ScanAgent.name).where(
        in_values(ScanAgent.id, {r.agent_id for r in rows if r.agent_id})))).all()) if rows else {}
    return {"items": [_run_read(r, agents) for r in rows]}


@router.get("/runs")
async def list_all_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(100, ge=1, le=500),
    result: str | None = Query(None, pattern="^(running|success|partial|failed)$"),
    kind: str | None = Query(None, pattern="^(test|collect)$"),
) -> dict[str, Any]:
    """「收集記錄」頁籤：所有目標最近的記錄（新到舊）。"""
    from app.models.device import Device
    from app.models.scan_agent import ScanAgent
    stmt = select(SnmpRun, SnmpTarget.device_id, Device.name).join(
        SnmpTarget, SnmpTarget.id == SnmpRun.target_id).join(Device, Device.id == SnmpTarget.device_id)
    if result:
        stmt = stmt.where(SnmpRun.result == result)
    if kind:
        stmt = stmt.where(SnmpRun.kind == kind)
    rows = (await session.execute(stmt.order_by(SnmpRun.started_at.desc()).limit(limit))).all()
    agents = dict((await session.execute(select(ScanAgent.id, ScanAgent.name).where(
        in_values(ScanAgent.id, {r.agent_id for r, _d, _n in rows if r.agent_id})))).all()) if rows else {}
    return {"items": [{**_run_read(r, agents), "device_id": str(d), "device_name": n} for r, d, n in rows]}


# ─────────────────── 收集者判定（表單的「自動」提示）、代理能力、鄰居 ───────────────────
@router.get("/collectors")
async def preview_collector(
    session: Annotated[AsyncSession, Depends(get_session)],
    device_id: uuid.UUID | None = None,
    ip: Annotated[str | None, Query(max_length=64)] = None,
    collector: str = Query("auto", pattern="^(auto|agent|server)$"),
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """表單上的「自動＝哪台代理」：給裝置（用它的主要 IP）或位址（覆寫），回判定結果或判定不出來的原因。"""
    import ipaddress
    from types import SimpleNamespace
    if ip:
        try:
            ip = str(ipaddress.ip_address(ip.strip()))
        except ValueError as exc:
            raise HTTPException(422, detail=ui_detail("snmp_bad_ip", "不是有效的 IP 位址")) from exc
    if not device_id and not ip:
        raise HTTPException(422, detail=ui_detail("snmp_bad_ip", "請給裝置或 IP 位址"))
    probe = SimpleNamespace(id="preview", device_id=device_id, ip_override=ip, collector=collector, agent_id=agent_id)
    r = await svc.resolve(session, probe)
    return {"ip": r.ip, "collector": collector, "agent_id": str(r.agent_id) if r.agent_id else None,
            "agent_name": r.agent_name, "capable": r.capable,
            "subnet_id": str(r.subnet_id) if r.subnet_id else None, "subnet_cidr": r.subnet_cidr,
            "error": r.error_detail()}


@router.get("/agents")
async def list_agents(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """掃描代理的 SNMP 能力與被指派的收集目標數（掃描代理頁只顯示這些，設定在 SNMP 頁）。"""
    from app.models.scan_agent import ScanAgent
    agents = list((await session.execute(select(ScanAgent).order_by(ScanAgent.name))).scalars().all())
    counts = await svc.assigned_counts(session)
    out = []
    for a in agents:
        tool = svc.agent_snmp_tool(a)
        out.append({"agent_id": str(a.id), "name": a.name, "is_local": a.is_local, "enabled": a.enabled,
                    "agent_version": a.agent_version, "capable": svc.agent_snmp_capable(a),
                    "tool_installed": tool is not None, "tool_version": (tool or {}).get("version"),
                    "version_ok": svc.agent_version_tuple(a.agent_version) >= svc.MIN_AGENT_VERSION,
                    "target_count": counts.get(a.id, 0),
                    "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None})
    return {"items": out, "min_agent_version": ".".join(str(x) for x in svc.MIN_AGENT_VERSION)}


@router.get("/neighbors")
async def list_neighbors(
    session: Annotated[AsyncSession, Depends(get_session)],
    target_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """LLDP／CDP 鄰居。`remote_device_id`：依宣告的管理位址、chassis MAC 或系統名稱對到的裝置（對到多台就不填）。"""
    from app.models.device import Device
    from app.services.snmp_collect import match_neighbors
    stmt = select(SnmpNeighbor)
    if target_id:
        stmt = stmt.where(SnmpNeighbor.target_id == target_id)
    if device_id:
        stmt = stmt.where(SnmpNeighbor.device_id == device_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list((await session.execute(stmt.order_by(SnmpNeighbor.device_id, SnmpNeighbor.local_port)
                                       .offset((page - 1) * page_size).limit(page_size))).scalars().all())
    matched = await match_neighbors(session, rows)
    names = dict((await session.execute(select(Device.id, Device.name).where(
        in_values(Device.id, {r.device_id for r in rows})))).all()) if rows else {}
    return {"items": [{
        "id": str(r.id), "target_id": str(r.target_id), "device_id": str(r.device_id),
        "device_name": names.get(r.device_id), "protocol": r.protocol, "local_port": r.local_port,
        "remote_sysname": r.remote_sysname, "remote_port": r.remote_port,
        "remote_chassis_id": r.remote_chassis_id, "remote_mac": r.remote_mac,
        "remote_mgmt_address": r.remote_mgmt_address, "remote_platform": r.remote_platform,
        **(matched.get(r.id) or {"remote_device_id": None, "remote_device_name": None}),
        "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
        "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
    } for r in rows], "total": total, "page": page, "page_size": page_size}
