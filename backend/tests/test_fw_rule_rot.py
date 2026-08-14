"""防火牆規則腐化偵測：懸空 NAT／any-any 放行／WAN 開管理埠。

這一頁最大的敵人是誤報 —— 異常清單狼來了幾次就沒有人看。所以對抗式重點是
**不該報的都要忍住**：
- 手動建立的 NAT（source_origin 為空）沒有連結 IP 是正常的，不是懸空。
- 已停用（disabled）的 NAT／規則不報 —— 它不在生效路徑上。
- LAN 介面上開 SSH 給 any 是日常，**只有 WAN 類介面**才算管理埠曝險。
- any-any 只抓「放行」；deny any-any 是預設拒絕的正常寫法。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.nat import NATTranslation
from app.services.anomaly import detect_fw_rule_rot


def _pf_rule(**kw):
    base = {"tracker": kw.pop("tracker", uuid.uuid4().hex[:8]), "type": "pass",
            "interface": "lan", "protocol": "tcp",
            "source": "any", "destination": "any", "destination_port": "",
            "descr": "", "disabled": False}
    base.update(kw)
    return base


@pytest.mark.anyio
async def test_dangling_nat_only_flags_synced_enabled_rows(db_session) -> None:
    """懸空 NAT：只報「防火牆同步來的、生效中的、目標不在 IPAM」那種。"""
    db_session.add_all([
        # 該報：pfSense 同步來的 port forward，目標解析不到 IPAM
        NATTranslation(name="pf-dangling", type="port_forward", protocol="tcp",
                       dst_port=8443, source_origin="pfsense:x", disabled=False),
        # 不該報：手動建立（source_origin 空）—— 手動 NAT 沒連 IP 是常態
        NATTranslation(name="manual-no-link", type="port_forward", protocol="tcp",
                       dst_port=80, source_origin=None, disabled=False),
        # 不該報：已停用
        NATTranslation(name="pf-disabled", type="port_forward", protocol="tcp",
                       dst_port=22, source_origin="pfsense:x", disabled=True),
    ])
    await db_session.flush()
    items = await detect_fw_rule_rot(db_session)
    names = {i["name"] for i in items if i["kind"] == "dangling_nat"}
    assert "pf-dangling" in names
    assert "manual-no-link" not in names, "手動 NAT 被誤報成懸空"
    assert "pf-disabled" not in names, "停用中的 NAT 不在生效路徑上，不該報"


@pytest.mark.anyio
async def test_any_any_flags_pass_not_block(db_session) -> None:
    """any-any 只抓放行；block any-any 是預設拒絕的正常寫法。"""
    from app.models.pfsense import PfSenseFirewall

    fw = PfSenseFirewall(name=f"pf-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.2",
                         api_key_enc=b"x", api_key_nonce=b"y",
                         rules=[
                             _pf_rule(descr="wide open"),                       # 該報
                             _pf_rule(type="block", descr="default deny"),      # 不該報
                             _pf_rule(disabled=True, descr="old wide open"),    # 不該報
                             _pf_rule(destination="192.0.2.10", descr="ok"),    # 不該報
                         ])
    db_session.add(fw)
    await db_session.flush()
    items = await detect_fw_rule_rot(db_session)
    descrs = {i.get("descr") for i in items if i["kind"] == "any_any"}
    assert "wide open" in descrs
    assert descrs.isdisjoint({"default deny", "old wide open", "ok"})


@pytest.mark.anyio
async def test_mgmt_port_only_on_wan_like_interfaces(db_session) -> None:
    """管理埠曝險只看 WAN 類介面：LAN 開 SSH 給 any 是日常，報了就是雜訊。"""
    from app.models.pfsense import PfSenseFirewall

    fw = PfSenseFirewall(name=f"pf-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.3",
                         api_key_enc=b"x", api_key_nonce=b"y",
                         rules=[
                             _pf_rule(interface="wan", destination="192.0.2.9",
                                      destination_port="3389", descr="rdp from wan"),   # 該報
                             _pf_rule(interface="lan", destination="any",
                                      destination_port="22", descr="lan ssh"),          # 不該報
                         ])
    db_session.add(fw)
    await db_session.flush()
    items = await detect_fw_rule_rot(db_session)
    descrs = {i.get("descr") for i in items if i["kind"] == "mgmt_exposed"}
    assert "rdp from wan" in descrs
    assert "lan ssh" not in descrs


@pytest.mark.anyio
async def test_report_includes_new_category(db_session) -> None:
    """AnomalyReport 要帶 fw_rule_rot（to_dict / total 都要跟上 —— 過去漏過欄位）。"""
    from app.services.anomaly import AnomalyReport

    r = AnomalyReport(fw_rule_rot=[{"kind": "any_any"}])
    d = r.to_dict()
    assert d["fw_rule_rot"] == [{"kind": "any_any"}]
    assert d["total"] >= 1, "新類別沒算進 total —— 儀表板數字會對不上"
