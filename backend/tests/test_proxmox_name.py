"""Proxmox VE 連線要有名稱（使用者 2026-10-22：戰情室整合來源出現兩個「Proxmox VE」分不出來，「沒有給取名稱的地方」）。

- 新增時可以取名；沒填就用 API 網址的主機名稱（不再只剩產品名）
- 編輯可以改名，留稽核
- 共用的整合清單（戰情室、診斷）顯示這個名稱；任何整合的名稱是空的時，改顯示主機名稱
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.anyio


def _body(**kw) -> dict:
    return {"api_url": "https://pve-a.example.net:8006", "auth_username": "root@pam", "auth_token_id": "jt",
            "token_secret": "secret-123456", **kw}


async def test_name_given_and_defaulted(client, auth_headers) -> None:
    r = await client.post("/api/v1/virt/proxmox", headers=auth_headers, json=_body(name="lab-cluster"))
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "lab-cluster"
    r2 = await client.post("/api/v1/virt/proxmox", headers=auth_headers,
                           json=_body(api_url="https://pve-b.example.net:8006"))
    assert r2.json()["name"] == "pve-b"                                   # 沒填：主機名稱的第一段


async def test_rename(client, auth_headers) -> None:
    pid = (await client.post("/api/v1/virt/proxmox", headers=auth_headers, json=_body())).json()["id"]
    r = await client.patch(f"/api/v1/virt/proxmox/{pid}", headers=auth_headers, json={"name": "office"})
    assert r.status_code == 200 and r.json()["name"] == "office"


async def test_registry_uses_name_or_host(db_session, client, auth_headers) -> None:
    from app.services import integration_registry as reg
    await client.post("/api/v1/virt/proxmox", headers=auth_headers, json=_body(name="lab-cluster"))
    rows = [r for r in await reg.instances(db_session) if r["kind"] == "proxmox"]
    assert "lab-cluster" in {r["name"] for r in rows}
    # 名稱空白的整合：用主機名稱，不是產品名
    assert reg.display_name(SimpleNamespace(name="", api_url="https://kea1.example.net:8000"), "Kea DHCP") == "kea1.example.net"
    assert reg.display_name(SimpleNamespace(name=None), "Kea DHCP") == "Kea DHCP"
    _ = datetime.now(UTC)
