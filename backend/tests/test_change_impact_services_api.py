"""IP 變更評估 M2 的服務 API 與維護／停機建案。"""
from __future__ import annotations

import uuid

import pytest
from app.models.audit import AuditLog
from app.models.device import Device
from app.models.permission import Permission
from app.services.change_impact import ai as impact_ai
from app.services.change_impact import jobs
from sqlalchemy import select

from tests.test_change_impact_api import _enable, _hdr, _user

URL = "/api/v1/change-impact/services"


@pytest.fixture(autouse=True)
def _inline(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jobs, "SPAWN_IN_BACKGROUND", False)
    monkeypatch.setattr(impact_ai, "SPAWN_IN_BACKGROUND", False)


async def _devices(db, *names: str) -> list[Device]:  # type: ignore[no-untyped-def]
    out = []
    for n in names:
        d = Device(name=n, type="server")
        db.add(d)
        out.append(d)
    await db.commit()
    return out


def _body(name: str, members: list[dict], required: int = 1, **kw) -> dict:  # type: ignore[no-untyped-def]
    return {"name": name, "criticality": "high", "groups": [{"name": "app", "required_count": required,
                                                             "confirmed": True, "members": members}], **kw}


async def test_service_crud_and_validation(client, auth_headers, db_session) -> None:
    await _enable(db_session)
    a, b = await _devices(db_session, "app-01", "app-02")
    m = [{"object_type": "device", "object_id": str(a.id), "relation_type": "hosted_on"},
         {"object_type": "device", "object_id": str(b.id), "relation_type": "hosted_on"}]
    r = await client.post(URL, headers=auth_headers, json=_body("ERP", m, required=3))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "impact_group_required_exceeds_members"
    r = await client.post(URL, headers=auth_headers, json=_body("ERP", m + [
        {"object_type": "device", "object_id": str(uuid.uuid4()), "relation_type": "hosted_on"}], required=1))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "impact_service_object_missing"
    r = await client.post(URL, headers=auth_headers, json=_body("ERP", m, required=1))
    assert r.status_code == 201, r.text
    erp = r.json()
    assert [x["label"] for x in erp["groups"][0]["members"]] == ["app-01", "app-02"]
    r = await client.post(URL, headers=auth_headers, json=_body("erp", m))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "impact_service_name_taken"
    # 另一個服務依賴 ERP；改 ERP 時版本對不上 → 409；刪 ERP 會一併拿掉指向它的成員
    r = await client.post(URL, headers=auth_headers, json=_body("Portal", [
        {"object_type": "service", "object_id": erp["id"], "relation_type": "requires_service"}]))
    portal = r.json()
    r = await client.put(f"{URL}/{erp['id']}", headers=auth_headers,
                         json=_body("ERP", m, expected_version=erp["version"] + 5))
    assert r.status_code == 409
    r = await client.put(f"{URL}/{erp['id']}", headers=auth_headers,
                         json=_body("ERP", [{"object_type": "service", "object_id": erp["id"],
                                             "relation_type": "requires_service"}]))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "impact_service_self_dependency"
    assert (await client.delete(f"{URL}/{erp['id']}", headers=auth_headers)).status_code == 204
    got = (await client.get(f"{URL}/{portal['id']}", headers=auth_headers)).json()
    assert got["groups"][0]["members"] == []
    actions = {x.action for x in (await db_session.execute(select(AuditLog).where(
        AuditLog.object_type == "impact_service"))).scalars()}
    assert {"impact_service_create", "impact_service_delete"} <= actions


async def test_services_need_global_read_and_admin_to_edit(client, db_session) -> None:
    await _enable(db_session)
    u = await _user(db_session)
    r = await client.get(URL, headers=_hdr(u))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "impact_services_global_read"
    db_session.add(Permission(object_type="subnet", object_id=None, principal_type="user", principal_id=u.id,
                              level="read"))
    await db_session.commit()
    assert (await client.get(URL, headers=_hdr(u))).status_code == 200
    assert (await client.post(URL, headers=_hdr(u), json=_body("X", []))).status_code == 403


async def test_switch_maintenance_plan_through_the_api(client, auth_headers, db_session) -> None:
    await _enable(db_session)
    sw, sw2 = await _devices(db_session, "sw-core-01", "sw-core-02")
    r = await client.post("/api/v1/change-plans", headers=auth_headers, json={
        "title": "core maintenance", "scenario_type": "switch_maintenance", "target_type": "device",
        "target_id": str(sw.id), "parameters": {"also_down": [str(sw2.id)]}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["parameters"]["also_down"] == [str(sw2.id)]
    run = (await client.post(f"/api/v1/change-plans/{pid}/runs", headers=auth_headers)).json()
    got = (await client.get(f"/api/v1/impact-runs/{run['id']}", headers=auth_headers)).json()
    assert got["job_status"] in ("completed", "partial"), got
    tasks = (await client.get(f"/api/v1/change-plans/{pid}/tasks", headers=auth_headers)).json()["items"]
    assert {"confirm_owner_window", "perform_maintenance", "rollback_maintenance"} <= {t["template_code"] for t in tasks}
    # 一起停機的裝置也要有修改權限
    u = await _user(db_session)
    db_session.add(Permission(object_type="device", object_id=sw.id, principal_type="user", principal_id=u.id,
                              level="write"))
    await db_session.commit()
    r = await client.post("/api/v1/change-plans", headers=_hdr(u), json={
        "title": "x", "scenario_type": "switch_maintenance", "target_type": "device",
        "target_id": str(sw.id), "parameters": {"also_down": [str(sw2.id)]}})
    assert r.status_code in (403, 404), r.text
    r = await client.post("/api/v1/change-plans", headers=auth_headers, json={
        "title": "x", "scenario_type": "node_downtime", "target_type": "device",
        "target_id": str(sw.id), "parameters": {"mode": "explode"}})
    assert r.status_code == 422
