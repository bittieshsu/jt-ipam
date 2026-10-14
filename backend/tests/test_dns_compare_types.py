"""DNS 比對群組可以選比對哪些紀錄類型（使用者 2026-10-14：「加入一個比對類型的複選，預設 A AAAA PTR，其它有 CNAME MX TXT…」）。

起因：兩台 UCS 的別名（CNAME）其實不一樣也看不出來。拉取時刻意只存 A／AAAA／PTR（其他類型對 IPAM 沒有對應），
所以比對群組勾了別的類型，同步要把那些類型也存下來 —— 只限加入了比對群組、而且群組有勾的伺服器；
沒分組的伺服器照舊只存三種，DNS 與 IPAM 的一致性統計也只看這三種。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.dns import DNSRecord, DNSServer, DNSZone
from app.models.dns_compare_group import DNSCompareGroup
from app.services import dns_compare, dns_sync
from app.services.dns.base import DNSRecordOp, DNSZoneInfo

pytestmark = pytest.mark.anyio
API = "/api/v1/dns"


class _FakeAdapter:
    def __init__(self, records):  # type: ignore[no-untyped-def]
        self._records = records

    async def list_zones(self):  # type: ignore[no-untyped-def]
        return [DNSZoneInfo(name="corp.example", kind="forward")]

    async def list_records(self, _zone):  # type: ignore[no-untyped-def]
        return list(self._records)

    async def close(self) -> None:
        return None


RECORDS = [
    DNSRecordOp(name="www.corp.example", type="A", value="192.0.2.10"),
    DNSRecordOp(name="book.corp.example", type="CNAME", value="kb1.corp.example."),
    DNSRecordOp(name="corp.example", type="MX", value="10 MG1.corp.example."),
    DNSRecordOp(name="corp.example", type="TXT", value='"v=spf1 -all"'),
    DNSRecordOp(name="corp.example", type="SOA", value="ns1 hostmaster 1 2 3 4 5"),
]


async def _types(db, server) -> set[str]:  # type: ignore[no-untyped-def]
    from sqlalchemy import select
    rows = (await db.execute(select(DNSRecord.type).join(DNSZone, DNSZone.id == DNSRecord.zone_id)
                             .where(DNSZone.server_id == server.id))).scalars().all()
    return set(rows)


def _patch(monkeypatch, records):  # type: ignore[no-untyped-def]
    async def fake(_s, _srv):  # type: ignore[no-untyped-def]
        return _FakeAdapter(records)
    monkeypatch.setattr(dns_sync, "get_adapter", fake)


async def test_new_group_defaults_to_a_aaaa_ptr(client, auth_headers, db_session) -> None:
    r = await client.post(f"{API}/compare-groups", json={"name": f"g-{uuid.uuid4().hex[:6]}"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["compare_types"] == ["A", "AAAA", "PTR"]
    gid = r.json()["id"]
    r = await client.patch(f"{API}/compare-groups/{gid}", json={"compare_types": ["cname", "A", "MX", "A"]},
                           headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["compare_types"] == ["A", "CNAME", "MX"], "大寫、去重複、固定順序"
    for bad in ([], ["SOA"], ["BOGUS"]):
        r = await client.patch(f"{API}/compare-groups/{gid}", json={"compare_types": bad}, headers=auth_headers)
        assert r.status_code == 422, bad


async def test_sync_keeps_the_types_the_group_compares(db_session, monkeypatch) -> None:
    g = DNSCompareGroup(name=f"g-{uuid.uuid4().hex[:6]}", compare_types=["A", "AAAA", "PTR", "CNAME", "MX"])
    db_session.add(g)
    await db_session.flush()
    grouped = DNSServer(name=f"ucs-a-{uuid.uuid4().hex[:4]}", type="univention_ucs", compare_group_id=g.id)
    alone = DNSServer(name=f"ucs-b-{uuid.uuid4().hex[:4]}", type="univention_ucs")
    db_session.add_all([grouped, alone])
    await db_session.flush()
    _patch(monkeypatch, RECORDS)
    await dns_sync.pull_server(db_session, grouped)
    await dns_sync.pull_server(db_session, alone)
    assert await _types(db_session, grouped) == {"A", "CNAME", "MX"}, "群組勾的才存；TXT 沒勾、SOA 永遠不存"
    assert await _types(db_session, alone) == {"A"}, "沒分組照舊只存 A／AAAA／PTR"

    # 群組拿掉 CNAME：下一次拉取把已存的 CNAME 清掉
    g.compare_types = ["A", "AAAA", "PTR"]
    await db_session.flush()
    await dns_sync.pull_server(db_session, grouped)
    assert await _types(db_session, grouped) == {"A"}


async def test_comparison_only_looks_at_the_chosen_types(db_session) -> None:
    g = DNSCompareGroup(name=f"g-{uuid.uuid4().hex[:6]}", grace_minutes=0, compare_types=["A", "AAAA", "PTR"])
    db_session.add(g)
    await db_session.flush()
    servers = []
    for n, cname in (("a", "kb1.corp.example"), ("b", "kb2.corp.example")):
        s = DNSServer(name=f"{n}-{uuid.uuid4().hex[:4]}", type="bind9", compare_group_id=g.id,
                      last_sync_at=datetime.now(UTC) - timedelta(minutes=1))
        db_session.add(s)
        await db_session.flush()
        z = DNSZone(server_id=s.id, name="corp.example", type="forward")
        db_session.add(z)
        await db_session.flush()
        for name, rtype, value in (("www", "A", "192.0.2.10"), ("book", "CNAME", cname)):
            db_session.add(DNSRecord(zone_id=z.id, name=name, type=rtype, value=value, source="from_dns_pulled",
                                     name_norm=dns_compare.normalize_name(name, "corp.example"),
                                     value_norm=dns_compare.normalize_value(rtype, value)))
        servers.append(s)
    await db_session.flush()
    await dns_compare.check_group(db_session, g)
    assert g.last_status == "ok", "CNAME 不同但沒勾 CNAME：一致"

    g.compare_types = ["A", "AAAA", "PTR", "CNAME"]
    await db_session.flush()
    await dns_compare.check_group(db_session, g)
    assert g.last_status in ("mismatch", "pending")
    from sqlalchemy import select

    from app.models.dns_compare_group import DNSCompareGroupDiff as DNSCompareDiff
    diffs = (await db_session.execute(select(DNSCompareDiff).where(DNSCompareDiff.group_id == g.id))).scalars().all()
    assert {d.type for d in diffs} == {"CNAME"}


def test_values_compare_the_same_across_products() -> None:
    nv = dns_compare.normalize_value
    assert nv("MX", "10 MG1.corp.example.") == nv("MX", "10  mg1.corp.example") == "10 mg1.corp.example"
    assert nv("SRV", "0 100 389 DC1.corp.example.") == "0 100 389 dc1.corp.example"
    assert nv("TXT", '"v=spf1 -all"') == nv("TXT", "v=spf1 -all") == "v=spf1 -all"
    assert nv("TXT", '"part one" "part two"') == "part onepart two", "多段 TXT 接起來（BIND 的寫法）"


async def test_consistency_report_ignores_types_ipam_does_not_track(client, auth_headers, db_session, monkeypatch) -> None:
    g = DNSCompareGroup(name=f"g-{uuid.uuid4().hex[:6]}", compare_types=["A", "AAAA", "PTR", "CNAME", "MX", "TXT"])
    db_session.add(g)
    await db_session.flush()
    s = DNSServer(name=f"c-{uuid.uuid4().hex[:4]}", type="bind9", compare_group_id=g.id)
    db_session.add(s)
    await db_session.flush()
    _patch(monkeypatch, RECORDS)
    await dns_sync.pull_server(db_session, s)
    await db_session.commit()
    r = await client.get(f"{API}/consistency/inconsistent", params={"limit": 2000}, headers=auth_headers)
    assert r.status_code == 200
    mine = [x for x in r.json() if x["server_name"] == s.name]
    assert {x["type"] for x in mine} == {"A"}, "CNAME／MX／TXT 不是 IPAM 對得上的東西，不算「只在 DNS」"
