"""OPNsense 升級中回的規則資料不可當真（使用者 10-22：一台 OPNsense 升級時收到「新增 1 條、移除 20 條：＋ ? any → any」）。

正式站台查到的經過：升級時 /api/firewall/filter/searchRule 失敗，退回舊端點 /api/firewall/filter/get，
它回的是 {filter: {rules: {rule: {<uuid>: {...}}}}} —— 多一層 `rule`。舊的解析把 "rule" 當成一條規則的 uuid，
於是 20 條規則被刪、多出一條空白的「? any → any」，九分鐘後恢復又全部加回來，發出兩次異動通知。

規則：
- 舊端點多一層 `rule` 要解開；uuid 不像 UUID 的列不收。
- 這一輪拿到的規則有任何一列不合格、或從有規則變成零條：當作讀取不完整，不新增也不刪除，回報錯誤。
"""
from __future__ import annotations

import uuid

import pytest
from app.models.firewall import OPNsenseFirewall
from app.models.firewall_rule import OPNsenseRule
from app.services import opnsense_firewall as svc
from app.services.opnsense_firewall import OPNsenseError
from sqlalchemy import select

pytestmark = pytest.mark.anyio

U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())


async def _fw(db_session) -> OPNsenseFirewall:
    fw = OPNsenseFirewall(name=f"opn-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                          api_key_enc=b"x", api_key_nonce=b"y", api_secret_enc=b"x", api_secret_nonce=b"y")
    db_session.add(fw)
    await db_session.flush()
    for u, d in ((U1, "PVE"), (U2, "NVR")):
        db_session.add(OPNsenseRule(firewall_id=fw.id, legacy_uuid=u, enabled=True, action="pass",
                                    interface="WAN", description=d))
    await db_session.flush()
    return fw


def _patch(monkeypatch, *, search=None, legacy=None):
    async def post(fw, path, body):
        if search is None:
            raise OPNsenseError("searchRule unavailable during upgrade")
        return search

    async def get(fw, path):
        if legacy is None:
            raise OPNsenseError("down")
        return legacy

    async def ifmap(fw):
        return {}
    monkeypatch.setattr(svc, "_api_post", post)
    monkeypatch.setattr(svc, "_api_get", get)
    monkeypatch.setattr(svc, "_iface_name_map", ifmap)


async def _uuids(db_session, fw) -> set[str]:
    return set((await db_session.execute(select(OPNsenseRule.legacy_uuid).where(
        OPNsenseRule.firewall_id == fw.id))).scalars().all())


async def test_legacy_nested_rule_layer_is_unwrapped(db_session, monkeypatch) -> None:
    fw = await _fw(db_session)
    _patch(monkeypatch, legacy={"filter": {"rules": {"rule": {
        U1: {"enabled": "1", "action": "pass", "description": "PVE"},
        U2: {"enabled": "1", "action": "pass", "description": "NVR"}}}}})
    out = await svc.sync_filter_rules(db_session, fw)
    assert out.get("removed") == 0
    assert "error" not in out
    assert await _uuids(db_session, fw) == {U1, U2}


async def test_row_without_a_real_uuid_aborts_without_changes(db_session, monkeypatch) -> None:
    fw = await _fw(db_session)
    # 升級當下實際收到的形狀：被誤解成一條 uuid="rule" 的空白規則
    _patch(monkeypatch, search={"rows": [{"uuid": "rule", "enabled": "0"}]})
    out = await svc.sync_filter_rules(db_session, fw)
    assert out.get("error")
    assert out.get("removed") == 0
    assert await _uuids(db_session, fw) == {U1, U2}


async def test_empty_list_after_having_rules_is_incomplete(db_session, monkeypatch) -> None:
    fw = await _fw(db_session)
    _patch(monkeypatch, search={"rows": []})
    out = await svc.sync_filter_rules(db_session, fw)
    assert out.get("error")
    assert out.get("removed") == 0
    assert await _uuids(db_session, fw) == {U1, U2}


async def test_normal_removal_still_works(db_session, monkeypatch) -> None:
    fw = await _fw(db_session)
    _patch(monkeypatch, search={"rows": [{"uuid": U1, "enabled": "1", "action": "pass", "description": "PVE"}]})
    out = await svc.sync_filter_rules(db_session, fw)
    assert out.get("removed") == 1
    assert "error" not in out
    assert await _uuids(db_session, fw) == {U1}


async def test_automatic_rules_with_hex_ids_are_valid(db_session, monkeypatch) -> None:
    """新版 OPNsense 的系統自動規則（is_automatic）識別碼是 32 位十六進位、沒有連字號（10-23 正式站台兩台都是）。
    10-22 的檢查只認標準 UUID，把它們當成格式錯誤，整批規則同步每輪失敗。"""
    fw = await _fw(db_session)
    auto = "8164f82fd0b4f3b0e0c494228a454c17"
    _patch(monkeypatch, search={"rows": [
        {"uuid": U1, "enabled": "1", "action": "pass", "description": "PVE"},
        {"uuid": U2, "enabled": "1", "action": "pass", "description": "NVR"},
        {"uuid": auto, "enabled": "1", "action": "block", "description": "Default deny / state violation rule",
         "is_automatic": True, "legacy": True}]})
    out = await svc.sync_filter_rules(db_session, fw)
    assert "error" not in out, out
    assert await _uuids(db_session, fw) == {U1, U2, auto}
