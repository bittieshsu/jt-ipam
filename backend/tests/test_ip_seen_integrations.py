"""IP 詳細資料的「各來源最後出現」只列有設定的整合（使用者 2026-10-08：沒設定 AdGuard，卻有一列「AdGuard 設定」）。

畫面要知道哪些來源有整合，但 /system/integration-presence 要全域讀取權，只能看某個子網路的人拿不到；
所以由 IP 詳細資料帶 `seen_integrations`（只有布林值，不洩漏任何整合的內容）。
"""
from __future__ import annotations

import uuid

from app.models.address import IPAddress
from app.models.adguard import AdGuardInstance
from app.models.section import Section
from app.models.subnet import Subnet


async def _ip(db) -> IPAddress:  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.9", state="active")
    db.add(ip)
    await db.commit()
    return ip


async def test_detail_says_which_source_integrations_exist(client, auth_headers, db_session) -> None:
    ip = await _ip(db_session)
    body = (await client.get(f"/api/v1/addresses/{ip.id}", headers=auth_headers)).json()
    assert body["seen_integrations"] == {"librenms": False, "adguard": False}

    db_session.add(AdGuardInstance(name=f"ag-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.53", api_user="u",
                                   api_password_enc=b"x", api_password_nonce=b"y"))
    await db_session.commit()
    body = (await client.get(f"/api/v1/addresses/{ip.id}", headers=auth_headers)).json()
    assert body["seen_integrations"]["adguard"] is True


async def test_subnet_only_reader_gets_it_too(client, db_session) -> None:
    from tests.test_change_impact_api import _grant, _hdr, _user
    ip = await _ip(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", ip.subnet_id, "read")
    r = await client.get(f"/api/v1/addresses/{ip.id}", headers=_hdr(u))
    assert r.status_code == 200 and r.json()["seen_integrations"] == {"librenms": False, "adguard": False}
