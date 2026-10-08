"""IP 變更評估的審核人名單與送審通知（使用者 2026-10-07：「送審給誰審核？在哪裡設定誰審？」）。

- 系統設定可以指定審核的使用者或群組；有名單時只有名單裡（而且看得到目標）的人能覆核，管理員例外
- 送審時通知名單裡的人（不含送審者本人）；沒有名單時維持原規則（對目標有修改權的人可覆核），通知管理員
- 覆核完成通知建立者
"""
from __future__ import annotations

import uuid

from app.models.notification import Notification
from app.models.user import Group, UserGroupMember
from sqlalchemy import select

import pytest
from app.services.change_impact import ai as impact_ai
from app.services.change_impact import jobs

from tests.test_change_impact_api import _enable, _grant, _hdr, _plan, _setup, _user


@pytest.fixture(autouse=True)
def _inline(monkeypatch):  # type: ignore[no-untyped-def]
    # 背景分析會另開連線，跟測試結束時的清表互卡（死結）：直接在請求裡跑完
    monkeypatch.setattr(jobs, "SPAWN_IN_BACKGROUND", False)
    monkeypatch.setattr(impact_ai, "SPAWN_IN_BACKGROUND", False)


async def _run_and_submit(client, headers, ip_id) -> str:  # type: ignore[no-untyped-def]
    pid = (await _plan(client, headers, ip_id)).json()["id"]
    r = await client.post(f"/api/v1/change-plans/{pid}/runs", headers=headers)
    assert r.status_code in (200, 201, 202), r.text
    r = await client.post(f"/api/v1/change-plans/{pid}/transitions", headers=headers, json={"action": "submit"})
    assert r.status_code == 200, r.text
    return pid


async def _notified(db, user_id, plan_id) -> list[Notification]:  # type: ignore[no-untyped-def]
    return list((await db.execute(select(Notification).where(
        Notification.user_id == user_id, Notification.object_id == uuid.UUID(plan_id)))).scalars().all())


async def test_reviewer_list_decides_who_reviews_and_who_is_told(client, db_session) -> None:
    _sec, sub, ip = await _setup(db_session)
    creator, writer, reviewer = await _user(db_session), await _user(db_session), await _user(db_session)
    await _grant(db_session, creator, "subnet", sub.id, "write")
    await _grant(db_session, writer, "subnet", sub.id, "write")
    await _grant(db_session, reviewer, "subnet", sub.id, "read")
    g = Group(name=f"net-review-{uuid.uuid4().hex[:6]}")
    db_session.add(g)
    await db_session.flush()
    db_session.add(UserGroupMember(user_id=reviewer.id, group_id=g.id))
    await db_session.commit()
    await _enable(db_session, reviewer_group_ids=[str(g.id)])

    pid = await _run_and_submit(client, _hdr(creator), ip.id)
    assert len(await _notified(db_session, reviewer.id, pid)) == 1
    assert not await _notified(db_session, writer.id, pid)
    assert not await _notified(db_session, creator.id, pid)

    mine = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(reviewer))).json()
    assert mine["can_review"] is True
    assert any(r["id"] == str(reviewer.id) for r in mine["reviewers"]), mine["reviewers"]
    other = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(writer))).json()
    assert other["can_review"] is False

    todo = (await client.get("/api/v1/change-plans", params={"awaiting_my_review": "true"},
                             headers=_hdr(reviewer))).json()["items"]
    assert [p["id"] for p in todo] == [pid]
    todo = (await client.get("/api/v1/change-plans", params={"awaiting_my_review": "true"},
                             headers=_hdr(writer))).json()["items"]
    assert todo == []

    body = {"decision": "request_changes", "rationale": "please fix", "dispositions": {}}
    r = await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(writer), json=body)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "impact_not_reviewer", r.text
    r = await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(reviewer), json=body)
    assert r.status_code in (200, 201), r.text
    assert len(await _notified(db_session, creator.id, pid)) == 1   # 覆核結果通知建立者


async def test_without_a_list_writers_review_and_admins_are_told(client, db_session, admin_user) -> None:
    _sec, sub, ip = await _setup(db_session)
    creator, writer, reader = await _user(db_session), await _user(db_session), await _user(db_session)
    await _grant(db_session, creator, "subnet", sub.id, "write")
    await _grant(db_session, writer, "subnet", sub.id, "write")
    await _grant(db_session, reader, "subnet", sub.id, "read")
    await _enable(db_session)
    pid = await _run_and_submit(client, _hdr(creator), ip.id)
    assert len(await _notified(db_session, admin_user.id, pid)) == 1
    assert (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(writer))).json()["can_review"] is True
    assert (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(reader))).json()["can_review"] is False
    assert (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(creator))).json()["can_review"] is False


async def test_reviewer_settings_keep_only_existing_ids(client, auth_headers, db_session) -> None:
    g = Group(name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(g)
    await db_session.commit()
    r = await client.put("/api/v1/change-impact/settings", headers=auth_headers,
                         json={"enabled": True, "reviewer_group_ids": [str(g.id), str(uuid.uuid4()), "nope"],
                               "reviewer_user_ids": [str(uuid.uuid4())]})
    assert r.status_code == 200, r.text
    cfg = (await client.get("/api/v1/change-impact/settings", headers=auth_headers)).json()["config"]
    assert cfg["reviewer_group_ids"] == [str(g.id)] and cfg["reviewer_user_ids"] == []
