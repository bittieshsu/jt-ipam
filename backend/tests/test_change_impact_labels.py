"""關係圖與影響清單上的名稱（使用者 2026-10-08：「proxmox proxmox」沒人看得懂）。位址一律用 RFC 5737。"""
from __future__ import annotations

import uuid

from app.services.change_impact.labels import display_label, integration_label

from tests.test_change_impact_more_refs import OLD, _by_rule, _root, _run


def test_integration_label_uses_the_product_name_and_skips_repeats() -> None:
    assert integration_label("proxmox", None, "proxmox") == "Proxmox VE"
    assert integration_label("proxmox", "pve-cluster-a", "pve1.example.test") == "Proxmox VE pve-cluster-a"
    assert integration_label("proxmox", None, "pve1.example.test") == "Proxmox VE pve1.example.test"
    assert integration_label("opnsense", "fw-a") == "OPNsense fw-a"
    assert integration_label("unknown_kind", "x") == "unknown_kind x"


def test_old_stored_labels_are_tidied_when_read() -> None:
    assert display_label("integration", "proxmox proxmox", {"kind": "proxmox"}) == "Proxmox VE"
    assert display_label("integration", "opnsense fw-a", {"kind": "opnsense"}) == "OPNsense fw-a"
    assert display_label("integration", "Proxmox VE pve-cluster-a", {"kind": "proxmox"}) == "Proxmox VE pve-cluster-a"
    # 其他類型照原樣
    assert display_label("dns_record", "proxmox proxmox", {"kind": "proxmox"}) == "proxmox proxmox"
    assert display_label("integration", "x", {}) == "x"
    # 舊結果的 OCS 電腦存成「OCS #6」，畫面又會加「OCS：」→ 讀取時去掉重複的前綴
    assert display_label("ocs_computer", "OCS #6", {}) == "#6"
    assert display_label("ocs_computer", "laptop-07", {}) == "laptop-07"


async def test_proxmox_endpoint_is_named_after_its_cluster(db_session, admin_user) -> None:
    from app.models.virt import ProxmoxInstance, VirtCluster
    _sub, root = await _root(db_session)
    cl = VirtCluster(name=f"cluster-{uuid.uuid4().hex[:6]}", type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    db_session.add(ProxmoxInstance(api_url=f"https://{OLD}:8006", auth_username="root@pam", auth_token_id="t",
                                   cluster_id=cl.id))
    await db_session.commit()
    res = await _run(db_session, admin_user, root)
    labels = {f.subject_label for f in _by_rule(res, "config.integration_endpoint")}
    assert f"Proxmox VE {cl.name}" in labels, labels
