"""OPNsense Unbound 停用的 host override 不採用（使用者 2026-10-13：「停用的不要抓」）。

以前 list_zones／list_records 不看 `enabled`：停用的覆寫照樣變成 DNS 紀錄 → DNS 主機名稱、
失效 DNS、DNS 比對都被它影響。jt-ipam 自己推紀錄時找既有項目則照舊找得到停用的（才不會重複新增）。
"""
from __future__ import annotations

from app.services.dns.unbound_opnsense import UnboundOPNsenseAdapter

SETTINGS = {"unbound": {"hosts": {"host": {
    "u1": {"enabled": "1", "hostname": "web", "domain": "corp.example", "rr": "A", "server": "198.51.100.10"},
    "u2": {"enabled": "0", "hostname": "old", "domain": "corp.example", "rr": "A", "server": "198.51.100.11"},
    "u3": {"enabled": "0", "hostname": "x", "domain": "gone.example", "rr": "A", "server": "198.51.100.12"},
    "u4": {"hostname": "nofield", "domain": "corp.example", "rr": "A", "server": "198.51.100.13"},
}}}}


def _adapter(monkeypatch) -> UnboundOPNsenseAdapter:
    a = UnboundOPNsenseAdapter(api_url="https://fw.example", api_key="k", api_secret="s")

    async def _get(path: str) -> dict:
        return SETTINGS
    monkeypatch.setattr(a, "_get", _get)
    return a


async def test_disabled_overrides_are_not_records(monkeypatch) -> None:
    a = _adapter(monkeypatch)
    zones = [z.name for z in await a.list_zones()]
    assert zones == ["corp.example"], "只有停用的覆寫的網域不算 zone"
    names = sorted(r.name for r in await a.list_records("corp.example"))
    assert names == ["nofield.corp.example", "web.corp.example"], "沒有 enabled 欄位的當作啟用"


async def test_push_still_finds_a_disabled_entry(monkeypatch) -> None:
    from app.services.dns.base import DNSRecordOp
    a = _adapter(monkeypatch)
    uid = await a._find_existing_uuid("corp.example", DNSRecordOp(name="old.corp.example", type="A",
                                                                   value="198.51.100.11", ttl=3600))
    assert uid == "u2"
