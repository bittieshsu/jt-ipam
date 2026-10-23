"""Bigstack CubeCOS／OpenStack 整合 endpoints（Beta）。

設定、測試、同步、子網路對應、CubeCOS 節點：僅管理員（`router`）。
IP 頁面要看的「這個 IP 是哪個 OpenStack port」：跟虛擬化清單同一個門檻（全域讀取）加上那筆 IP 的讀取權限
（`read_router`），不回任何連線設定。

`/test` 同步回應（短逾時），結果也存成這個連線的 capabilities（所以有稽核）；`/sync` 在背景跑，
同一個連線已經有同步在排隊或執行時直接回那個作業（不重複開）。
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import (
    CurrentUser,
    require_admin,
    require_global_read,
    require_object_perm,
)
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import ui_detail
from app.models.openstack import OpenStackInstance, OpenStackPort, OpenStackSubnet
from app.schemas.openstack import (
    OpenStackCreate,
    OpenStackPortRead,
    OpenStackRead,
    OpenStackSubnetMapping,
    OpenStackSubnetRead,
    OpenStackTestResult,
    OpenStackUpdate,
    check_auth_fields,
)
from app.services import openstack as svc
from app.services.background_tasks import spawn_task

router = APIRouter(prefix="/openstack-instances", tags=["openstack"], dependencies=[Depends(require_admin)])
read_router = APIRouter(prefix="/openstack-instances", tags=["openstack"],
                        dependencies=[Depends(require_global_read)])

_MASKED = "***"   # 稽核裡只記「改了密碼」，不記內容
_NOT_FOUND = ui_detail("openstack_not_found", "OpenStack connection not found")


def _name_taken(name: str) -> HTTPException:
    return HTTPException(409, detail=ui_detail("openstack_name_taken", f"name {name!r} already exists", reason=name))


async def _get_or_404(session: AsyncSession, iid: uuid.UUID) -> OpenStackInstance:
    inst = await session.get(OpenStackInstance, iid)
    if inst is None:
        raise HTTPException(404, detail=_NOT_FOUND)
    return inst


def _audit_kw(user: Any, request: Request) -> dict[str, Any]:
    return {"actor_user_id": str(user.id), "actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


async def _read(session: AsyncSession, rows: list[OpenStackInstance]) -> list[dict[str, Any]]:
    from app.models.virt import VirtualMachine
    cids = [r.cluster_id for r in rows if r.cluster_id]
    vms: dict[Any, int] = {}
    iids = [r.id for r in rows]
    if cids:
        vms = dict((await session.execute(select(VirtualMachine.cluster_id, func.count()).where(
            VirtualMachine.cluster_id.in_(cids))  # bounded: 一個 OpenStack 連線一個叢集
            .group_by(VirtualMachine.cluster_id))).all())
    ports: dict[Any, int] = {}
    if rows:
        ports = dict((await session.execute(select(OpenStackPort.instance_id, func.count()).where(
            OpenStackPort.instance_id.in_(iids))  # bounded: OpenStack 連線的數量
            .group_by(OpenStackPort.instance_id))).all())
    out = []
    for r in rows:
        out.append(OpenStackRead(
            id=r.id, name=r.name, product=r.product, auth_url=r.auth_url, auth_method=r.auth_method,
            username=r.username, user_domain=r.user_domain, project_name=r.project_name,
            project_domain=r.project_domain, app_credential_id=r.app_credential_id,
            has_secret=bool(r.secret_enc), region=r.region, interface=r.interface, verify_tls=r.verify_tls,
            has_ca_cert=bool((r.ca_cert or "").strip()), all_projects=r.all_projects, cubecos_url=r.cubecos_url,
            enabled=r.enabled, cluster_id=r.cluster_id, sync_interval_seconds=r.sync_interval_seconds,
            last_sync_at=r.last_sync_at, last_error=r.last_error, last_full_success_at=r.last_full_success_at,
            scope_subnet_ids=[str(x) for x in r.scope_subnet_ids] if r.scope_subnet_ids else None,
            auto_create_ips=r.auto_create_ips, description=r.description, capabilities=r.capabilities,
            vm_count=int(vms.get(r.cluster_id, 0)) if r.cluster_id else 0, port_count=int(ports.get(r.id, 0)),
            node_count=len(r.nodes or []), created_at=r.created_at,
        ).model_dump(mode="json"))
    return out


@router.get("", response_model=list[OpenStackRead])
async def list_instances(
    _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    rows = list((await session.execute(select(OpenStackInstance).order_by(OpenStackInstance.name))).scalars().all())
    return await _read(session, rows)


@router.post("", response_model=OpenStackRead, status_code=201)
async def create_instance(
    payload: OpenStackCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    data = payload.model_dump(exclude={"secret", "ca_cert"})
    if data.get("scope_subnet_ids") is not None:
        data["scope_subnet_ids"] = [str(x) for x in data["scope_subnet_ids"]] or None
    inst = OpenStackInstance(id=uuid.uuid4(), **data, ca_cert=payload.ca_cert or None,
                             secret_enc=b"placeholder", secret_nonce=b"placeholder")
    inst.secret_enc, inst.secret_nonce = svc.encrypt_secret_for(inst.id, payload.secret)
    session.add(inst)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _name_taken(payload.name) from exc
    await append_audit(session, object_type="openstack_instance", object_id=str(inst.id), action="create",
                       diff={"name": inst.name, "product": inst.product, "auth_url": inst.auth_url,
                             "auth_method": inst.auth_method, "ca_cert": bool(inst.ca_cert)},
                       **_audit_kw(user, request))
    await session.commit()
    await session.refresh(inst)
    return (await _read(session, [inst]))[0]


@router.patch("/{instance_id}", response_model=OpenStackRead)
async def update_instance(
    instance_id: uuid.UUID, payload: OpenStackUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    inst = await _get_or_404(session, instance_id)
    data = payload.model_dump(exclude_unset=True)
    new_secret = data.pop("secret", None)
    ca_set = "ca_cert" in data
    ca = data.pop("ca_cert", None)
    for k in ("name", "auth_url", "auth_method", "user_domain", "project_domain", "interface", "product",
              "sync_interval_seconds", "verify_tls", "all_projects", "enabled", "auto_create_ips"):
        if k in data and data[k] is None:
            data.pop(k)                   # 必填欄位給 null＝不改
    for k, v in data.items():
        if k == "scope_subnet_ids":
            v = [str(x) for x in v] if v else None
        setattr(inst, k, v)
    if ca_set and ca is not None:
        inst.ca_cert = ca or None         # "" 清掉
    if new_secret:
        inst.secret_enc, inst.secret_nonce = svc.encrypt_secret_for(inst.id, new_secret)
    try:
        check_auth_fields(inst.auth_method, inst.username, inst.project_name, inst.app_credential_id)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _name_taken(str(data.get("name") or inst.name)) from exc
    diff: dict[str, Any] = {k: str(v)[:120] for k, v in data.items()}
    if new_secret:
        diff["secret"] = _MASKED
    if ca_set and ca is not None:
        diff["ca_cert"] = "set" if ca else "cleared"
    await append_audit(session, object_type="openstack_instance", object_id=str(inst.id), action="update",
                       diff=diff, **_audit_kw(user, request))
    await session.commit()
    await session.refresh(inst)
    return (await _read(session, [inst]))[0]


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    inst = await _get_or_404(session, instance_id)
    await append_audit(session, object_type="openstack_instance", object_id=str(inst.id), action="delete",
                       diff={"name": inst.name}, **_audit_kw(user, request))
    # 它寫進共用表的東西一併收回：主機名稱回報、VM 鏡像（網卡 CASCADE）；port／子網路對應跟著連線 CASCADE。
    # 叢集本身留著 —— 上面可能有使用者設的地點／客戶
    from app.services.integration_cleanup import forget_openstack_instance
    await forget_openstack_instance(session, inst)
    await session.delete(inst)
    await session.commit()


@router.post("/{instance_id}/test", response_model=OpenStackTestResult)
# 函式名不用 test_ 開頭：ruff 的 pytest 規則會把它誤認成測試（路由路徑仍是 /test）
async def check_connection(
    instance_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    inst = await _get_or_404(session, instance_id)
    result = await svc.diagnose(inst)
    # 能力存起來（畫面顯示「最後一次測試／同步」的結果）；節點清單不動
    prev = inst.capabilities or {}
    caps = {**result["capabilities"]}
    if "unbound_floating_ips" in prev:
        caps["unbound_floating_ips"] = prev["unbound_floating_ips"]
    inst.capabilities = caps
    await append_audit(session, object_type="openstack_instance", object_id=str(inst.id), action="test",
                       diff={"ok": result["ok"], "error": (result["error"] or {}).get("code")},
                       **_audit_kw(user, request))
    await session.commit()
    return result


@router.post("/{instance_id}/sync")
async def sync_now(
    instance_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """非同步：立刻回 task_id，同步在背景跑。已經在排隊或執行的，回同一個作業（不重複開）。"""
    from app.models.background_task import BackgroundTask
    inst = await _get_or_404(session, instance_id)
    running = (await session.execute(select(BackgroundTask).where(
        BackgroundTask.kind == "openstack.sync", BackgroundTask.target_id == instance_id,
        BackgroundTask.status.in_(("pending", "running"))).order_by(BackgroundTask.queued_at.desc()).limit(1))
    ).scalars().first()
    if running is not None:
        return {"task_id": str(running.id), "status": running.status, "queued_at": running.queued_at.isoformat(),
                "merged": True}
    actor_user_id, inst_name = user.id, inst.name
    kw = _audit_kw(user, request)

    async def _runner(sess: AsyncSession, _task: Any) -> dict[str, Any]:
        obj = await sess.get(OpenStackInstance, instance_id)
        if obj is None:
            raise RuntimeError("OpenStack instance disappeared")
        summary = await svc.sync_instance(sess, obj)
        await append_audit(sess, object_type="openstack_instance", object_id=str(instance_id), action="sync",
                           diff={k: v for k, v in summary.items() if k != "errors"}, **kw)
        await sess.commit()
        return summary

    task = await spawn_task(
        session=session, kind="openstack.sync", target_type="openstack_instance",
        target_id=instance_id, target_label=inst_name, actor_user_id=actor_user_id, runner=_runner,
    )
    return {"task_id": str(task.id), "status": task.status, "queued_at": task.queued_at.isoformat()}


@router.get("/{instance_id}/subnets", response_model=list[OpenStackSubnetRead])
async def list_subnets(
    instance_id: uuid.UUID, _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    from app.models.subnet import Subnet
    await _get_or_404(session, instance_id)
    rows = (await session.execute(
        select(OpenStackSubnet, Subnet.cidr, Subnet.description)
        .outerjoin(Subnet, Subnet.id == OpenStackSubnet.mapped_subnet_id)
        .where(OpenStackSubnet.instance_id == instance_id)
        .order_by(OpenStackSubnet.project_name.nulls_last(), OpenStackSubnet.cidr))).all()
    return [_subnet_out(r, cidr, desc) for r, cidr, desc in rows]


def _subnet_out(r: OpenStackSubnet, cidr: Any, desc: str | None) -> dict[str, Any]:
    return OpenStackSubnetRead(
        id=r.id, subnet_id=r.subnet_id, name=r.name, network_id=r.network_id, network_name=r.network_name,
        project_id=r.project_id, project_name=r.project_name, cidr=str(r.cidr), ip_version=r.ip_version,
        gateway_ip=str(r.gateway_ip) if r.gateway_ip else None, allocation_pools=r.allocation_pools,
        enable_dhcp=r.enable_dhcp, shared=r.shared, mapped_subnet_id=r.mapped_subnet_id,
        mapped_subnet_cidr=str(cidr) if cidr else None, mapped_subnet_name=desc,
        mapped_manually=r.mapped_manually, pending=r.mapped_subnet_id is None, missing_syncs=r.missing_syncs,
        first_seen_at=r.first_seen_at, last_seen_at=r.last_seen_at,
    ).model_dump(mode="json")


@router.patch("/{instance_id}/subnets/{row_id}", response_model=OpenStackSubnetRead)
async def map_subnet(
    instance_id: uuid.UUID, row_id: uuid.UUID, payload: OpenStackSubnetMapping, user: CurrentUser,
    request: Request, session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """手動指定（或手動設成不對應）：之後同步一律不覆蓋。"""
    from app.models.subnet import Subnet
    await _get_or_404(session, instance_id)
    row = await session.get(OpenStackSubnet, row_id)
    if row is None or row.instance_id != instance_id:
        raise HTTPException(404, detail=_NOT_FOUND)
    target = None
    if payload.mapped_subnet_id is not None:
        target = await session.get(Subnet, payload.mapped_subnet_id)
        if target is None:
            raise HTTPException(404, detail=ui_detail("openstack_not_found", "subnet not found"))
    before = str(row.mapped_subnet_id) if row.mapped_subnet_id else None
    row.mapped_subnet_id = target.id if target is not None else None
    row.mapped_manually = True
    await append_audit(session, object_type="openstack_subnet", object_id=str(row.id), action="update",
                       diff={"instance_id": str(instance_id), "subnet_id": row.subnet_id, "cidr": str(row.cidr),
                             "mapped_subnet_id": [before, str(row.mapped_subnet_id) if row.mapped_subnet_id else None],
                             "mapped_manually": True},
                       **_audit_kw(user, request))
    await session.commit()
    await session.refresh(row)
    return _subnet_out(row, target.cidr if target is not None else None,
                       target.description if target is not None else None)


@router.get("/{instance_id}/nodes")
async def list_nodes(
    instance_id: uuid.UUID, _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    inst = await _get_or_404(session, instance_id)
    return list(inst.nodes or [])


@read_router.get(
    "/ports/by-ip/{ip_id}", response_model=list[OpenStackPortRead],
    dependencies=[Depends(require_object_perm("ip", "read", path_param="ip_id"))])
async def ports_by_ip(
    ip_id: uuid.UUID, _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """這筆 IP 是哪個 OpenStack port 的配置位址：只算 Neutron 子網路對應到這筆 IP 所在子網路的（重疊網段不混）。"""
    from sqlalchemy import cast, literal
    from sqlalchemy.dialects.postgresql import JSONB

    from app.models.address import IPAddress
    from app.models.virt import VirtualMachine

    ip = await session.get(IPAddress, ip_id)
    if ip is None:
        raise HTTPException(404, detail="Not found")
    addr = str(ip.ip).split("/")[0]
    subs = (await session.execute(select(OpenStackSubnet.instance_id, OpenStackSubnet.subnet_id).where(
        OpenStackSubnet.mapped_subnet_id == ip.subnet_id))).all()
    if not subs:
        return []
    out = []
    for inst_id, sub_id in subs:
        probe = cast(literal(json.dumps([{"subnet_id": sub_id, "ip_address": addr}])), JSONB)
        rows = (await session.execute(
            select(OpenStackPort, OpenStackInstance, VirtualMachine)
            .join(OpenStackInstance, OpenStackInstance.id == OpenStackPort.instance_id)
            .outerjoin(VirtualMachine, VirtualMachine.id == OpenStackPort.vm_id)
            .where(OpenStackPort.instance_id == inst_id, OpenStackPort.fixed_ips.op("@>")(probe)))).all()
        for p, inst, vm in rows:
            out.append(OpenStackPortRead(
                id=p.id, instance_id=inst.id, instance_name=inst.name, product=inst.product, port_id=p.port_id,
                mac=str(p.mac) if p.mac else None, status=p.status, device_owner=p.device_owner,
                network_id=p.network_id, network_name=p.network_name, project_id=p.project_id,
                project_name=p.project_name, fixed_ips=p.fixed_ips or [], floating_ips=p.floating_ips or [],
                binding_host=p.binding_host, vm_id=vm.id if vm else None, vm_name=vm.name if vm else None,
                vm_external_id=vm.external_id if vm else None, vm_status=vm.status if vm else None,
                vm_node=vm.node if vm else None, last_seen_at=p.last_seen_at,
                instance_last_sync_at=inst.last_sync_at,
            ).model_dump(mode="json"))
    return out
