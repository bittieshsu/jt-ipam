"""ISOinsight 整合：同步（mock HTTP 伺服器＋測試資料庫）。

涵蓋規格 §14 的資料面：子網路（最長首碼、重疊、未配對、跨租戶阻擋）、合併（人工欄位不覆蓋、
空值不清除、重跑冪等）、非破壞性（失敗、空清單、缺席、停用、刪除來源都不刪正式 IP）、
任務（手動與排程互斥、交易還原、設定途中改變、429 延後、登入失敗暫停排程）。

工作用自己的 session（比照正式環境 autoflush=False），所以準備資料要先 commit。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.models.address import IPAddress
from app.models.customer import Customer
from app.models.dhcp import DHCPLeaseSighting
from app.models.ip_change_log import IPChangeLog
from app.models.isoinsight import IsoInsightLease, IsoInsightSource, IsoInsightSyncRun
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.vrf import VRF
from app.services.isoinsight import config as icfg
from app.services.isoinsight import job
from app.services.isoinsight.errors import IsoError
from sqlalchemy import func, select

from tests.isoinsight_mock import MockServer, json_resp, lease_body

PW = "s3cret pass&=中"
TPE = ZoneInfo("Asia/Taipei")


def _allow(_h, _a) -> None:
    return None


def _t(hours: float, base: datetime | None = None) -> str:
    return ((base or datetime.now(UTC)) + timedelta(hours=hours)).astimezone(TPE).strftime("%Y/%m/%d %H:%M:%S")


def _row(ip: str, mac: str | None = "02:00:5e:00:53:20", name: str | None = "laptop-07",
         start: float = -1, end: float = 3) -> dict:
    return {"ip": ip, "mac": mac, "name": name, "start_time": _t(start), "end_time": _t(end)}


@pytest.fixture
def sf(_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    return async_sessionmaker(_engine, expire_on_commit=False, autoflush=False, class_=AsyncSession)


@pytest.fixture
def srv():
    s = MockServer()
    s.route("POST", "/api/logon", (200, [("Set-Cookie", "SID=abc; Path=/"), ("Content-Type", "application/json")],
                                   b"{}"))
    yield s
    s.close()


def _leases(srv: MockServer, rows: list[dict]) -> None:
    srv.route("GET", "/isosvc", lease_body(rows))


async def _net(db, cidr: str = "192.0.2.0/24", *, customer=None, vrf=None) -> Subnet:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr, customer_id=customer.id if customer else None,
                 vrf_id=vrf.id if vrf else None)
    db.add(sub)
    await db.flush()
    return sub


async def _source(db, srv: MockServer, subnets: list[Subnet], **kw) -> IsoInsightSource:
    src = IsoInsightSource(name=kw.pop("name", f"iso-{uuid.uuid4().hex[:6]}"), base_url=srv.url,
                           username="operator", password_enc=b"x", password_nonce=b"x",
                           scope_subnet_ids=[s.id for s in subnets], **kw)
    db.add(src)
    await db.flush()
    src.password_enc, src.password_nonce = icfg.encrypt_password(src.id, PW)
    await db.commit()
    return src


async def _sync(src, sf, **kw) -> dict:
    return await job.run_sync(src.id, session_factory=sf, check=_allow, **kw)


async def _ip(db, ip: str) -> IPAddress | None:
    return (await db.execute(select(IPAddress).where(IPAddress.ip == ip)
                             .execution_options(populate_existing=True))).scalar_one_or_none()


async def _src(db, sid) -> IsoInsightSource:
    return await db.get(IsoInsightSource, sid, populate_existing=True)


async def _count(db, model, *conds) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*conds))).scalar_one()


# ── 基本：套用到既有 IP、新增、過期 ─────────────────────────────────────────

async def test_sync_applies_mac_hostname_and_lease_flag_to_an_existing_ip(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20")])
    out = await _sync(src, sf)
    assert out["result"] == "success", out
    assert out["updated"] == 1 and out["fetched"] == 1
    ip = await _ip(db_session, "192.0.2.20")
    assert str(ip.mac) == "02:00:5e:00:53:20" and ip.mac_source == "isoinsight"
    assert ip.hostname == "laptop-07"
    assert ip.in_dhcp_lease is True
    # 不是上線證據
    assert not ip.arp_seen and ip.last_seen_scanner is None
    row = (await db_session.execute(select(IsoInsightLease))).scalar_one()
    assert row.subnet_id == sub.id and row.ip_address_id == ip.id and row.match_status == "matched"
    run = (await db_session.execute(select(IsoInsightSyncRun))).scalar_one()
    assert run.result == "success" and run.kind == "sync" and run.updated == 1
    s = await _src(db_session, src.id)
    assert s.last_full_success_at is not None and s.last_commit_at is not None and s.last_fetch_ok_at is not None
    assert s.running_since is None


async def test_active_lease_in_an_allowed_subnet_creates_an_ip(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21", name="printer-2")])
    out = await _sync(src, sf)
    assert out["created"] == 1
    ip = await _ip(db_session, "192.0.2.21")
    assert ip is not None and ip.discovery_source == "isoinsight" and ip.subnet_id == sub.id
    assert ip.hostname == "printer-2"
    assert await _count(db_session, IPChangeLog, IPChangeLog.ip_id == ip.id,
                        IPChangeLog.event_type == "created") == 1


async def test_create_ips_off_only_observes(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub], create_ips=False)
    _leases(srv, [_row("192.0.2.21")])
    out = await _sync(src, sf)
    assert out["created"] == 0 and out["observed_only"] == 1
    assert await _ip(db_session, "192.0.2.21") is None
    assert await _count(db_session, IsoInsightLease) == 1


async def test_expired_and_unknown_time_leases_never_create_or_touch_ips(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.30", state="active"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.22", start=-5, end=-1),
                  _row("192.0.2.30", mac="02:00:5e:00:53:30", start=-5, end=-1),
                  {**_row("192.0.2.23", mac="02:00:5e:00:53:23"), "end_time": "permanent"}])
    out = await _sync(src, sf)
    assert out["expired"] == 2 and out["unknown_time"] == 1 and out["created"] == 0
    assert out["result"] == "partial"                     # 時間不明是品質問題
    assert await _ip(db_session, "192.0.2.22") is None and await _ip(db_session, "192.0.2.23") is None
    ip = await _ip(db_session, "192.0.2.30")
    assert ip.mac is None and ip.in_dhcp_lease is False
    assert await _count(db_session, IsoInsightLease) == 3   # 留在來源觀察


async def test_rerun_is_idempotent(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20"), _row("192.0.2.21", mac="02:00:5e:00:53:21", name="pc-21")])
    await _sync(src, sf)
    logs = await _count(db_session, IPChangeLog)
    ips = await _count(db_session, IPAddress)
    second = await _sync(src, sf)
    assert second["result"] == "success"
    assert second["created"] == 0 and second["updated"] == 0 and second["unchanged"] == 2
    assert await _count(db_session, IPChangeLog) == logs
    assert await _count(db_session, IPAddress) == ips
    assert await _count(db_session, IsoInsightLease) == 2
    assert await _count(db_session, DHCPLeaseSighting) == 2


# ── 合併規則 ────────────────────────────────────────────────────────────────

async def test_manual_name_and_mac_are_never_overwritten(db_session, srv, sf) -> None:
    from app.services.hostname import apply_observation
    sub = await _net(db_session)
    ip = IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active", mac="02:00:5e:00:53:99", mac_source="manual")
    db_session.add(ip)
    await db_session.flush()
    await apply_observation(db_session, ip=ip, source="manual", hostname="desk-manual")
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20", mac="02:00:5e:00:53:20", name="laptop-07")])
    await _sync(src, sf)
    got = await _ip(db_session, "192.0.2.20")
    assert got.hostname == "desk-manual" and str(got.mac) == "02:00:5e:00:53:99"
    assert got.in_dhcp_lease is True


async def test_empty_name_does_not_clear_the_previous_name(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20", name="laptop-07")])
    await _sync(src, sf)
    _leases(srv, [_row("192.0.2.20", name="")])
    await _sync(src, sf)
    assert (await _ip(db_session, "192.0.2.20")).hostname == "laptop-07"


async def test_invalid_mac_does_not_overwrite_a_valid_one(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active", mac="02:00:5e:00:53:77",
                             mac_source="scanner"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20", mac="not-a-mac")])
    out = await _sync(src, sf)
    assert out["result"] == "partial"
    got = await _ip(db_session, "192.0.2.20")
    assert str(got.mac) == "02:00:5e:00:53:77" and got.in_dhcp_lease is True


async def test_overlapping_leases_with_different_macs_are_kept_as_a_conflict(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20", mac="02:00:5e:00:53:01"), _row("192.0.2.20", mac="02:00:5e:00:53:02")])
    out = await _sync(src, sf)
    assert out["conflicts"] == 2 and out["result"] == "partial"
    got = await _ip(db_session, "192.0.2.20")
    assert got.mac is None and got.in_dhcp_lease is False
    assert await _count(db_session, IsoInsightLease) == 2       # 兩個 MAC 各留證據


# ── 子網路 ──────────────────────────────────────────────────────────────────

async def test_longest_prefix_wins_inside_the_allowed_scope(db_session, srv, sf) -> None:
    wide = await _net(db_session, "192.0.2.0/24")
    narrow = await _net(db_session, "192.0.2.0/25")
    src = await _source(db_session, srv, [wide, narrow])
    _leases(srv, [_row("192.0.2.20")])
    await _sync(src, sf)
    assert (await _ip(db_session, "192.0.2.20")).subnet_id == narrow.id


async def test_overlapping_vrfs_are_left_unmatched(db_session, srv, sf) -> None:
    v1, v2 = VRF(name=f"vrf-a-{uuid.uuid4().hex[:4]}"), VRF(name=f"vrf-b-{uuid.uuid4().hex[:4]}")
    db_session.add_all([v1, v2])
    await db_session.flush()
    a = await _net(db_session, "198.51.100.0/24", vrf=v1)
    b = await _net(db_session, "198.51.100.0/24", vrf=v2)
    src = await _source(db_session, srv, [a, b])
    _leases(srv, [_row("198.51.100.5")])
    out = await _sync(src, sf)
    assert out["unmatched"] == 1 and out["created"] == 0 and out["result"] == "partial"
    assert await _ip(db_session, "198.51.100.5") is None
    row = (await db_session.execute(select(IsoInsightLease))).scalar_one()
    assert row.match_status == "ambiguous" and row.subnet_id is None
    assert (await _src(db_session, src.id)).last_error_code == "SUBNET_UNMAPPED"


async def test_addresses_outside_the_allowed_scope_are_not_touched(db_session, srv, sf) -> None:
    allowed = await _net(db_session, "192.0.2.0/24")
    other = await _net(db_session, "203.0.113.0/24")
    db_session.add(IPAddress(subnet_id=other.id, ip="203.0.113.5", state="active"))
    src = await _source(db_session, srv, [allowed])
    _leases(srv, [_row("203.0.113.5"), _row("203.0.113.6", mac="02:00:5e:00:53:06")])
    out = await _sync(src, sf)
    assert out["unmatched"] == 2
    got = await _ip(db_session, "203.0.113.5")
    assert got.mac is None and got.in_dhcp_lease is False
    assert await _ip(db_session, "203.0.113.6") is None


async def test_a_subnet_of_another_tenant_is_excluded_at_sync_time(db_session, srv, sf) -> None:
    ca, cb = Customer(name=f"cust-a-{uuid.uuid4().hex[:4]}"), Customer(name=f"cust-b-{uuid.uuid4().hex[:4]}")
    db_session.add_all([ca, cb])
    await db_session.flush()
    mine = await _net(db_session, "192.0.2.0/24", customer=ca)
    theirs = await _net(db_session, "198.51.100.0/24", customer=cb)
    # 直接寫進資料庫（API 會擋；這裡模擬子網路事後被改到別的客戶名下）
    src = await _source(db_session, srv, [mine, theirs], customer_id=ca.id)
    _leases(srv, [_row("192.0.2.20"), _row("198.51.100.20", mac="02:00:5e:00:53:44")])
    out = await _sync(src, sf)
    assert out["created"] == 1 and out["unmatched"] == 1 and out["scope_excluded"] == 1
    assert await _ip(db_session, "198.51.100.20") is None


# ── 非破壞性 ────────────────────────────────────────────────────────────────

async def test_absent_or_empty_responses_do_not_release_active_leases(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21", name="pc-21")])
    await _sync(src, sf)
    _leases(srv, [])
    out = await _sync(src, sf)
    assert out["result"] == "success" and out["fetched"] == 0 and out["carried_over"] == 1
    ip = await _ip(db_session, "192.0.2.21")
    assert ip is not None and ip.in_dhcp_lease is True and ip.hostname == "pc-21"
    assert await _count(db_session, IsoInsightLease) == 1


async def test_suspicious_or_ipv6_observations_are_never_carried_over(db_session, srv, sf) -> None:
    """開始時間在未來（時鐘或資料異常）與 IPv6（待驗證）的觀察，時間過去之後也不會被沿用到 IP 記錄。"""
    sub = await _net(db_session)
    sub6 = await _net(db_session, "2001:db8::/64")
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.30", state="active"))
    db_session.add(IPAddress(subnet_id=sub6.id, ip="2001:db8::10", state="active"))
    src = await _source(db_session, srv, [sub, sub6])
    _leases(srv, [_row("192.0.2.30", mac="02:00:5e:00:53:30", start=1, end=5),
                  _row("2001:db8::10", mac="02:00:5e:00:53:36")])
    first = await _sync(src, sf)
    assert first["unknown_time"] == 1 and first["observed_only"] == 1
    _leases(srv, [])
    await _sync(src, sf, now=datetime.now(UTC) + timedelta(hours=2))
    for ip in ("192.0.2.30", "2001:db8::10"):
        got = await _ip(db_session, ip)
        assert got.in_dhcp_lease is False and got.mac is None, ip


async def test_time_based_expiry_drops_the_flag_but_keeps_the_ip(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21", start=-1, end=1)])
    await _sync(src, sf)
    _leases(srv, [])
    await _sync(src, sf, now=datetime.now(UTC) + timedelta(hours=2))
    ip = await _ip(db_session, "192.0.2.21")
    assert ip is not None and ip.in_dhcp_lease is False
    assert await _count(db_session, IsoInsightLease) == 1


async def test_a_failed_login_changes_nothing_and_holds_the_schedule(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21")])
    await _sync(src, sf)
    before = (await _count(db_session, IPAddress), await _count(db_session, IsoInsightLease))
    srv.route("POST", "/api/logon", (401, [], b""))
    out = await _sync(src, sf)
    assert out["result"] == "failed" and out["error_code"] == "AUTH_FAILED"
    assert (await _count(db_session, IPAddress), await _count(db_session, IsoInsightLease)) == before
    assert (await _ip(db_session, "192.0.2.21")).in_dhcp_lease is True
    s = await _src(db_session, src.id)
    assert s.auth_hold is True and s.last_error_code == "AUTH_FAILED" and s.last_full_success_at is not None
    assert PW not in (s.last_error or "")


async def test_invalid_structure_fails_without_touching_data(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    srv.route("GET", "/isosvc", json_resp({"dhcp_lease": None}))
    out = await _sync(src, sf)
    assert out["error_code"] == "INVALID_RESPONSE"
    assert await _count(db_session, IsoInsightLease) == 0


@pytest.mark.parametrize(("where", "boom", "code"), [
    ("parse_leases", RecursionError("nested too deep"), "INVALID_RESPONSE"),
    ("IsoClient.fetch_leases", ValueError("surprise"), "INTERNAL_ERROR"),
])
async def test_unexpected_errors_still_close_the_run(db_session, srv, sf, monkeypatch, where, boom, code) -> None:
    """意外的例外也要收成失敗記錄：不可以讓同步記錄永遠停在「執行中」、鎖也要放掉。"""
    from app.services.isoinsight import client as iso_client

    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21")])

    def raiser(*_a, **_k):
        raise boom

    async def araiser(*_a, **_k):
        raise boom
    if where == "parse_leases":
        monkeypatch.setattr(job, "parse_leases", raiser)
    else:
        monkeypatch.setattr(iso_client.IsoClient, "fetch_leases", araiser)
    out = await _sync(src, sf)
    assert out["result"] == "failed" and out["error_code"] == code
    run = (await db_session.execute(select(IsoInsightSyncRun))).scalar_one()
    assert run.result == "failed" and run.finished_at is not None and run.error_code == code
    assert (await _src(db_session, src.id)).running_since is None
    assert await _count(db_session, IsoInsightLease) == 0


async def test_a_disabled_source_is_skipped(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub], enabled=False)
    _leases(srv, [_row("192.0.2.21")])
    out = await _sync(src, sf)
    assert out["result"] == "skipped" and not srv.requests


async def test_write_failure_rolls_back_everything(db_session, srv, sf, monkeypatch) -> None:
    from app.services.isoinsight import reconcile
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21")])
    real = reconcile.apply

    async def boom(session, *a, **kw):
        await real(session, *a, **kw)          # 寫到一半（IP 已新增、觀察已寫）再失敗
        raise RuntimeError("disk full")
    monkeypatch.setattr(reconcile, "apply", boom)
    out = await _sync(src, sf)
    assert out["result"] == "failed" and out["error_code"] == "WRITE_FAILED"
    assert await _ip(db_session, "192.0.2.21") is None
    assert await _count(db_session, IsoInsightLease) == 0
    assert (await _src(db_session, src.id)).running_since is None


async def test_settings_changed_mid_run_are_not_committed(db_session, srv, sf, monkeypatch) -> None:
    from app.services.isoinsight import parser
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21")])
    real = parser.parse_leases

    def bump(*a, **kw):
        import asyncio
        from sqlalchemy import update

        async def _b():
            async with sf() as s:
                await s.execute(update(IsoInsightSource).where(IsoInsightSource.id == src.id)
                                .values(config_version=IsoInsightSource.config_version + 1))
                await s.commit()
        asyncio.get_running_loop().create_task(_b())
        return real(*a, **kw)
    monkeypatch.setattr(job, "parse_leases", bump)
    import asyncio
    out = await _sync(src, sf)
    await asyncio.sleep(0.2)
    if out["result"] == "success":                  # 排程任務太晚跑：重試一次確保真的測到
        out = await _sync(src, sf)
    assert out["error_code"] == "CONFIG_CHANGED"


async def test_deleting_the_source_keeps_ips_and_withdraws_its_evidence(client, auth_headers, db_session, srv,
                                                                        sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.21", mac="02:00:5e:00:53:21", name="pc-21")])
    await _sync(src, sf)
    r = await client.delete(f"/api/v1/isoinsight/sources/{src.id}", headers=auth_headers)
    assert r.status_code == 204, r.text
    ip = await _ip(db_session, "192.0.2.21")
    assert ip is not None                                 # 正式 IP 留著
    assert ip.in_dhcp_lease is False and ip.hostname is None
    assert await _count(db_session, IsoInsightLease) == 0
    assert await _count(db_session, DHCPLeaseSighting) == 0


# ── 鎖與排程 ────────────────────────────────────────────────────────────────

async def test_one_job_per_source(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    tok = await job.claim(sf, src.id)
    assert tok is not None
    with pytest.raises(IsoError) as ei:
        await _sync(src, sf)
    assert ei.value.spec_code == "SYNC_ALREADY_RUNNING"
    with pytest.raises(IsoError):
        await job.run_test(src.id, session_factory=sf, check=_allow)
    await job.release(sf, src.id, tok)
    _leases(srv, [])
    assert (await _sync(src, sf))["result"] == "success"


async def test_a_stale_lock_is_taken_over(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    src.running_since = datetime.now(UTC) - timedelta(hours=1)
    src.running_token = uuid.uuid4()
    await db_session.commit()
    _leases(srv, [])
    assert (await _sync(src, sf))["result"] == "success"


async def test_schedule_syncs_previewed_due_sources_and_reports_the_rest(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    now = datetime.now(UTC)
    _leases(srv, [])
    due = await _source(db_session, srv, [sub], name="due", schedule_enabled=True, preview_ok_at=now)
    await _source(db_session, srv, [sub], name="no-preview", schedule_enabled=True)
    await _source(db_session, srv, [sub], name="schedule-off", schedule_enabled=False, preview_ok_at=now)
    await _source(db_session, srv, [sub], name="held", schedule_enabled=True, preview_ok_at=now, auth_hold=True)
    await _source(db_session, srv, [sub], name="429", schedule_enabled=True, preview_ok_at=now,
                  retry_after_until=now + timedelta(minutes=10))
    await _source(db_session, srv, [sub], name="recent", schedule_enabled=True, preview_ok_at=now,
                  last_attempt_at=now - timedelta(seconds=60))
    res = await job.run_due(sf, now, check=_allow)
    assert [r["name"] for r in res] == ["due", "no-preview"]
    assert res[0]["result"] == "success" and res[0]["id"] == due.id
    # 沒預覽過：輪到了也不同步，結果寫明原因（不是靜靜略過），也不留同步記錄
    assert res[1]["result"] == "skipped" and res[1]["error_code"] == "PREVIEW_REQUIRED"
    run = (await db_session.execute(select(IsoInsightSyncRun))).scalar_one()
    assert run.trigger == "scheduled" and run.source_id == due.id
    held = (await db_session.execute(select(IsoInsightSource).where(IsoInsightSource.name == "no-preview"))).scalar_one()
    await db_session.refresh(held)
    assert held.last_result == "skipped" and held.last_error_code == "PREVIEW_REQUIRED"
    # 記下嘗試時間：下一分鐘不會再排到，照間隔來
    assert held.last_attempt_at is not None
    assert [r["name"] for r in await job.run_due(sf, now + timedelta(seconds=60), check=_allow)] == []


async def test_429_postpones_the_schedule(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub], schedule_enabled=True, preview_ok_at=datetime.now(UTC))
    srv.route("GET", "/isosvc", (429, [("Retry-After", "600")], b""))
    out = await _sync(src, sf)
    assert out["error_code"] == "RATE_LIMITED"
    s = await _src(db_session, src.id)
    assert s.retry_after_until is not None and s.retry_after_until > datetime.now(UTC) + timedelta(minutes=9)
    async with sf() as s2:
        assert await job.due_source_ids(s2, datetime.now(UTC) + timedelta(minutes=6)) == []


# ── 測試連線與預覽不寫入 ────────────────────────────────────────────────────

async def test_test_and_preview_never_write_ip_or_lease_data(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    db_session.add(IPAddress(subnet_id=sub.id, ip="192.0.2.20", state="active"))
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20"), _row("192.0.2.21", mac="02:00:5e:00:53:21")])
    t = await job.run_test(src.id, session_factory=sf, check=_allow)
    assert t["ok"] is True and [s["stage"] for s in t["stages"]] == ["login", "fetch", "validate"]
    p = await job.run_preview(src.id, session_factory=sf, check=_allow)
    assert p["ok"] is True and p["applied"] is False
    assert p["counts"]["created"] == 1 and p["counts"]["unchanged"] == 1
    assert {r["bucket"] for r in p["rows"]} == {"would_create", "would_apply"}
    assert await _ip(db_session, "192.0.2.21") is None
    assert (await _ip(db_session, "192.0.2.20")).mac is None
    assert await _count(db_session, IsoInsightLease) == 0
    s = await _src(db_session, src.id)
    assert s.preview_ok_at is not None and s.last_test_ok_at is not None and s.last_attempt_at is None
    assert await _count(db_session, IsoInsightSyncRun, IsoInsightSyncRun.kind.in_(["test", "preview"])) == 2


async def test_failed_test_reports_the_stage_without_secrets(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    srv.route("POST", "/api/logon", (405, [], b""))
    t = await job.run_test(src.id, session_factory=sf, check=_allow, anonymous=False)
    assert t["ok"] is False and t["error_code"] == "METHOD_UNSUPPORTED" and t["stage"] == "login"
    run = (await db_session.execute(select(IsoInsightSyncRun))).scalar_one()
    assert run.result == "failed" and PW not in repr(run.stages) + (run.error_detail or "")


async def test_anonymous_check_is_reported_separately(db_session, srv, sf) -> None:
    sub = await _net(db_session)
    src = await _source(db_session, srv, [sub])
    _leases(srv, [_row("192.0.2.20")])
    t = await job.run_test(src.id, session_factory=sf, check=_allow, anonymous=True)
    assert t["ok"] is True and t["anonymous"]["readable"] is True


# ── 大量 ───────────────────────────────────────────────────────────────────

async def test_twenty_thousand_leases_in_one_run(db_session, srv, sf) -> None:
    """RFC 5737 只有三個 /24，大量測試用 RFC 2544 的測試網段（198.18.0.0/15），MAC 用本地管理位元。"""
    import time
    sub = await _net(db_session, "198.18.0.0/15")
    src = await _source(db_session, srv, [sub])
    rows = [_row(f"198.18.{i // 250}.{i % 250 + 1}", mac=f"02:00:5e:10:{i // 256 % 256:02x}:{i % 256:02x}",
                 name=f"h-{i}") for i in range(20_000)]
    _leases(srv, rows)
    t0 = time.monotonic()
    out = await _sync(src, sf)
    assert out["created"] == 20_000, out
    second = await _sync(src, sf)
    assert second["unchanged"] == 20_000
    assert time.monotonic() - t0 < 300
