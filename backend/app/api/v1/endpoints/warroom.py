"""戰情室（大螢幕看板）端點。規格：docs/SPEC_WARROOM_zh-TW.md。

兩種存取：
- 一般登入：看到自己權限範圍內的資料
- 電視用的顯示權杖（`X-Warroom-Token`，預設關閉）：只能讀這裡的 /warroom/* 端點，看到的是設定裡
  指定帳號（principal_user_id）的可見範圍；帳號停用、權杖過期或撤銷都立即失效

頁面資料依（身分, 主題, 頁碼, 設定）快取 CACHE_SECONDS 秒：好幾台電視每分鐘刷新，不會把資料庫打爆。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, _bearer, get_current_user, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import ui_detail
from app.models.user import User
from app.schemas.base import StrictModel
from app.services.warroom import config as wcfg
from app.services.warroom.common import full_access, scope_active

router = APIRouter(prefix="/warroom", tags=["warroom"])
admin_router = APIRouter(prefix="/system/warroom", tags=["warroom"], dependencies=[Depends(require_admin)])

CACHE_SECONDS = 30
_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_CACHE_MAX = 256


@dataclass
class Principal:
    user: User
    via: str               # session／display


async def warroom_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal:
    token = request.headers.get("x-warroom-token")
    cfg = await wcfg.get_warroom(session)
    if token:
        from app.services.system_config import public_token_problem
        problem = public_token_problem(cfg, token) if cfg["display_enabled"] else "disabled"
        if problem:
            raise HTTPException(401, detail=ui_detail(f"warroom_token_{problem}", "display token is not valid"))
        user = await session.get(User, uuid.UUID(cfg["principal_user_id"])) if cfg["principal_user_id"] else None
        if user is None or not user.is_active:
            raise HTTPException(401, detail=ui_detail("warroom_token_principal", "display account is not usable"))
        return Principal(user, "display")
    user = await get_current_user(request, credentials, session)
    if not cfg["enabled"]:
        raise HTTPException(403, detail=ui_detail("warroom_disabled", "the war room is turned off"))
    return Principal(user, "session")


WarroomPrincipal = Annotated[Principal, Depends(warroom_principal)]


@router.get("/config")
async def warroom_config(p: WarroomPrincipal, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """畫面用的設定（不含權杖、不含代表帳號）。"""
    cfg = await wcfg.get_warroom(session)
    return {
        "pages": [k for k in cfg["pages"] if k in wcfg.READY_PAGES],
        "all_pages": list(wcfg.PAGES), "ready_pages": list(wcfg.READY_PAGES),
        "dwell_seconds": cfg["dwell_seconds"], "max_subpages": cfg["max_subpages"],
        "group_by": cfg["group_by"], "language": cfg["language"], "reduce_motion": cfg["reduce_motion"],
        "thresholds": {"warn": cfg["capacity_warn"], "crit": cfg["capacity_crit"]},
        "via": p.via, "is_admin": bool(p.user.is_admin),
        # 管理員或戰情室檢視者：頁首寫「全部站台」而不是「可見範圍」
        "full": await full_access(session, p.user),
        # 顯示範圍（頁首顯示「部分範圍：…」）；名稱只給前幾個，不洩漏觀看者看不到的東西以外的資訊
        "scope": await _scope_summary(session, cfg) if scope_active(cfg) else None,
    }


async def _scope_labels(session: AsyncSession, cfg: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """設定的顯示範圍 → 每一種的 [{value, label}]（設定頁的選單要顯示名稱；已刪除的物件會被略過）。"""
    from sqlalchemy import select

    from app.core.sqlin import in_values
    from app.models.customer import Customer
    from app.models.section import Section
    from app.models.subnet import Subnet

    sc = cfg.get("scope") or {}
    out: dict[str, list[dict[str, str]]] = {"customers": [], "sections": [], "subnets": []}
    ids = {k: [uuid.UUID(x) for x in sc.get(k) or []] for k in out}
    if ids["customers"]:
        out["customers"] = [{"value": str(i), "label": t or n} for i, n, t in (await session.execute(
            select(Customer.id, Customer.name, Customer.title).where(in_values(Customer.id, ids["customers"])))).all()]
    if ids["sections"]:
        out["sections"] = [{"value": str(i), "label": n} for i, n in (await session.execute(
            select(Section.id, Section.name).where(in_values(Section.id, ids["sections"])))).all()]
    if ids["subnets"]:
        out["subnets"] = [{"value": str(i), "label": f"{c} {d}".strip() if d else str(c)} for i, c, d in (
            await session.execute(select(Subnet.id, Subnet.cidr, Subnet.description)
                                  .where(in_values(Subnet.id, ids["subnets"])))).all()]
    return out


async def _scope_summary(session: AsyncSession, cfg: dict[str, Any]) -> dict[str, Any]:
    labels = await _scope_labels(session, cfg)
    names = [x["label"] for k in ("customers", "sections", "subnets") for x in labels[k]]
    return {"names": names[:2], "more": max(len(names) - 2, 0)}


@router.get("/pages/{key}")
async def warroom_page(
    key: str, p: WarroomPrincipal, session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
) -> dict[str, Any]:
    from app.services.warroom import (
        anomalies,
        assets,
        capacity,
        collection,
        overview,
        services,
        topology,
    )

    builders = {"overview": overview.build, "capacity": capacity.build, "topology": topology.build,
                "services": services.build, "anomalies": anomalies.build, "assets": assets.build,
                "collection": collection.build}
    if key not in builders:
        raise HTTPException(404, detail=ui_detail("warroom_page_unknown", "unknown war room page", key=key))
    cfg = await wcfg.get_warroom(session)
    sc = cfg.get("scope") or {}
    ck = (str(p.user.id), key, page, cfg["group_by"], cfg["capacity_warn"], cfg["capacity_crit"],
          tuple(tuple(sorted(sc.get(k) or [])) for k in ("customers", "sections", "subnets")))
    hit = _cache.get(ck)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    data = await builders[key](session, user=p.user, cfg=cfg, page=page)
    if len(_cache) >= _CACHE_MAX:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 4]:
            _cache.pop(k, None)
    _cache[ck] = (now, data)
    return data


# ─────────────────────────── 管理：設定與顯示權杖 ───────────────────────────

class WarroomScope(StrictModel):
    customers: list[str] = Field(default_factory=list, max_length=wcfg.MAX_SCOPE)
    sections: list[str] = Field(default_factory=list, max_length=wcfg.MAX_SCOPE)
    subnets: list[str] = Field(default_factory=list, max_length=wcfg.MAX_SCOPE)


class WarroomPatch(StrictModel):
    enabled: bool = True
    pages: list[str] = Field(default_factory=lambda: list(wcfg.READY_PAGES), max_length=20)
    dwell_seconds: int = Field(20, ge=5, le=600)
    max_subpages: int = Field(2, ge=1, le=50)
    group_by: str = "location"
    capacity_warn: int = Field(80, ge=1, le=99)
    capacity_crit: int = Field(90, ge=2, le=100)
    language: str = ""
    reduce_motion: bool = False
    ai_summary: str = "off"
    display_enabled: bool = False
    principal_user_id: str | None = None
    scope: WarroomScope = Field(default_factory=WarroomScope)


class TokenRotate(StrictModel):
    days: int = Field(365, ge=1, le=365)


def _audit_kw(user: Any, request: Request) -> dict[str, Any]:
    return {"actor_user_id": str(user.id), "actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


def _out(cfg: dict[str, Any]) -> dict[str, Any]:
    v = wcfg.public_view(cfg)
    v.pop("ready_seen", None)
    exp = v.get("token_expires_at")
    v["token_expires_at"] = exp.isoformat() if exp else None
    v["all_pages"], v["ready_pages"] = list(wcfg.PAGES), list(wcfg.READY_PAGES)
    return v


@admin_router.get("")
async def get_settings(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    cfg = await wcfg.get_warroom(session)
    return {**_out(cfg), "scope_labels": await _scope_labels(session, cfg)}


@admin_router.get("/scope-options")
async def scope_options(
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: Annotated[str, Query(pattern="^(customers|sections|subnets)$")],
    q: Annotated[str, Query(max_length=100)] = "",
) -> list[dict[str, str]]:
    """顯示範圍的選單：依名稱（子網路也比 CIDR 與說明）搜尋，最多回 50 筆（十萬個子網路也不會整份載入）。"""
    from sqlalchemy import String, cast, or_, select

    from app.models.customer import Customer
    from app.models.section import Section
    from app.models.subnet import Subnet

    like = f"%{q.strip()}%"
    if kind == "customers":
        rows = (await session.execute(select(Customer.id, Customer.name, Customer.title).where(
            or_(Customer.name.ilike(like), Customer.title.ilike(like))).order_by(Customer.name).limit(50))).all()
        return [{"value": str(i), "label": t or n} for i, n, t in rows]
    if kind == "sections":
        rows = (await session.execute(select(Section.id, Section.name).where(Section.name.ilike(like))
                                      .order_by(Section.name).limit(50))).all()
        return [{"value": str(i), "label": n} for i, n in rows]
    rows = (await session.execute(select(Subnet.id, Subnet.cidr, Subnet.description).where(
        Subnet.archived_at.is_(None),
        or_(cast(Subnet.cidr, String).ilike(like), Subnet.description.ilike(like)))
        .order_by(Subnet.cidr).limit(50))).all()
    return [{"value": str(i), "label": f"{c} {d}".strip() if d else str(c)} for i, c, d in rows]


@admin_router.put("")
async def put_settings(payload: WarroomPatch, user: CurrentUser, request: Request,
                       session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    data = payload.model_dump()
    if data["capacity_warn"] >= data["capacity_crit"]:
        raise HTTPException(422, detail=ui_detail("warroom_thresholds", "warning threshold must be below critical"))
    if data["display_enabled"]:
        pid = data.get("principal_user_id")
        try:
            target = await session.get(User, uuid.UUID(pid)) if pid else None
        except ValueError:
            target = None
        if target is None or not target.is_active:
            raise HTTPException(422, detail=ui_detail(
                "warroom_principal_required", "choose an active account whose visibility the display shows"))
    cfg = await wcfg.set_warroom(session, data, updated_by_user_id=user.id)
    await append_audit(session, object_type="system_setting", object_id=None, action="update",
                       diff={"setting": "warroom", **{k: data[k] for k in (
                           "enabled", "pages", "group_by", "display_enabled", "principal_user_id", "scope")}},
                       **_audit_kw(user, request))
    await session.commit()
    _cache.clear()
    return {**_out(cfg), "scope_labels": await _scope_labels(session, cfg)}


@admin_router.post("/token")
async def rotate_token(payload: TokenRotate, user: CurrentUser, request: Request,
                       session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """產生（或更換）顯示權杖：舊的立即失效。明文只在這次回傳一次。"""
    cfg = await wcfg.rotate_display_token(session, days=payload.days, updated_by_user_id=user.id)
    await append_audit(session, object_type="system_setting", object_id=None, action="update",
                       diff={"setting": "warroom_display_token", "changes": "rotated", "expires_in_days": payload.days},
                       **_audit_kw(user, request))
    await session.commit()
    exp = cfg["token_expires_at"]
    return {"token": cfg["token"], "expires_at": exp.isoformat() if exp else None}


@admin_router.get("/token")
async def reveal_token(user: CurrentUser, request: Request,
                       session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    cfg = await wcfg.get_warroom(session)
    if not cfg["token"]:
        raise HTTPException(404, detail="Not found")
    await append_audit(session, object_type="system_setting", object_id=None, action="secret_view",
                       diff={"setting": "warroom_display_token"}, **_audit_kw(user, request))
    await session.commit()
    exp = cfg["token_expires_at"]
    return {"token": cfg["token"], "expires_at": exp.isoformat() if exp else None}


@admin_router.delete("/token", status_code=204)
async def revoke_token(user: CurrentUser, request: Request,
                       session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    await wcfg.revoke_display_token(session, updated_by_user_id=user.id)
    await append_audit(session, object_type="system_setting", object_id=None, action="update",
                       diff={"setting": "warroom_display_token", "changes": "revoked"}, **_audit_kw(user, request))
    await session.commit()
    _cache.clear()
