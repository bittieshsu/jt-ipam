"""Hyper-V 整合 endpoints（Beta，僅管理員）。

`/test` 逐台、逐步回報（WinRM 連得上嗎、Hyper-V／VMM 模組讀得到嗎、幾台 VM）—— 沒有實機時這是唯一能看出
卡在哪一步的方式。`/sync` 在背景跑（叢集好幾個節點逐台連，可能超過反向代理的 30 秒）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.models.hyperv import HyperVInstance
from app.schemas.hyperv import HyperVCreate, HyperVRead, HyperVUpdate
from app.services import hyperv as svc
from app.services.background_tasks import spawn_task

router = APIRouter(prefix="/hyperv", tags=["hyperv"], dependencies=[Depends(require_admin)])


async def _get_or_404(session: AsyncSession, iid: uuid.UUID) -> HyperVInstance:
    inst = await session.get(HyperVInstance, iid)
    if inst is None:
        raise HTTPException(404, detail="Not found")
    return inst


def _audit_kw(user: Any, request: Request) -> dict[str, Any]:
    return {"actor_user_id": str(user.id), "actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


@router.get("/capabilities")
async def capabilities(_user: CurrentUser) -> dict[str, Any]:
    """這台伺服器能不能用 Kerberos（缺 krb5-user 或 pykerberos 就不行；畫面據此反灰並附安裝方法）。"""
    from app.services import winrm_kerberos
    lack = winrm_kerberos.missing()
    return {"kerberos": not lack, "missing": lack}


@router.get("", response_model=list[HyperVRead])
async def list_instances(
    _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    return (await session.execute(select(HyperVInstance).order_by(HyperVInstance.name))).scalars().all()


@router.post("", response_model=HyperVRead, status_code=201)
async def create_instance(
    payload: HyperVCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    data = payload.model_dump(exclude={"password"})
    data["auto_create_ips"] = bool(data.get("auto_create_ips"))
    inst = HyperVInstance(**data, password_enc=b"placeholder", password_nonce=b"placeholder")
    session.add(inst)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="name already exists") from exc
    inst.password_enc, inst.password_nonce = svc.encrypt_password(inst.id, payload.password)
    await append_audit(session, object_type="hyperv_instance", object_id=str(inst.id), action="create",
                       diff={"name": inst.name, "mode": inst.mode, "host": inst.host}, **_audit_kw(user, request))
    await session.commit()
    await session.refresh(inst)
    return inst


@router.patch("/{instance_id}", response_model=HyperVRead)
async def update_instance(
    instance_id: uuid.UUID, payload: HyperVUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    inst = await _get_or_404(session, instance_id)
    data = payload.model_dump(exclude_unset=True)
    new_pwd = data.pop("password", None)
    for k, v in data.items():
        if k == "extra_hosts":
            v = v or None
        if k == "auto_create_ips":
            v = bool(v)
        setattr(inst, k, v)
    if new_pwd:
        inst.password_enc, inst.password_nonce = svc.encrypt_password(inst.id, new_pwd)
    from app.schemas.hyperv import _kerberos_hosts
    try:
        _kerberos_hosts(inst.auth_method, inst.host, inst.extra_hosts)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="name already exists") from exc
    await append_audit(session, object_type="hyperv_instance", object_id=str(inst.id), action="update",
                       diff={**{k: str(v)[:120] for k, v in data.items()}, **({"password": "***"} if new_pwd else {})},
                       **_audit_kw(user, request))
    await session.commit()
    await session.refresh(inst)
    return inst


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    inst = await _get_or_404(session, instance_id)
    await append_audit(session, object_type="hyperv_instance", object_id=str(inst.id), action="delete",
                       diff={"name": inst.name}, **_audit_kw(user, request))
    # 它寫進共用表的東西一併收回：主機名稱回報、主機上的裝置埠；VM 鏡像（網卡 CASCADE）。
    # 叢集本身留著 —— 上面可能有使用者設的地點／客戶
    from app.services.integration_cleanup import forget_hyperv_instance
    await forget_hyperv_instance(session, inst)
    await session.delete(inst)
    await session.commit()


@router.post("/{instance_id}/test")
# 函式名不用 test_ 開頭：ruff 的 pytest 規則會把它誤認成測試（路由路徑仍是 /test）
async def check_connection(
    instance_id: uuid.UUID, _user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    inst = await _get_or_404(session, instance_id)
    steps = await svc.diagnose(inst)
    return {"ok": bool(steps) and all(s["ok"] for s in steps), "steps": steps}


@router.post("/{instance_id}/sync")
async def sync_now(
    instance_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """非同步：立刻回 task_id，同步在背景跑。"""
    inst = await _get_or_404(session, instance_id)
    actor_user_id, inst_name = user.id, inst.name
    kw = _audit_kw(user, request)

    async def _runner(sess: AsyncSession, _task: Any) -> dict[str, Any]:
        obj = await sess.get(HyperVInstance, instance_id)
        if obj is None:
            raise RuntimeError("Hyper-V instance disappeared")
        summary = await svc.sync_instance(sess, obj)
        await append_audit(sess, object_type="hyperv_instance", object_id=str(instance_id), action="sync",
                           diff=summary, **kw)
        await sess.commit()
        return summary

    task = await spawn_task(
        session=session, kind="hyperv.sync", target_type="hyperv_instance",
        target_id=instance_id, target_label=inst_name, actor_user_id=actor_user_id, runner=_runner,
    )
    return {"task_id": str(task.id), "status": task.status, "queued_at": task.queued_at.isoformat()}
