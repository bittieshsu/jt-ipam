"""外部 DNS 解析：一輪查詢、變動通知、設備自身位址、端點（docs/SPEC_EXTERNAL_DNS_zh-TW.md）。"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.models.external_dns import ExternalDnsChange, ExternalDnsResult, IntegrationOwnAddress
from app.services import external_dns as ed

pytestmark = pytest.mark.anyio

SETTINGS = {"resolvers": [{"name": "cf", "address": "192.0.2.53"}], "public_domains": ["example.com"],
            "query_authoritative": False}


@pytest.fixture
def world(monkeypatch):
    """假的對外開放清單與假的解析器：answers[fqdn] 決定這一輪答案。"""
    state: dict[str, Any] = {"answers": {"www.example.com": ["203.0.113.10"]}, "asked": [], "notified": []}

    async def fqdns(session):
        return {"www.example.com": {"fw-a"}, "intra.corp.lan": {"fw-a"}}

    async def resolve(name, settings):
        state["asked"].append(name)
        ips = state["answers"].get(name, [])
        return [ed.Answer("cf", "NOERROR" if ips else "NXDOMAIN", a=list(ips))]

    async def notify(session, **kw):
        state["notified"].append(kw)
        return 1

    monkeypatch.setattr(ed, "surface_fqdns", fqdns)
    monkeypatch.setattr(ed, "resolve_name", resolve)
    monkeypatch.setattr("app.services.notification.notify_admins_event", notify)
    return state


async def test_round_queries_only_public_names_and_reports_changes(db_session, admin_user, world) -> None:
    await ed.save_settings(db_session, SETTINGS, user_id=admin_user.id)
    await db_session.commit()

    s1 = await ed.run_round(db_session)
    await db_session.commit()
    assert world["asked"] == ["www.example.com"]            # 內部名稱不送出去
    assert s1["names"] == 1
    assert s1["changed"] == 0                               # 第一次查到不算變動
    assert not world["notified"]

    world["answers"]["www.example.com"] = ["198.51.100.99"]
    s2 = await ed.run_round(db_session)
    await db_session.commit()
    assert s2["changed"] == 1
    ch = (await db_session.execute(select(ExternalDnsChange))).scalars().all()
    assert [(c.old, c.new) for c in ch] == [(["203.0.113.10"], ["198.51.100.99"])]
    assert world["notified"][0]["event"] == "external_dns.changed"
    assert world["notified"][0]["link"] == "/attack-surface?tab=fqdn"
    rows = (await db_session.execute(select(ExternalDnsResult))).scalars().all()
    assert [(r.fqdn, r.a) for r in rows] == [("www.example.com", ["198.51.100.99"])]


async def test_unreachable_round_is_not_a_change(db_session, admin_user, world, monkeypatch) -> None:
    await ed.save_settings(db_session, SETTINGS, user_id=admin_user.id)
    await db_session.commit()
    await ed.run_round(db_session)

    async def down(name, settings):
        return [ed.Answer("cf", "TIMEOUT", error="timeout")]
    monkeypatch.setattr(ed, "resolve_name", down)
    s = await ed.run_round(db_session)
    assert s["changed"] == 0
    assert s["failed"] == 1


async def test_not_configured_does_nothing(db_session, world) -> None:
    assert (await ed.run_round(db_session)) == {"skipped": "not_configured"}
    assert world["asked"] == []


async def test_view_uses_own_addresses_and_classifies(db_session, admin_user, world) -> None:
    await ed.save_settings(db_session, SETTINGS, user_id=admin_user.id)
    db_session.add(IntegrationOwnAddress(source_type="opnsense", source_id=uuid.uuid4(), instance_name="fw-a",
                                         address="203.0.113.10"))
    await db_session.commit()
    await ed.run_round(db_session)
    v = await ed.view(db_session)
    assert v["configured"] is True
    www = v["names"]["www.example.com"]
    assert www["status"] == "consistent"
    assert www["owners"] == {"203.0.113.10": "fw-a（opnsense）"}
    assert v["names"]["intra.corp.lan"]["status"] == "outside_domains"


async def test_own_addresses_follow_each_complete_read(db_session) -> None:
    from app.services.iface_evidence import record_own
    from app.services.integration_cleanup import forget_instance
    fw = uuid.uuid4()
    await record_own(db_session, "opnsense", fw, "fw-a", ["203.0.113.10/29", "198.51.100.1"])
    await record_own(db_session, "opnsense", fw, "fw-a", ["203.0.113.10"])     # 198.51.100.1 這輪不見了
    got = (await db_session.execute(select(func.host(IntegrationOwnAddress.address)))).scalars().all()
    assert got == ["203.0.113.10"]
    await forget_instance(db_session, source="opnsense", source_id=fw)
    assert not (await db_session.execute(select(IntegrationOwnAddress))).scalars().all()


async def test_settings_endpoint_refuses_bad_entries_and_audits(client, auth_headers, db_session) -> None:
    body = {"resolvers": [{"name": "cf", "address": "1.1.1.1"}], "public_domains": ["example.com"],
            "public_ranges": ["203.0.113.0/24"], "interval_hours": 6}
    r = await client.put("/api/v1/system/dns-resolvers", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["public_domains"] == ["example.com"]
    bad = {**body, "resolvers": [{"address": "not-an-ip"}]}
    r = await client.put("/api/v1/system/dns-resolvers", json=bad, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ext_dns_invalid_entries"
    from app.models.audit import AuditLog
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "update"))).scalars().all()
    assert any((x.diff or {}).get("target") == "dns_resolvers" for x in rows)


async def test_run_endpoint(client, auth_headers, db_session, admin_user, monkeypatch) -> None:
    r = await client.post("/api/v1/anomalies/attack-surface/external-dns/run", headers=auth_headers)
    assert r.status_code == 422                                   # 還沒設定
    await ed.save_settings(db_session, SETTINGS, user_id=admin_user.id)
    await db_session.commit()

    async def fake_round(session):
        return {"names": 0}
    monkeypatch.setattr(ed, "run_round", fake_round)
    r = await client.post("/api/v1/anomalies/attack-surface/external-dns/run", headers=auth_headers)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    # 背景作業跑完才結束這個測試：留著跑的話，下一個測試清表（TRUNCATE）會跟它死結
    import asyncio
    for _ in range(100):
        st = (await client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)).json()["status"]
        if st not in ("pending", "running"):
            break
        await asyncio.sleep(0.05)
    assert st == "succeeded"


async def test_view_endpoint_needs_global_read(client, auth_headers) -> None:
    r = await client.get("/api/v1/anomalies/attack-surface/external-dns", headers=auth_headers)
    assert r.status_code == 200
    assert "names" in r.json()


def test_notify_event_is_registered() -> None:
    from app.services.system_config import NOTIFY_EVENTS
    assert any(e[0] == "external_dns.changed" for e in NOTIFY_EVENTS)


async def test_ai_tool_carries_external_dns(db_session, admin_user, monkeypatch) -> None:
    """AI 工具少一個欄位，模型就會說「系統沒有這個資料」（project_ai_tool_fields_and_ui_map）。"""
    from app.mcp.tools import TOOLS
    item = {"via": "nat", "source": "opnsense", "firewall": "fw-a", "name": "web", "protocol": "tcp",
            "port": 443, "descr": "", "identity": {"registered": True, "ip": "198.51.100.7",
                                                   "fqdns": ["www.example.com"]}}

    async def surface(session, limit=None, nat_limit=None):
        return [item]

    async def view(session):
        return {"names": {"www.example.com": {"status": "consistent", "addresses": ["203.0.113.10"],
                                              "owners": {"203.0.113.10": "fw-a（opnsense）"},
                                              "checked_at": None, "answers": []}}}
    monkeypatch.setattr("app.services.fw_lookup.attack_surface", surface)
    monkeypatch.setattr(ed, "view", view)
    fn = TOOLS["list_attack_surface"]["fn"]
    assert (await fn(db_session, user=admin_user))["external_dns"] is None      # 還沒設定
    await ed.save_settings(db_session, SETTINGS, user_id=admin_user.id)
    out = await fn(db_session, user=admin_user)
    assert out["external_dns"]["www.example.com"]["status"] == "consistent"
