"""OCS Inventory NG 整合的解析與比對規則。

樣本取自 2026-09-18 對官方映像檔 2.10／2.11 的實機探測（見 project_ocs_integration_design）。
這個整合最容易出的錯都是**安靜的**：把虛擬網卡的假 MAC 灌進來、拿過期盤點蓋掉正確值、
把同一 MAC 的多台機器亂配、或替 OCS 自己建出髒 IP —— 所以規則要逐條釘住。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.services import ocs as svc

# ─────────────────── 純函式 ───────────────────


@pytest.mark.parametrize(("text", "expected"), [
    ("2.11.0", (2, 11, 0)), ("2.10", (2, 10)), ("2.12.5", (2, 12, 5)),
    ("", ()), (None, ()), ("garbage", ()),
])
def test_parse_version(text: str | None, expected: tuple[int, ...]) -> None:
    assert svc.parse_version(text) == expected


def test_usable_nics_drops_virtual_and_zero_mac() -> None:
    """VPN／docker0（VIRTUALDEV=1）與全零 MAC 不可以進比對 —— 否則假 MAC 灌一堆進來。

    樣本就是實測那台筆電：有線（Down 但有 MAC，要留）、Wi-Fi（Up）、VPN（虛擬、零 MAC）。
    """
    nics = [
        {"DESCRIPTION": "Ethernet", "MACADDR": "aa:bb:cc:00:00:07", "STATUS": "Down",
         "VIRTUALDEV": 0},
        {"DESCRIPTION": "Wi-Fi", "MACADDR": "aa:bb:cc:00:00:08", "STATUS": "Up",
         "VIRTUALDEV": 0},
        {"DESCRIPTION": "VPN adapter", "MACADDR": "00:00:00:00:00:00", "VIRTUALDEV": 1},
        {"DESCRIPTION": "empty", "MACADDR": "", "VIRTUALDEV": 0},
    ]
    macs = [n["MACADDR"] for n in svc.usable_nics(nics)]
    assert macs == ["aa:bb:cc:00:00:07", "aa:bb:cc:00:00:08"]
    assert svc.usable_nics(None) == []


def test_parse_os_prefers_comments_and_maps_family() -> None:
    g, f = svc.parse_os({"OSNAME": "Windows", "OSVERSION": "10.0.19045",
                         "OSCOMMENTS": "Windows 10 Pro"})
    assert g == "Windows 10 Pro" and f == "windows"
    g, f = svc.parse_os({"OSNAME": "Linux", "OSVERSION": "6.8.0",
                         "OSCOMMENTS": "Ubuntu 24.04.1 LTS"})
    assert g == "Ubuntu 24.04.1 LTS" and f == "linux"
    # 沒有 comments 時退回 OSNAME + OSVERSION
    g, f = svc.parse_os({"OSNAME": "Linux", "OSVERSION": "6.8.0", "OSCOMMENTS": ""})
    assert g == "Linux 6.8.0" and f == "linux"
    assert svc.parse_os({}) == (None, None)


def test_bios_asset_rejects_placeholder_junk() -> None:
    """主機板沒燒 DMI 時的佔位字串不可以當成序號 —— 那會製造一堆假資產。"""
    real = svc.bios_asset([{"SMANUFACTURER": "Dell Inc.", "SMODEL": "OptiPlex 7090",
                            "SSN": "SN-PC001"}])
    assert real == {"vendor": "Dell Inc.", "model": "OptiPlex 7090", "serial": "SN-PC001"}
    junk = svc.bios_asset([{"SMANUFACTURER": "System manufacturer",
                            "SMODEL": "To be filled by O.E.M.", "SSN": "Default string"}])
    assert junk == {"vendor": None, "model": None, "serial": None}
    assert svc.bios_asset([]) == {"vendor": None, "model": None, "serial": None}


def test_lastdate_and_staleness() -> None:
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    fresh = svc.lastdate_of({"LASTDATE": "2026-09-18 08:00:00"})
    assert fresh == datetime(2026, 9, 18, 8, 0, tzinfo=UTC)
    assert svc.is_stale(fresh, stale_after_days=30, now=now) is False
    old = svc.lastdate_of({"LASTDATE": "2025-08-01 08:00:00"})
    assert svc.is_stale(old, stale_after_days=30, now=now) is True
    assert svc.is_stale(None, stale_after_days=30, now=now) is True
    assert svc.lastdate_of({"LASTDATE": ""}) is None


def test_dedup_sections_drops_the_duplicate_software_key() -> None:
    """/computer/:id 把軟體放在 `software` 與空字串 key 各一份，只留具名的。"""
    got = svc.dedup_sections({"hardware": {"ID": 1}, "software": [1, 2], "": [1, 2]})
    assert "" not in got and got["software"] == [1, 2]


def test_decide_match_only_matches_never_guesses() -> None:
    """只比不建、多筆不猜（比照 Proxmox）。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    idx = {"aabbcc000001": [a], "aabbcc000002": [b, uuid.uuid4()]}
    assert svc.decide_match("aa:bb:cc:00:00:01", idx) == a       # 唯一 → 配
    assert svc.decide_match("aa:bb:cc:00:00:02", idx) is None     # 多筆 → 不猜
    assert svc.decide_match("de:ad:be:ef:00:00", idx) is None     # 查無 → 不建


def test_ocs_is_a_hostname_source() -> None:
    from app.models.ip_hostname import HOSTNAME_SOURCES
    assert "ocs" in HOSTNAME_SOURCES


def test_ocs_inventory_time_is_not_a_liveness_signal() -> None:
    """last_seen_ocs 是顯示用，**絕不可**進上線判定 —— 盤點時間不代表機器還活著。

    守法：effective_status 的計算（librenms.py）不可以參照 last_seen_ocs。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "librenms.py").read_text()
    # effective_status 那段若引用了 last_seen_ocs，就是把盤點時間當活性訊號了
    assert "last_seen_ocs" not in src


# ─────────────────── DB 比對（只比不建） ───────────────────

async def _mk_ip(session, ip_str: str, mac: str, *, device_id=None):
    """建一個掛在真實 section/subnet 底下的 IP（subnet_id 是 NOT NULL）。"""
    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name="t")
    session.add(sec)
    await session.flush()
    net = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    session.add(net)
    await session.flush()
    obj = IPAddress(subnet_id=net.id, ip=ip_str, mac=mac, device_id=device_id)
    session.add(obj)
    await session.flush()
    return obj


def _server(**kw: Any) -> Any:
    from types import SimpleNamespace
    base = {"id": uuid.uuid4(), "name": "ocs", "source_type": "rest",
            "base_url": "https://192.0.2.10", "sync_bios": True, "stale_after_days": 30}
    base.update(kw)
    return SimpleNamespace(**base)


def _computer(name: str, mac: str, *, os_comments: str = "Windows 10 Pro",
              lastdate: str = "2026-09-18 08:00:00", serial: str = "SN-X",
              extra_nics: list[dict] | None = None) -> dict[str, Any]:
    nics = [{"MACADDR": mac, "VIRTUALDEV": 0, "IPADDRESS": "198.51.100.41"}]
    nics += extra_nics or []
    return {
        "hardware": {"NAME": name, "OSNAME": "Windows", "OSVERSION": "10.0.19045",
                     "OSCOMMENTS": os_comments, "LASTDATE": lastdate},
        "networks": nics,
        "bios": [{"SMANUFACTURER": "Dell Inc.", "SMODEL": "OptiPlex 7090", "SSN": serial}],
    }


@pytest.mark.anyio
async def test_apply_matches_existing_ip_by_mac_and_fills_identity(db_session) -> None:
    from app.services.arp_precedence import normalize_mac
    ip = await _mk_ip(db_session, "198.51.100.41", "aa:bb:cc:00:00:41")
    idx = {normalize_mac("aa:bb:cc:00:00:41"): [ip.id]}

    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    res = await svc._apply_computer(
        db_session, _server(), _computer("host-107", "aa:bb:cc:00:00:41"), idx, now)
    await db_session.flush()

    assert res["matched"] == 1
    await db_session.refresh(ip)
    assert ip.os_guess == "Windows 10 Pro"
    assert ip.os_family == "windows"
    assert ip.last_seen_ocs == datetime(2026, 9, 18, 8, 0, tzinfo=UTC)


@pytest.mark.anyio
async def test_apply_never_creates_an_ip(db_session) -> None:
    """OCS 說有這個 MAC，但我們沒有對應的 IP → 什麼都不建。"""
    from app.models.address import IPAddress
    from sqlalchemy import func, select
    before = (await db_session.execute(select(func.count()).select_from(IPAddress))).scalar()
    res = await svc._apply_computer(
        db_session, _server(), _computer("ghost", "de:ad:be:ef:00:00"), {}, datetime.now(UTC))
    await db_session.flush()
    after = (await db_session.execute(select(func.count()).select_from(IPAddress))).scalar()
    assert res["matched"] == 0 and after == before


@pytest.mark.anyio
async def test_apply_skips_ambiguous_mac(db_session) -> None:
    """同一 MAC 對到兩個 IP（複製 VM／重疊網段）→ 一個都不動。"""
    from app.services.arp_precedence import normalize_mac
    a = await _mk_ip(db_session, "198.51.100.41", "aa:bb:cc:00:00:41")
    b = await _mk_ip(db_session, "198.51.100.42", "aa:bb:cc:00:00:41")
    idx = {normalize_mac("aa:bb:cc:00:00:41"): [a.id, b.id]}
    res = await svc._apply_computer(
        db_session, _server(), _computer("dup", "aa:bb:cc:00:00:41"), idx, datetime.now(UTC))
    await db_session.flush()
    assert res["matched"] == 0
    await db_session.refresh(a)
    assert a.os_guess is None and a.last_seen_ocs is None


@pytest.mark.anyio
async def test_stale_inventory_does_not_stamp_last_seen_or_overwrite_serial(db_session) -> None:
    """一年沒盤點的機器：主機名稱仍記（多源保存），但不 stamp 盤點時間、不動裝置序號。"""
    from app.models.device import Device
    from app.services.arp_precedence import normalize_mac
    dev = Device(name="host-old", serial="REAL-SERIAL")
    db_session.add(dev)
    await db_session.flush()
    ip = await _mk_ip(db_session, "198.51.100.99", "aa:bb:cc:00:00:99", device_id=dev.id)
    idx = {normalize_mac("aa:bb:cc:00:00:99"): [ip.id]}

    await svc._apply_computer(
        db_session, _server(),
        _computer("host-old", "aa:bb:cc:00:00:99", lastdate="2025-08-01 08:00:00",
                  serial="STALE-SERIAL"),
        idx, datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
    await db_session.flush()
    await db_session.refresh(ip)
    await db_session.refresh(dev)
    assert ip.last_seen_ocs is None, "過期盤點不該 stamp 成現在還看得到"
    assert dev.serial == "REAL-SERIAL", "過期盤點不該蓋掉裝置既有序號"


@pytest.mark.anyio
async def test_serial_fills_only_empty_device_fields(db_session) -> None:
    """新鮮盤點會補**空的**裝置欄位，但不覆寫已填的。"""
    from app.models.device import Device
    from app.services.arp_precedence import normalize_mac
    dev = Device(name="host-107", vendor="Acme")     # vendor 已填、serial/model 空
    db_session.add(dev)
    await db_session.flush()
    ip = await _mk_ip(db_session, "198.51.100.41", "aa:bb:cc:00:00:41", device_id=dev.id)
    idx = {normalize_mac("aa:bb:cc:00:00:41"): [ip.id]}

    await svc._apply_computer(
        db_session, _server(), _computer("host-107", "aa:bb:cc:00:00:41", serial="SN-PC001"),
        idx, datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
    await db_session.flush()
    await db_session.refresh(dev)
    assert dev.serial == "SN-PC001" and dev.model == "OptiPlex 7090"
    assert dev.vendor == "Acme", "已填的 vendor 不可被覆寫"


@pytest.mark.anyio
async def test_ocs_hostname_does_not_override_a_manual_one(db_session) -> None:
    """人工指定的主機名稱不被 OCS 默默覆蓋（apply_observation 走多源優先序）。"""
    from app.services import hostname as hn
    from app.services.arp_precedence import normalize_mac
    ip = await _mk_ip(db_session, "198.51.100.41", "aa:bb:cc:00:00:41")
    await hn.apply_observation(db_session, ip=ip, source="manual", hostname="the-real-name")
    await db_session.flush()

    idx = {normalize_mac("aa:bb:cc:00:00:41"): [ip.id]}
    await svc._apply_computer(
        db_session, _server(), _computer("ocs-name", "aa:bb:cc:00:00:41"),
        idx, datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
    await db_session.flush()
    await db_session.refresh(ip)
    assert ip.hostname == "the-real-name", "OCS 不可壓過人工名稱"


# ─────────────────── API（CRUD、密碼永不回傳） ───────────────────

@pytest.mark.anyio
async def test_crud_roundtrip_and_password_never_returned(client, auth_headers) -> None:
    # 建立（不帶密碼 —— OCS 預設無驗證）
    r = await client.post("/api/v1/ocs", headers=auth_headers, json={
        "name": "ocs-a", "base_url": "https://192.0.2.10"})
    assert r.status_code == 201, r.text
    body = r.json()
    sid = body["id"]
    assert body["has_password"] is False
    assert body["sync_software"] is False, "軟體區段預設必須是關的"
    assert "api_password" not in body and "api_password_enc" not in body

    # 設密碼 → has_password 變 True，但明文不回
    r = await client.patch(f"/api/v1/ocs/{sid}", headers=auth_headers,
                           json={"api_username": "svc", "api_password": "s3cret-XYZ"})
    assert r.status_code == 200
    body = r.json()
    assert body["has_password"] is True and body["api_username"] == "svc"
    assert "s3cret-XYZ" not in r.text

    # 清單也不外洩密碼
    r = await client.get("/api/v1/ocs", headers=auth_headers)
    assert r.status_code == 200 and "s3cret-XYZ" not in r.text
    assert any(it["id"] == sid for it in r.json()["items"])

    # 清掉憑證（改回無驗證）
    r = await client.patch(f"/api/v1/ocs/{sid}", headers=auth_headers,
                           json={"clear_credentials": True})
    assert r.status_code == 200 and r.json()["has_password"] is False

    # 刪除
    r = await client.delete(f"/api/v1/ocs/{sid}", headers=auth_headers)
    assert r.status_code == 204


@pytest.mark.anyio
async def test_duplicate_name_is_409(client, auth_headers) -> None:
    p = {"name": "ocs-dup", "base_url": "https://192.0.2.11"}
    assert (await client.post("/api/v1/ocs", headers=auth_headers, json=p)).status_code == 201
    assert (await client.post("/api/v1/ocs", headers=auth_headers, json=p)).status_code == 409


@pytest.mark.anyio
async def test_stored_password_decrypts_back(client, auth_headers, db_session) -> None:
    """AAD 綁 id 的加密要能解回原文（換 id 就解不開，AAD 有守住）。"""
    from app.models.ocs import OcsServer
    from app.services import ocs as svc
    r = await client.post("/api/v1/ocs", headers=auth_headers, json={
        "name": "ocs-crypt", "base_url": "https://192.0.2.12",
        "api_username": "u", "api_password": "round-trip-42"})
    sid = r.json()["id"]
    obj = await db_session.get(OcsServer, uuid.UUID(sid))
    assert svc._auth(obj) == ("u", "round-trip-42")


# ─────────────────── HTTP 呼叫路徑（簽名回歸） ───────────────────

class _FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)


@pytest.mark.anyio
async def test_diagnose_uses_the_real_safe_request_signature(monkeypatch) -> None:
    """回歸：`safe_request` 沒有 `auth=`、client 是關鍵字參數。

    原本 diagnose/sync 用了 `safe_request(client, "GET", url, auth=...)` —— 簽名全錯，
    每次呼叫都丟 TypeError，而 diagnose 又把它吞成「連不到這套 OCS」。單元測試當時只測
    純函式與 `_apply_computer`，完全沒碰 HTTP 路徑，所以沒抓到（2026-09-18 實機才爆）。
    這個測試用假的 safe_request 跑一次 diagnose，記下每次呼叫的參數並檢查簽名。
    """
    from types import SimpleNamespace

    import app.services.ocs as ocs

    calls: list[dict] = []

    class _C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *e):
            return False

    def fake_client(*a, **k):
        return _C()

    async def fake_request(method, url, *, client=None, headers=None, verify=True, **extra):
        calls.append({"method": method, "url": url, "headers": headers or {},
                      "verify": verify, "extra": extra})
        # listID / lastupdate 都回 200 + 空陣列
        return _FakeResp(200, [])

    monkeypatch.setattr(ocs, "safe_client", fake_client)
    monkeypatch.setattr(ocs, "safe_request", fake_request)

    srv = SimpleNamespace(
        id=uuid.uuid4(), name="ocs", source_type="rest",
        base_url="https://192.0.2.10", verify_tls=False,
        api_username="jtipam", api_password_enc=None, api_password_nonce=None)
    out = await ocs.diagnose(srv)

    assert out["reachable"] is True
    assert calls, "diagnose 沒有真的發出任何請求"
    for c in calls:
        assert c["method"] == "GET"
        assert "auth" not in c["extra"], "又用了 safe_request 不支援的 auth= 參數"
    # 有帶帳號時，帶認證的請求要有 Authorization 標頭
    assert any("Authorization" in c["headers"] for c in calls)


@pytest.mark.anyio
async def test_full_sync_pages_and_applies(monkeypatch, db_session) -> None:
    """端到端（假 HTTP）：沒有 lastupdate → 全量分頁 → 比對既有 IP。"""
    import app.services.ocs as ocs

    ip = await _mk_ip(db_session, "198.51.100.41", "aa:bb:cc:00:00:41")

    page = {"1": {"hardware": {"NAME": "host-x", "OSNAME": "Linux",
                              "OSCOMMENTS": "Ubuntu 24.04 LTS", "LASTDATE": "2026-09-18 08:00:00"},
                  "networks": [{"MACADDR": "aa:bb:cc:00:00:41", "VIRTUALDEV": 0}],
                  "bios": [{"SSN": "SN-1", "SMODEL": "M", "SMANUFACTURER": "V"}]}}

    class _C2:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False

    def fake_client(*a, **k):
        return _C2()

    async def fake_request(method, url, *, client=None, headers=None, verify=True, **extra):
        if "lastupdate" in url:
            return _FakeResp(404, None)                 # 這台沒有增量
        if "start=0" in url:
            return _FakeResp(200, page)
        return _FakeResp(200, {})                       # 第二頁空 → 停

    monkeypatch.setattr(ocs, "safe_client", fake_client)
    monkeypatch.setattr(ocs, "safe_request", fake_request)

    from types import SimpleNamespace
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="ocs", source_type="rest", base_url="https://192.0.2.10",
        verify_tls=False, api_username=None, api_password_enc=None, api_password_nonce=None,
        sync_bios=True, stale_after_days=30, last_incremental_epoch=None,
        detected_version=None, last_sync_at=None, last_success_at=None,
        last_error="x", last_cost=None)
    summary = await ocs.sync_instance(db_session, srv)
    await db_session.flush()

    assert summary["computers"] == 1 and summary["matched_ips"] == 1
    assert summary["mode"] == "full"
    await db_session.refresh(ip)
    assert ip.os_guess == "Ubuntu 24.04 LTS"
    assert ip.last_seen_ocs is not None
