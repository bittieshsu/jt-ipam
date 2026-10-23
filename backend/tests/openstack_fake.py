"""假的 OpenStack（Keystone／Nova／Neutron）＋ CubeCOS，給 httpx.MockTransport 用。

回應的 JSON 形狀照官方 API 文件與 DevStack 的真實回應（欄位名稱、分頁的 `*_links`、Keystone 的
X-Subject-Token、Nova 的版本文件、Neutron 的 `binding:host_id`、CubeCOS 的 `{code,status,msg,data}` 包裝），
欄位多帶一些用不到的，確保程式不是靠「剛好只有這幾個欄位」才對。

位址一律用 TEST-NET（192.0.2.0/24、198.51.100.0/24、203.0.113.0/24）：IP 字面值不必查 DNS，SSRF 檢查照常跑。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

HOST = "192.0.2.10"
KEYSTONE = f"https://{HOST}:5000"
NOVA = f"https://{HOST}:8774/v2.1"
NEUTRON = f"https://{HOST}:9696"
CUBECOS = f"https://{HOST}"


def uid(n: int) -> str:
    return str(uuid.UUID(int=n))


ADMIN_PROJECT = uid(0x1000)
DEMO_PROJECT = uid(0x2000)
ALT_PROJECT = uid(0x3000)


def server(sid: str, name: str, *, status: str = "ACTIVE", project: str = DEMO_PROJECT,
           host: str | None = "compute-1.example", flavor: dict[str, Any] | None = None,
           hide_host: bool = False, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": sid, "name": name, "status": status, "tenant_id": project, "user_id": uid(0x9999),
        "metadata": {}, "hostId": "a1b2c3", "image": {"id": uid(0x7777), "links": []},
        "flavor": flavor if flavor is not None else {
            "vcpus": 2, "ram": 4096, "disk": 40, "ephemeral": 0, "swap": 0,
            "original_name": "m1.medium", "extra_specs": {}},
        "created": "2026-10-01T00:00:00Z", "updated": "2026-10-20T00:00:00Z",
        "addresses": {}, "accessIPv4": "", "accessIPv6": "", "links": [],
        "OS-DCF:diskConfig": "MANUAL", "progress": 0, "OS-EXT-AZ:availability_zone": "nova",
        "config_drive": "", "key_name": None, "OS-SRV-USG:launched_at": "2026-10-01T00:01:00.000000",
        "OS-SRV-USG:terminated_at": None, "OS-EXT-STS:task_state": None, "OS-EXT-STS:vm_state": "active",
        "OS-EXT-STS:power_state": 1, "os-extended-volumes:volumes_attached": [], "locked": False,
        "description": None, "tags": [], "security_groups": [{"name": "default"}],
    }
    if not hide_host:
        out["OS-EXT-SRV-ATTR:host"] = (host or "").split(".")[0] or None
        out["OS-EXT-SRV-ATTR:instance_name"] = "instance-00000001"
        out["OS-EXT-SRV-ATTR:hypervisor_hostname"] = host
    out.update(extra)
    return out


def port(pid: str, *, device_id: str = "", owner: str = "compute:nova", network: str = uid(0x500),
         project: str = DEMO_PROJECT, mac: str = "fa:16:3e:00:00:01",
         fixed: list[tuple[str, str]] | None = None, binding_host: str | None = "compute-1",
         hide_binding: bool = False, status: str = "ACTIVE") -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": pid, "name": "", "network_id": network, "tenant_id": project, "project_id": project,
        "mac_address": mac, "admin_state_up": True, "status": status, "device_id": device_id,
        "device_owner": owner,
        "fixed_ips": [{"subnet_id": s, "ip_address": ip} for s, ip in (fixed or [])],
        "allowed_address_pairs": [], "extra_dhcp_opts": [], "security_groups": [uid(0x5ec)],
        "description": "", "port_security_enabled": True, "tags": [], "revision_number": 4,
        "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z",
    }
    if not hide_binding:
        out.update({"binding:vnic_type": "normal", "binding:profile": {}, "binding:host_id": binding_host or "",
                    "binding:vif_type": "ovs", "binding:vif_details": {"port_filter": True}})
    return out


def subnet(sid: str, cidr: str, *, network: str = uid(0x500), project: str = DEMO_PROJECT,
           name: str = "private-subnet", gateway: str | None = None) -> dict[str, Any]:
    v = 6 if ":" in cidr else 4
    return {
        "id": sid, "name": name, "tenant_id": project, "project_id": project, "network_id": network,
        "ip_version": v, "subnetpool_id": None, "enable_dhcp": True, "ipv6_ra_mode": None,
        "ipv6_address_mode": None, "gateway_ip": gateway, "cidr": cidr,
        "allocation_pools": [{"start": "x", "end": "y"}], "host_routes": [], "dns_nameservers": [],
        "description": "", "service_types": [], "tags": [], "revision_number": 0,
        "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
    }


def network(nid: str, name: str, *, project: str = DEMO_PROJECT, shared: bool = False,
            external: bool = False) -> dict[str, Any]:
    return {
        "id": nid, "name": name, "tenant_id": project, "project_id": project, "admin_state_up": True,
        "mtu": 1450, "status": "ACTIVE", "subnets": [], "shared": shared, "availability_zone_hints": [],
        "availability_zones": ["nova"], "ipv4_address_scope": None, "ipv6_address_scope": None,
        "router:external": external, "description": "", "port_security_enabled": True, "tags": [],
        "revision_number": 1, "provider:network_type": "vxlan", "provider:segmentation_id": 1001,
    }


def floating(fid: str, addr: str, *, port_id: str | None, fixed: str | None = None,
             router: str | None = uid(0x600), project: str = DEMO_PROJECT) -> dict[str, Any]:
    return {
        "id": fid, "tenant_id": project, "project_id": project, "floating_ip_address": addr,
        "floating_network_id": uid(0x501), "router_id": router if port_id else None, "port_id": port_id,
        "fixed_ip_address": fixed, "status": "ACTIVE" if port_id else "DOWN", "description": "",
        "dns_domain": "", "dns_name": "", "tags": [], "revision_number": 2,
    }


@dataclass
class FakeCloud:
    """一朵假的雲。改欄位就能模擬各種情況；`calls` 記下每個請求（方法、網址、標頭——不含內容）。"""

    servers: list[dict[str, Any]] = field(default_factory=list)
    ports: list[dict[str, Any]] = field(default_factory=list)
    subnets: list[dict[str, Any]] = field(default_factory=list)
    networks: list[dict[str, Any]] = field(default_factory=list)
    floatingips: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=lambda: [
        {"id": ADMIN_PROJECT, "name": "admin", "domain_id": "default", "enabled": True},
        {"id": DEMO_PROJECT, "name": "demo", "domain_id": "default", "enabled": True},
        {"id": ALT_PROJECT, "name": "alt_demo", "domain_id": "default", "enabled": True},
    ])
    flavors: dict[str, dict[str, Any]] = field(default_factory=dict)
    nova_max: str = "2.96"
    page_size: int = 1000                       # 伺服器端的 max_limit
    region_two: bool = True                     # 目錄裡另有 RegionTwo
    neutron_catalog_suffix: str = ""            # 例如 "/v2.0"
    token_ttl: timedelta = timedelta(hours=1)
    token_scope_project: str = ADMIN_PROJECT
    password: str = "Secret-2026"
    app_cred: tuple[str, str] = ("appcred-1", "appcred-secret")
    forbid_all_tenants: bool = False            # Nova 拒絕 all_tenants=1
    forbid_projects_list: bool = False          # /v3/projects 403（一般使用者）
    unauthorized_next: int = 0                  # 接下來 N 個服務請求回 401（token 被撤銷）
    fail_page: dict[str, int] = field(default_factory=dict)   # 資源 → 第幾頁（從 1 起）回 500
    forbid_page: dict[str, int] = field(default_factory=dict)  # 資源 → 第幾頁回 403（分頁途中權限被收回）
    cubecos_mode: str = "ok"                    # ok／redirect／html／unauthorized／down
    cubecos_prefix: str = ""                    # 有些部署把 API 放在 /cos-api/ 底下
    cubecos_nodes: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    tokens_issued: int = 0
    auth_bodies: list[dict[str, Any]] = field(default_factory=list)
    _valid: set[str] = field(default_factory=set)

    # ── 傳輸層 ──
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client_factory(self) -> Any:
        def _make(verify: Any, timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=self.transport(), timeout=timeout, follow_redirects=False)
        return _make

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hdrs = {k.lower(): v for k, v in request.headers.items()}
        self.calls.append((request.method, url, hdrs))
        sp = urlsplit(url)
        q = {k: v[-1] for k, v in parse_qs(sp.query).items()}
        base = f"{sp.scheme}://{sp.netloc}"
        path = sp.path
        if base == KEYSTONE:
            return self._keystone(request, path, hdrs)
        if base == CUBECOS:
            if self.cubecos_prefix:
                if not path.startswith(self.cubecos_prefix + "/"):
                    return httpx.Response(404, json={"code": 404, "status": "not found", "msg": "no route"})
                path = path[len(self.cubecos_prefix):]
            return self._cubecos(request, path, hdrs)
        if not self._authorized(hdrs):
            return httpx.Response(401, json={"error": {"code": 401, "message": "auth required"}})
        if url.startswith(NOVA):
            return self._nova(request, path[len(urlsplit(NOVA).path):], q, hdrs)
        if base == NEUTRON:
            return self._neutron(path, q)
        return httpx.Response(404, json={"error": "no route"})

    def _authorized(self, hdrs: dict[str, str]) -> bool:
        if self.unauthorized_next > 0:
            self.unauthorized_next -= 1
            return False
        return hdrs.get("x-auth-token") in self._valid

    # ── Keystone ──
    def _keystone(self, request: httpx.Request, path: str, hdrs: dict[str, str]) -> httpx.Response:
        if request.method == "POST" and path == "/v3/auth/tokens":
            body = json.loads(request.content)
            self.auth_bodies.append(body)
            ident = body["auth"]["identity"]
            ok = False
            if ident["methods"] == ["password"]:
                ok = ident["password"]["user"]["password"] == self.password
            elif ident["methods"] == ["application_credential"]:
                ac = ident["application_credential"]
                ok = (ac.get("id"), ac.get("secret")) == self.app_cred
            if not ok:
                return httpx.Response(401, json={"error": {"code": 401, "title": "Unauthorized",
                                                           "message": "The request you have made requires authentication."}})
            self.tokens_issued += 1
            tok = f"gAAAAAB-token-{self.tokens_issued}"
            self._valid = {tok}                    # 舊 token 失效（模擬撤銷／到期）
            exp = (datetime.now(UTC) + self.token_ttl).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
            return httpx.Response(201, headers={"X-Subject-Token": tok}, json={"token": {
                "methods": ident["methods"], "expires_at": exp, "issued_at": "2026-10-22T00:00:00.000000Z",
                "audit_ids": ["abc"], "is_domain": False,
                "user": {"id": uid(0x9999), "name": "ipam-reader", "domain": {"id": "default", "name": "Default"}},
                "project": {"id": self.token_scope_project, "name": "admin",
                            "domain": {"id": "default", "name": "Default"}},
                "roles": [{"id": uid(0x8888), "name": "reader"}],
                "catalog": self._catalog(),
            }})
        if hdrs.get("x-auth-token") not in self._valid:
            return httpx.Response(401, json={"error": {"code": 401}})
        if path == "/v3/projects":
            if self.forbid_projects_list:
                return httpx.Response(403, json={"error": {"code": 403}})
            return httpx.Response(200, json={"projects": self.projects,
                                             "links": {"self": f"{KEYSTONE}/v3/projects", "previous": None, "next": None}})
        if path == "/v3/auth/projects":
            return httpx.Response(200, json={"projects": [p for p in self.projects if p["id"] == self.token_scope_project],
                                             "links": {"self": "", "previous": None, "next": None}})
        return httpx.Response(404, json={"error": {"code": 404}})

    def _catalog(self) -> list[dict[str, Any]]:
        def eps(url: str, other: str) -> list[dict[str, Any]]:
            out = [
                {"id": uuid.uuid4().hex, "interface": "public", "region_id": "RegionOne", "region": "RegionOne", "url": url},
                {"id": uuid.uuid4().hex, "interface": "internal", "region_id": "RegionOne", "region": "RegionOne",
                 "url": url.replace(HOST, "10.255.0.10")},
            ]
            if self.region_two:
                out.append({"id": uuid.uuid4().hex, "interface": "public", "region_id": "RegionTwo",
                            "region": "RegionTwo", "url": other})
            return out
        return [
            {"id": uuid.uuid4().hex, "type": "identity", "name": "keystone",
             "endpoints": eps(f"{KEYSTONE}/v3", "https://198.51.100.99:5000/v3")},
            {"id": uuid.uuid4().hex, "type": "compute", "name": "nova",
             "endpoints": eps(NOVA, "https://198.51.100.99:8774/v2.1")},
            {"id": uuid.uuid4().hex, "type": "network", "name": "neutron",
             "endpoints": eps(NEUTRON + self.neutron_catalog_suffix, "https://198.51.100.99:9696")},
            {"id": uuid.uuid4().hex, "type": "image", "name": "glance",
             "endpoints": eps(f"https://{HOST}:9292", "https://198.51.100.99:9292")},
        ]

    # ── Nova ──
    def _nova(self, request: httpx.Request, path: str, q: dict[str, str], hdrs: dict[str, str]) -> httpx.Response:
        if path in ("", "/"):
            return httpx.Response(200, json={"version": {
                "id": "v2.1", "status": "CURRENT", "version": self.nova_max, "min_version": "2.1",
                "updated": "2013-07-23T11:33:21Z", "links": [{"rel": "self", "href": f"{NOVA}/"}],
                "media-types": [{"base": "application/json", "type": "application/vnd.openstack.compute+json;version=2.1"}]}})
        if path == "/servers/detail":
            if q.get("all_tenants") and self.forbid_all_tenants:
                return httpx.Response(403, json={"forbidden": {"code": 403, "message": "Policy doesn't allow"}})
            items = self.servers if q.get("all_tenants") else [
                s for s in self.servers if s.get("tenant_id") == self.token_scope_project]
            items = self._embed_flavor(items, hdrs)
            return self._page("servers", items, q, f"{NOVA}/servers/detail")
        if path.startswith("/flavors/"):
            fid = path.split("/")[-1]
            if fid not in self.flavors:
                return httpx.Response(404, json={"itemNotFound": {"code": 404, "message": "Flavor not found"}})
            return httpx.Response(200, json={"flavor": self.flavors[fid]})
        return httpx.Response(404, json={"itemNotFound": {"code": 404}})

    def _embed_flavor(self, items: list[dict[str, Any]], hdrs: dict[str, str]) -> list[dict[str, Any]]:
        mv = (hdrs.get("openstack-api-version") or "compute 2.1").split()[-1]
        if tuple(int(x) for x in mv.split(".")) >= (2, 47):
            return items
        # 2.47 之前：flavor 只有 id 與連結
        out = []
        for s in items:
            s = dict(s)
            if isinstance(s.get("flavor"), dict) and "vcpus" in s["flavor"]:
                s["flavor"] = {"id": s["flavor"].get("id", "m1.medium"), "links": []}
            out.append(s)
        return out

    # ── Neutron ──
    def _neutron(self, path: str, q: dict[str, str]) -> httpx.Response:
        if not path.startswith("/v2.0/") or "/v2.0/v2.0/" in path:
            return httpx.Response(404, json={"NeutronError": {"type": "HTTPNotFound", "message": "bad path"}})
        res = path[len("/v2.0/"):].rstrip("/")
        if res == "extensions":
            return httpx.Response(200, json={"extensions": [
                {"name": "Port Binding", "alias": "binding", "description": "", "updated": ""},
                {"name": "Neutron L3 Router", "alias": "router", "description": "", "updated": ""},
                {"name": "Pagination support", "alias": "pagination", "description": "", "updated": ""}]})
        data = {"ports": self.ports, "subnets": self.subnets, "networks": self.networks,
                "floatingips": self.floatingips}.get(res)
        if data is None:
            return httpx.Response(404, json={"NeutronError": {"type": "HTTPNotFound", "message": res}})
        return self._page(res, data, q, f"{NEUTRON}/v2.0/{res}")

    def _page(self, key: str, items: list[dict[str, Any]], q: dict[str, str], self_url: str) -> httpx.Response:
        limit = min(int(q.get("limit") or self.page_size), self.page_size)
        start = 0
        if q.get("marker"):
            ids = [i["id"] for i in items]
            if q["marker"] not in ids:
                return httpx.Response(400, json={"badRequest": {"code": 400, "message": "marker not found"}})
            start = ids.index(q["marker"]) + 1
        page_no = start // max(limit, 1) + 1
        if self.forbid_page.get(key) == page_no:
            return httpx.Response(403, json={"forbidden": {"code": 403, "message": "Policy doesn't allow"}})
        if self.fail_page.get(key) == page_no:
            return httpx.Response(500, json={"computeFault": {"code": 500, "message": "boom"}})
        chunk = items[start:start + limit]
        body: dict[str, Any] = {key: chunk}
        if len(chunk) == limit and chunk:
            body[f"{key}_links"] = [{"rel": "next", "href": f"{self_url}?limit={limit}&marker={chunk[-1]['id']}"}]
        return httpx.Response(200, json=body)

    # ── CubeCOS ──
    def _cubecos(self, request: httpx.Request, path: str, hdrs: dict[str, str]) -> httpx.Response:
        if self.cubecos_mode == "down":
            raise httpx.ConnectError("Connection refused", request=request)
        if path == "/api/v1/datacenters":
            return httpx.Response(200, json={"code": 200, "status": "ok", "msg": "fetch data center list successfully",
                                             "data": [{"type": "cloud", "name": "dc-tpe", "roles": ["control"],
                                                       "registeredRoles": [], "version": "3.0.1",
                                                       "virtualIp": HOST, "isLocal": True, "isHaEnabled": True,
                                                       "firmware": {"version": "3.0.1", "updatedAt": ""},
                                                       "fixpack": {"name": "", "version": "", "updatedAt": ""},
                                                       "additional": {"nodeLicenseStatus": {"valid": 3, "expired": 0,
                                                                                            "unlicense": 0}}}]})
        if path == "/api/v1/datacenters/dc-tpe/nodes":
            if self.cubecos_mode == "redirect":
                return httpx.Response(302, headers={"Location": f"{CUBECOS}/saml/login?RelayState=x"})
            if self.cubecos_mode == "html":
                return httpx.Response(200, headers={"Content-Type": "text/html"},
                                      content=b"<!DOCTYPE html><html><body>Login</body></html>")
            if self.cubecos_mode == "unauthorized" or hdrs.get("x-auth-token") not in self._valid:
                # v3.1.0 實際行為：401＋JSON 信封，msg 是 Keycloak SAML 登入網址，另外設 saml_* cookie（沒有 302）
                return httpx.Response(401, headers={"Set-Cookie": "saml_abc=1; Path=/"},
                                      json={"code": 401, "data": None, "status": "unauthorized",
                                            "msg": f"{CUBECOS}/auth/realms/master/protocol/saml?SAMLRequest=x"})
            if q_has_paging(request):
                return httpx.Response(400, json={"code": 400, "status": "bad request", "msg": "paging is broken"})
            return httpx.Response(200, json={"code": 200, "status": "ok", "msg": "fetch nodes list successfully",
                                             "data": {"nodes": self.cubecos_nodes,
                                                      "page": {"total": 1, "number": 1, "size": len(self.cubecos_nodes),
                                                               "totalItemCount": len(self.cubecos_nodes)}}})
        return httpx.Response(404, json={"code": 404, "status": "not found", "msg": "no route"})


def q_has_paging(request: httpx.Request) -> bool:
    """CubeCOS 的節點分頁有 bug（先切頁再排序、順序隨機）：程式不可以帶 pageNum／pageSize。"""
    q = parse_qs(urlsplit(str(request.url)).query)
    return any(k.lower() in ("pagenum", "pagesize", "page", "size") for k in q)


def cube_node(hostname: str, *, role: str = "control-converged", status: str = "up", mgmt: str = "192.0.2.11",
              ip: str | None = None, storage: str = "10.2.0.11", serial: str = "SN-0001") -> dict[str, Any]:
    # 上線的節點 ip 等於管理 IP；只有離線的節點才看得到 provider IP（v3.1.0 原始碼）
    ip = ip or mgmt
    return {"id": uuid.uuid4().hex, "serialNumber": serial, "boardSerial": "BS-1", "dataCenter": "dc-tpe",
            "hostname": hostname, "role": role, "protocol": "https", "address": f"{ip}:8082", "ip": ip,
            "managementIP": mgmt, "storageIP": storage, "cpuSpec": "Intel Xeon", "networkInterfaces": [],
            "blockDevices": [], "vcpu": {}, "memory": {}, "storage": {}, "isVirtualIpOwner": False,
            "ipmi": {"isSupported": False}, "license": {}, "firmware": {}, "status": status, "uptimeSeconds": 12345.0}
