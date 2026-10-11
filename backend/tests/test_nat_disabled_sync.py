"""NAT 規則的啟用／停用要跟著防火牆走（使用者 2026-10-11：「有的防火牆有這種設定，如 OPNsense」）。

停用的規則照樣同步進來、標成停用：NAT 頁要看得到它（變淡、標「已停用」），而曝險清單、防火牆反查、
IP 變更評估本來就只看啟用的（各自有 `disabled.is_(False)`）。以前 MikroTik 直接略過停用的規則、
Palo Alto／FortiGate 沒帶這個欄位、OPNsense 只認 `disabled` 而 Outbound NAT／1:1 用的是 `enabled`。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from app.models.nat import NATTranslation


async def _nat(db, origin: str) -> dict[str, NATTranslation]:
    rows = (await db.execute(select(NATTranslation).where(NATTranslation.source_origin == origin))).scalars().all()
    return {r.external_id: r for r in rows}


async def test_opnsense_reads_both_disabled_and_enabled(db_session, monkeypatch) -> None:
    """Port forward（d_nat）的欄位叫 disabled；Outbound NAT 與 1:1 的模型用 enabled（OPNsense 原始碼）。"""
    from app.models.firewall import OPNsenseFirewall
    from app.services import opnsense_firewall as opn
    fw = OPNsenseFirewall(name=f"opn-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                          api_key_enc=b"x", api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()
    rows = {
        "/api/firewall/d_nat/searchRule": [{"uuid": "pf-off", "disabled": "1", "description": "pf off"},
                                           {"uuid": "pf-on", "disabled": "0", "description": "pf on"}],
        "/api/firewall/one_to_one/searchRule": [{"uuid": "bin-off", "enabled": "0", "description": "1:1 off"}],
        "/api/firewall/source_nat/searchRule": [{"uuid": "snat-on", "enabled": "1", "description": "snat on"}],
    }

    async def _post(_fw: Any, path: str, _body: Any = None, **_k: Any) -> dict[str, Any]:
        return {"rows": rows[path]}

    async def _ifmap(_fw: Any) -> dict[str, str]:
        return {}
    monkeypatch.setattr(opn, "_api_post", _post)
    monkeypatch.setattr(opn, "_iface_name_map", _ifmap)
    await opn.sync_nat_rules(db_session, fw)
    got = {k: v.disabled for k, v in (await _nat(db_session, f"opnsense:{fw.id}")).items()}
    assert got == {"pf-off": True, "pf-on": False, "bin-off": True, "snat-on": False}


async def test_mikrotik_keeps_disabled_rules_marked(db_session, monkeypatch) -> None:
    """以前停用的規則直接略過 —— NAT 頁看不到它，也看不出「這條其實存在、只是關著」。"""
    from app.models.mikrotik import MikroTikRouter
    from app.services import mikrotik as mt
    router = MikroTikRouter(name=f"ccr-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.9", api_username="ipam",
                            api_password_enc=b"x", api_password_nonce=b"y")
    db_session.add(router)
    await db_session.flush()

    async def _get(_router: Any, _ep: str, **_k: Any) -> list[dict[str, Any]]:
        return [
            {".id": "*1", "chain": "dstnat", "action": "dst-nat", "dst-port": "443", "protocol": "tcp",
             "to-addresses": "192.0.2.10", "to-ports": "8443", "disabled": "true", "comment": "web (off)"},
            {".id": "*2", "chain": "srcnat", "action": "masquerade", "disabled": "false", "comment": "masq"},
        ]
    monkeypatch.setattr(mt, "_get", _get)
    await mt.sync_nat(db_session, router, client=None)   # type: ignore[arg-type]
    rows = await _nat(db_session, f"mikrotik:{router.id}")
    by_name = {r.name: r for r in rows.values()}
    assert by_name["web (off)"].disabled is True and by_name["web (off)"].type == "port_forward"
    assert by_name["masq"].disabled is False


async def test_paloalto_maps_disabled_rules(db_session, monkeypatch) -> None:
    from app.services import paloalto as pa
    fw = SimpleNamespace(id=uuid.uuid4())

    async def _rest_get(_fw: Any, _res: str, **_k: Any) -> list[dict[str, Any]]:
        return [
            {"@name": "web-in", "disabled": "yes", "destination-translation": {"translated-address": "192.0.2.10"}},
            {"@name": "app-in", "disabled": "no", "destination-translation": {"translated-address": "192.0.2.11"}},
            {"@name": "mail-in", "destination-translation": {"translated-address": "192.0.2.12"}},
        ]
    monkeypatch.setattr(pa, "_rest_get", _rest_get)
    await pa.sync_nat(db_session, fw, ["vsys1"])   # type: ignore[arg-type]
    got = {k: v.disabled for k, v in (await _nat(db_session, f"paloalto:{fw.id}")).items()}
    assert got == {"vsys1:web-in": True, "vsys1:app-in": False, "vsys1:mail-in": False}
