"""「有 DHCP 租約」旗標改成逐來源目擊、衍生旗標全域重算（2026-09-26 稽核）。

以前 `in_dhcp_lease` 是六個 DHCP 來源共用的一個布林，清除各寫各的：
- 沒設「關聯子網路」→ 永遠不清（租約早就沒了，畫面仍顯示「DHCP」）
- Palo Alto 從來不清
- 有設範圍時用子網路清 → 別的 DHCP 來源還發著的租約也被清掉，下一輪又被設回來
改成比照固定分配（dhcp_reservations）：每個來源各記各的，旗標＝還有任何一個來源回報。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.address import IPAddress
from app.models.dhcp import DHCPLeaseSighting
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import dhcp_leases as dl
from sqlalchemy import select

A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


async def _ips(db, n: int = 1) -> list[IPAddress]:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    out = [IPAddress(subnet_id=sub.id, ip=f"198.51.100.{i + 1}") for i in range(n)]
    db.add_all(out)
    await db.flush()
    return out


async def _run(db, source_type, source_id, ips, *, complete=True):
    run = dl.LeaseRun(db, source_type=source_type, source_id=source_id)
    for ip in ips:
        run.saw(ip)
    out = await run.finish(complete=complete)
    for ip in ips:
        await db.refresh(ip)
    return out


async def _flag(db, ip) -> bool:
    await db.refresh(ip)
    return bool(ip.in_dhcp_lease)


async def test_a_lease_that_is_gone_clears_the_flag_without_any_scope(db_session) -> None:
    """沒設關聯子網路（最常見的設定）：以前永遠不清。"""
    a, b = await _ips(db_session, 2)
    await _run(db_session, "opnsense", A, [a, b])
    assert await _flag(db_session, a)
    await _run(db_session, "opnsense", A, [b])
    assert not await _flag(db_session, a)
    assert await _flag(db_session, b)


async def test_another_source_still_leasing_keeps_the_flag(db_session) -> None:
    """兩個 DHCP 來源都發過這個位址（例如搬遷期間）：A 沒了、B 還在 → 旗標留著。"""
    (ip,) = await _ips(db_session)
    await _run(db_session, "opnsense", A, [ip])
    await _run(db_session, "windows_dhcp", B, [ip])
    await _run(db_session, "opnsense", A, [])
    assert await _flag(db_session, ip)
    await _run(db_session, "windows_dhcp", B, [])
    assert not await _flag(db_session, ip)


async def test_an_incomplete_read_clears_nothing(db_session) -> None:
    (ip,) = await _ips(db_session)
    await _run(db_session, "fortigate", A, [ip])
    await _run(db_session, "fortigate", A, [], complete=False)
    assert await _flag(db_session, ip)


async def test_an_empty_read_does_not_wipe_a_source(db_session) -> None:
    """完整讀取卻回 0 筆、之前卻有一堆 → 多半是權限或 API 問題，不清並講出來。"""
    ips = await _ips(db_session, 6)
    await _run(db_session, "mikrotik", A, ips)
    out = await _run(db_session, "mikrotik", A, [])
    assert out["breaker"]
    assert await _flag(db_session, ips[0])


async def test_legacy_flags_are_claimed_then_follow_the_rules(db_session) -> None:
    """升級前的旗標不知道是哪個來源設的：真實同步看到就認領，之後照一般規則處理。"""
    a, b = await _ips(db_session, 2)
    for ip in (a, b):
        ip.in_dhcp_lease = True
        db_session.add(DHCPLeaseSighting(ip_address_id=ip.id, source_type=dl.LEGACY,
                                         source_id=dl.LEGACY_ID, last_seen_at=datetime.now(UTC)))
    await db_session.flush()
    await _run(db_session, "opnsense", A, [a])
    rows = (await db_session.execute(select(DHCPLeaseSighting.source_type).where(
        DHCPLeaseSighting.ip_address_id == a.id))).scalars().all()
    assert rows == ["opnsense"]
    # b 沒被認領：寬限期內不清（可能是另一個還沒跑的來源的）
    assert await _flag(db_session, b)


async def test_unclaimed_legacy_flags_go_after_the_grace_period(db_session) -> None:
    (ip,) = await _ips(db_session)
    ip.in_dhcp_lease = True
    db_session.add(DHCPLeaseSighting(
        ip_address_id=ip.id, source_type=dl.LEGACY, source_id=dl.LEGACY_ID,
        last_seen_at=datetime.now(UTC) - dl.LEGACY_GRACE - timedelta(minutes=1)))
    await db_session.flush()
    (other,) = await _ips(db_session)
    await _run(db_session, "pfsense", A, [other])
    assert not await _flag(db_session, ip)


async def test_a_source_that_stopped_syncing_ages_out(db_session) -> None:
    """來源停用、刪掉或一直同步失敗：它最後回報的租約不能永遠算數。"""
    (ip,) = await _ips(db_session)
    db_session.add(DHCPLeaseSighting(ip_address_id=ip.id, source_type="paloalto", source_id=B,
                                     last_seen_at=datetime.now(UTC) - dl.MAX_AGE - timedelta(hours=1)))
    ip.in_dhcp_lease = True
    await db_session.flush()
    (other,) = await _ips(db_session)
    await _run(db_session, "opnsense", A, [other], complete=False)
    assert not await _flag(db_session, ip)


async def test_forget_source_drops_its_leases(db_session) -> None:
    (ip,) = await _ips(db_session)
    await _run(db_session, "windows_dhcp", A, [ip])
    await dl.forget_source(db_session, source_type="windows_dhcp", source_id=A)
    assert not await _flag(db_session, ip)


# ── 各整合接對了 ──

async def test_palo_alto_clears_leases_that_are_gone(db_session, monkeypatch) -> None:
    """Palo Alto 以前從來不清。"""
    import xml.etree.ElementTree as ET

    from app.models.paloalto import PaloAltoFirewall
    from app.services import paloalto as pa

    a, b = await _ips(db_session, 2)
    fw = PaloAltoFirewall(name="pa-a", api_url="https://pa.example.com", api_key_enc=b"x",
                          api_key_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()

    def _leases(*ips):
        xml = "<result>" + "".join(f"<entry><ip>{i}</ip><mac>00:00:5e:00:53:0{n}</mac></entry>"
                                   for n, i in enumerate(ips)) + "</result>"
        return ET.fromstring(xml)

    async def _get(_fw, _params, **_kw):
        return state["xml"]
    monkeypatch.setattr(pa, "_xml_get", _get)
    state = {"xml": _leases("198.51.100.1", "198.51.100.2")}
    await pa.sync_dhcp_leases(db_session, fw)
    assert await _flag(db_session, a)
    state["xml"] = _leases("198.51.100.2")
    await pa.sync_dhcp_leases(db_session, fw)
    assert not await _flag(db_session, a)
    assert await _flag(db_session, b)


async def test_windows_dhcp_inactive_reservations_are_not_leases(db_session, monkeypatch) -> None:
    """Windows 的租約清單也列出沒人在用的保留位址（InactiveReservation）與過期的 —— 那不是租約。"""
    from app.models.windows_dhcp import WindowsDhcpServer
    from app.services import windows_dhcp as wd

    a, b, c = await _ips(db_session, 3)

    class _Fake:
        def get_scopes(self):
            return [{"ScopeId": "198.51.100.0"}]

        def get_leases(self, _sid):
            return [{"IPAddress": "198.51.100.1", "AddressState": "Active"},
                    {"IPAddress": "198.51.100.2", "AddressState": "InactiveReservation"},
                    {"IPAddress": "198.51.100.3", "AddressState": "Expired"}]
    inst = WindowsDhcpServer(name="dhcp-a", host="192.0.2.5", username="u",
                             password_enc=b"x", password_nonce=b"x")
    db_session.add(inst)
    await db_session.flush()
    monkeypatch.setattr(wd, "_client", lambda _i: _Fake())
    await wd.sync_leases(db_session, inst)
    assert await _flag(db_session, a)
    assert not await _flag(db_session, b)
    assert not await _flag(db_session, c)


async def test_windows_dhcp_inactive_scope_gives_no_leases_or_reservations(db_session, monkeypatch) -> None:
    """停用（Inactive）的 scope 不會發放：它的租約與保留都不算（使用者 2026-10-13：「停用的不要抓」）。
    以前只有集區跳過停用的 scope，租約與保留照收。"""
    from app.models.dhcp import DHCPReservation
    from app.models.windows_dhcp import WindowsDhcpServer
    from app.services import windows_dhcp as wd
    from sqlalchemy import select

    a, b = await _ips(db_session, 2)

    class _Fake:
        def get_scopes(self):
            return [{"ScopeId": "198.51.100.0", "State": "Active"},
                    {"ScopeId": "203.0.113.0", "State": "Inactive"}]

        def get_leases(self, sid):
            return ([{"IPAddress": "198.51.100.1", "AddressState": "Active"}] if sid == "198.51.100.0"
                    else [{"IPAddress": "198.51.100.2", "AddressState": "Active"}])

        def get_reservations(self, sid):
            return ([{"IPAddress": "198.51.100.1", "ClientId": "02-00-00-00-00-01", "Name": "a"}]
                    if sid == "198.51.100.0" else
                    [{"IPAddress": "203.0.113.9", "ClientId": "02-00-00-00-00-09", "Name": "z"}])
    inst = WindowsDhcpServer(name="dhcp-b", host="192.0.2.6", username="u",
                             password_enc=b"x", password_nonce=b"x")
    db_session.add(inst)
    await db_session.flush()
    monkeypatch.setattr(wd, "_client", lambda _i: _Fake())
    await wd.sync_leases(db_session, inst)
    await wd.sync_reservations(db_session, inst)
    await db_session.flush()
    assert await _flag(db_session, a)
    assert not await _flag(db_session, b), "停用 scope 的租約不算"
    ips = set((await db_session.execute(select(DHCPReservation.ip).where(
        DHCPReservation.source_id == inst.id))).scalars().all())
    assert ips == {"198.51.100.1"}, "停用 scope 的保留不算"


def test_lease_state_helper_only_drops_explicitly_inactive_rows() -> None:
    """防火牆租約：來源明確寫著過期／釋放／拒絕的才不算（使用者 2026-10-13：「停用的不要抓」）。
    沒有狀態欄位、或值不認得 → 照舊收（欄位沒在每一家實機驗證過，寧可漏擋也不要把有效租約丟掉）。"""
    from app.services.fw_sightings import lease_inactive
    assert lease_inactive({"state": "expired"})
    assert lease_inactive({"binding_state": "free"})
    assert lease_inactive({"binding-state": "abandoned"})
    assert lease_inactive({"active_status": "expired"})
    assert lease_inactive({"status": "Released"})
    assert lease_inactive({"state": "2"}, kea=True), "Kea：2＝過期回收"
    assert lease_inactive({"state": 1}, kea=True), "Kea：1＝拒絕"
    assert not lease_inactive({"state": 0}, kea=True)
    assert not lease_inactive({"state": "2"}), "不是 Kea 的數字狀態不猜"
    assert not lease_inactive({})
    assert not lease_inactive({"state": "active"})
    assert not lease_inactive({"status": "leased"})
    assert not lease_inactive({"state": "committed"})


async def test_palo_alto_expired_lease_is_not_a_lease(db_session, monkeypatch) -> None:
    import xml.etree.ElementTree as ET

    from app.models.paloalto import PaloAltoFirewall
    from app.services import paloalto as pa

    a, b = await _ips(db_session, 2)
    fw = PaloAltoFirewall(name="pa-x", api_url="https://pa.example.com", api_key_enc=b"x", api_key_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()
    xml = ("<result><entry><ip>198.51.100.1</ip><mac>02:00:00:00:00:01</mac><state>committed</state></entry>"
           "<entry><ip>198.51.100.2</ip><mac>02:00:00:00:00:02</mac><state>expired</state></entry></result>")

    async def _get(_fw, _params, **_kw):
        return ET.fromstring(xml)
    monkeypatch.setattr(pa, "_xml_get", _get)
    await pa.sync_dhcp_leases(db_session, fw)
    assert await _flag(db_session, a)
    assert not await _flag(db_session, b)


async def test_opnsense_expired_and_reclaimed_leases_are_not_leases(db_session, monkeypatch) -> None:
    from app.models.firewall import OPNsenseFirewall
    from app.services import opnsense_firewall as op

    a, b, c = await _ips(db_session, 3)
    fw = OPNsenseFirewall(name=f"fw-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.254",
                          api_key_enc=b"x", api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()
    data = {
        "/api/kea/leases4/search": {"rows": [
            {"address": "198.51.100.1", "hwaddr": "02:00:00:00:00:01", "state": "0"},
            {"address": "198.51.100.2", "hwaddr": "02:00:00:00:00:02", "state": "2"}]},
        "/api/dhcpv4/leases/searchLease": {"rows": [
            {"address": "198.51.100.3", "mac": "02:00:00:00:00:03", "state": "expired"}]},
    }

    async def _get(_fw, path, **_kw):
        if path in data:
            return data[path]
        raise op.OPNsenseError(f"{path}: 404")
    monkeypatch.setattr(op, "_api_get", _get)
    await op.sync_dhcp_leases(db_session, fw)
    assert await _flag(db_session, a)
    assert not await _flag(db_session, b), "Kea 過期回收的不是租約"
    assert not await _flag(db_session, c), "ISC 過期的不是租約"
