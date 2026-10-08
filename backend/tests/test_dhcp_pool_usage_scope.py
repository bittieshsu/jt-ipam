"""DHCP 集區使用率只算集區所屬子網路的 IP（重疊網段不可以混算）。

以前「已用」是全站所有 IP 記錄裡落在範圍內的數量：兩個單位都用 198.51.100.0/24 時，
另一個單位的 IP 也算進來，集區「快滿了」的告警就是假的；而且每輪都把全站 IP 載進記憶體。
"""
from __future__ import annotations

import uuid
from typing import Any

from app.models.address import IPAddress
from app.models.dhcp import DHCPPoolRange
from app.models.ip_range import IPRange
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.dhcp_usage import pool_usage


async def _subnet(db, cidr: str = "198.51.100.0/24") -> Subnet:  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    s = Subnet(section_id=sec.id, cidr=cidr)
    db.add(s)
    await db.flush()
    return s


async def _ips(db, sub: Subnet, last_octets: range) -> None:  # type: ignore[no-untyped-def]
    for i in last_octets:
        db.add(IPAddress(subnet_id=sub.id, ip=f"198.51.100.{i}", state="active"))
    await db.flush()


def _by(pools: list[tuple[Any, int, int]], pid: Any) -> tuple[int, int]:
    return next((u, n) for p, u, n in pools if p.id == pid)


async def test_manual_pool_counts_only_its_own_subnet(db_session) -> None:
    a = await _subnet(db_session)
    b = await _subnet(db_session)            # 另一個單位、同一段位址
    await _ips(db_session, a, range(150, 152))
    await _ips(db_session, b, range(150, 160))
    r = IPRange(subnet_id=a.id, start_ip="198.51.100.150", end_ip="198.51.100.159", purpose="dhcp")
    db_session.add(r)
    await db_session.commit()
    assert _by(await pool_usage(db_session), r.id) == (2, 10)


async def test_integration_pool_uses_the_integration_scope(db_session) -> None:
    from app.models.firewall import OPNsenseFirewall
    a = await _subnet(db_session)
    b = await _subnet(db_session)
    await _ips(db_session, a, range(150, 153))
    await _ips(db_session, b, range(150, 160))
    fw = OPNsenseFirewall(name=f"fw-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.254", api_key_enc=b"x",
                          api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x", enabled=True,
                          scope_subnet_ids=[a.id])
    db_session.add(fw)
    await db_session.flush()
    p = DHCPPoolRange(source_type="opnsense", source_id=fw.id, source_name=fw.name, subnet_cidr="198.51.100.0/24",
                      start_ip="198.51.100.150", end_ip="198.51.100.159", source="isc")
    db_session.add(p)
    await db_session.commit()
    assert _by(await pool_usage(db_session), p.id) == (3, 10)


async def test_ambiguous_overlap_is_not_guessed(db_session) -> None:
    """兩個一模一樣的子網路、整合也沒設範圍：不知道是哪一個，就不拿別人的 IP 來算（不發假告警）。"""
    a = await _subnet(db_session)
    b = await _subnet(db_session)
    await _ips(db_session, a, range(150, 160))
    await _ips(db_session, b, range(150, 160))
    p = DHCPPoolRange(source_type="opnsense", source_id=uuid.uuid4(), subnet_cidr="198.51.100.0/24",
                      start_ip="198.51.100.150", end_ip="198.51.100.159", source="isc")
    db_session.add(p)
    await db_session.commit()
    assert _by(await pool_usage(db_session), p.id) == (0, 10)


async def test_unique_and_nested_subnets_pick_the_most_specific(db_session) -> None:
    big = await _subnet(db_session, "198.51.0.0/16")
    small = await _subnet(db_session, "198.51.100.0/24")
    await _ips(db_session, small, range(150, 155))
    p = DHCPPoolRange(source_type="kea_dhcp", source_id=uuid.uuid4(), subnet_cidr=None,
                      start_ip="198.51.100.150", end_ip="198.51.100.159", source="kea")
    db_session.add(p)
    await db_session.commit()
    assert big.id != small.id
    assert _by(await pool_usage(db_session), p.id) == (5, 10)


async def test_ip_list_flags_only_pools_of_the_same_subnet(client, auth_headers, db_session) -> None:
    """IP 清單的「在 DHCP 範圍」：另一個單位同一段位址的集區不可以標到這邊的 IP。"""
    from app.models.firewall import OPNsenseFirewall
    a = await _subnet(db_session)
    b = await _subnet(db_session)
    await _ips(db_session, a, range(150, 151))
    await _ips(db_session, b, range(150, 151))
    await _ips(db_session, b, range(170, 171))
    fw = OPNsenseFirewall(name=f"fw-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.254", api_key_enc=b"x",
                          api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x", enabled=True,
                          scope_subnet_ids=[a.id])
    db_session.add(fw)
    await db_session.flush()
    db_session.add_all([
        DHCPPoolRange(source_type="opnsense", source_id=fw.id, subnet_cidr="198.51.100.0/24",
                      start_ip="198.51.100.150", end_ip="198.51.100.159", source="isc"),
        IPRange(subnet_id=a.id, start_ip="198.51.100.170", end_ip="198.51.100.179", purpose="dhcp"),
    ])
    await db_session.commit()

    def flags(r):  # type: ignore[no-untyped-def]
        return {x["ip"].split("/")[0]: x.get("in_dhcp_range") for x in r.json()["items"]}
    fa = flags(await client.get(f"/api/v1/addresses?subnet_id={a.id}", headers=auth_headers))
    fb = flags(await client.get(f"/api/v1/addresses?subnet_id={b.id}", headers=auth_headers))
    assert fa["198.51.100.150"] is True
    assert fb["198.51.100.150"] is False, "A 的防火牆集區不可以標到 B"
    assert fb["198.51.100.170"] is False, "A 的手動集區不可以標到 B"


async def test_firewall_as_dhcp_server_flag_stays_in_its_own_subnet(client, auth_headers, db_session) -> None:
    """防火牆的 API 位址是「DHCP 伺服器（自動）」：另一個單位同一個位址的 IP 不可以跟著被標。"""
    from app.models.firewall import OPNsenseFirewall
    a = await _subnet(db_session)
    b = await _subnet(db_session)
    await _ips(db_session, a, range(1, 2))
    await _ips(db_session, b, range(1, 2))
    db_session.add(OPNsenseFirewall(name=f"fw-{uuid.uuid4().hex[:6]}", api_url="https://198.51.100.1",
                                    api_key_enc=b"x", api_key_nonce=b"x", api_secret_enc=b"x",
                                    api_secret_nonce=b"x", enabled=True, scope_subnet_ids=[a.id]))
    await db_session.commit()

    def flag(r):  # type: ignore[no-untyped-def]
        return {x["ip"].split("/")[0]: x.get("dhcp_server_auto") for x in r.json()["items"]}["198.51.100.1"]
    assert flag(await client.get(f"/api/v1/addresses?subnet_id={a.id}", headers=auth_headers)) is True
    assert flag(await client.get(f"/api/v1/addresses?subnet_id={b.id}", headers=auth_headers)) is False


async def test_firewall_lookup_nat_uses_the_right_record_in_an_overlap(db_session) -> None:
    """IP 詳細資料的防火牆／NAT 反查：同一個位址有兩筆（兩個單位）時，NAT 要依這一筆 IP 找，不可以隨便取一筆。"""
    from app.models.nat import NATTranslation
    from app.services.fw_lookup import rules_touching_ip
    a = await _subnet(db_session)
    b = await _subnet(db_session)
    ipa = IPAddress(subnet_id=a.id, ip="198.51.100.10", state="active")
    ipb = IPAddress(subnet_id=b.id, ip="198.51.100.10", state="active")
    db_session.add_all([ipa, ipb])
    await db_session.flush()
    db_session.add(NATTranslation(name="b-forward", type="port_forward", dst_ip_id=ipb.id))
    await db_session.commit()
    mine = await rules_touching_ip(db_session, "198.51.100.10", ip_id=ipa.id)
    theirs = await rules_touching_ip(db_session, "198.51.100.10", ip_id=ipb.id)
    assert mine["nat"] == [], "另一個單位的 NAT 不可以出現在這一筆"
    assert len(theirs["nat"]) == 1
    # 沒給 ip_id、位址又有兩筆：不猜
    assert (await rules_touching_ip(db_session, "198.51.100.10"))["nat"] == []


async def test_ip_context_for_ai_lists_every_owner_in_an_overlap(db_session, admin_user) -> None:
    """鑑識卡／規則解讀給 AI 的證據：同一個位址兩個單位都有時，兩個都列出來並說明無法確定，不挑一個說成事實。"""
    from app.models.customer import Customer
    from app.services.ip_triage import full_ip_context
    ca, cb = Customer(name=f"cust-a-{uuid.uuid4().hex[:4]}"), Customer(name=f"cust-b-{uuid.uuid4().hex[:4]}")
    db_session.add_all([ca, cb])
    await db_session.flush()
    a = await _subnet(db_session)
    b = await _subnet(db_session)
    a.customer_id, b.customer_id = ca.id, cb.id
    db_session.add_all([IPAddress(subnet_id=a.id, ip="198.51.100.10", state="active"),
                        IPAddress(subnet_id=b.id, ip="198.51.100.10", state="active")])
    await db_session.commit()
    text = "\n".join(await full_ip_context(db_session, admin_user, "198.51.100.10"))
    assert ca.name in text and cb.name in text
    assert "重疊" in text
