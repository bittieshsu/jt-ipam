"""停用的 DNS 項目不採用（使用者 2026-10-13：「停用的不要抓」）。

- AdGuard Home：新版的 DNS rewrite 有 `enabled`，明確是 false 的不拿來命名 IP（舊版沒有這個欄位 → 照舊）
- Windows DNS：暫停（IsPaused）或關閉（IsShutdown）的 zone 不拉紀錄
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.models.address import IPAddress
from app.models.adguard import AdGuardInstance
from app.models.ip_hostname import IPHostnameObservation
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import adguard as adguard_svc
from app.services.dns.windows_dns import WindowsDNSAdapter


async def test_adguard_disabled_rewrite_is_not_a_name(db_session, monkeypatch) -> None:
    sec = Section(name=f"ag-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sn = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sn)
    await db_session.flush()
    a = IPAddress(subnet_id=sn.id, ip="198.51.100.1", state="used")
    b = IPAddress(subnet_id=sn.id, ip="198.51.100.2", state="used")
    c = IPAddress(subnet_id=sn.id, ip="198.51.100.3", state="used")
    db_session.add_all([a, b, c])
    inst = AdGuardInstance(name=f"ag-{uuid.uuid4().hex[:6]}", api_url="https://adguard.example",
                           api_user="admin", api_password_enc=b"x", api_password_nonce=b"x", enabled=True)
    db_session.add(inst)
    await db_session.flush()

    async def fake_api_get(_inst, path, *, timeout=15.0):
        return [{"domain": "on.example", "answer": "198.51.100.1", "enabled": True},
                {"domain": "off.example", "answer": "198.51.100.2", "enabled": False},
                {"domain": "old.example", "answer": "198.51.100.3"}]
    monkeypatch.setattr(adguard_svc, "_api_get", fake_api_get)
    await adguard_svc.sync_rewrites(db_session, inst)
    await db_session.flush()
    rows = dict((await db_session.execute(select(IPHostnameObservation.ip_id, IPHostnameObservation.hostname).where(
        IPHostnameObservation.source == "adguard"))).all())
    assert a.id in rows
    assert b.id not in rows, "停用的 rewrite 不命名 IP"
    assert c.id in rows, "舊版沒有 enabled 欄位 → 照舊"


async def test_windows_dns_paused_or_shutdown_zones_are_skipped(monkeypatch) -> None:
    a = WindowsDNSAdapter(host="192.0.2.53", username="u", password="p")
    zones = [{"ZoneName": "corp.example", "IsReverseLookupZone": False, "IsPaused": False, "IsShutdown": False},
             {"ZoneName": "paused.example", "IsReverseLookupZone": False, "IsPaused": True, "IsShutdown": False},
             {"ZoneName": "down.example", "IsReverseLookupZone": False, "IsPaused": False, "IsShutdown": True},
             {"ZoneName": "100.51.198.in-addr.arpa", "IsReverseLookupZone": True}]
    seen: list[str] = []

    def fake_ps(script: str) -> str:
        seen.append(script)
        return json.dumps(zones)
    monkeypatch.setattr(a, "_run_ps", fake_ps)
    names = [z.name for z in await a.list_zones()]
    assert names == ["corp.example", "100.51.198.in-addr.arpa"]
    assert "IsPaused" in seen[0] and "IsShutdown" in seen[0]
