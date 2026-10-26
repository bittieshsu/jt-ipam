"""IP 變更評估的模板待辦（2026-10-25 實測：新位址衝突被寫成「更新活動的引用」「更新 IPAM 的引用」）。

新位址的衝突（有人在用、有 IP 申請、IPAM 已配發、DHCP 租約／保留、跨子網路要確認的事）不是「要去改的引用」，
是改址前要先解決的事 —— 掛在前置的「確認新位址」底下，不另外生「更新引用」的待辦。舊位址的引用照舊分類。
"""
from __future__ import annotations

import uuid

from app.models.change_impact import ImpactFinding
from app.services.change_impact.model import RULES
from app.services.change_impact.plans import _template_tasks


def _f(rule: str) -> ImpactFinding:
    r = RULES[rule]
    return ImpactFinding(id=uuid.uuid4(), rule_id=rule, category=r.category, impact=r.impact, severity=r.severity,
                         disposition=r.disposition, reason_code=r.reason, subject_label="x", params={})


def test_new_ip_conflicts_go_under_confirm_new_ip_not_update_refs() -> None:
    seen, req, dns, fw = (_f("activity.new_ip_seen"), _f("ipam.new_ip_requested"), _f("dns.address_record"),
                          _f("fw.rule_exact"))
    tasks = _template_tasks("ip_renumber", [seen, req, dns, fw], {"new_ip": "198.51.100.121"}, "198.51.100.21")
    codes = {code: fids for _phase, code, _p, fids in tasks}
    assert "update_refs_activity" not in codes and "update_refs_ipam" not in codes
    assert set(codes["confirm_new_ip"]) == {str(seen.id), str(req.id)}
    assert codes["update_refs_dns"] == [str(dns.id)] and codes["update_refs_firewall"] == [str(fw.id)]


def test_new_ip_rule_list_matches_the_rules_table() -> None:
    from app.services.change_impact.plans import NEW_IP_RULES
    assert NEW_IP_RULES <= set(RULES)
    assert {r for r in RULES if ".new_ip_" in r} <= NEW_IP_RULES
