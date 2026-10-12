"""IP 位址頁補上子網路頁有的欄位（使用者 2026-10-12）：說明、裝置、製造商、最後出現、失聯要能排序。

這頁是伺服器端分頁，排序也要在伺服器做（只排當頁會排錯）。沒有值的列一律排最後，升冪降冪都一樣。
最後出現＝掃描代理／LibreNMS／ARP 三者最新的那個；失聯天數只看掃描代理與 LibreNMS（跟子網路頁一致）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.device import Device
from app.models.oui import OUIVendor
from app.models.section import Section
from app.models.subnet import Subnet

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sample(db_session):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db_session.add(sub)
    db_session.add_all([OUIVendor(prefix="020000", short_name="Beta", name="Beta Inc"),
                        OUIVendor(prefix="060000", short_name="Alpha", name="Alpha Inc")])
    da, db = Device(name="dev-a"), Device(name="dev-b")
    db_session.add_all([da, db])
    await db_session.flush()
    db_session.add_all([
        IPAddress(subnet_id=sub.id, ip="203.0.113.1", description="b-desc", device_id=db.id,
                  mac="02:00:00:00:00:01", last_seen_scanner=now - timedelta(hours=1)),
        IPAddress(subnet_id=sub.id, ip="203.0.113.2", description="a-desc", device_id=da.id,
                  mac="06:00:00:00:00:02", last_seen_librenms=now - timedelta(days=3),
                  last_seen_arp=now - timedelta(days=2)),
        IPAddress(subnet_id=sub.id, ip="203.0.113.3"),
    ])
    await db_session.commit()
    return sub


async def _order(client, auth_headers, sub, key, direction):  # type: ignore[no-untyped-def]
    r = await client.get("/api/v1/addresses", headers=auth_headers,
                         params={"subnet_id": str(sub.id), "sort": key, "order": direction, "page_size": 50})
    assert r.status_code == 200, r.text
    return [x["ip"].split("/")[0].rsplit(".", 1)[1] for x in r.json()["items"]]


@pytest.mark.parametrize(("key", "asc", "desc"), [
    ("description", ["2", "1", "3"], ["1", "2", "3"]),
    ("device", ["2", "1", "3"], ["1", "2", "3"]),
    ("mac_vendor", ["2", "1", "3"], ["1", "2", "3"]),
    # 最後出現：.2 的 ARP（2 天前）比它的 LibreNMS（3 天前）新，仍比 .1（1 小時前）舊
    ("last_seen", ["2", "1", "3"], ["1", "2", "3"]),
    # 失聯天數：少的在前；從沒看過的最久（升冪排最後、降冪排最前）
    ("stale_days", ["1", "2", "3"], ["3", "2", "1"]),
])
async def test_new_columns_sort_on_the_server(client, auth_headers, sample, key, asc, desc) -> None:
    assert await _order(client, auth_headers, sample, key, "asc") == asc
    assert await _order(client, auth_headers, sample, key, "desc") == desc


async def test_rows_carry_what_the_new_columns_show(client, auth_headers, sample) -> None:
    r = await client.get("/api/v1/addresses", headers=auth_headers, params={"subnet_id": str(sample.id)})
    by = {x["ip"].split("/")[0]: x for x in r.json()["items"]}
    assert by["203.0.113.1"]["device_name"] == "dev-b"
    assert by["203.0.113.2"]["mac_vendor"] == "Alpha"
    assert by["203.0.113.2"]["description"] == "a-desc"
