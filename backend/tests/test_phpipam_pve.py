"""Proxmox VE SDN 的 phpIPAM 外掛接 jt-ipam 的相容 API（使用者 10-24：「pve 有支援 phpipam 整合，
我們的相容 api 都支援必要的 endpoint 嗎」）。

照 pve-network `PVE/Network/SDN/Ipams/PhpIpamPlugin.pm` 的實際呼叫逐一走一遍（路徑寫法也照抄）：
- 標頭 `Token`、`Content-Type: application/json`；回應不是 2xx 就失敗（`api_request`）
- 多數路徑**不帶結尾斜線**；LWP 對 PATCH／DELETE 不跟轉址 → 307 等於失敗（httpx 預設也不跟）
- 設定裡的「區段」是**整數**（properties: section => integer）；jt-ipam 的區段 ID 是 UUID，所以要有數字的相容 ID
- POST `addresses/first_free/{subnetId}/` 要真的佔用並回 `data`＝IP 字串
- 加閘道 IP 時帶 `is_gateway=1`；已存在時外掛會查 `addresses/search/{ip}` 的 `is_gateway`
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.security import hash_api_token
from app.models.address import IPAddress
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import APIToken


async def _token(db_session, user) -> str:
    raw = f"jt_{uuid.uuid4().hex}"
    db_session.add(APIToken(user_id=user.id, name="pve-sdn", token_hash=hash_api_token(raw),
                            token_prefix=raw[:8], scopes=[], expires_at=datetime.now(UTC) + timedelta(days=30)))
    await db_session.commit()
    return raw


def _h(token: str) -> dict[str, str]:
    return {"Token": token, "Content-Type": "application/json; charset=UTF-8"}


async def test_pve_sdn_plugin_call_sequence(client, db_session, admin_user) -> None:
    sec = Section(name="PVE SDN")
    db_session.add(sec)
    await db_session.commit()
    await db_session.refresh(sec)
    assert isinstance(sec.compat_id, int) and sec.compat_id > 0, "區段要有數字的相容 ID，PVE 的區段欄位只收整數"
    tok = await _token(db_session, admin_user)
    url = "/api/phpipam/pve"

    # verify_api：GET sections/{整數}（無斜線）
    r = await client.get(f"{url}/sections/{sec.compat_id}", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    # add_subnet：先查（查不到＝非 2xx），再建
    r = await client.get(f"{url}/subnets/cidr/198.51.100.0/24", headers=_h(tok))
    # 查不到：非 2xx，或 2xx 但 data 空 —— PVE 兩種都會判成「沒有 → 建立」（get_prefix_id 讀 data[0].id）
    assert r.status_code >= 400 or not (r.json().get("data") or [])
    r = await client.post(f"{url}/subnets/", headers=_h(tok),
                          json={"subnet": "198.51.100.0", "mask": "24", "sectionId": sec.compat_id})
    assert r.status_code in (200, 201), r.text
    r = await client.get(f"{url}/subnets/cidr/198.51.100.0/24", headers=_h(tok))
    assert r.status_code == 200, r.text
    subnet_id = r.json()["data"][0]["id"]

    # add_ip：閘道
    r = await client.post(f"{url}/addresses/", headers=_h(tok),
                          json={"ip": "198.51.100.1", "subnetId": subnet_id, "hostname": None,
                                "description": None, "is_gateway": 1})
    assert r.status_code in (200, 201), r.text
    r = await client.get(f"{url}/addresses/search/198.51.100.1", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["is_gateway"] in ("1", 1, True)
    sn = (await db_session.execute(select(Subnet).where(Subnet.id == uuid.UUID(subnet_id)))).scalar_one()
    await db_session.refresh(sn)
    assert str(sn.gateway).split("/")[0] == "198.51.100.1"

    # add_next_freeip：POST first_free（有結尾斜線），回 IP 字串並真的佔用
    r = await client.post(f"{url}/addresses/first_free/{subnet_id}/", headers=_h(tok),
                          json={"hostname": "vm101.example.net", "description": "101", "mac": "BC:24:11:00:01:01"})
    assert r.status_code in (200, 201), r.text
    ip = r.json()["data"]
    assert ip == "198.51.100.2"
    row = (await db_session.execute(select(IPAddress).where(IPAddress.ip == ip))).scalar_one()
    assert row.hostname == "vm101.example.net"

    # update_ip：先 search 取 id，再 PATCH（無斜線）
    r = await client.get(f"{url}/addresses/search/{ip}", headers=_h(tok))
    ip_id = r.json()["data"][0]["id"]
    assert r.json()["data"][0]["is_gateway"] in ("0", 0, False)
    r = await client.patch(f"{url}/addresses/{ip_id}", headers=_h(tok),
                           json={"hostname": "vm101-renamed.example.net", "description": "101", "mac": "BC:24:11:00:01:01"})
    assert r.status_code == 200, r.text

    # get_ips_from_mac：GET search_mac（無斜線）
    r = await client.get(f"{url}/addresses/search_mac/BC:24:11:00:01:01", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert [a["ip"] for a in r.json()["data"]] == [ip]

    # del_ip、del_subnet：DELETE（無斜線）
    r = await client.delete(f"{url}/addresses/{ip_id}", headers=_h(tok))
    assert r.status_code == 200, r.text
    gw_id = (await client.get(f"{url}/addresses/search/198.51.100.1", headers=_h(tok))).json()["data"][0]["id"]
    assert (await client.delete(f"{url}/addresses/{gw_id}", headers=_h(tok))).status_code == 200
    r = await client.delete(f"{url}/subnets/{subnet_id}", headers=_h(tok))
    assert r.status_code == 200, r.text


async def test_section_by_name_and_uuid_still_work(client, db_session, admin_user) -> None:
    sec = Section(name="by-name")
    db_session.add(sec)
    await db_session.commit()
    tok = await _token(db_session, admin_user)
    for ident in (str(sec.id), "by-name"):
        assert (await client.get(f"/api/phpipam/x/sections/{ident}/", headers=_h(tok))).status_code == 200


async def test_read_only_token_cannot_allocate(client, db_session, admin_user) -> None:
    sec = Section(name="ro")
    db_session.add(sec)
    await db_session.flush()
    sn = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db_session.add(sn)
    raw = f"jt_{uuid.uuid4().hex}"
    db_session.add(APIToken(user_id=admin_user.id, name="ro", token_hash=hash_api_token(raw), token_prefix=raw[:8],
                            scopes=["read"], expires_at=datetime.now(UTC) + timedelta(days=1)))
    await db_session.commit()
    r = await client.post(f"/api/phpipam/x/addresses/first_free/{sn.id}/", headers=_h(raw), json={})
    assert r.status_code == 403


async def test_rest_section_shows_the_compat_id(client, db_session, auth_headers) -> None:
    """網頁要看得到這個數字（PVE 的「區段」欄位要填它）。"""
    r = await client.post("/api/v1/sections", headers=auth_headers, json={"name": "compat-shown"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert isinstance(body["compat_id"], int)
    got = (await client.get(f"/api/v1/sections/{body['id']}", headers=auth_headers)).json()
    assert got["compat_id"] == body["compat_id"]
