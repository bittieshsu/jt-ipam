"""全站搜尋：真的包含查詢字的結果不可以被模糊命中擠出 LIMIT（GitHub issue #55，v1.0.7 回報）。

回報的情境：5 筆主機名稱含 `svc11` 的 IP（名稱都很長），另有很多名稱相近、但不含 `svc11` 的短名稱
（`svc-bi-d`、`svc2-bi-d`…）。搜 `svc11` 只出現 3 筆 —— 主機名稱越長 trigram 相似度越低，
`svc11-gamesrv-docker`（0.39）輸給 `svc-bi-d`（0.43），每類 8 筆的 LIMIT 先被模糊命中佔掉，
最後的「子字串優先」篩選再把模糊命中濾掉，被擠掉的子字串命中已經回不來。

第二個問題：設備用說明／序號命中時，那些欄位不在標題與副標裡，「子字串優先」篩選認不得，
只要有別的類型有子字串命中，這些設備就整批消失（7 台設備的說明含 storage，一台都沒出現）。

另外補上 VM 名稱：以前只有查詢是純數字時比對 VMID，沒有 IP 記錄的 VM 用名字搜不到。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

LONG = ["svc11-bi-docker", "svc11-bi-db-docker", "svc11-game-db-docker", "svc11-gamesrv-docker",
        "svc11-frontend-docker"]
# 不含 svc11、但 trigram 相似度一定比上面的長名稱高（svc12 對 svc11 約 0.5，長名稱約 0.27～0.38）。
# 回報者的資料庫裡 svc-bi-d 就贏了；相似度隨 PostgreSQL 版本與設定不同，用這組在哪裡都重現得出來。
NEAR = ["svc1", "svc10", "svc12", "svc13", "svc14", "svc15", "svc16", "svc17", "svc18", "svc19",
        "svc1a", "svc1b"]


async def _subnet(db_session, cidr="198.51.100.0/24"):  # type: ignore[no-untyped-def]
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr)
    db_session.add(sub)
    await db_session.flush()
    return sub


async def _search(client, auth_headers, q, **params):  # type: ignore[no-untyped-def]
    r = await client.get("/api/v1/search", params={"q": q, **params}, headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


async def test_long_hostnames_containing_the_query_are_not_pushed_out(client: AsyncClient, auth_headers,
                                                                       db_session) -> None:
    from app.models.address import IPAddress
    sub = await _subnet(db_session)
    for i, name in enumerate(LONG + NEAR, start=10):
        db_session.add(IPAddress(subnet_id=sub.id, ip=f"198.51.100.{i}", hostname=name))
    await db_session.commit()
    hits = await _search(client, auth_headers, "svc11")
    labels = sorted(h["label"] for h in hits if h["type"] == "ip_address")
    assert labels == sorted(LONG), "5 筆都含 svc11，預設的每類 8 筆就要全部回來"


async def test_device_matched_by_description_or_serial_survives_other_substring_hits(
        client: AsyncClient, auth_headers, db_session) -> None:
    from app.models.address import IPAddress
    from app.models.device import Device
    sub = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.20", hostname="storage-gw"))
    db_session.add(Device(name="nas-a", type="server", description="primary storage for backups"))
    db_session.add(Device(name="nas-b", type="server", serial="STORAGE-7731"))
    await db_session.commit()
    hits = await _search(client, auth_headers, "storage")
    devs = {h["label"]: h for h in hits if h["type"] == "device"}
    assert set(devs) == {"nas-a", "nas-b"}, "說明／序號命中的設備不可以被「子字串優先」濾掉"
    # 副標指出是哪一欄命中的，否則畫面上只是一個跟查詢字看不出關係的名稱
    assert "storage" in (devs["nas-a"]["sublabel"] or "").lower()
    assert "storage" in (devs["nas-b"]["sublabel"] or "").lower()


async def test_other_blocks_rank_substring_hits_before_fuzzy_ones(client: AsyncClient, auth_headers,
                                                                   db_session) -> None:
    """同樣的 LIMIT 問題在其他區塊（這裡用單位）：真的包含查詢字的那一筆要回來。"""
    from app.models.customer import Customer
    for name in NEAR:
        db_session.add(Customer(name=f"cust-{name}"))
    db_session.add(Customer(name="cust-svc11-very-long-department-name"))
    await db_session.commit()
    hits = await _search(client, auth_headers, "svc11")
    assert [h["label"] for h in hits if h["type"] == "customer"] == ["cust-svc11-very-long-department-name"]


async def test_typo_still_falls_back_to_fuzzy_hits(client: AsyncClient, auth_headers, db_session) -> None:
    """完全沒有子字串命中時（打錯字）才保留模糊結果，維持容錯。"""
    from app.models.address import IPAddress
    sub = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.30", hostname="fileserver01"))
    await db_session.commit()
    hits = await _search(client, auth_headers, "fileservr01")
    assert "fileserver01" in [h["label"] for h in hits]


async def _vm(db_session, name, *, ip=None, platform="proxmox"):  # type: ignore[no-untyped-def]
    from app.models.virt import VirtCluster, VirtualMachine
    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}", type=platform)
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name=name, kind="vm", primary_ip_id=ip.id if ip else None)
    db_session.add(vm)
    await db_session.flush()
    return vm


async def test_vm_found_by_name(client: AsyncClient, auth_headers, db_session) -> None:
    """有 IP 記錄的 VM 導到那個 IP（可開主控台）；沒有 IP 記錄的 VM 導到虛擬化頁（帶平台）。"""
    from app.models.address import IPAddress
    sub = await _subnet(db_session)
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.40", hostname="unrelated-host")
    db_session.add(ip)
    await db_session.flush()
    await _vm(db_session, "vm-orders-api", ip=ip)
    no_ip = await _vm(db_session, "vm-orders-batch", platform="vmware")
    await db_session.commit()
    hits = await _search(client, auth_headers, "vm-orders")
    by = {h["label"]: h for h in hits if h["type"] in ("vm", "vm_record")}
    assert by["vm-orders-api"]["type"] == "vm"
    assert by["vm-orders-api"]["id"] == str(ip.id)
    assert by["vm-orders-batch"]["type"] == "vm_record"
    assert by["vm-orders-batch"]["id"] == f"vmware.{no_ip.id}"


async def test_vm_without_an_ip_needs_global_read(client: AsyncClient, db_session) -> None:
    """虛擬化頁要全域讀取權限；只被授權部分子網路的帳號搜不到沒有 IP 記錄的 VM（名稱也不可以外洩）。"""
    from app.core.security import hash_password
    from app.models.address import IPAddress
    from app.models.permission import Permission
    from app.models.user import User
    from app.services.auth import issue_access_token
    sub = await _subnet(db_session)
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.41", hostname="x")
    db_session.add(ip)
    await db_session.flush()
    await _vm(db_session, "vm-scoped-web", ip=ip)
    await _vm(db_session, "vm-scoped-secret")
    u = User(username=f"s-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@e.test",
             password_hash=hash_password("Xx!12345678xX"), is_admin=False, is_active=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(Permission(object_type="subnet", object_id=sub.id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    hits = await _search(client, {"Authorization": f"Bearer {issue_access_token(u)}"}, "vm-scoped")
    labels = {h["label"] for h in hits}
    assert "vm-scoped-web" in labels, "看得到那個子網路，就搜得到上面的 VM"
    assert "vm-scoped-secret" not in labels
