"""戰情室 10 IP 可用性追蹤（2026-10-23）。

守的是規格 §7：
1. 追蹤清單沿用儀表板卡片（user_preferences.pinned["uptime_ips"]），各帳號各自的清單；顯示權杖用代表帳號的
2. 只列看得到、在顯示範圍內的 IP；其餘只給數量
3. 沒有資料是未知，不是上線；格內有中斷不會被蓋掉（mixed）；最後觀察不無限延伸
4. 反覆中斷只算確定的上線↔離線切換（未知不算）
5. 可用率要有有效時長才有數字（否則 None＝「—」），另給資料覆蓋率
6. 只提供歷史涵蓋得到的時間範圍
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.address import IPAddress
from app.models.ip_change_log import IPChangeLog
from app.models.ip_liveness import IPLivenessDay
from app.models.permission import Permission
from app.models.scan_agent import ScanAgent
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User, UserPreference
from app.services.warroom.availability import (
    FLAP_MIN,
    Seg,
    apply_observations,
    build_timeline,
    cap_tail,
    cell_state,
    choose_window,
    confirmed_flip,
    durations,
    pct,
    unknown_before,
)

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC).replace(microsecond=0)
H = timedelta(hours=1)


# ───────────────────────────── 純函式 ─────────────────────────────

def test_short_outage_inside_a_cell_is_not_hidden() -> None:
    start = NOW - 6 * H
    segs = build_timeline(start, NOW, "online (scanner)",
                          [(start + 2 * H, "offline"), (start + 2 * H + timedelta(minutes=10), "online (scanner)")])
    assert [s.state for s in segs] == ["online", "offline", "online"]
    assert cell_state(segs, start, NOW, NOW) == "mixed"           # 不只取最後一次（上線）
    assert cell_state(segs, start, start + H, NOW) == "online"


def test_offline_with_unknown_is_offline_and_thin_online_is_unknown() -> None:
    start = NOW - 6 * H
    segs = [Seg(start, start + H, "offline"), Seg(start + H, NOW, "unknown")]
    assert cell_state(segs, start, NOW, NOW) == "offline"
    segs = [Seg(start, start + 2 * H, "online"), Seg(start + 2 * H, NOW, "unknown")]
    assert cell_state(segs, start, NOW, NOW) == "unknown"         # 上線不到一半：不畫成上線
    segs = [Seg(start, start + 4 * H, "online"), Seg(start + 4 * H, NOW, "unknown")]
    assert cell_state(segs, start, NOW, NOW) == "online"
    assert cell_state(segs, NOW, NOW + H, NOW) == "future"


def test_no_data_is_unknown_never_up() -> None:
    start = NOW - 24 * H
    segs = build_timeline(start, NOW, None, [])
    assert [s.state for s in segs] == ["unknown"]
    assert cell_state(segs, start, NOW, NOW) == "unknown"
    d = durations(segs, start, NOW)
    assert pct(d["online"], d["online"] + d["offline"]) is None   # 沒有有效時長：「—」
    assert build_timeline(start, NOW, "online (arp)", [])[0].state == "unknown"   # ARP 不會過期，不算上線


def test_daily_observations_veto_unproven_states() -> None:
    today = NOW.date()
    day0 = datetime.combine(today - timedelta(days=2), datetime.min.time(), tzinfo=UTC)
    segs = [Seg(day0, day0 + 72 * H, "online", "online (scanner)")]
    obs = {today - timedelta(days=2): (True, False), today: (False, False)}   # 昨天沒有記錄
    out = apply_observations(segs, obs, observed_from=today - timedelta(days=2))
    states = [(s.t0.date(), s.state) for s in out]
    assert states == [(today - timedelta(days=2), "online"), (today - timedelta(days=1), "unknown")]
    # 觀測開始之前：狀態宣稱的來源已經不在 → 不延續
    out = apply_observations(segs, {}, observed_from=None, carry=lambda raw: False)
    assert {s.state for s in out} == {"unknown"}


def test_before_creation_is_unknown() -> None:
    start = NOW - 10 * H
    segs = unknown_before([Seg(start, NOW, "online")], NOW - 4 * H)
    assert [(s.state, s.t1) for s in segs] == [("unknown", NOW - 4 * H), ("online", NOW)]


def test_last_observation_does_not_extend_forever() -> None:
    start = NOW - 10 * H
    segs = [Seg(start, NOW - 5 * H, "offline"), Seg(NOW - 5 * H, NOW, "online")]
    out = cap_tail(segs, {"online": NOW - 3 * H, "offline": None})
    assert [(s.state, s.t0) for s in out] == [("offline", start), ("online", NOW - 5 * H), ("unknown", NOW - 3 * H)]
    # 離線但沒有任何探測來源在運作：從轉為離線起就是未知（來源故障不是離線）
    out = cap_tail([Seg(start, NOW - 2 * H, "online"), Seg(NOW - 2 * H, NOW, "offline")],
                   {"online": None, "offline": None})
    assert out[-1].state == "unknown" and out[-1].t0 == NOW - 2 * H
    # 探測來源還在：離線維持
    out = cap_tail([Seg(start, NOW, "offline")], {"offline": NOW + H})
    assert out[-1].state == "offline"


def test_unknown_never_counts_as_a_flip() -> None:
    assert confirmed_flip("online (scanner)", "offline")
    assert confirmed_flip("offline", "online")
    assert not confirmed_flip("online (scanner)", "unknown")
    assert not confirmed_flip("unknown", "online")
    assert not confirmed_flip("online (arp)", "offline")         # ARP 撐著的上線不是確定狀態
    assert not confirmed_flip("online", "online (scanner)")


def test_only_windows_covered_by_history() -> None:
    win, sup = choose_window("7d", NOW - timedelta(days=3), NOW)
    assert win.key == "24h" and sup == ["24h"]
    win, sup = choose_window("7d", NOW - timedelta(days=8), NOW)
    assert win.key == "7d" and sup == ["24h", "7d"]
    win, sup = choose_window("90d", NOW - timedelta(days=40), NOW)
    assert win.key == "30d" and sup == ["24h", "7d", "30d"]
    win, _ = choose_window("7d", None, NOW)
    assert win.key == "24h"


# ───────────────────────────── 端點 ─────────────────────────────

@pytest.fixture(autouse=True)
def _no_cache():
    from app.api.v1.endpoints import warroom
    warroom._cache.clear()
    yield
    warroom._cache.clear()


def _hdr(user: User) -> dict[str, str]:
    from app.services.auth import issue_access_token
    return {"Authorization": f"Bearer {issue_access_token(user)}"}


async def _user(db, admin: bool = False) -> User:
    from app.core.security import hash_password
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"u-{uuid.uuid4().hex[:8]}@test.local",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=admin)
    db.add(u)
    await db.commit()
    return u


async def _pin(db, user: User, ips: list[IPAddress]) -> None:
    pref = await db.get(UserPreference, user.id)
    if pref is None:
        pref = UserPreference(user_id=user.id)
        db.add(pref)
    pref.pinned = {"uptime_ips": [str(i.id) for i in ips], "racks": ["not-related"]}
    await db.commit()


def _change(ip: IPAddress, at: datetime, old: str | None, new: str) -> IPChangeLog:
    return IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), field="effective_status",
                       event_type="online" if new.startswith("online") else "offline",
                       old_value=old, new_value=new, source="system", created_at=at)


def _days(ip: IPAddress, n: int, *, up: bool = True, down: bool = False) -> list[IPLivenessDay]:
    today: date = datetime.now(UTC).date()
    return [IPLivenessDay(ip_id=ip.id, day=today - timedelta(days=k), up=up, down=down) for k in range(n)]


async def _site(db, *, agent: bool = True) -> dict:
    sec = Section(name=f"av-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    ag = None
    if agent:
        ag = ScanAgent(name=f"agent-{uuid.uuid4().hex[:6]}", enroll_key_hash=uuid.uuid4().hex * 2, enabled=True,
                       last_seen_at=datetime.now(UTC))
        db.add(ag)
        await db.flush()
    lan = Subnet(section_id=sec.id, cidr="10.70.0.0/24", description="Service LAN",
                 scan_agent_id=ag.id if ag else None)
    other = Subnet(section_id=sec.id, cidr="10.71.0.0/24", description="Other")
    db.add_all([lan, other])
    await db.flush()
    now = datetime.now(UTC)
    created = now - timedelta(days=10)
    steady = IPAddress(subnet_id=lan.id, ip="10.70.0.3", hostname="dns-primary.example.internal",
                       effective_status="online (scanner)", last_seen_scanner=now - timedelta(minutes=2),
                       created_at=created)
    down = IPAddress(subnet_id=lan.id, ip="10.70.0.119", hostname="mail2", effective_status="offline",
                     last_seen_scanner=now - 2 * H, created_at=created)
    flappy = IPAddress(subnet_id=lan.id, ip="10.70.0.109", hostname="flappy-host",
                       effective_status="online (scanner)", last_seen_scanner=now - timedelta(minutes=1),
                       created_at=created)
    silent = IPAddress(subnet_id=lan.id, ip="10.70.0.50", hostname=None, effective_status=None, created_at=created)
    hidden = IPAddress(subnet_id=other.id, ip="10.71.0.9", hostname="elsewhere", effective_status="online (scanner)",
                       last_seen_scanner=now, created_at=created)
    db.add_all([steady, down, flappy, silent, hidden])
    await db.flush()
    db.add_all([*_days(steady, 10), *_days(down, 10, down=True), *_days(flappy, 10, down=True),
                *_days(silent, 10, up=False)])
    db.add(_change(down, now - 3 * H, "online (scanner)", "offline"))
    # flappy：最近 24 小時 4 次確定切換＋一次經過未知的（不算）
    t = now - 20 * H
    for k, (old, new) in enumerate([("online (scanner)", "offline"), ("offline", "online (scanner)"),
                                    ("online (scanner)", "offline"), ("offline", "online (scanner)"),
                                    ("online (scanner)", "unknown")]):
        db.add(_change(flappy, t + k * H, old, new))
    db.add(_change(flappy, t + 6 * H, "unknown", "online (scanner)"))
    await db.commit()
    return {"sec": sec, "agent": ag, "lan": lan, "other": other, "steady": steady, "down": down,
            "flappy": flappy, "silent": silent, "hidden": hidden}


async def _page(client, headers, page: int = 1) -> dict:
    r = await client.get(f"/api/v1/warroom/pages/availability?page={page}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_tracked_list_is_the_dashboard_list_of_each_account(client, admin_user, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await _pin(db_session, admin_user, [s["steady"], s["down"], s["flappy"], s["silent"]])
    other = await _user(db_session, admin=True)
    await _pin(db_session, other, [s["hidden"]])
    d = await _page(client, auth_headers)
    assert d["metrics"] == {"tracked": 4, "down_now": 1, "flapping": 1}
    rows = {r["ip"]: r for r in d["rows"]}
    assert set(rows) == {"10.70.0.3", "10.70.0.119", "10.70.0.109", "10.70.0.50"}
    assert [r["ip"] for r in d["rows"]][:2] == ["10.70.0.119", "10.70.0.109"]   # 失聯、反覆中斷在前
    assert rows["10.70.0.119"]["state"] == "offline"
    assert rows["10.70.0.109"]["flapping"] is True and rows["10.70.0.109"]["flips"] == FLAP_MIN
    assert rows["10.70.0.3"]["state"] == "online"
    assert rows["10.70.0.3"]["name"] == "dns-primary.example.internal"
    # 沒有任何資料：未知、可用率「—」，不是上線也不是 0%
    silent = rows["10.70.0.50"]
    assert silent["state"] == "unknown"
    assert set(silent["cells"]) <= {"unknown", "future"}
    assert silent["availability"] is None
    assert silent["coverage"] in (None, 0.0)
    # 有資料的：可用率與覆蓋率都有
    steady = rows["10.70.0.3"]
    assert steady["availability"] == 100.0
    assert steady["coverage"] and steady["coverage"] > 50
    assert "online" in steady["cells"] and "offline" not in steady["cells"]
    assert "mixed" in rows["10.70.0.109"]["cells"] or "offline" in rows["10.70.0.109"]["cells"]
    kinds = [(e["kind"], e["ip"]) for e in d["events"]]
    assert ("flapping", "10.70.0.109") in kinds
    assert ("offline", "10.70.0.119") in kinds
    assert all(e["ip"] != "10.70.0.109" or e["kind"] == "flapping" for e in d["events"])   # 反覆中斷合成一筆
    # 另一個帳號只看到自己的清單
    d2 = await _page(client, _hdr(other))
    assert [r["ip"] for r in d2["rows"]] == ["10.71.0.9"]


async def test_display_token_uses_the_principals_list(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    viewer = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["lan"].id, principal_type="user",
                              principal_id=viewer.id, level="read"))
    await _pin(db_session, viewer, [s["steady"], s["hidden"]])
    tok = (await client.post("/api/v1/system/warroom/token", json={"days": 30}, headers=auth_headers)).json()["token"]
    r = await client.put("/api/v1/system/warroom", headers=auth_headers,
                         json={"display_enabled": True, "principal_user_id": str(viewer.id)})
    assert r.status_code == 200, r.text
    d = await _page(client, {"X-Warroom-Token": tok})
    assert [r["ip"] for r in d["rows"]] == ["10.70.0.3"]          # hidden 的子網路代表帳號看不到
    assert d["body"]["excluded"] == 1                              # 只給數量，不給內容


async def test_display_scope_limits_tracked_ips(client, admin_user, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await _pin(db_session, admin_user, [s["steady"], s["hidden"]])
    await client.put("/api/v1/system/warroom", headers=auth_headers, json={"scope": {"subnets": [str(s["other"].id)]}})
    d = await _page(client, auth_headers)
    assert [r["ip"] for r in d["rows"]] == ["10.71.0.9"]
    assert d["body"]["excluded"] == 1


async def test_offline_without_a_running_probe_is_unknown(client, admin_user, auth_headers, db_session) -> None:
    s = await _site(db_session, agent=False)
    await _pin(db_session, admin_user, [s["down"]])
    d = await _page(client, auth_headers)
    row = d["rows"][0]
    assert row["state"] == "unknown"                               # 沒有在運作的探測來源：不判離線
    assert d["metrics"]["down_now"] == 0


async def test_empty_list_is_empty_state(client, auth_headers) -> None:
    d = await _page(client, auth_headers)
    assert d["body"]["state"] == "empty"
    assert d["metrics"] == {"tracked": 0, "down_now": 0, "flapping": 0}
    assert d["rows"] == [] and d["events"] == []


async def test_window_reports_supported_ranges(client, admin_user, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await _pin(db_session, admin_user, [s["steady"]])
    d = await _page(client, auth_headers)
    w = d["window"]
    assert w["key"] == "7d"                                        # 逐日觀測有 10 天：7 日涵蓋得到
    assert w["supported"] == ["24h", "7d"]
    assert w["cells"] == 28 and w["step_s"] == 6 * 3600
    assert len(d["rows"][0]["cells"]) == 28
    assert sum(c["span"] for c in w["columns"]) == 28
    assert d["last_valid_at"] is not None
