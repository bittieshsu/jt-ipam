"""戰情室 11 AI 巡檢結果（2026-10-23）。

守的是（規格 SPEC 第 8 節）：
1. 只讀已保存的結果：建這一頁絕不呼叫 LLM、不開始巡檢、不動巡檢狀態
2. 巡檢狀態分得清楚：未執行、進行中（顯示上次完成的結果並標明）、完成、部分完成、失敗；
   失敗與未執行不能看起來像「沒有發現」（指標是「—」不是 0）
3. 同一件事（同指紋）跨巡檢只算一次
4. 多租戶：發現點名的 IP／裝置觀看者看不到就不列；對不到記錄的發現、以及模型敘述可能夾著
   觀看者看不到的資料的發現，只給完整權限的帳號看，其餘只說「有發現未顯示」、不帶數量
5. 巡檢結束時記下確定性的結果（狀態、錯誤代碼、取樣範圍），不記錯誤原文
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.ai_finding import AIFinding
from app.models.device import Device
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.services import ai_audit as aa
from app.services import system_config as sc
from app.services.warroom import ai_audit as wa
from app.services.warroom import config as wcfg

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_cache():
    from app.api.v1.endpoints import warroom
    warroom._cache.clear()
    yield
    warroom._cache.clear()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """建這一頁絕對不可以碰 LLM 或開始巡檢：任何一個被呼叫就記下來（測試最後檢查）。"""
    from app.services import ai as ai_mod
    calls: list[str] = []

    def _trap(name: str):  # type: ignore[no-untyped-def]
        async def _f(*a, **kw):  # type: ignore[no-untyped-def]
            calls.append(name)
            raise AssertionError(f"{name} must not be called by the war room")
        return _f

    for name in ("raw_chat", "chat", "_post_chat", "_chat_stream", "embed"):
        if hasattr(ai_mod, name):
            monkeypatch.setattr(ai_mod, name, _trap(name))
    monkeypatch.setattr(aa, "run_audit", _trap("run_audit"))
    monkeypatch.setattr(aa, "_analyze", _trap("_analyze"))
    yield calls
    assert calls == [], f"war room builder called {calls}"


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


async def _site(db) -> dict:
    """兩個租戶的子網路，各一筆 IP；另有一筆 IP 在沒有勾 AI 巡檢的子網路。"""
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    a = Subnet(section_id=sec.id, cidr="192.0.2.0/24", ai_audit_enabled=True)
    b = Subnet(section_id=sec.id, cidr="198.51.100.0/24", ai_audit_enabled=True)
    db.add_all([a, b])
    await db.flush()
    now = datetime.now(UTC)
    ip_a = IPAddress(subnet_id=a.id, ip="192.0.2.58", hostname="finance-app-02.tenant-a.example.com",
                     last_seen_scanner=now - timedelta(hours=2), mac="00:11:22:33:44:55", mac_source="arp",
                     last_seen_arp=now - timedelta(hours=1))
    ip_b = IPAddress(subnet_id=b.id, ip="198.51.100.7", hostname="db-01.tenant-b.example.com")
    db.add_all([ip_a, ip_b])
    dev = Device(name="core-sw-tenant-a")
    db.add(dev)
    await db.commit()
    return {"a": a, "b": b, "ip_a": ip_a, "ip_b": ip_b, "dev": dev}


async def _finding(db, ips: list[str], *, severity: str = "medium", category: str = "conflict",
                   title: str = "同一 IP 出現不同 MAC", status: str = "open", run_id: uuid.UUID | None = None,
                   devices: list[str] | None = None, fp: str | None = None, note: str = "ARP 與 DHCP 的 MAC 不同") -> AIFinding:
    ev: dict = {"ips": ips, "note": note}
    if devices:
        ev["devices"] = devices
    item = {"category": category, "title": title, "evidence": ev}
    f = AIFinding(run_id=run_id or uuid.uuid4(), severity=severity, category=category, title=title,
                  detail="模型判讀的敘述", recommendation="**請確認**是否更換過網卡或租約尚未清除",
                  evidence=ev, fingerprint=fp or aa.fingerprint(item), status=status, model="gemma4:26b")
    db.add(f)
    await db.commit()
    return f


async def _completed(db, *, run_id: uuid.UUID, status: str = "completed", minutes_ago: int = 30,
                     scope: dict | None = None) -> None:
    """模擬一次已經結束、有結果的巡檢（跟 ai_audit._finish 寫的是同一組欄位）。"""
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    await sc.mark_ai_audit_started(db, at=at - timedelta(minutes=10))
    await sc.mark_ai_audit_finished(db, at=at, outcome={
        "status": status, "error_code": None if status == "completed" else "some_batches_failed",
        "run_id": str(run_id), "started_at": (at - timedelta(minutes=10)).isoformat(),
        "batches": 3, "failed_batches": 0 if status == "completed" else 1,
        "scope_counts": {"subnets": len((scope or {}).get("subnet_ids") or []),
                         "ips": len((scope or {}).get("ip_ids") or []), "devices": 0,
                         "capped": {"ips": False, "subnets": False, "devices": False}}})
    if scope is not None:
        await sc.remember_ai_audit_run_scope(db, run_id=str(run_id), at=at, scope=scope)


async def _build(db, user: User, page: int = 1) -> dict:
    cfg = await wcfg.get_warroom(db)
    return await wa.build(db, user=user, cfg=cfg, page=page)


def _ips(d: dict) -> list[str]:
    return [s.get("ip") for it in d["findings"].get("items", []) for s in it["subjects"] if s.get("ip")]


# ─────────────────────────── 巡檢狀態 ───────────────────────────

async def test_not_run_is_not_no_findings(db_session) -> None:
    admin = await _user(db_session, admin=True)
    d = await _build(db_session, admin)
    assert d["key"] == "ai_audit"
    assert d["run"]["status"] == "not_run"
    assert d["findings"]["state"] == "unavailable"        # 不是 empty（「沒有發現」）
    assert d["metrics"] == {"high": "—", "open": "—", "insufficient": "—"}
    assert d["run"]["schedule"]["enabled"] is False
    assert d["run"]["schedule"]["next_at"] is None       # 沒有排程就沒有下次時間


async def test_failed_without_previous_result_is_not_no_findings(db_session) -> None:
    admin = await _user(db_session, admin=True)
    at = datetime.now(UTC) - timedelta(minutes=5)
    await sc.mark_ai_audit_started(db_session, at=at)
    await sc.mark_ai_audit_finished(db_session, at=at, outcome={
        "status": "failed", "error_code": "llm_error", "run_id": str(uuid.uuid4()),
        "started_at": at.isoformat(), "batches": 2, "failed_batches": 2, "scope_counts": None})
    d = await _build(db_session, admin)
    assert d["run"]["status"] == "failed"
    assert d["run"]["error_code"] == "llm_error"
    assert d["run"]["completed_at"] is None
    assert d["findings"]["state"] == "unavailable"
    assert d["metrics"]["open"] == "—"


async def test_failed_keeps_showing_the_last_completed_result(db_session) -> None:
    s = await _site(db_session)
    admin = await _user(db_session, admin=True)
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], severity="high", run_id=rid)
    await _completed(db_session, run_id=rid, minutes_ago=120)
    at = datetime.now(UTC) - timedelta(minutes=5)
    await sc.mark_ai_audit_started(db_session, at=at)
    await sc.mark_ai_audit_finished(db_session, at=at, outcome={
        "status": "failed", "error_code": "parse_error", "run_id": str(uuid.uuid4()),
        "started_at": at.isoformat(), "batches": 1, "failed_batches": 1, "scope_counts": None})
    d = await _build(db_session, admin)
    assert d["run"]["status"] == "failed"
    assert d["run"]["completed_at"] is not None                 # 顯示的是之前完成的那次
    assert d["findings"]["state"] == "ok"
    assert d["metrics"] == {"high": 1, "open": 1, "insufficient": 0}
    assert _ips(d) == [str(s["ip_a"].ip)]


async def test_running_shows_the_last_completed_result_labelled(db_session) -> None:
    await _site(db_session)
    admin = await _user(db_session, admin=True)
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], run_id=rid)
    await _completed(db_session, run_id=rid, minutes_ago=90)
    await sc.mark_ai_audit_started(db_session, at=datetime.now(UTC) - timedelta(minutes=2))
    d = await _build(db_session, admin)
    assert d["run"]["status"] == "running"
    assert d["run"]["running_since"] is not None
    assert d["run"]["completed_at"] is not None
    assert d["run"]["completed_status"] == "completed"
    assert d["findings"]["state"] == "ok" and d["metrics"]["open"] == 1


async def test_partial_and_completed_empty(db_session) -> None:
    admin = await _user(db_session, admin=True)
    await _completed(db_session, run_id=uuid.uuid4(), status="partial")
    d = await _build(db_session, admin)
    assert d["run"]["status"] == "partial"
    assert d["findings"]["state"] == "empty"                    # 完成（部分）且確實沒有發現
    assert d["metrics"] == {"high": 0, "open": 0, "insufficient": 0}
    assert any(g["code"] == "partial_run" for g in d["gaps"])


async def test_schedule_next_run_only_when_enabled(db_session) -> None:
    admin = await _user(db_session, admin=True)
    await sc.set_llm_config(db_session, enabled=True, ai_audit_enabled=True, ai_audit_times=["03:30"])
    await _completed(db_session, run_id=uuid.uuid4(), minutes_ago=5)
    d = await _build(db_session, admin)
    sch = d["run"]["schedule"]
    assert sch["enabled"] is True
    assert sch["next_at"] and datetime.fromisoformat(sch["next_at"]) > datetime.now(UTC)


# ─────────────────────────── 去重 ───────────────────────────

async def test_same_fingerprint_across_runs_counted_once(db_session) -> None:
    await _site(db_session)
    admin = await _user(db_session, admin=True)
    r1, r2 = uuid.uuid4(), uuid.uuid4()
    a = await _finding(db_session, ["192.0.2.58"], severity="high", run_id=r1, title="重複的 IP 紀錄")
    await _finding(db_session, ["192.0.2.58"], severity="high", run_id=r2, title="重複的 IP 位址紀錄", fp=a.fingerprint)
    await _completed(db_session, run_id=r2)
    d = await _build(db_session, admin)
    assert d["metrics"]["open"] == 1 and d["metrics"]["high"] == 1
    assert d["total"] == 1 and len(d["findings"]["items"]) == 1


# ─────────────────────────── 可見範圍 ───────────────────────────

async def test_admin_sees_everything_including_unverified(db_session) -> None:
    s = await _site(db_session)
    admin = await _user(db_session, admin=True)
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], severity="high", run_id=rid)
    await _finding(db_session, ["198.51.100.7"], run_id=rid, title="B 租戶的發現")
    await _finding(db_session, ["203.0.113.99"], run_id=rid, title="對不到記錄的位址", severity="low")
    await _completed(db_session, run_id=rid)
    d = await _build(db_session, admin)
    assert d["metrics"] == {"high": 1, "open": 3, "insufficient": 1}
    assert d["withheld"] is None
    items = d["findings"]["items"]
    assert [it["priority"] for it in items] == ["high", "medium", "low"]
    unv = items[2]
    assert unv["unverified"] == 1
    assert unv["subjects"] == [{"kind": "unverified", "text": "203.0.113.99", "verified": False}]
    first = items[0]
    assert first["subjects"][0]["ip"] == "192.0.2.58"
    assert first["subjects"][0]["hostname"] == s["ip_a"].hostname
    assert first["review"] == "open"
    assert "scanner" in first["sources"] and "arp" in first["sources"]
    assert first["recommendation"] == "請確認是否更換過網卡或租約尚未清除"   # 純文字，不帶 markdown
    # 不帶模型的原始回應或任何多餘欄位
    for it in items:
        assert not ({"raw", "prompt", "thinking", "evidence"} & set(it))


async def test_limited_viewer_only_sees_findings_the_run_scope_allows(db_session) -> None:
    s = await _site(db_session)
    viewer = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["a"].id, principal_type="user",
                              principal_id=viewer.id, level="read"))
    await db_session.commit()
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], severity="high", run_id=rid)
    await _finding(db_session, ["198.51.100.7"], run_id=rid, title="B 租戶的發現")
    await _finding(db_session, ["203.0.113.99"], run_id=rid, title="對不到記錄的位址")
    # 這次巡檢只送了 A 租戶的資料進模型（例如以 A 租戶的帳號執行）
    await _completed(db_session, run_id=rid, scope={"subnet_ids": [str(s["a"].id)], "ip_ids": [str(s["ip_a"].id)],
                                                    "device_ids": []})
    d = await _build(db_session, viewer)
    assert _ips(d) == ["192.0.2.58"]
    assert d["metrics"] == {"high": 1, "open": 1, "insufficient": 0}
    assert d["withheld"] == {"count": None}                     # 說有未顯示的，但不說幾筆
    assert "B 租戶" not in str(d) and "203.0.113.99" not in str(d) and "198.51.100.7" not in str(d)
    assert d["run"]["scope"]["subnets"] == 1


async def test_limited_viewer_gets_nothing_when_run_input_had_other_tenants(db_session) -> None:
    """模型看過 B 租戶的資料：就算發現只點名 A 的 IP，敘述也可能夾著 B 的東西 → 不給 A 看。"""
    s = await _site(db_session)
    viewer = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["a"].id, principal_type="user",
                              principal_id=viewer.id, level="read"))
    await db_session.commit()
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], severity="high", run_id=rid)
    await _completed(db_session, run_id=rid, scope={
        "subnet_ids": [str(s["a"].id), str(s["b"].id)], "ip_ids": [str(s["ip_a"].id), str(s["ip_b"].id)],
        "device_ids": []})
    d = await _build(db_session, viewer)
    assert d["findings"].get("items", []) == []
    assert d["withheld"] == {"count": None}
    assert d["findings"]["state"] == "withheld"                  # 不是「沒有發現」
    assert d["metrics"]["open"] == "—"
    assert d["run"]["scope"]["subnets"] is None                  # 不洩漏整體涵蓋的規模


async def test_limited_viewer_and_unrecorded_run_scope_is_withheld(db_session) -> None:
    """舊版巡檢沒記送進模型的範圍：無從判斷 → 非完整權限一律不給。"""
    s = await _site(db_session)
    viewer = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["a"].id, principal_type="user",
                              principal_id=viewer.id, level="read"))
    await db_session.commit()
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], run_id=rid)
    await _completed(db_session, run_id=rid)
    d = await _build(db_session, viewer)
    assert d["findings"].get("items", []) == [] and d["withheld"] == {"count": None}


async def test_warroom_viewer_role_has_full_access(db_session) -> None:
    from app.models.user import Group, UserGroupMember
    from app.services.permission import WARROOM_ROLE, seed_default_roles
    from sqlalchemy import select
    await _site(db_session)
    await seed_default_roles(db_session)
    g = (await db_session.execute(select(Group).where(Group.name == WARROOM_ROLE))).scalar_one()
    u = await _user(db_session)
    db_session.add(UserGroupMember(user_id=u.id, group_id=g.id))
    await db_session.commit()
    rid = uuid.uuid4()
    await _finding(db_session, ["198.51.100.7"], run_id=rid)
    await _finding(db_session, ["203.0.113.99"], run_id=rid, title="對不到記錄的位址")
    await _completed(db_session, run_id=rid)
    d = await _build(db_session, u)
    assert d["metrics"]["open"] == 2 and d["withheld"] is None


async def test_display_scope_narrows_full_access(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], run_id=rid)
    await _finding(db_session, ["198.51.100.7"], run_id=rid, title="B 租戶的發現")
    await _finding(db_session, ["203.0.113.99"], run_id=rid, title="對不到記錄的位址")
    await _completed(db_session, run_id=rid)
    r = await client.put("/api/v1/system/warroom", headers=auth_headers,
                         json={"scope": {"subnets": [str(s["a"].id)]}})
    assert r.status_code == 200, r.text
    d = (await client.get("/api/v1/warroom/pages/ai_audit", headers=auth_headers)).json()
    assert _ips(d) == ["192.0.2.58"]
    assert d["withheld"] == {"count": 2}                         # 完整權限：可以說幾筆不在範圍內


async def test_display_token_uses_principal_visibility(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    viewer = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["b"].id, principal_type="user",
                              principal_id=viewer.id, level="read"))
    await db_session.commit()
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], run_id=rid)
    await _finding(db_session, ["198.51.100.7"], run_id=rid, title="B 租戶的發現")
    await _completed(db_session, run_id=rid, scope={"subnet_ids": [str(s["b"].id)], "ip_ids": [str(s["ip_b"].id)],
                                                    "device_ids": []})
    tok = (await client.post("/api/v1/system/warroom/token", json={"days": 30}, headers=auth_headers)).json()["token"]
    r = await client.put("/api/v1/system/warroom", headers=auth_headers,
                         json={"display_enabled": True, "principal_user_id": str(viewer.id)})
    assert r.status_code == 200, r.text
    d = (await client.get("/api/v1/warroom/pages/ai_audit", headers={"X-Warroom-Token": tok})).json()
    assert _ips(d) == ["198.51.100.7"]


async def test_dismissed_is_review_status_not_resolved(db_session) -> None:
    await _site(db_session)
    admin = await _user(db_session, admin=True)
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], run_id=rid)
    await _finding(db_session, ["198.51.100.7"], run_id=rid, status="dismissed", title="已忽略的")
    await _completed(db_session, run_id=rid)
    d = await _build(db_session, admin)
    assert d["metrics"]["open"] == 1
    assert d["dismissed"] == 1
    assert all(it["review"] == "open" for it in d["findings"]["items"])


async def test_pagination_three_per_page(db_session) -> None:
    await _site(db_session)
    admin = await _user(db_session, admin=True)
    rid = uuid.uuid4()
    for i in range(7):
        await _finding(db_session, [f"203.0.113.{i + 1}"], run_id=rid, title=f"發現 {i}")
    await _completed(db_session, run_id=rid)
    d1 = await _build(db_session, admin, 1)
    d3 = await _build(db_session, admin, 3)
    assert d1["pages"] == 3 and d1["per_page"] == 3 and d1["total"] == 7
    assert [it["rank"] for it in d1["findings"]["items"]] == [1, 2, 3]
    assert [it["rank"] for it in d3["findings"]["items"]] == [7]


async def test_building_the_page_does_not_touch_run_state(db_session, _no_llm) -> None:
    await _site(db_session)
    admin = await _user(db_session, admin=True)
    rid = uuid.uuid4()
    await _finding(db_session, ["192.0.2.58"], run_id=rid)
    await _completed(db_session, run_id=rid)
    before = await sc.get_ai_audit_state(db_session)
    for p in (1, 2, 1):
        await _build(db_session, admin, p)
    assert await sc.get_ai_audit_state(db_session) == before
    assert _no_llm == []


# ─────────────────────────── 巡檢結束時記下結果 ───────────────────────────

class _FakeUser:
    id = uuid.uuid4()


async def _coro(v):  # type: ignore[no-untyped-def]
    return v


def _snapshot(n_ips: int, scope: dict | None = None) -> dict:
    return {"subnets": [{"cidr": "192.0.2.0/24"}], "devices": [], "empty": False,
            "ips": [{"ip": f"192.0.2.{i}", "hostname": f"h{i}"} for i in range(1, n_ips + 1)],
            "_scope": scope or {"subnet_ids": ["s1"], "ip_ids": [f"i{i}" for i in range(n_ips)], "device_ids": [],
                                "capped": {"subnets": False, "ips": False, "devices": False}}}


@pytest.fixture
def _real_run(monkeypatch):
    """這一組要真的跑 run_audit（LLM 以假的取代），把上面的攔截拿掉。"""
    from app.services import ai_audit as mod
    monkeypatch.setattr(mod, "run_audit", _ORIG_RUN)
    monkeypatch.setattr(mod, "_analyze", _ORIG_ANALYZE)
    return mod


_ORIG_RUN = aa.run_audit
_ORIG_ANALYZE = aa._analyze


async def test_outcome_completed_records_scope(monkeypatch, db_session, _real_run, _no_llm) -> None:
    from app.services import ai as ai_mod
    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(5)))
    monkeypatch.setattr(ai_mod, "raw_chat", lambda s, p, **kw: _coro('{"findings":[]}'))
    r = await aa.run_audit(db_session, _FakeUser())
    assert r.status == "completed"
    st = await sc.get_ai_audit_state(db_session)
    assert st["last_status"] == "completed" and st["completed_status"] == "completed"
    assert st["completed_scope"]["ips"] == 5 and st["completed_scope"]["subnets"] == 1
    scopes = await sc.get_ai_audit_run_scopes(db_session)
    assert scopes[str(r.run_id)]["ip_ids"] == {f"i{i}" for i in range(5)}
    _no_llm.clear()


async def test_outcome_failed_keeps_previous_completed_and_no_raw_text(monkeypatch, db_session, _real_run,
                                                                        _no_llm) -> None:
    from app.services import ai as ai_mod
    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(5)))
    monkeypatch.setattr(ai_mod, "raw_chat", lambda s, p, **kw: _coro('{"findings":[]}'))
    ok = await aa.run_audit(db_session, _FakeUser())
    monkeypatch.setattr(ai_mod, "raw_chat", lambda s, p, **kw: _coro("SECRET-RAW-MODEL-TEXT not json"))
    bad = await aa.run_audit(db_session, _FakeUser())
    assert bad.status == "failed" and bad.error_code == "parse_error"
    st = await sc.get_ai_audit_state(db_session)
    assert st["last_status"] == "failed"
    assert st["completed_run_id"] == str(ok.run_id)              # 失敗不會蓋掉上次有結果的那次
    assert "SECRET-RAW-MODEL-TEXT" not in str(st)
    _no_llm.clear()


async def test_outcome_partial(monkeypatch, db_session, _real_run, _no_llm) -> None:
    from app.services import ai as ai_mod
    calls = {"n": 0}

    async def flaky(s, p, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise ai_mod.AIUnreachable("transport: timeout")
        # 成功的批次要有發現：每批都沒發現又有一批失敗，ai_audit 本來就當成失敗（沒有存任何結果）
        return ('{"findings":[{"severity":"low","category":"stale","title":"t%d",'
                '"evidence":{"ips":["192.0.2.%d"]}}]}' % (calls["n"], calls["n"]))

    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(130)))   # 3 批
    monkeypatch.setattr(ai_mod, "raw_chat", flaky)
    r = await aa.run_audit(db_session, _FakeUser())
    assert r.status == "partial" and r.batches == 3
    st = await sc.get_ai_audit_state(db_session)
    assert st["last_status"] == "partial" and st["completed_status"] == "partial"
    assert st["completed_failed_batches"] == 1
    _no_llm.clear()


async def test_outcome_exception_is_failed(monkeypatch, db_session, _real_run, _no_llm) -> None:
    from app.services import ai as ai_mod

    async def boom(s, p, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("killed")

    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(5)))
    monkeypatch.setattr(ai_mod, "raw_chat", boom)
    with pytest.raises(RuntimeError):
        await aa.run_audit(db_session, _FakeUser())
    st = await sc.get_ai_audit_state(db_session)
    assert st["last_status"] == "failed" and st["last_error_code"] == "exception"
    assert "completed_at" not in st
    _no_llm.clear()
