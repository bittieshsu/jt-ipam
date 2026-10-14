"""「已知 DHCP 伺服器」只有一份判斷（客戶 2026-10-14）。

客戶把 Windows DHCP 伺服器加進整合之後，IP 清單上那台的紅色「非法 DHCP 伺服器」圖示還在。
原因是同一件事有兩份判斷：
- 異常偵測「非授權 DHCP」認得十種整合
- IP 清單的紅色圖示（addresses.py 的 dhcp_server_auto）只認 OPNsense／pfSense
加整合的時候只改到其中一份。兩份都漏了 MikroTik。

現在兩邊（加上 IP 調查）共用 app.services.dhcp_known：
- 每一種 DHCP 來源（DHCP_SOURCE_TYPES）都要有收集方式（守門）
- 整合的主機填名稱（WinRM 常用 FQDN）時，用 IP 記錄的主機名稱對應
- 經由 DHCP 中繼收到的回應，IP 清單跟異常偵測一樣不算
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.address import IPAddress
from app.models.dhcp import DHCP_SOURCE_TYPES
from app.models.dhcp_sighting import DHCPSighting
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.anomaly import detect_rogue_dhcp

pytestmark = pytest.mark.anyio

B = b"x"
SERVER = "198.51.100.2"


def _n(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _integration(db, kind: str, host: str) -> None:  # type: ignore[no-untyped-def]
    """建一個指向 host（IP 或名稱）的 DHCP 整合。"""
    url = f"https://{host}"
    if kind == "opnsense":
        from app.models.firewall import OPNsenseFirewall
        db.add(OPNsenseFirewall(name=_n("opn"), api_url=url, api_key_enc=B, api_key_nonce=B,
                                api_secret_enc=B, api_secret_nonce=B))
    elif kind == "pfsense":
        from app.models.pfsense import PfSenseFirewall
        db.add(PfSenseFirewall(name=_n("pf"), api_url=url, api_key_enc=B, api_key_nonce=B))
    elif kind == "fortigate":
        from app.models.fortigate import FortiGateFirewall
        db.add(FortiGateFirewall(name=_n("fgt"), api_url=url, api_token_enc=B, api_token_nonce=B))
    elif kind == "paloalto":
        from app.models.paloalto import PaloAltoFirewall
        db.add(PaloAltoFirewall(name=_n("pa"), api_url=url, api_key_enc=B, api_key_nonce=B))
    elif kind == "mikrotik":
        from app.models.mikrotik import MikroTikRouter
        db.add(MikroTikRouter(name=_n("mt"), api_url=url, api_username="u", api_password_enc=B,
                              api_password_nonce=B))
    elif kind == "kea_dhcp":
        from app.models.dhcp_standalone import KeaDhcpServer
        db.add(KeaDhcpServer(name=_n("kea"), api_url=f"http://{host}:8000"))
    elif kind == "isc_dhcp":
        from app.models.dhcp_standalone import IscDhcpServer
        from app.models.scan_agent import ScanAgent
        agent = ScanAgent(name=_n("agent"), last_source_ip=host)
        db.add(agent)
        await db.flush()
        db.add(IscDhcpServer(name=_n("isc"), agent_id=agent.id))
    elif kind == "windows_dhcp":
        from app.models.windows_dhcp import WindowsDhcpServer
        db.add(WindowsDhcpServer(name=_n("win"), host=host, username="u", password_enc=B, password_nonce=B))
    elif kind == "isoinsight":
        from app.models.isoinsight import IsoInsightSource
        db.add(IsoInsightSource(name=_n("iso"), base_url=url, username="u", password_enc=B, password_nonce=B))
    elif kind == "technitium":
        from app.models.technitium import TechnitiumDhcpServer
        db.add(TechnitiumDhcpServer(name=_n("tdns"), api_url=f"http://{host}:5380"))
    elif kind == "checkpoint":
        from app.models.checkpoint import CheckPointGateway, CheckPointServer
        srv = CheckPointServer(name=_n("cpm"), api_url="https://cp-mgmt.example")
        db.add(srv)
        await db.flush()
        db.add(CheckPointGateway(server_id=srv.id, uid=uuid.uuid4().hex, name=_n("gw"), ipv4_address=host))
    else:  # pragma: no cover
        raise AssertionError(f"測試沒有 {kind} 的建立方式")
    await db.flush()


async def _subnet_with_server(db, *, hostname: str | None = None, via_relay: bool = False):  # type: ignore[no-untyped-def]
    sec = Section(name=_n("sec"))
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    db.add(IPAddress(subnet_id=sub.id, ip=SERVER, hostname=hostname, state="used"))
    now = datetime.now(UTC)
    db.add(DHCPSighting(subnet_id=sub.id, server_ip=SERVER, via_relay=via_relay, first_seen_at=now, last_seen_at=now))
    await db.commit()          # API 走另一個連線，要看得到
    return sub


async def _row(client, headers, sub) -> dict:  # type: ignore[no-untyped-def]
    r = await client.get(f"/api/v1/addresses?subnet_id={sub.id}", headers=headers)
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["items"] if x["ip"].split("/")[0] == SERVER)


def _rogue_in_list(row: dict) -> bool:
    """與前端 IpRoleTags 的 isRogueDhcp 同一條規則。"""
    return bool(row.get("dhcp_observed_at")) and not (row.get("is_dhcp_server") or row.get("dhcp_server_auto"))


async def _rogue_in_anomaly(db) -> bool:  # type: ignore[no-untyped-def]
    return any(o["server_ip"] == SERVER for o in await detect_rogue_dhcp(db))


def test_every_dhcp_source_type_is_collected() -> None:
    """新增 DHCP 來源整合時，「已知 DHCP 伺服器」要一起認得它（這次就是漏了 Windows DHCP 與 MikroTik）。"""
    from app.services.dhcp_known import COLLECTORS
    assert set(COLLECTORS) == set(DHCP_SOURCE_TYPES)


@pytest.mark.parametrize("kind", sorted(DHCP_SOURCE_TYPES))
async def test_integrated_dhcp_server_is_not_rogue_anywhere(kind, client, auth_headers, db_session) -> None:
    from app.services.dhcp_known import LABELS
    sub = await _subnet_with_server(db_session)
    assert _rogue_in_list(await _row(client, auth_headers, sub)), "前提：沒有整合時是紅的"
    assert await _rogue_in_anomaly(db_session)

    await _integration(db_session, kind, SERVER)
    await db_session.commit()
    row = await _row(client, auth_headers, sub)
    assert row["dhcp_server_auto"] is True, f"{kind}：IP 清單還是紅的"
    assert LABELS[kind] in (row.get("dhcp_server_integration") or ""), "提示要講出是哪個整合"
    assert not await _rogue_in_anomaly(db_session), f"{kind}：異常偵測還在報"


@pytest.mark.parametrize("host", ["dc-02.corp.example", "DC-02", "dc-02.corp.example."])
async def test_windows_dhcp_by_host_name_matches_the_ip_record(host, client, auth_headers, db_session) -> None:
    """WinRM 常用名稱連線：主機欄位填 FQDN 或短名稱時，用 IP 記錄的主機名稱對應。"""
    sub = await _subnet_with_server(db_session, hostname="dc-02.corp.example")
    await _integration(db_session, "windows_dhcp", host)
    await db_session.commit()
    row = await _row(client, auth_headers, sub)
    assert not _rogue_in_list(row)
    assert "Windows DHCP" in (row.get("dhcp_server_integration") or "")
    assert not await _rogue_in_anomaly(db_session)


@pytest.mark.parametrize("host", ["dc-03.corp.example", "dc-02.other.example", "dc-03"])
async def test_a_different_name_does_not_match(host, client, auth_headers, db_session) -> None:
    sub = await _subnet_with_server(db_session, hostname="dc-02.corp.example")
    await _integration(db_session, "windows_dhcp", host)
    await db_session.commit()
    assert _rogue_in_list(await _row(client, auth_headers, sub))
    assert await _rogue_in_anomaly(db_session)


async def test_relayed_replies_do_not_light_up_the_list(client, auth_headers, db_session) -> None:
    """經由中繼收到的回應，異常偵測本來就不算；IP 清單也不可以因此標紅。"""
    sub = await _subnet_with_server(db_session, via_relay=True)
    await db_session.commit()
    row = await _row(client, auth_headers, sub)
    assert row["dhcp_observed_at"] is None
    assert not await _rogue_in_anomaly(db_session)


async def test_investigate_names_the_integration(db_session, admin_user) -> None:
    from app.services.investigate import collect_dossier
    await _subnet_with_server(db_session, hostname="dc-02.corp.example")
    await _integration(db_session, "windows_dhcp", "dc-02.corp.example")
    await db_session.commit()
    d = await collect_dossier(db_session, user=admin_user, ip=SERVER)
    rows = d["sections"]["dhcp_rogue"] if "sections" in d else d["dhcp_rogue"]
    assert rows and "Windows DHCP" in (rows[0].get("server_integration") or "")
