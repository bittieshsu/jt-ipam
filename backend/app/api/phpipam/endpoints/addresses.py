"""phpIPAM `/addresses/`：唯讀 + 第一空閒查詢。"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.phpipam.helpers import (
    address_to_phpipam,
    phpipam_current_user,
    phpipam_response,
)
from app.core.db import get_session
from app.core.ui_error import detail_of
from app.models.address import IPAddress
from app.models.subnet import Subnet
from app.models.user import User
from app.services.permission import get_object_permission, has_permission
from app.services.subnet import find_first_free_address

router = APIRouter()


async def _gateway_of(session: AsyncSession, subnet_id: uuid.UUID) -> object:
    s = await session.get(Subnet, subnet_id)
    return s.gateway if s else None


async def _apply_gateway(session: AsyncSession, subnet: Subnet, ip: str, payload: dict[str, object]) -> None:
    """phpIPAM 的 is_gateway＝這個位址是子網路的閘道：jt-ipam 存在子網路的 gateway 欄位。"""
    if "is_gateway" not in payload:
        return
    if _bool(payload["is_gateway"]):
        subnet.gateway = ip  # type: ignore[assignment]
    elif subnet.gateway is not None and str(subnet.gateway).split("/")[0] == ip:
        subnet.gateway = None  # type: ignore[assignment]


async def _ensure_subnet_read(session: AsyncSession, user: User, subnet_id: uuid.UUID) -> Subnet:
    s = await session.get(Subnet, subnet_id)
    if s is None:
        raise HTTPException(404, detail="Not found")
    level = await get_object_permission(
        session, user=user, object_type="subnet", object_id=s.id
    )
    if not has_permission(level, "read"):
        raise HTTPException(404, detail="Not found")
    return s


@router.get("/{app_id}/addresses/{address_id}/")
async def get_address(
    app_id: str,
    address_id: uuid.UUID,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    started = time.perf_counter()
    a = await session.get(IPAddress, address_id)
    if a is None:
        raise HTTPException(404, detail="Address not found")
    await _ensure_subnet_read(session, user, a.subnet_id)
    return phpipam_response(success=True, data=address_to_phpipam(a), started=started)


async def get_address_by_ip_subnet(
    app_id: str,
    ip: str,
    subnet_id: uuid.UUID,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    started = time.perf_counter()
    await _ensure_subnet_read(session, user, subnet_id)
    a = (
        await session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet_id,
                IPAddress.ip == ip,
            )
        )
    ).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, detail="Address not found")
    return phpipam_response(success=True, data=address_to_phpipam(a), started=started)


@router.get("/{app_id}/addresses/search/{ip}/")
async def search_by_ip(
    app_id: str,
    ip: str,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    started = time.perf_counter()
    rows = list(
        (await session.execute(select(IPAddress).where(IPAddress.ip == ip))).scalars().all()
    )
    # 過濾 user 對 subnet 有權的
    out = []
    for r in rows:
        try:
            await _ensure_subnet_read(session, user, r.subnet_id)
        except HTTPException:
            continue
        out.append(address_to_phpipam(r, await _gateway_of(session, r.subnet_id)))
    return phpipam_response(success=True, data=out, started=started)


@router.get("/{app_id}/addresses/search_mac/{mac}/")
async def search_by_mac(
    app_id: str,
    mac: str,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """依 MAC 找位址（PVE SDN 的 get_ips_from_mac 會呼叫）。MAC 任何寫法都可以。"""
    from app.services.arp_precedence import normalize_mac
    started = time.perf_counter()
    key = normalize_mac(mac)
    if not key:
        return phpipam_response(success=False, code=400, message="Invalid MAC", started=started)
    pretty = ":".join(key[i:i + 2] for i in range(0, 12, 2))
    rows = list((await session.execute(select(IPAddress).where(IPAddress.mac == pretty))).scalars().all())
    out = []
    for r in rows:
        try:
            await _ensure_subnet_read(session, user, r.subnet_id)
        except HTTPException:
            continue
        out.append(address_to_phpipam(r, await _gateway_of(session, r.subnet_id)))
    return phpipam_response(success=True, data=out, started=started)


@router.get("/{app_id}/addresses/search_hostname/{hostname}/")
async def search_by_hostname(
    app_id: str,
    hostname: str,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    started = time.perf_counter()
    rows = list(
        (
            await session.execute(
                select(IPAddress).where(IPAddress.hostname == hostname)
            )
        ).scalars().all()
    )
    out = []
    for r in rows:
        try:
            await _ensure_subnet_read(session, user, r.subnet_id)
        except HTTPException:
            continue
        out.append(address_to_phpipam(r))
    return phpipam_response(success=True, data=out, started=started)


@router.get("/{app_id}/addresses/first_free/{subnet_id}/")
async def first_free(
    app_id: str,
    subnet_id: uuid.UUID,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    started = time.perf_counter()
    s = await _ensure_subnet_read(session, user, subnet_id)
    ip = await find_first_free_address(session, s)
    if ip is None:
        return phpipam_response(success=False, code=404, message="No free address", started=started)
    return phpipam_response(success=True, data=ip, started=started)


from fastapi import Body, Request

from app.core.audit import append_audit
from app.services.address import (
    IPAlreadyExists,
    IPNotInSubnet,
    SubnetFull,
    allocate_first_free,
    create_ip,
)
from app.services.permission import get_object_permission as _get_perm
from app.services.permission import has_permission as _has_perm


def _bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return v.lower() in {"1", "true", "yes", "on"}
    return False


async def _require_subnet_write(session: AsyncSession, user: User, subnet_id: uuid.UUID) -> Any:
    s = await session.get(Subnet, subnet_id)
    if s is None:
        raise HTTPException(404, detail="Subnet not found")
    level = await _get_perm(session, user=user, object_type="subnet", object_id=s.id)
    if not _has_perm(level, "write"):
        raise HTTPException(404, detail="Subnet not found")
    return s


@router.post("/{app_id}/addresses/")
async def create_address(
    app_id: str,
    request: Request,
    payload: Annotated[dict[str, object], Body()],
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    started = time.perf_counter()
    raw_subnet = payload.get("subnetId")
    raw_ip = payload.get("ip")
    if not raw_subnet:
        raise HTTPException(400, detail="subnetId is required")
    try:
        subnet_id = uuid.UUID(str(raw_subnet))
    except ValueError as exc:
        raise HTTPException(400, detail="Invalid subnetId") from exc
    subnet = await _require_subnet_write(session, user, subnet_id)

    try:
        if not raw_ip:
            obj = await allocate_first_free(
                session, subnet=subnet,
                hostname=payload.get("hostname"),  # type: ignore[arg-type]
                description=payload.get("description"),  # type: ignore[arg-type]
                mac=payload.get("mac"),  # type: ignore[arg-type]
                state=str(payload.get("tag") or "active"),
            )
        else:
            obj = await create_ip(
                session, subnet=subnet, ip=str(raw_ip),
                hostname=payload.get("hostname"),  # type: ignore[arg-type]
                description=payload.get("description"),  # type: ignore[arg-type]
                mac=payload.get("mac"),  # type: ignore[arg-type]
                state=str(payload.get("tag") or "active"),
            )
    except IPNotInSubnet as exc:
        raise HTTPException(400, detail=detail_of(exc, "ip_not_in_subnet")) from exc
    except IPAlreadyExists as exc:
        raise HTTPException(409, detail=detail_of(exc, "ip_already_exists")) from exc
    except SubnetFull as exc:
        raise HTTPException(409, detail=detail_of(exc, "subnet_full")) from exc

    if "owner" in payload:
        obj.owner = payload["owner"]  # type: ignore[assignment]
    if "port" in payload:
        obj.switch_port = payload["port"]  # type: ignore[assignment]
    if "note" in payload:
        obj.note = payload["note"]  # type: ignore[assignment]
    if "excludePing" in payload:
        obj.exclude_from_ping = _bool(payload["excludePing"])
    if "PTRignore" in payload:
        obj.ptr_ignore = _bool(payload["PTRignore"])
    await _apply_gateway(session, subnet, str(obj.ip).split("/")[0], payload)

    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="ip_address",
        object_id=str(obj.id),
        action="create",
        diff={"after": {"ip": str(obj.ip), "subnet_id": str(obj.subnet_id)}, "via": "phpipam"},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    await session.refresh(obj)
    return phpipam_response(
        success=True, code=201, message="Address created",
        data={"id": str(obj.id), "ip": str(obj.ip).split("/")[0]}, started=started,
    )


@router.patch("/{app_id}/addresses/{address_id}/")
async def update_address(
    app_id: str,
    address_id: uuid.UUID,
    request: Request,
    payload: Annotated[dict[str, object], Body()],
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    started = time.perf_counter()
    a = await session.get(IPAddress, address_id)
    if a is None:
        raise HTTPException(404, detail="Address not found")
    await _require_subnet_write(session, user, a.subnet_id)

    before = {"hostname": a.hostname, "tag": a.state, "description": a.description}
    if "hostname" in payload:
        a.hostname = payload["hostname"]  # type: ignore[assignment]
    if "description" in payload:
        a.description = payload["description"]  # type: ignore[assignment]
    if "tag" in payload:
        a.state = str(payload["tag"])
    if "owner" in payload:
        a.owner = payload["owner"]  # type: ignore[assignment]
    if "port" in payload:
        a.switch_port = payload["port"]  # type: ignore[assignment]
    if "note" in payload:
        a.note = payload["note"]  # type: ignore[assignment]
    if "mac" in payload:
        old_mac = a.mac
        a.mac = payload["mac"]  # type: ignore[assignment]
        # 透過 API 明確寫入的 MAC 等同人工編輯：標成 manual（優先序最高）；清空時一併清掉來源
        a.mac_source = "manual" if a.mac else None  # type: ignore[assignment]
        if str(old_mac or "").lower() != str(a.mac or "").lower():
            # 以前這條路徑改 MAC 不留異動記錄，也不清上一台的資料（2026-10-12）
            from app.services.ip_history import log_change
            await log_change(session, ip=a, event_type="mac_changed", field="mac",
                             old=str(old_mac) if old_mac else None, new=str(a.mac) if a.mac else None,
                             source="manual", actor_user_id=user.id)
            if old_mac and a.mac:
                from app.services.device_turnover import on_device_replaced
                await on_device_replaced(session, ip=a, source="manual", actor_user_id=user.id)

    if "is_gateway" in payload:
        sub = await session.get(Subnet, a.subnet_id)
        if sub is not None:
            await _apply_gateway(session, sub, str(a.ip).split("/")[0], payload)

    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="ip_address",
        object_id=str(a.id),
        action="update",
        diff={"before": before, "via": "phpipam"},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return phpipam_response(success=True, message="Address updated", started=started)


@router.delete("/{app_id}/addresses/{address_id}/")
async def delete_address(
    app_id: str,
    address_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    started = time.perf_counter()
    a = await session.get(IPAddress, address_id)
    if a is None:
        raise HTTPException(404, detail="Address not found")
    # 跟 REST 一樣要子網路 admin，也走同一套刪除步驟（異動記錄、冷卻期）。以前只要寫入權、
    # 而且直接刪掉：網路操作員自建一個 token 就能刪任何 IP，位址也馬上能再配給別人（2026-10-07 稽核）
    s = await session.get(Subnet, a.subnet_id)
    level = await _get_perm(session, user=user, object_type="subnet", object_id=a.subnet_id) if s else "none"
    if not _has_perm(level, "admin"):
        raise HTTPException(404, detail="Subnet not found")
    # 別名照實說它會寫稽核（tests/test_audit_coverage.py 靠呼叫名稱判斷，跨檔的 helper 看不到裡面）
    from app.api.v1.endpoints.addresses import _delete_ip as _delete_ip_and_audit
    await _delete_ip_and_audit(session, a, user=user, request=request, bulk=False, via="phpipam")
    await session.commit()
    return phpipam_response(success=True, message="Address deleted", started=started)


@router.post("/{app_id}/addresses/first_free/{subnet_id}/")
async def allocate_first_free_address(
    app_id: str,
    subnet_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(phpipam_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: Annotated[dict[str, object] | None, Body()] = None,
) -> dict[str, object]:
    """佔用子網路的下一個空位並回 IP 字串（phpIPAM 的 POST addresses/first_free；Proxmox VE SDN 幫 VM 配位址用）。

    跟 POST addresses/ 不帶 ip 走同一條：同樣的寫入權、稽核與 IP 異動記錄。
    """
    body = dict(payload or {})
    body["subnetId"] = str(subnet_id)
    body.pop("ip", None)
    r = await create_address(app_id, request, body, user, session)
    data = r.get("data") or {}
    return phpipam_response(success=True, code=201, message="Address created",
                            data=data.get("ip") if isinstance(data, dict) else data)


# 放最後：兩段的 {ip}/{subnet_id} 會吃掉 search/{ip}、search_mac/{mac}、first_free/{id} 這類路徑
# （FastAPI 照註冊順序比對，比對到才驗 UUID，驗失敗就回 422 不再往下找）
router.get("/{app_id}/addresses/{ip}/{subnet_id}/")(get_address_by_ip_subnet)
