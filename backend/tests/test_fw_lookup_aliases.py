"""IP 詳細頁的「所屬別名」：要看得出是哪一台防火牆的別名。

規則那幾行都寫著防火牆名稱（fw-01｜pass …），別名卻只有名稱與廠牌 ——
有兩台 OPNsense 時，看不出 `web_hosts` 是哪一台上的。MikroTik 以前還用清單名稱
去重：兩台路由器剛好都有同名清單時只剩一筆，另一台就消失了。
"""
from __future__ import annotations

import pytest
from app.services.fw_lookup import rules_touching_ip


@pytest.mark.anyio
async def test_aliases_carry_the_firewall_name(db_session) -> None:
    from app.models.firewall import OPNsenseFirewall, OPNsenseSyncedAlias
    from app.models.mikrotik import MikroTikAddressList, MikroTikRouter
    from app.models.pfsense import PfSenseFirewall, PfSenseSyncedAlias

    opn = OPNsenseFirewall(name="opn-a", api_url="https://192.0.2.1",
                           api_key_enc=b"x", api_key_nonce=b"y",
                           api_secret_enc=b"x", api_secret_nonce=b"y")
    pf = PfSenseFirewall(name="pf-b", api_url="https://192.0.2.2",
                         api_key_enc=b"x", api_key_nonce=b"y")
    r1 = MikroTikRouter(name="mt-c", api_url="https://192.0.2.3", api_username="u",
                        api_password_enc=b"x", api_password_nonce=b"y")
    r2 = MikroTikRouter(name="mt-d", api_url="https://192.0.2.4", api_username="u",
                        api_password_enc=b"x", api_password_nonce=b"y")
    db_session.add_all([opn, pf, r1, r2])
    await db_session.flush()
    db_session.add_all([
        OPNsenseSyncedAlias(firewall_id=opn.id, name="web_servers", alias_type="host",
                            enabled=True, content=["198.51.100.50"], description="public web"),
        PfSenseSyncedAlias(firewall_id=pf.id, name="pf_hosts", alias_type="host",
                           members=["198.51.100.50"]),
        # 兩台路由器都有同名清單 —— 兩台都要列出來
        MikroTikAddressList(router_id=r1.id, list_name="trusted", address="198.51.100.50"),
        MikroTikAddressList(router_id=r2.id, list_name="trusted", address="198.51.100.0/24"),
    ])
    await db_session.flush()

    out = await rules_touching_ip(db_session, "198.51.100.50")
    got = {(a["source_type"], a["firewall"], a["name"]) for a in out["aliases"]}
    assert ("opnsense", "opn-a", "web_servers") in got
    assert ("pfsense", "pf-b", "pf_hosts") in got
    assert ("mikrotik", "mt-c", "trusted") in got
    assert ("mikrotik", "mt-d", "trusted") in got, "同名清單在另一台路由器上被去重吃掉了"
    web = next(a for a in out["aliases"] if a["name"] == "web_servers")
    assert web["descr"] == "public web"


@pytest.mark.anyio
async def test_mikrotik_list_rules_are_found_through_the_list(db_session) -> None:
    """MikroTik 的 address-list 要反查得到（`list:<清單名>` 的規則也靠它命中）。

    address 是單一字串，以前直接傳給「成員清單」比對 → 逐字元比，永遠比不到，
    所以這類規則在 IP 詳細頁從來沒出現過。"""
    from app.models.mikrotik import MikroTikAddressList, MikroTikRouter

    r = MikroTikRouter(name="mt-e", api_url="https://192.0.2.5", api_username="u",
                       api_password_enc=b"x", api_password_nonce=b"y")
    db_session.add(r)
    await db_session.flush()
    db_session.add(MikroTikAddressList(router_id=r.id, list_name="web", address="198.51.100.60"))
    await db_session.flush()
    out = await rules_touching_ip(db_session, "198.51.100.60")
    assert [(a["firewall"], a["name"]) for a in out["aliases"]] == [("mt-e", "web")]
