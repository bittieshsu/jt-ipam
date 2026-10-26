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


async def test_reviewer_who_cannot_see_some_review_items_is_told_and_cannot_approve(client, db_session) -> None:
    """2026-10-25 實測：管理員建的計畫有只有管理員看得到的待覆核項目（Wazuh 綁定位址），指定的審核人看不到。
    以前伺服器只回「還有 2 項沒處置」，審核人看不到是哪兩項、永遠核准不了。現在：分析結果告訴他有看不到的項目
    （只說有、不說幾項，跟「看不到的發現不從數字透露」一致），核准時回明確的代碼；要求修改、退回照常可用。
    名單不會擴大可見範圍，所以不能替看不到的項目背書。"""
    from datetime import UTC, datetime
    from app.models.wazuh import WazuhAgent, WazuhInstance
    _sec, sub, ip = await _setup(db_session)
    admin, reviewer = await _user(db_session, admin=True), await _user(db_session)
    await _grant(db_session, reviewer, "subnet", sub.id, "read")
    wi = WazuhInstance(name=f"wz-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.9:55000", api_user="ro",
                       api_password_enc=b"x", api_password_nonce=b"x", enabled=True, last_sync_at=datetime.now(UTC))
    db_session.add(wi)
    await db_session.flush()
    db_session.add(WazuhAgent(instance_id=wi.id, agent_id="017", name="erp", ip=str(ip.ip).split("/")[0],
                              register_ip=str(ip.ip).split("/")[0], status="active", jt_ipam_address_id=ip.id))
    await db_session.commit()
    await _enable(db_session, reviewer_user_ids=[str(reviewer.id)])

    pid = await _run_and_submit(client, _hdr(admin), ip.id)
    plan = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(reviewer))).json()
    assert plan["can_review"] is True
    run_id = plan["current_run_id"] if "current_run_id" in plan else (
        await client.get(f"/api/v1/change-plans/{pid}/runs", headers=_hdr(reviewer))).json()["items"][0]["id"]
    run = (await client.get(f"/api/v1/impact-runs/{run_id}", headers=_hdr(reviewer))).json()
    assert run["review_items_hidden"] is True, run
    mine = (await client.get(f"/api/v1/impact-runs/{run_id}/findings", headers=_hdr(reviewer),
                             params={"disposition": "review", "page_size": 200})).json()["items"]
    disp = {f["id"]: {"action": "will_update"} for f in mine}
    for decision in ("approve", "accept_risk"):
        r = await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(reviewer),
                              json={"decision": decision, "rationale": "checked all items", "dispositions": disp})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "impact_review_items_hidden", r.text
    r = await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(reviewer),
                          json={"decision": "request_changes", "rationale": "need an admin to check", "dispositions": {}})
    assert r.status_code in (200, 201), r.text
    # 管理員看得到全部：沒有看不到的項目
    run = (await client.get(f"/api/v1/impact-runs/{run_id}", headers=_hdr(admin))).json()
    assert run["review_items_hidden"] is False


async def test_returned_plan_tells_the_creator_why(client, db_session) -> None:
    """2026-10-25 實測：退回修改後，申請人的通知只說「退回了」、計畫頁也看不到理由（要翻到歷史頁籤）。
    通知內文要帶理由；覆核紀錄要帶審核人名稱，計畫頁才能在最上方顯示「誰退回、為什麼」。"""
    _sec, sub, ip = await _setup(db_session)
    creator, reviewer = await _user(db_session), await _user(db_session)
    await _grant(db_session, creator, "subnet", sub.id, "write")
    await _grant(db_session, reviewer, "subnet", sub.id, "read")
    await _enable(db_session, reviewer_user_ids=[str(reviewer.id)])
    pid = await _run_and_submit(client, _hdr(creator), ip.id)
    r = await client.post(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(reviewer),
                          json={"decision": "request_changes", "rationale": "請改用 .122，.121 已有人在用", "dispositions": {}})
    assert r.status_code in (200, 201), r.text
    n = (await _notified(db_session, creator.id, pid))[-1]
    assert "請改用 .122" in (n.body or ""), n.body
    assert (n.params or {}).get("reason") == "請改用 .122，.121 已有人在用", n.params
    rv = (await client.get(f"/api/v1/change-plans/{pid}/reviews", headers=_hdr(creator))).json()["items"][0]
    assert rv["reviewer_name"] == reviewer.display_name


async def test_deleted_plan_clears_its_notifications_and_cannot_be_reviewed(client, db_session) -> None:
    """2026-10-25 實測：送審中的計畫刪除（封存）後，審核人的通知還在，點進去看得到「覆核」按鈕，按了才說計畫已關閉。"""
    _sec, sub, ip = await _setup(db_session)
    creator, reviewer = await _user(db_session), await _user(db_session)
    await _grant(db_session, creator, "subnet", sub.id, "write")
    await _grant(db_session, reviewer, "subnet", sub.id, "read")
    await _enable(db_session, reviewer_user_ids=[str(reviewer.id)])
    pid = await _run_and_submit(client, _hdr(creator), ip.id)
    assert len(await _notified(db_session, reviewer.id, pid)) == 1
    r = await client.delete(f"/api/v1/change-plans/{pid}", headers=_hdr(creator))
    assert r.status_code == 200, r.text
    assert await _notified(db_session, reviewer.id, pid) == []
    plan = (await client.get(f"/api/v1/change-plans/{pid}", headers=_hdr(reviewer))).json()
    assert plan["archived_at"] and plan["can_review"] is False
