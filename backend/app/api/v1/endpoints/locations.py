"""Location + Rack endpoints。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.config import get_settings
from app.core.db import get_session
from app.models.location import Location, Rack
from app.schemas.base import Paginated, StrictModel
from app.schemas.location import (
    LocationCreate,
    LocationRead,
    LocationUpdate,
    RackCreate,
    RackPositionsUpdate,
    RackRead,
    RackUpdate,
)

router = APIRouter(tags=["locations"])

# 機房平面圖上傳限制（A03/A08：限類型 + 限大小 + 驗 magic bytes，禁 SVG 避免 XSS）
_MAX_FLOORPLAN_BYTES = 8 * 1024 * 1024
_IMG_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _detect_image_ext(data: bytes) -> str | None:
    """只認可信任的點陣圖格式；用 magic bytes 而非副檔名/Content-Type。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _floorplan_dir() -> Path:
    return Path(get_settings().upload_dir) / "floorplans"


# ─────────────────── Locations ───────────────────
@router.get("/locations", response_model=Paginated[LocationRead])
async def list_locations(
    _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=500),
) -> Paginated[LocationRead]:
    stmt = select(Location)
    cstmt = select(func.count()).select_from(Location)
    from app.services.permission import visible_ids
    vis = await visible_ids(session, user=_user, object_type="location")
    if vis is not None:
        stmt = stmt.where(Location.id.in_(vis)); cstmt = cstmt.where(Location.id.in_(vis))
    rows = list(
        (await session.execute(
            stmt.order_by(Location.name).offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
    )
    total = int(await session.scalar(cstmt) or 0)
    return Paginated[LocationRead](
        items=[LocationRead.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get("/locations/{location_id}", response_model=LocationRead)
async def get_location(
    location_id: uuid.UUID,
    _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationRead:
    obj = await session.get(Location, location_id)
    if obj is None:
        raise HTTPException(404, detail="Location not found")
    return LocationRead.model_validate(obj)


@router.post("/locations", response_model=LocationRead, status_code=201,
             dependencies=[Depends(require_admin)])
async def create_location(
    payload: LocationCreate,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationRead:
    obj = Location(**payload.model_dump())
    session.add(obj)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Location name conflict") from exc
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="location", object_id=str(obj.id), action="create",
        diff={"after": payload.model_dump(mode="json")},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    await session.refresh(obj)
    return LocationRead.model_validate(obj)


@router.patch("/locations/{location_id}", response_model=LocationRead,
              dependencies=[Depends(require_admin)])
async def update_location(
    location_id: uuid.UUID,
    payload: LocationUpdate,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationRead:
    obj = await session.get(Location, location_id)
    if obj is None:
        raise HTTPException(404, detail="Location not found")
    before = {"name": obj.name, "address": obj.address}
    changes = payload.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(obj, k, v)
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="location", object_id=str(obj.id), action="update",
        diff={"before": before, "changes": changes},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    await session.refresh(obj)
    return LocationRead.model_validate(obj)


@router.delete("/locations/{location_id}", status_code=204,
               dependencies=[Depends(require_admin)])
async def delete_location(
    location_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    obj = await session.get(Location, location_id)
    if obj is None:
        raise HTTPException(404, detail="Location not found")
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="location", object_id=str(obj.id), action="delete",
        diff={"before": {"name": obj.name}},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.delete(obj)
    await session.commit()


# ─────────────────── 機房平面圖 ───────────────────
@router.post("/locations/{location_id}/floorplan", response_model=LocationRead,
             dependencies=[Depends(require_admin)])
async def upload_floorplan(
    location_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File()],
) -> LocationRead:
    obj = await session.get(Location, location_id)
    if obj is None:
        raise HTTPException(404, detail="Location not found")
    data = await file.read(_MAX_FLOORPLAN_BYTES + 1)
    if len(data) > _MAX_FLOORPLAN_BYTES:
        raise HTTPException(413, detail="floor plan too large (max 8 MB)")
    ext = _detect_image_ext(data)
    if ext is None:
        raise HTTPException(415, detail="unsupported image type (png / jpg / gif / webp only)")

    base = _floorplan_dir()
    base.mkdir(parents=True, exist_ok=True)
    rel = f"floorplans/{location_id}.{ext}"
    dest = base / f"{location_id}.{ext}"
    # 清掉同 id 但不同副檔名的舊圖（避免換格式後殘留）
    for old in base.glob(f"{location_id}.*"):
        if old != dest:
            old.unlink(missing_ok=True)
    dest.write_bytes(data)
    dest.chmod(0o640)

    obj.floor_plan_path = rel
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="location", object_id=str(obj.id), action="update",
        diff={"changes": {"floor_plan_path": rel}, "bytes": len(data)},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    await session.refresh(obj)
    return LocationRead.model_validate(obj)


@router.get("/locations/{location_id}/floorplan")
async def get_floorplan(
    location_id: uuid.UUID,
    _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FileResponse:
    obj = await session.get(Location, location_id)
    if obj is None or not obj.floor_plan_path:
        raise HTTPException(404, detail="No floor plan")
    base = _floorplan_dir().resolve()
    path = (Path(get_settings().upload_dir) / obj.floor_plan_path).resolve()
    # path traversal 防護：解析後必須仍在 floorplans 目錄內
    if base != path.parent or not path.is_file():
        raise HTTPException(404, detail="No floor plan")
    media = _IMG_MEDIA.get(path.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(path, media_type=media)


@router.delete("/locations/{location_id}/floorplan", status_code=204,
               dependencies=[Depends(require_admin)])
async def delete_floorplan(
    location_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    obj = await session.get(Location, location_id)
    if obj is None:
        raise HTTPException(404, detail="Location not found")
    if obj.floor_plan_path:
        for old in _floorplan_dir().glob(f"{location_id}.*"):
            old.unlink(missing_ok=True)
        obj.floor_plan_path = None
        await append_audit(
            session, actor_user_id=str(user.id),
            actor_ip=request.client.host if request.client else None,
            actor_user_agent=request.headers.get("user-agent"),
            object_type="location", object_id=str(obj.id), action="update",
            diff={"changes": {"floor_plan_path": None}},
            request_id=getattr(request.state, "request_id", None),
        )
        await session.commit()


@router.put("/locations/{location_id}/rack-positions",
            dependencies=[Depends(require_admin)])
async def set_rack_positions(
    location_id: uuid.UUID,
    payload: RackPositionsUpdate,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    """設定此機房內機櫃在平面圖上的座標（0..1）。positions 視為「完整擺放狀態」：
    清單內的設座標，清單外（此機房的其他機櫃）一律清空座標 → 對應前端的「移除擺放」。
    只動屬於此 location 的機櫃。"""
    racks = {
        r.id: r for r in (await session.execute(
            select(Rack).where(Rack.location_id == location_id)
        )).scalars().all()
    }
    pos_map = {p.id: p for p in payload.positions}
    updated = 0
    for rid, r in racks.items():
        p = pos_map.get(rid)
        if p is not None:
            if (r.pos_x != p.pos_x or r.pos_y != p.pos_y or r.pos_rot != p.pos_rot
                    or r.pos_w != p.pos_w or r.pos_h != p.pos_h):
                updated += 1
            r.pos_x, r.pos_y, r.pos_rot = p.pos_x, p.pos_y, p.pos_rot
            r.pos_w, r.pos_h = p.pos_w, p.pos_h
        elif r.pos_x is not None or r.pos_y is not None:
            r.pos_x, r.pos_y = None, None   # 清單外 → 清空擺放
            r.pos_rot = 0
            r.pos_w, r.pos_h = None, None
            updated += 1
    if updated:
        await append_audit(
            session, actor_user_id=str(user.id),
            actor_ip=request.client.host if request.client else None,
            actor_user_agent=request.headers.get("user-agent"),
            object_type="location", object_id=str(location_id), action="update",
            diff={"changes": {"rack_positions": updated}},
            request_id=getattr(request.state, "request_id", None),
        )
        await session.commit()
    return {"updated": updated}


# ─────────────────── Racks ───────────────────
@router.get("/racks", response_model=Paginated[RackRead])
async def list_racks(
    _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    location_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=500),
) -> Paginated[RackRead]:
    stmt = select(Rack)
    cstmt = select(func.count()).select_from(Rack)
    if location_id is not None:
        stmt = stmt.where(Rack.location_id == location_id)
        cstmt = cstmt.where(Rack.location_id == location_id)
    from app.services.permission import visible_ids
    vis = await visible_ids(session, user=_user, object_type="rack")
    if vis is not None:
        stmt = stmt.where(Rack.id.in_(vis)); cstmt = cstmt.where(Rack.id.in_(vis))
    stmt = stmt.order_by(Rack.name).offset((page - 1) * page_size).limit(page_size)
    rows = list((await session.execute(stmt)).scalars().all())
    total = int(await session.scalar(cstmt) or 0)
    return Paginated[RackRead](
        items=[RackRead.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post("/racks", response_model=RackRead, status_code=201,
             dependencies=[Depends(require_admin)])
async def create_rack(
    payload: RackCreate,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RackRead:
    obj = Rack(**payload.model_dump())
    session.add(obj)
    await session.flush()
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="rack", object_id=str(obj.id), action="create",
        diff={"after": payload.model_dump(mode="json")},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    await session.refresh(obj)
    return RackRead.model_validate(obj)


@router.patch("/racks/{rack_id}", response_model=RackRead,
              dependencies=[Depends(require_admin)])
async def update_rack(
    rack_id: uuid.UUID,
    payload: RackUpdate,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RackRead:
    obj = await session.get(Rack, rack_id)
    if obj is None:
        raise HTTPException(404, detail="Rack not found")
    before = {"name": obj.name, "u_height": obj.u_height,
              "location_id": str(obj.location_id) if obj.location_id else None}
    changes = payload.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(obj, k, v)
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="rack", object_id=str(obj.id), action="update",
        diff={"before": before, "changes": payload.model_dump(exclude_unset=True, mode="json")},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    await session.refresh(obj)
    return RackRead.model_validate(obj)


@router.delete("/racks/{rack_id}", status_code=204,
               dependencies=[Depends(require_admin)])
async def delete_rack(
    rack_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    obj = await session.get(Rack, rack_id)
    if obj is None:
        raise HTTPException(404, detail="Rack not found")
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="rack", object_id=str(obj.id), action="delete",
        diff={"before": {"name": obj.name}},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.delete(obj)
    await session.commit()


class _BulkDelete(StrictModel):
    ids: list[uuid.UUID]


async def _bulk(session, model_cls, object_type, user, ids, actor_ip, actor_ua, request_id):
    if not ids: return {"deleted": 0, "failed": 0, "errors": []}
    if len(ids) > 500:
        raise HTTPException(400, detail="too many ids (max 500)")
    deleted, errors = 0, []
    for oid in ids:
        obj = await session.get(model_cls, oid)
        if obj is None:
            errors.append({"id": str(oid), "error": "not_found"}); continue
        await append_audit(
            session, actor_user_id=str(user.id),
            actor_ip=actor_ip, actor_user_agent=actor_ua,
            object_type=object_type, object_id=str(obj.id), action="delete",
            diff={"before": {"name": getattr(obj, "name", None)}, "bulk": True},
            request_id=request_id,
        )
        await session.delete(obj)
        deleted += 1
    await session.commit()
    return {"deleted": deleted, "failed": len(errors), "errors": errors[:50]}


@router.post("/locations/bulk-delete", dependencies=[Depends(require_admin)])
async def bulk_delete_locations(
    payload: _BulkDelete, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    return await _bulk(session, Location, "location", user, payload.ids,
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
        getattr(request.state, "request_id", None))


@router.post("/racks/bulk-delete", dependencies=[Depends(require_admin)])
async def bulk_delete_racks(
    payload: _BulkDelete, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    return await _bulk(session, Rack, "rack", user, payload.ids,
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
        getattr(request.state, "request_id", None))
