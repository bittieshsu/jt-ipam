"""每個 AI 工具的每個參數都實際跑一次（使用者 2026-10-13：「所有其它相關的 tool 也要檢查」）。

事故：AI 對話問「某個網段（只給網路位址、沒寫遮罩）的 dhcp range」，模型帶 subnet_cidr 呼叫 list_dhcp_ranges ——
那段 SQL 拿字串欄位跟 cidr 比（`character varying = cidr`），**只要帶範圍就一定失敗**。
接著同一個交易變成「已中止」，後面的 list_subnets、search_ip、stats_overview 全部跟著失敗，
最後連存對話紀錄都失敗，畫面什麼都沒出來。

既有的 test_ai_chat_tools 對每個工具只用一組參數（多半是空的）跑一次，篩選條件那幾條路從沒
被執行過，所以抓不到。這裡在有資料的庫裡：
  - 只帶必填、每個選用參數各單獨帶一次（列舉值逐一）、全部一起帶
  - 管理員跑一輪，只看得到那個子網路的一般帳號再跑一輪（可見性過濾是另一段 SQL）
  - 每次呼叫包在 SAVEPOINT 裡、跑完就還原：一個工具壞掉不會連累後面的，失敗清單才準
唯一允許的例外是 IPAMToolError（查無資料、參數不合法、權限）。其他任何例外＝那個工具對 AI 不可用。
"""
from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pytest
from app.mcp.tools import TOOLS, IPAMToolError, authorize_tool, call_tool
from sqlalchemy.exc import InvalidRequestError

from tests.test_ai_chat_tools import SAMPLE_ARGS

pytestmark = pytest.mark.anyio

# 不碰資料庫的工具（純計算、對外查詢）：篩選參數的 SQL 問題與它們無關，對外查詢也不該在測試裡亂打
NO_DB = {n for n in TOOLS if n.startswith("calc_")} | {"power_calc", "dns_resolve", "dns_mail_check", "geoip_locate"}

CIDR = "198.51.100.0/24"
IP = "198.51.100.10"
MAC = "02:00:00:00:00:01"


async def _seed(db) -> dict[str, Any]:
    from app.models.address import IPAddress
    from app.models.customer import Customer
    from app.models.device import Device
    from app.models.dhcp import DHCPPoolRange
    from app.models.location import Location, Rack
    from app.models.section import Section
    from app.models.subnet import Subnet

    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    cust = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    loc = Location(name=f"loc-{uuid.uuid4().hex[:6]}")
    db.add_all([sec, cust, loc])
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr=CIDR, description="office")
    other = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    rack = Rack(name=f"r-{uuid.uuid4().hex[:6]}", location_id=loc.id, u_height=42)
    db.add_all([sub, other, rack])
    await db.flush()
    dev = Device(name="sw-01", type="switch", rack_id=rack.id)
    db.add(dev)
    await db.flush()
    db.add(IPAddress(subnet_id=sub.id, ip=IP, hostname="web-01", mac=MAC, state="used", device_id=dev.id))
    src = uuid.uuid4()
    db.add_all([
        DHCPPoolRange(source_type="windows_dhcp", source_id=src, source_name="dhcp-a", subnet_cidr=CIDR,
                      start_ip="198.51.100.100", end_ip="198.51.100.150", family=4, source="windows"),
        # pfSense 的範圍不一定給得出 CIDR：subnet_cidr 是空的，照樣要算進這個子網路
        DHCPPoolRange(source_type="pfsense", source_id=src, source_name="pf-a", subnet_cidr=None,
                      start_ip="198.51.100.200", end_ip="198.51.100.220", family=4, source="kea"),
        DHCPPoolRange(source_type="windows_dhcp", source_id=src, source_name="dhcp-b", subnet_cidr="203.0.113.0/24",
                      start_ip="203.0.113.100", end_ip="203.0.113.150", family=4, source="windows"),
    ])
    await db.commit()
    return {"section_id": str(sec.id), "subnet_id": str(sub.id), "other_subnet_id": str(other.id),
            "location_id": str(loc.id), "rack_id": str(rack.id), "device_id": str(dev.id),
            "customer_id": str(cust.id), "customer": cust.name}


def _candidates(prop: str, schema: dict[str, Any], ctx: dict[str, Any]) -> list[Any]:
    """一個參數要試的值：列舉逐一、布林兩種、其他給一個指到種子資料的合理值。"""
    if "enum" in schema:
        return list(schema["enum"])
    kind = schema.get("type")
    if kind == "boolean":
        return [True, False]
    if kind in ("integer", "number"):
        n = {"limit": 5, "offset": 0, "count": 2, "new_prefix": 26, "number": 10}.get(prop, 30)
        return [n, str(n)]          # 模型常把數字寫成字串
    if kind == "array":
        return [[0]] if prop == "indices" else [[IP]]
    if kind == "object":
        return [{}]
    fixed: dict[str, list[Any]] = {
        # 模型常只給網路位址、不給遮罩（這次的問法就是只給網路位址）
        "subnet_cidr": [CIDR, "198.51.100.0"], "cidr": [CIDR], "ip": [IP], "target_ip": [IP],
        "new_ip": ["198.51.100.11"], "requested_ip": ["198.51.100.12"], "mac": [MAC],
        "name": ["web-01"], "hostname": ["web-01"], "q": ["web"], "fqdn": ["web-01.example.com"],
        "customer": [ctx["customer"]], "prefix": ["02"], "state": ["used"], "status": ["pending"],
        "kind": ["ip_conflicts"], "type": ["switch"], "rtype": ["A"],
        "subnet_id": [ctx["subnet_id"]], "target_subnet_id": [ctx["subnet_id"]],
        "section_id": [ctx["section_id"]], "location_id": [ctx["location_id"]], "rack_id": [ctx["rack_id"]],
        "device_id": [ctx["device_id"]], "customer_id": [ctx["customer_id"]],
        "gateway": ["198.51.100.1"],
    }
    # 位址、MAC、ID 類參數再加一個亂寫的值：模型偶爾會這樣填，要回看得懂的錯誤，
    # 不可以是資料庫的型別轉換錯誤（那會讓整個交易中止）
    bad = ["not-valid"] if prop in _FORMATTED or prop.endswith("_id") else []
    if prop in fixed:
        return [*fixed[prop], *bad]
    if prop.endswith("_id"):
        return [str(uuid.uuid4()), *bad]
    return ["x"]


_FORMATTED = {"subnet_cidr", "cidr", "ip", "target_ip", "new_ip", "requested_ip", "mac", "gateway", "prefix"}


def _calls(name: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    params = TOOLS[name]["parameters"]
    props: dict[str, Any] = params.get("properties", {})
    required = set(params.get("required", []))
    base: dict[str, Any] = {}
    for p in required:
        base[p] = SAMPLE_ARGS.get(name, {}).get(p) if p in ("draft_token", "scenario_type", "target_type", "indices") \
            else _candidates(p, props[p], ctx)[0]
    calls = [dict(base)]
    optional = [p for p in props if p not in required]
    for p in optional:
        for v in _candidates(p, props[p], ctx):
            calls.append({**base, p: v})
    if optional:
        calls.append({**base, **{p: _candidates(p, props[p], ctx)[0] for p in optional}})
    return calls


async def _run_all(db, user, ctx: dict[str, Any]) -> list[str]:
    from sqlalchemy import text
    failures: list[str] = []
    for name in sorted(TOOLS):
        if name in NO_DB:
            continue
        if await authorize_tool(db, user, name) is not None:
            continue                       # AI 路徑上這個帳號根本叫不到它
        for args in _calls(name, ctx):
            sp = await db.begin_nested()
            try:
                await call_tool(db, user, name, args)
            except IPAMToolError:
                pass
            except Exception as exc:  # 要抓的就是所有非預期例外
                failures.append(f"{name}({args}): {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            else:
                # 工具自己吞掉資料庫錯誤、照樣回傳：交易其實已經中止，後面的工具會全部跟著失敗
                if sp.is_active:
                    try:
                        await db.execute(text("SELECT 1"))
                    except Exception as exc:
                        failures.append(f"{name}({args}): left the transaction aborted: {type(exc).__name__}")
            finally:
                # flush 失敗後 SAVEPOINT 是「待還原」（is_active 為 False 也要 rollback）；工具自己 commit 過才是已關閉
                with contextlib.suppress(InvalidRequestError):
                    await sp.rollback()
    return failures


async def test_every_parameter_of_every_tool_as_admin(db_session, admin_user) -> None:
    failures = await _run_all(db_session, admin_user, await _seed(db_session))
    assert not failures, "AI 工具帶參數就壞掉：\n" + "\n".join(failures)


async def test_every_parameter_of_every_tool_as_a_scoped_user(db_session) -> None:
    from app.models.permission import Permission

    from tests.test_mcp_rbac_scope import _nonadmin
    ctx = await _seed(db_session)
    u = await _nonadmin(db_session)
    db_session.add(Permission(object_type="section", object_id=uuid.UUID(ctx["section_id"]),
                              principal_type="user", principal_id=u.id, level="read"))
    await db_session.commit()
    failures = await _run_all(db_session, u, ctx)
    assert not failures, "AI 工具帶參數就壞掉（一般帳號）：\n" + "\n".join(failures)


async def test_dhcp_ranges_scoped_to_a_subnet(db_session, admin_user) -> None:
    """這次的事故本身：帶子網路要回那個子網路裡的範圍（含沒有 CIDR 的 pfSense 範圍），不含別的子網路。"""
    ctx = await _seed(db_session)
    fn = TOOLS["list_dhcp_ranges"]["fn"]
    for kw in ({"subnet_cidr": CIDR}, {"subnet_cidr": "198.51.100.0"}, {"subnet_id": ctx["subnet_id"]}):
        out = await fn(db_session, user=admin_user, **kw)
        assert out["scope"] == CIDR, kw
        assert sorted(r["start_ip"] for r in out["ranges"]) == ["198.51.100.100", "198.51.100.200"], kw
        assert out["count"] == 2, kw
    everything = await fn(db_session, user=admin_user)
    assert everything["scope"] == "all"
    assert everything["count"] == 3


async def test_subnet_argument_forms(db_session, admin_user) -> None:
    """範圍參數：只給網路位址也找得到；亂寫要是看得懂的錯誤，不可以是資料庫錯誤（會讓整個交易中止）。"""
    ctx = await _seed(db_session)
    fn = TOOLS["get_subnet_detail"]["fn"]
    for value in (CIDR, "198.51.100.0", "198.51.100.77"):
        out = await fn(db_session, user=admin_user, subnet_cidr=value)
        assert out["cidr"] == CIDR, value
    from sqlalchemy import text
    for bad in ("abc", "198.51.100.0/99", "  ", "198.51.100.0/24; drop table subnets"):
        sp = await db_session.begin_nested()
        with pytest.raises(IPAMToolError):
            await fn(db_session, user=admin_user, subnet_cidr=bad)
        # 資料庫錯誤會讓交易中止，之後的查詢全部失敗；看得懂的錯誤不會
        assert (await db_session.execute(text("SELECT 1"))).scalar() == 1, f"{bad!r} 讓交易中止了"
        await sp.rollback()
    with pytest.raises(IPAMToolError):
        await fn(db_session, user=admin_user, subnet_id="not-a-uuid")
    assert ctx


async def test_a_failing_tool_does_not_break_the_tools_after_it(db_session, admin_user, monkeypatch) -> None:
    """一個工具的 SQL 出錯，同一輪後面的工具照常回答（以前全部變成 InFailedSQLTransactionError）。"""
    from app.services import ai as ai_mod
    from sqlalchemy import text
    await _seed(db_session)

    async def broken(session, *, user, **kw):
        await session.execute(text("SELECT 'x'::int"))
        return {}
    monkeypatch.setitem(TOOLS, "zz_broken", {**TOOLS["stats_overview"], "fn": broken})
    out = await ai_mod._run_tool_calls(db_session, admin_user, [
        {"function": {"name": "zz_broken", "arguments": {}}},
        {"function": {"name": "list_dhcp_ranges", "arguments": {"subnet_cidr": CIDR}}},
        {"function": {"name": "search_ip", "arguments": {"ip": IP}}},
    ])
    assert "tool failed" in out[0]["content"]
    assert "198.51.100.100" in out[1]["content"], out[1]["content"][:300]
    assert "web-01" in out[2]["content"], out[2]["content"][:300]
    # 交易還能用：之後存對話紀錄不會 PendingRollbackError
    assert (await db_session.execute(text("SELECT 1"))).scalar() == 1


async def test_chat_still_answers_when_saving_the_conversation_fails(client, auth_headers, monkeypatch) -> None:
    """存對話紀錄失敗時，模型的回答照樣送到畫面（以前整段換成錯誤，畫面什麼都沒有）。"""
    import json

    from app.api.v1.endpoints import ai as ai_ep
    from app.services import ai_chat_store, system_config
    from app.services.system_config import LLMConfig

    async def _cfg(_s):
        return LLMConfig(enabled=True, url="http://llm.example.test:11434", embedding_model="e", chat_model="m",
                         timeout=30.0)
    monkeypatch.setattr(system_config, "get_llm_config", _cfg)

    async def stream(*a, **kw):
        yield {"type": "done", "answer": "範圍是 .100 到 .150", "trace_messages": [], "model": "m", "elapsed_ms": 5}
    monkeypatch.setattr(ai_ep.ai_service, "chat_stream", stream)

    async def broken_save(*a, **kw):
        raise RuntimeError("db went away")
    monkeypatch.setattr(ai_chat_store, "save_turn", broken_save)

    r = await client.post("/api/v1/ai/chat/stream", headers=auth_headers,
                          json={"messages": [{"role": "user", "content": "dhcp range?"}]})
    assert r.status_code == 200, r.text
    evs = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert evs, r.text
    assert evs[-1]["type"] == "done", evs
    assert evs[-1]["answer"] == "範圍是 .100 到 .150"
    assert evs[-1].get("history_saved") is False


def test_tools_are_only_called_through_call_tool() -> None:
    """AI 對話、MCP、確認端點都要走 call_tool（參數檢查＋SAVEPOINT）；直接 TOOLS[x]["fn"](...) 就繞過了。"""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"""\[\s*["']fn["']\s*\]""")
    hits = []
    for path in sorted(app_dir.rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(app_dir.parent)}:{no}: {line.strip()}")
    msg = "工具只能經由 app.mcp.tools.call_tool 呼叫：\n" + "\n".join(hits)
    assert len(hits) == 1, msg
    assert 'result = await TOOLS[name]["fn"](session, user=user, **clean)' in hits[0], msg
