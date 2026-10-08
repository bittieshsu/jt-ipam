"""模擬 Check Point 閘道的 Gaia API（R81.20 T634 實機是 **Gaia API 1.6**）。

形狀照官方 Gaia API Reference 與官方 Ansible 模組（check_point.gaia），**2026-10-08 已對實機（VM，R81.20 T634）校正**：
- 登入回 `sid`、`api-server-version`、`read-only`、`session-timeout`、`url`（第二次起多 `last-login-was-at`）
- `show-dhcp-server` 的 `netmask`、`default-lease`、`max-lease` 是**字串**（"24"、"43200"），`dns` 一定有 `tertiary`（可能空字串）
- `run-script` 回 `task-id`；`show-task` 的 `task-details[0].output` 是 **base64**
- 唯讀角色（monitorRole）可以登入、可以 `show-dhcp-server`，`run-script` 回 **HTTP 500**
  `generic_err_no_permissions`（不是 403）
- `show-roles` 在 1.6 是 404（1.7 才有）

- `POST /gaia_api/<指令>`，JSON；`login`（`user`／`password`）→ `sid`，之後帶 `X-chkp-sid`；`logout`
- `show-dhcp-server`：`enabled`＋`subnets`（`subnet`、`netmask`、`ip-pools`（start/end/include/enabled）、
  `default-gateway`、`dns`（domain-name/primary/secondary/tertiary）、`default-lease`、`max-lease`、`enabled`）
- `run-script`（`script`）→ `task-id`；`show-task`（`task-id`）→ `tasks[0]`：`status`、`task-details[0]`
  的 `output`／`error`／`return-value`
沒有讀租約、ARP 的指令 —— 那些只能靠 run-script 跑固定的讀檔／讀表指令。

實機回應不同時以實機為準，改這裡與 services/checkpoint_gaia.py。
"""
from __future__ import annotations

import base64
import json
from typing import Any

from tests.isoinsight_mock import MockServer

USER, PASSWORD = "jtipam", "gaia-pass-0123456789"
RO_USER = "jtipam-ro"           # 唯讀角色：run-script 會被拒絕

DHCP = {
    "enabled": True,
    "subnets": [
        {"subnet": "198.51.100.0", "netmask": "24", "enabled": True, "default-lease": "43200", "max-lease": "86400",
         "default-gateway": "198.51.100.1",
         "dns": {"domain-name": "lab.example.test", "primary": "198.51.100.53", "secondary": "198.51.100.54",
                 "tertiary": ""},
         "ip-pools": [
             {"start": "198.51.100.100", "end": "198.51.100.150", "include": "include", "enabled": True},
             {"start": "198.51.100.120", "end": "198.51.100.125", "include": "exclude", "enabled": True},
             {"start": "198.51.100.200", "end": "198.51.100.210", "include": "include", "enabled": False},
         ]},
        # 停用的子網路：不發位址
        {"subnet": "203.0.113.0", "netmask": "25", "enabled": False, "default-lease": "3600", "max-lease": "7200",
         "default-gateway": "203.0.113.1",
         "dns": {"domain-name": "", "primary": "203.0.113.53", "secondary": "", "tertiary": ""},
         "ip-pools": [{"start": "203.0.113.10", "end": "203.0.113.20", "include": "include", "enabled": True}]},
    ],
}

# `ip -s neigh show`：used <用過>/<確認過>/<更新過> 秒（實機格式，例如 `used 81/1941/81 probes 0 STALE`）
NEIGH = """\
198.51.100.10 dev eth1 lladdr 00:00:5e:00:53:10 ref 1 used 12/8/5 probes 1 REACHABLE
198.51.100.20 dev eth1 lladdr 00:00:5e:00:53:20 used 600/540/500 probes 4 STALE
198.51.100.30 dev eth1 lladdr 00:00:5e:00:53:30 PERMANENT
198.51.100.40 dev eth1  used 3/3/3 probes 6 FAILED
198.51.100.41 dev eth1 INCOMPLETE
192.0.2.254 dev eth0 lladdr 00:00:5e:00:53:fe router used 30/2/2 probes 1 REACHABLE
fe80::1 dev eth0 lladdr 00:00:5e:00:53:01 router STALE
"""

LEASES = """\
# The format of this file is documented in the dhcpd.leases(5) manual page.
lease 198.51.100.10 {
  starts 4 2026/10/08 01:00:00;
  ends 4 2099/10/08 13:00:00;
  binding state active;
  hardware ethernet 00:00:5e:00:53:10;
  client-hostname "web-01";
}
lease 198.51.100.20 {
  starts 4 2026/10/01 01:00:00;
  ends 4 2026/10/01 13:00:00;
  binding state free;
  hardware ethernet 00:00:5e:00:53:20;
}
lease 198.51.100.21 {
  starts 4 2026/10/08 01:00:00;
  ends never;
  binding state active;
  hardware ethernet 00:00:5e:00:53:21;
  client-hostname "printer \\"lobby\\"";
}
lease 198.51.100.10 {
  starts 4 2026/10/08 02:00:00;
  ends 4 2099/10/08 14:00:00;
  binding state active;
  hardware ethernet 00:00:5e:00:53:10;
  client-hostname "web-01";
}
"""


class GaiaMock(MockServer):
    def __init__(self, *, base64_output: bool = True) -> None:
        super().__init__()
        self.sessions: dict[str, str] = {}      # sid → user
        self.logged_out: list[str] = []
        self.scripts: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.base64_output = base64_output
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}
        self.outputs: dict[str, str] = {"ip -s neigh": NEIGH, "dhcpd.leases": "JTIPAM_SIZE 1234\n" + LEASES,
                                        "echo jt-ipam": "jt-ipam\n"}
        self.pending_polls = 1                  # show-task 先回幾次 in progress
        self.dhcp: dict[str, Any] = DHCP         # show-dhcp-server 的回應（測試可換成伺服器關閉）
        for cmd in ("login", "logout", "show-api-versions", "show-dhcp-server", "run-script", "show-task"):
            self.route("POST", f"/gaia_api/{cmd}", self._handler(cmd))

    @staticmethod
    def _json(obj: Any, status: int = 200):  # type: ignore[no-untyped-def]
        return status, [("Content-Type", "application/json")], json.dumps(obj).encode()

    def _handler(self, cmd: str):  # type: ignore[no-untyped-def]
        def h(req: dict[str, Any]):  # type: ignore[no-untyped-def]
            body = json.loads(req["body"] or b"{}")
            if cmd in self.fail:
                st, out = self.fail[cmd]
                return self._json(out, st)
            if cmd == "login":
                if body.get("password") != PASSWORD or body.get("user") not in (USER, RO_USER):
                    return self._json({"code": "err_login_failed", "message": "Authentication to server failed."}, 400)
                sid = f"gsid-{len(self.sessions) + 1}"
                self.sessions[sid] = body["user"]
                return self._json({"sid": sid, "session-timeout": 600, "api-server-version": "1.6", "read-only": False,
                                   "url": "https://127.0.0.1:443/gaia_api"})
            sid = req["headers"].get("x-chkp-sid")
            if sid not in self.sessions:
                return self._json({"code": "generic_err_wrong_session_id", "message": "Wrong session id"}, 401)
            if cmd == "logout":
                self.logged_out.append(sid)
                return self._json({"message": "OK"})
            if cmd == "show-api-versions":
                return self._json({"current-version": "1.6",
                                   "supported-versions": ["1", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6"]})
            if cmd == "show-dhcp-server":
                return self._json(self.dhcp)
            if cmd == "run-script":
                if self.sessions[sid] == RO_USER:
                    # 實機：HTTP 500，不是 403
                    return self._json({"code": "generic_err_no_permissions",
                                       "errors": "User doesn't have permission to perform this action",
                                       "message": "No Permission"}, 500)
                script = str(body.get("script") or "")
                self.scripts.append(script)
                out = next((v for k, v in self.outputs.items() if k in script), "")
                tid = f"task-{len(self.tasks) + 1}"
                self.tasks[tid] = {"output": out, "polls": self.pending_polls}
                return self._json({"task-id": tid})
            if cmd == "show-task":
                t = self.tasks.get(str(body.get("task-id")))
                if t is None:
                    return self._json({"code": "generic_err_object_not_found", "message": "Task not found"}, 404)
                if t["polls"] > 0:
                    t["polls"] -= 1
                    return self._json({"tasks": [{"task-id": body["task-id"], "status": "in progress",
                                                  "progress-percentage": 50}]})
                out = t["output"]
                if self.base64_output:
                    out = base64.b64encode(out.encode()).decode()
                return self._json({"tasks": [{
                    "task-id": body["task-id"], "task-name": "/run-script", "status": "succeeded", "status-code": 200,
                    "progress-percentage": 100, "progress-description": "succeeded", "execution-time": "0.02",
                    "start-time": "2026-10-08T15:46+8.00.0", "last-update-time": "2026-10-08T15:46+8.00.0",
                    "time-spent-in-queue": "0.00",
                    "task-details": [{"output": out, "error": "", "return-value": 0}]}]})
            return 404, [], b"{}"
        return h
