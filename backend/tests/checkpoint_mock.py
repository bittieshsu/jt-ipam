"""模擬 Check Point Management API（R81.20，API 1.9）。

形狀照官方 Management API Reference（`/web_api/<指令>`，POST JSON，登入後帶 `X-chkp-sid`）：
- login（`api-key` 或 `user`+`password`，`read-only`、`domain`）→ `sid`、`api-server-version`
- show-* 清單：`offset`／`limit`，回 `objects`（或 `packages`）＋`from`／`to`／`total`
- show-access-rulebase／show-nat-rulebase：`rulebase` 裡有段落（`access-section`／`nat-section`），
  `use-object-dictionary` 時欄位是 uid、名稱在 `objects-dictionary`
2026-10-08 已對照實機（R81.20 Take 634 Standalone，API 1.9）修正：
- 唯讀登入不可以帶 session-name／session-description／session-comments（400 generic_err_invalid_parameter）
- show-groups（details-level full）的 members 是 uid 字串，不是物件
- 內嵌層規則的動作是 Global 物件「Inner Layer」；NAT 規則沒有 name 欄位
- 主機的自動靜態 NAT：規則的原始目的地與轉換後目的地都是那台主機，對外位址只在主機的
  nat-settings.ipv4-address（show-hosts／show-object full 才有，物件字典裡沒有）
- hits 的 last-date.iso-8601 不帶秒（2026-10-08T15:23+0800）
"""
from __future__ import annotations

import json
from typing import Any

from tests.isoinsight_mock import MockServer

API_KEY = "cp-key-0123456789abcdef"


def _obj(uid: str, name: str, typ: str, **kw: Any) -> dict[str, Any]:
    return {"uid": uid, "name": name, "type": typ, **kw}


ANY = _obj("u-any", "Any", "CpmiAnyObject")
ACCEPT = _obj("u-accept", "Accept", "RulebaseAction")
DROP = _obj("u-drop", "Drop", "RulebaseAction")
APPLY = _obj("u-apply", "Inner Layer", "Global")
ORIGINAL = _obj("u-orig", "Original", "Global")
TARGETS = _obj("u-targets", "Policy Targets", "Global")
HTTPS = _obj("u-https", "https", "service-tcp", port="443")

HOSTS = [
    _obj("h-web", "web-01", "host", **{"ipv4-address": "198.51.100.10", "comments": "ERP 前台",
                                        "nat-settings": {"auto-rule": True, "ipv4-address": "203.0.113.11",
                                                         "ipv6-address": "", "install-on": "cp-gw-01",
                                                         "method": "static"}}),
    _obj("h-db", "db-01", "host", **{"ipv4-address": "198.51.100.20"}),
    _obj("h-pub", "web-public", "host", **{"ipv4-address": "203.0.113.10"}),
]
NETWORKS = [_obj("n-lan", "net-lan", "network", subnet4="198.51.100.0", **{"mask-length4": 24})]
RANGES = [_obj("r-dhcp", "range-dhcp", "address-range",
               **{"ipv4-address-first": "198.51.100.100", "ipv4-address-last": "198.51.100.150"})]
# 實機：details-level full 的 members 是 uid 字串
GROUPS = [_obj("g-srv", "grp-servers", "group", members=["h-web", "h-db"])]
GWE = [_obj("x-lan", "lan-but-dhcp", "group-with-exclusion",
            include=_obj("n-lan", "net-lan", "network"), **{"except": _obj("r-dhcp", "range-dhcp", "address-range")})]
GATEWAYS = [_obj("gw-1", "cp-gw-01", "simple-gateway", **{"ipv4-address": "192.0.2.1"}),
            _obj("mgmt-1", "cp-mgmt", "checkpoint-host", **{"ipv4-address": "192.0.2.2"})]


def _rule(uid: str, no: int, name: str, src: list[str], dst: list[str], action: str, **kw: Any) -> dict[str, Any]:
    return {"uid": uid, "type": "access-rule", "rule-number": no, "name": name, "source": src,
            "source-negate": False, "destination": dst, "destination-negate": False, "service": [HTTPS["uid"]],
            "action": action, "enabled": True, "install-on": [TARGETS["uid"]], "comments": "",
            "hits": {"percentage": "0%", "level": "low", "value": 7,
                     "last-date": {"posix": 1791158400000, "iso-8601": "2026-10-01T08:00+0800"}}, **kw}


NETWORK_LAYER = [
    {"type": "access-section", "name": "Web", "rulebase": [
        _rule("rl-1", 1, "web in", [ANY["uid"]], ["h-web"], ACCEPT["uid"]),
        _rule("rl-2", 2, "servers", ["n-lan"], ["g-srv"], ACCEPT["uid"]),
    ]},
    _rule("rl-3", 3, "not lan", ["n-lan"], [ANY["uid"]], DROP["uid"], **{"source-negate": True}),
    _rule("rl-4", 4, "apps", [ANY["uid"]], [ANY["uid"]], APPLY["uid"], **{"inline-layer": "lay-apps"}),
    _rule("rl-5", 5, "off", ["h-db"], [ANY["uid"]], DROP["uid"], enabled=False),
]
APPS_LAYER = [_rule("rl-a1", 1, "app web", ["x-lan"], ["h-web"], ACCEPT["uid"])]
# 物件字典的項目是精簡版：沒有 nat-settings、群組沒有 members
DICT = [ANY, ACCEPT, DROP, APPLY, ORIGINAL, TARGETS, HTTPS,
        *({k: v for k, v in o.items() if k not in ("nat-settings", "members")} for o in HOSTS),
        *NETWORKS, *RANGES, *({k: v for k, v in o.items() if k != "members"} for o in GROUPS), *GWE]
# 實機的形狀：手動規則在最上面（不在段落裡）、自動產生的規則在「Automatic Generated Rules : …」段落；
# 沒有 name 欄位
NAT_RULES = [
    {"uid": "nat-1", "type": "nat-rule", "rule-number": 1, "method": "static", "enabled": True, "auto-generated": False,
     "original-source": ANY["uid"], "original-destination": "h-pub", "original-service": ANY["uid"],
     "translated-source": ORIGINAL["uid"], "translated-destination": "h-web", "translated-service": ORIGINAL["uid"],
     "comments": ""},
    {"type": "nat-section", "name": "Automatic Generated Rules : Machine Static NAT", "rulebase": [
        # 主機自動靜態 NAT 的兩條：出向（來源換成對外位址）與入向（對外位址 → 主機）
        {"uid": "nat-a1", "type": "nat-rule", "rule-number": 2, "method": "static", "enabled": True,
         "auto-generated": True, "original-source": "h-web", "original-destination": ANY["uid"],
         "original-service": ANY["uid"], "translated-source": "h-web", "translated-destination": ORIGINAL["uid"],
         "translated-service": ORIGINAL["uid"], "comments": ""},
        {"uid": "nat-a2", "type": "nat-rule", "rule-number": 3, "method": "static", "enabled": True,
         "auto-generated": True, "original-source": ANY["uid"], "original-destination": "h-web",
         "original-service": ANY["uid"], "translated-source": ORIGINAL["uid"], "translated-destination": "h-web",
         "translated-service": ORIGINAL["uid"], "comments": ""},
    ]},
    {"type": "nat-section", "name": "Manual Lower Rules", "rulebase": [
        {"uid": "nat-2", "type": "nat-rule", "rule-number": 4, "method": "hide", "enabled": True,
         "auto-generated": False, "original-source": "n-lan", "original-destination": ANY["uid"],
         "original-service": ANY["uid"], "translated-source": "h-pub", "translated-destination": ORIGINAL["uid"],
         "translated-service": ORIGINAL["uid"], "comments": "outbound"},
    ]},
]


def _page(items: list[Any], body: dict[str, Any], key: str = "objects") -> dict[str, Any]:
    off, lim = int(body.get("offset") or 0), int(body.get("limit") or 50)
    chunk = items[off:off + lim]
    return {key: chunk, "from": off + 1 if chunk else 0, "to": off + len(chunk), "total": len(items)}


class CheckPointMock(MockServer):
    def __init__(self, *, page_limit_cap: int = 2, dereference: bool = True) -> None:
        super().__init__()
        self.dereference = dereference   # False＝舊版不認得 dereference-group-members，成員一律 uid
        self.sessions: dict[str, dict[str, Any]] = {}
        self.logged_out: list[str] = []
        self.cap = page_limit_cap          # 每頁最多幾筆（測分頁）
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}
        for cmd in ("login", "logout", "show-api-versions", "show-gateways-and-servers", "show-hosts", "show-networks",
                    "show-address-ranges", "show-groups", "show-groups-with-exclusion", "show-packages",
                    "show-access-rulebase", "show-nat-rulebase", "show-session", "show-object"):
            self.route("POST", f"/web_api/{cmd}", self._handler(cmd))

    def _handler(self, cmd: str):  # type: ignore[no-untyped-def]
        def h(req: dict[str, Any]):  # type: ignore[no-untyped-def]
            body = json.loads(req["body"] or b"{}")
            if cmd in self.fail:
                st, out = self.fail[cmd]
                return st, [("Content-Type", "application/json")], json.dumps(out).encode()
            if cmd == "login":
                if body.get("read-only") and any(k in body for k in ("session-name", "session-description",
                                                                      "session-comments")):
                    return 400, [("Content-Type", "application/json")], json.dumps(
                        {"code": "generic_err_invalid_parameter",
                         "message": "session-name/session-comments/session-description are unexpected, "
                                    "when login is done in the readonly mode."}).encode()
                if body.get("api-key") != API_KEY:
                    return 400, [("Content-Type", "application/json")], json.dumps(
                        {"code": "err_login_failed", "message": "Authentication to server failed."}).encode()
                sid = f"sid-{len(self.sessions) + 1}"
                self.sessions[sid] = body
                return self._json({"sid": sid, "api-server-version": "1.9", "session-timeout": 600, "uid": "s"})
            sid = req["headers"].get("x-chkp-sid")
            if sid not in self.sessions:
                return 401, [("Content-Type", "application/json")], json.dumps(
                    {"code": "generic_err_wrong_session_id", "message": "Wrong session id"}).encode()
            if cmd == "logout":
                self.logged_out.append(sid)
                return self._json({"message": "OK"})
            capped = {**body, "limit": min(int(body.get("limit") or 50), self.cap)}
            if cmd == "show-api-versions":
                return self._json({"current-version": "1.9", "supported-versions": ["1.8", "1.9"]})
            if cmd == "show-gateways-and-servers":
                return self._json(_page(GATEWAYS, capped))
            table = {"show-hosts": HOSTS, "show-networks": NETWORKS, "show-address-ranges": RANGES,
                     "show-groups": GROUPS, "show-groups-with-exclusion": GWE}
            if cmd in table:
                items = table[cmd]
                if cmd == "show-groups" and body.get("dereference-group-members") and self.dereference:
                    # 實機：dereference-group-members 時成員是帶名稱的物件
                    by_uid = {o["uid"]: o for o in (*HOSTS, *NETWORKS, *RANGES, *GROUPS, *GWE)}
                    items = [{**g, "members": [{k: v for k, v in by_uid[m].items() if k in ("uid", "name", "type")}
                                               for m in g["members"]]} for g in items]
                return self._json(_page(items, capped))
            if cmd == "show-object":
                full = {o["uid"]: o for o in (*HOSTS, *NETWORKS, *RANGES, *GROUPS, *GWE)}
                o = full.get(body.get("uid"))
                if o is None:
                    return 404, [("Content-Type", "application/json")], json.dumps(
                        {"code": "generic_err_object_not_found", "message": "Requested object not found"}).encode()
                return self._json({"object": o})
            if cmd == "show-packages":
                return self._json(_page([{"name": "Standard", "uid": "pkg-1", "access": True, "nat-policy": True,
                                          "access-layers": [{"name": "Network", "uid": "lay-net"}]}], capped, "packages"))
            if cmd == "show-access-rulebase":
                layer = NETWORK_LAYER if body.get("name") in ("Network", "lay-net") or body.get("uid") == "lay-net" \
                    else APPS_LAYER
                out = _page(layer, capped, "rulebase")
                out.update({"name": body.get("name") or body.get("uid"), "objects-dictionary": DICT})
                return self._json(out)
            if cmd == "show-nat-rulebase":
                out = _page(NAT_RULES, capped, "rulebase")
                out["objects-dictionary"] = DICT
                return self._json(out)
            return 404, [], b"{}"
        return h

    @staticmethod
    def _json(obj: dict[str, Any]):  # type: ignore[no-untyped-def]
        return 200, [("Content-Type", "application/json")], json.dumps(obj).encode()
