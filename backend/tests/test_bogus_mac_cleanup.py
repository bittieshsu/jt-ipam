"""兩種假 MAC（2026-10-18 外部 AI 實測 MCP 回報）。

1. LibreNMS 的 ARP 表偶爾給「只有第一個位元組」的殘缺值（c4:00:00:00:00:00、26:00:00:00:00:00 …，
   正式環境 22 種），以前照收成觀測，AI 看到就說「這個位址被一張假網卡占用」。全零與廣播也一樣沒擋。
2. 2026-09-26 以前，IP 編輯表單每次存檔都把當時顯示的 MAC 一起送出，後端就把它標成「手動」（優先序最高）：
   DHCP 位址當時是誰，就永遠是誰，之後租約、ARP、掃描都看到新的 MAC 也寫不進來（正式環境 45 筆、
   沒有一筆是真的有人改過 MAC）。表單那邊 09-26 修了，舊資料沒有清 —— 0220 依稽核記錄清掉。
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from app.core.audit import append_audit
from app.models.address import IPAddress
from app.models.librenms import ARPEntry, LibreNMSDevice, LibreNMSInstance
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import librenms as lib
from app.services.arp_evidence import normalize
from sqlalchemy import func, select, text


def test_truncated_and_placeholder_macs_are_not_addresses() -> None:
    for bad in ("c4:00:00:00:00:00", "26:00:00:00:00:00", "c40000000000", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        assert normalize(bad) is None, bad
    # 後面幾個位元組是 0 的真位址照收（只有「第一個位元組以外全零」才是殘缺）
    assert normalize("00:00:5e:00:00:00") == "00:00:5e:00:00:00"
    assert normalize("c4:00:00:00:00:01") == "c4:00:00:00:00:01"


async def test_librenms_arp_sync_drops_bogus_macs(db_session, monkeypatch) -> None:
    db_session.autoflush = False
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(cidr="198.51.100.0/24", section_id=sec.id)
    inst = LibreNMSInstance(name=f"lnms-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=b"x", api_token_nonce=b"y")
    db_session.add_all([sub, inst])
    await db_session.flush()
    db_session.add(LibreNMSDevice(instance_id=inst.id, legacy_device_id=7, hostname="sw-01"))
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.40")
    db_session.add(ip)
    await db_session.commit()

    async def fake(_inst, path, *, timeout=30.0):
        return {"arp": [
            {"ipv4_address": "198.51.100.40", "mac_address": "c40000000000", "device_id": 7},
            {"ipv4_address": "198.51.100.41", "mac_address": "000000000000", "device_id": 7},
            {"ipv4_address": "198.51.100.42", "mac_address": "ffffffffffff", "device_id": 7},
            {"ipv4_address": "198.51.100.43", "mac_address": "00005e005343", "device_id": 7},
        ]} if path.startswith("/api/v0/resources/ip/arp") else {}
    monkeypatch.setattr(lib, "_api_get", fake)
    await lib.sync_arp(db_session, inst)
    await db_session.commit()
    rows = (await db_session.execute(select(func.host(ARPEntry.ip)).where(ARPEntry.instance_id == inst.id))).all()
    assert [r[0] for r in rows] == ["198.51.100.43"]
    await db_session.refresh(ip)
    assert ip.mac is None


def test_precedence_never_writes_a_bogus_mac_onto_an_ip() -> None:
    from app.services.arp_precedence import DEFAULT_ARP_ORDER, would_take
    for bad in ("c4:00:00:00:00:00", "000000000000", "ff:ff:ff:ff:ff:ff", "c4:00"):
        assert not would_take(DEFAULT_ARP_ORDER, [], cur_mac=None, cur_source=None, mac=bad, source="librenms"), bad
    assert would_take(DEFAULT_ARP_ORDER, [], cur_mac=None, cur_source=None, mac="00:00:5e:00:53:01", source="librenms")


def _migration():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0220_bogus_mac_cleanup.py"
    spec = importlib.util.spec_from_file_location("m0220", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_migration_clears_manual_marks_that_no_one_typed(db_session, admin_user) -> None:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(cidr="198.51.100.0/24", section_id=sec.id)
    db_session.add(sub)
    await db_session.flush()
    resubmitted = IPAddress(subnet_id=sub.id, ip="198.51.100.51", mac="00:00:5e:00:53:51", mac_source="manual")
    typed = IPAddress(subnet_id=sub.id, ip="198.51.100.52", mac="00:00:5e:00:53:52", mac_source="manual")
    created = IPAddress(subnet_id=sub.id, ip="198.51.100.53", mac="00:00:5e:00:53:53", mac_source="manual")
    via_api = IPAddress(subnet_id=sub.id, ip="198.51.100.54", mac="00:00:5e:00:53:54", mac_source="manual")
    no_history = IPAddress(subnet_id=sub.id, ip="198.51.100.55", mac="00:00:5e:00:53:55", mac_source="manual")
    db_session.add_all([resubmitted, typed, created, via_api, no_history])
    await db_session.flush()

    async def audit(obj: IPAddress, action: str, diff: dict) -> None:  # type: ignore[type-arg]
        await append_audit(db_session, actor_user_id=str(admin_user.id), actor_ip=None, actor_user_agent=None,
                           object_type="ip_address",
                           object_id=str(obj.id), action=action, diff=diff, request_id=None)

    # 舊表單：只改了說明，MAC 原封不動一起送出（大小寫、分隔不同也算同一個）
    await audit(resubmitted, "update", {"before": {"mac": "00:00:5e:00:53:51", "description": None},
                                        "changes": {"mac": "00005E005351", "description": "x"}})
    await audit(typed, "update", {"before": {"mac": "00:00:5e:00:53:00"}, "changes": {"mac": "00:00:5e:00:53:52"}})
    await audit(created, "create", {"after": {"ip": "198.51.100.53", "mac": "00:00:5e:00:53:53"}})
    await audit(via_api, "update", {"before": {"mac": None}, "via": "phpipam"})
    await db_session.commit()

    for stmt in _migration().UPGRADE_SQL:
        await db_session.execute(text(stmt))
    await db_session.commit()
    for o in (resubmitted, typed, created, via_api, no_history):
        await db_session.refresh(o)
    assert resubmitted.mac_source is None          # 優先序最低：之後租約／ARP 看到的 MAC 可以蓋過去
    assert str(resubmitted.mac) == "00:00:5e:00:53:51"   # MAC 本身不動
    assert typed.mac_source == "manual"
    assert created.mac_source == "manual"
    assert via_api.mac_source == "manual"
    assert no_history.mac_source == "manual"       # 沒有記錄就不猜（例如從別台匯入的）


async def test_migration_deletes_truncated_arp_observations(db_session) -> None:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(cidr="198.51.100.0/24", section_id=sec.id)
    db_session.add(sub)
    await db_session.flush()
    for mac in ("c4:00:00:00:00:00", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "00:00:5e:00:00:00"):
        db_session.add(ARPEntry(ip="198.51.100.60", mac=mac, source="librenms", subnet_id=None))
    await db_session.commit()
    for stmt in _migration().UPGRADE_SQL:
        await db_session.execute(text(stmt))
    await db_session.commit()
    rows = (await db_session.execute(select(ARPEntry.mac).where(ARPEntry.ip == "198.51.100.60"))).scalars().all()
    assert [str(m) for m in rows] == ["00:00:5e:00:00:00"]
