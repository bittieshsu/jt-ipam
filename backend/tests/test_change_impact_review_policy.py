"""IP 變更評估的審核關卡（使用者 2026-10-08：「申請審核設定」分成 IP 申請審核與 IP 變更評估審核，各自設定關卡）。

模式比照 IP 申請審核：僅管理員、管理員＋指定人員、多組會簽（全部通過）、依序多關卡；
另外保留「對目標有修改權的人」（升級前的預設行為）。每一關的審核人還是要看得到目標；管理員任何一關都能核准。
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.models.notification import Notification
from app.services.change_impact import ai as impact_ai
from app.services.change_impact import jobs
from sqlalchemy import select

from tests.test_change_impact_api import _enable, _grant, _hdr, _plan, _setup, _user


@pytest.fixture(autouse=True)
def _inline(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jobs, "SPAWN_IN_BACKGROUND", False)
    monkeypatch.setattr(impact_ai, "SPAWN_IN_BACKGROUND", False)


async def _set_policy(client, headers, **pol: Any):  # type: ignore[no-untyped-def]
    body = {"approver_mode": "editors", "designated_user_ids": [], "designated_group_ids": [],
            "allow_self_approve": False, "stages": [], **pol}
    return await client.put("/api/v1/change-impact/review-policy", headers=headers, json=body)


async def _submitted(client, headers, ip_id) -> str:  # type: ignore[no-untyped-def]
    pid = (await _plan(client, headers, ip_id)).json()["id"]
    assert (await client.post(f"/api/v1/change-plans/{pid}/runs", headers=headers)).status_code == 202
    r = await client.post(f"/api/v1/change-plans/{pid}/transitions", headers=headers, json={"action": "submit"})
    assert r.status_code == 200, r.text
    return pid


async def _approve(client, headers, pid):  # type: ignore[no-untyped-def]
    run_id = (await client.get(f"/api/v1/change-plans/{pid}", headers=headers)).json()["current_run_id"]
    items = (await client.get(f"/api/v1/impact-runs/{run_id}/findings", headers=headers,
                              params={"page_size": 200})).json()["items"]
    disp = {f["id"]: {"action": "fix", "note": "ok"} for f in items if f["disposition"] == "review"}
    return await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=headers,
                             json={"decision": "accept_risk", "rationale": "checked by the team", "dispositions": disp})


async def _people(db, sub, n: int):  # type: ignore[no-untyped-def]
    out = []
    for _ in range(n):
        u = await _user(db)
        await _grant(db, u, "subnet", sub.id, "write")
        out.append(u)
    return out


async def _lifecycle(client, headers, pid) -> str:  # type: ignore[no-untyped-def]
    return (await client.get(f"/api/v1/change-plans/{pid}", headers=headers)).json()["lifecycle"]


async def test_default_policy_keeps_the_old_behaviour(client, auth_headers, db_session) -> None:
    """沒存過新設定：有舊的審核人名單就是「指定人員」，沒有就是「有修改權的人」，升級後行為不變。"""
    await _enable(db_session)
    r = await client.get("/api/v1/change-impact/review-policy", headers=auth_headers)
    assert r.status_code == 200 and r.json()["approver_mode"] == "editors"
    from app.models.user import Group
    g = Group(name=f"rv-{uuid.uuid4().hex[:6]}")
    db_session.add(g)
    await db_session.commit()
    gid = str(g.id)
    await _enable(db_session, reviewer_group_ids=[gid], allow_self_review=True)
    body = (await client.get("/api/v1/change-impact/review-policy", headers=auth_headers)).json()
    assert body["approver_mode"] == "designated" and body["designated_group_ids"] == [gid]
    assert body["allow_self_approve"] is True


async def test_only_admins_can_change_the_policy_and_it_is_audited(client, auth_headers, db_session) -> None:
    from app.models.audit import AuditLog
    await _enable(db_session)
    u = await _user(db_session)
    assert (await _set_policy(client, _hdr(u), approver_mode="admin")).status_code == 403
    r = await _set_policy(client, auth_headers, approver_mode="admin")
    assert r.status_code == 200 and r.json()["approver_mode"] == "admin"
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.object_type == "system_setting"))).scalars().all()
    assert any("change_impact_review_policy" in (a.diff or {}) for a in rows)
    # 多關卡模式至少要一關、每關要有審核人
    r = await _set_policy(client, auth_headers, approver_mode="stages", stages=[])
    assert r.status_code == 422
    r = await _set_policy(client, auth_headers, approver_mode="stages", stages=[{"name": "x", "user_ids": [], "group_ids": []}])
    assert r.status_code == 422


async def test_sequential_stages(client, auth_headers, db_session) -> None:
    await _enable(db_session)
    _sec, sub, ip = await _setup(db_session)
    creator, first, second = await _people(db_session, sub, 3)
    r = await _set_policy(client, auth_headers, approver_mode="stages", stages=[
        {"name": "網路組", "user_ids": [str(first.id)], "group_ids": []},
        {"name": "主管", "user_ids": [str(second.id)], "group_ids": []}])
    assert r.status_code == 200, r.text
    pid = await _submitted(client, _hdr(creator), ip.id)

    # 第一關才通知、才能審
    told = lambda uid: db_session.execute(select(Notification).where(  # noqa: E731
        Notification.user_id == uid, Notification.object_id == uuid.UUID(pid)))
    assert len((await told(first.id)).scalars().all()) == 1
    assert not (await told(second.id)).scalars().all()
    view = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(second))).json()
    assert view["can_review"] is False
    assert [s["name"] for s in view["review_steps"]] == ["網路組", "主管"]
    assert view["review_steps"][0]["is_current"] is True
    r = await _approve(client, _hdr(second), pid)
    assert r.status_code == 403, r.text

    assert (await _approve(client, _hdr(first), pid)).status_code == 201
    assert await _lifecycle(client, _hdr(creator), pid) == "in_review"      # 還有下一關
    assert len((await told(second.id)).scalars().all()) == 1                 # 輪到第二關才通知
    view = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(second))).json()
    assert view["can_review"] is True and view["review_steps"][0]["approved"] is True

    assert (await _approve(client, _hdr(second), pid)).status_code == 201
    assert await _lifecycle(client, _hdr(creator), pid) == "approved"


async def test_parallel_needs_every_group(client, auth_headers, db_session) -> None:
    await _enable(db_session)
    _sec, sub, ip = await _setup(db_session)
    creator, a, b = await _people(db_session, sub, 3)
    await _set_policy(client, auth_headers, approver_mode="parallel", stages=[
        {"name": "A", "user_ids": [str(a.id)], "group_ids": []},
        {"name": "B", "user_ids": [str(b.id)], "group_ids": []}])
    pid = await _submitted(client, _hdr(creator), ip.id)
    assert (await _approve(client, _hdr(b), pid)).status_code == 201       # 不分先後
    assert await _lifecycle(client, _hdr(creator), pid) == "in_review"
    assert (await _approve(client, _hdr(a), pid)).status_code == 201
    assert await _lifecycle(client, _hdr(creator), pid) == "approved"


async def test_admin_only_mode(client, auth_headers, db_session) -> None:
    await _enable(db_session)
    _sec, sub, ip = await _setup(db_session)
    creator, writer = await _people(db_session, sub, 2)
    await _set_policy(client, auth_headers, approver_mode="admin")
    pid = await _submitted(client, _hdr(creator), ip.id)
    assert (await _approve(client, _hdr(writer), pid)).status_code == 403
    assert (await _approve(client, auth_headers, pid)).status_code == 201
    assert await _lifecycle(client, _hdr(creator), pid) == "approved"


async def test_resubmitting_starts_the_stages_over(client, auth_headers, db_session) -> None:
    """退回修改後重新送審：先前通過的關卡不算，要重新走一次。"""
    await _enable(db_session)
    _sec, sub, ip = await _setup(db_session)
    creator, first, second = await _people(db_session, sub, 3)
    await _set_policy(client, auth_headers, approver_mode="stages", stages=[
        {"name": "1", "user_ids": [str(first.id)], "group_ids": []},
        {"name": "2", "user_ids": [str(second.id)], "group_ids": []}])
    pid = await _submitted(client, _hdr(creator), ip.id)
    assert (await _approve(client, _hdr(first), pid)).status_code == 201
    r = await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(second),
                          json={"decision": "request_changes", "rationale": "redo", "dispositions": {}})
    assert r.status_code == 201 and await _lifecycle(client, _hdr(creator), pid) == "draft"
    r = await client.post(f"/api/v1/change-plans/{pid}/transitions", headers=_hdr(creator), json={"action": "submit"})
    assert r.status_code == 200
    view = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(second))).json()
    assert view["can_review"] is False and view["review_steps"][0]["approved"] is False


async def test_awaiting_my_review_follows_the_current_stage(client, auth_headers, db_session) -> None:
    await _enable(db_session)
    _sec, sub, ip = await _setup(db_session)
    creator, first, second = await _people(db_session, sub, 3)
    await _set_policy(client, auth_headers, approver_mode="stages", stages=[
        {"name": "1", "user_ids": [str(first.id)], "group_ids": []},
        {"name": "2", "user_ids": [str(second.id)], "group_ids": []}])
    pid = await _submitted(client, _hdr(creator), ip.id)

    async def todo(u):  # type: ignore[no-untyped-def]
        return [p["id"] for p in (await client.get("/api/v1/change-plans", headers=_hdr(u),
                                                   params={"awaiting_my_review": "true"})).json()["items"]]
    assert await todo(first) == [pid] and await todo(second) == []
    await _approve(client, _hdr(first), pid)
    assert await todo(first) == [] and await todo(second) == [pid]


async def test_ai_tool_shows_the_review_progress(client, auth_headers, db_session, admin_user) -> None:
    from tests.test_change_impact_mcp import _call
    await _enable(db_session)
    _sec, sub, ip = await _setup(db_session)
    creator, first = await _people(db_session, sub, 2)
    await _set_policy(client, auth_headers, approver_mode="stages", stages=[
        {"name": "網路組", "user_ids": [str(first.id)], "group_ids": []},
        {"name": "主管", "user_ids": [str(admin_user.id)], "group_ids": []}])
    pid = await _submitted(client, _hdr(creator), ip.id)
    await _approve(client, _hdr(first), pid)
    _r, body = await _call(admin_user, "impact_list_plans", {"lifecycle": "in_review"})
    plan = next(p for p in body["plans"] if p["id"] == pid)
    assert plan["review"]["mode"] == "stages"
    assert [(s["name"], s["approved"], s["current"]) for s in plan["review"]["stages"]] == [
        ("網路組", True, False), ("主管", False, True)]
