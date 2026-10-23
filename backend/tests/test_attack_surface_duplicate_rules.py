"""對外開放服務：防火牆上完全相同的規則合併成一列，標出重複（使用者 10-22：
「某台主機的 443 怎麼有兩筆一樣的？」—— 防火牆上真的有兩條一模一樣的放行規則，序號 511 與 801）。

清單是「哪裡對外開了」，同一個開口列兩次只會讓人以為是系統重複；重複規則本身是要清的設定問題，所以標出序號。
來源不同（一條限國家、一條 any）就是不同的開口，不可合併。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.firewall import OPNsenseFirewall
from app.models.firewall_rule import OPNsenseRule


def _rule(fw, seq: int, *, src: str = "allowlist_country_https", proto: str = "TCP", descr: str = "Reverse Proxy"):
    return OPNsenseRule(firewall_id=fw.id, legacy_uuid=str(uuid.uuid4()), enabled=True, sequence=seq,
                        action="pass", interface="WAN2", direction="in", protocol=proto,
                        source_net=src, destination_net="198.51.100.105", destination_port="443",
                        description=descr)


@pytest.mark.anyio
async def test_identical_rules_merge_with_sequences(db_session) -> None:
    from app.services.fw_lookup import attack_surface

    fw = OPNsenseFirewall(name=f"opn-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                          api_key_enc=b"x", api_key_nonce=b"y", api_secret_enc=b"x", api_secret_nonce=b"y")
    db_session.add(fw)
    await db_session.flush()
    db_session.add_all([
        _rule(fw, 801), _rule(fw, 511),
        _rule(fw, 600, src="any"),                 # 來源不同：另一個開口
        _rule(fw, 911, proto="UDP"),               # 協定不同：另一個開口
    ])
    await db_session.flush()

    items = [i for i in await attack_surface(db_session) if i.get("firewall") == fw.name]
    assert len(items) == 3, items
    merged = next(i for i in items if str(i["protocol"]).upper() == "TCP" and i.get("src") != "any")
    assert merged["duplicates"] == [511, 801], "序號由小到大，第一條是實際生效的"
    others = [i for i in items if i is not merged]
    assert all(not i.get("duplicates") for i in others)
