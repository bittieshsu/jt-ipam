#!/usr/bin/env python3
# ruff: noqa: S101, S104, S310, S603 -- 單機測試靶（不隨產品安裝）：assert 是內部一致性檢查、監聽位址由參數指定、只向設定的 Keystone 驗 token、openssl 產生自簽憑證
"""假的 Bigstack CubeCOS API 伺服器，只給 jt-ipam 的 CubeCOS／OpenStack 整合做測試用。

只用 Python 標準函式庫（3.11+），不用 pip。每個欄位名稱、狀態碼、回應外框與行為都照
cube-cos-api v3.1.0（commit e9d4cd2，VERSION 檔為 v3.1.0）的 Go 原始碼，註解裡的
`檔案:行號` 都是相對於該 repo 根目錄。少數行為來自它依賴的上游套件（gin、gocloak、
gophercloud、crewjam/saml、go-micro），註解會註明是哪個套件。

不是原始碼裡有的行為（只為了測試方便），一律在註解寫「非原始碼行為」。

用法：python3 fake_cubecos.py --help
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

# ---------------------------------------------------------------------------
# 預設種子資料（與 seed.example.json 相同）。全部是 RFC 5737／RFC 1918 測試位址，不含任何客戶資料。
# ---------------------------------------------------------------------------
DEFAULT_SEED: dict = {
    "datacenter": {
        "name": "fake-dc",
        "virtual_ip": "192.0.2.10",
        "is_ha_enabled": False,
        # 格式是「產品描述 版本 建置標籤」：internal/cubecos/datacenter.go:84；內容是編的。
        "firmware_version": "CubeCOS 3.1.0 20260901-1200 TZ:+0800",
        # time.LocalRFC3339：internal/cubecos/datacenter.go:264-265
        "firmware_updated_at": "2026-09-01T12:00:00+08:00",
        "utc_time_zone": "+08:00",
        "fixpack": {"name": "", "version": "", "updatedAt": ""},
        # Keycloak（SAML IdP）的埠：configs/cube-cos-api.yaml.template:28
        "keycloak_port": 10443,
        # SAML 驗證後導向的路徑：configs/cube-cos-api.yaml.template:12、internal/runtime/parse.go:220-230
        "redirect_path": "/home",
        # cube-cos-api 自己的 listen port：configs/cube-cos-api.yaml.template:7
        "api_port": 8082,
    },
    "users": [{"name": "admin", "password": "Fake-CubeCOS-Pass1"}],
    "token": {
        # access.token.lifespan＝7200：internal/definition/v1/auths/auths.go:38-40
        "access_ttl": 7200,
        # Keycloak realm 預設的 SSO Session Idle（1800 秒）；非 cube-cos-api 原始碼裡的值。
        "refresh_ttl": 1800,
    },
    "nodes": [
        {
            "hostname": "os-ctrl-1",
            "role": "control-converged",
            "status": "up",
            "management_ip": "192.0.2.11",
            "provider_ip": "198.51.100.11",
            "storage_ip": "10.0.0.11",
            "serial_number": "FAKE-SN-0001",
            "board_serial": "FAKE-BS-0001",
            "cpu_spec": "QEMU Virtual CPU version 2.5+",
            "is_virtual_ip_owner": True,
            "is_gpu_enabled": False,
            "uptime_seconds": 864000.25,
            "firmware": {"active": "CubeCOS 3.1.0 20260901-1200 TZ:+0800", "inactive": ""},
            "vcpu": {"totalCores": 32, "usedCores": 8, "usedPercent": 25, "freeCores": 24, "freePercent": 75},
            "memory": {"totalMiB": 131072, "usedMiB": 65536, "usedPercent": 50, "freeMiB": 65536, "freePercent": 50},
            "storage": {"totalMiB": 1048576, "usedMiB": 262144, "usedPercent": 25, "freeMiB": 786432, "freePercent": 75},
            "network_interfaces": [
                {"interface": "eth0", "label": "eth0", "busIdSlaves": "0000:00:03.0", "driver": "virtio_net",
                 "state": "up", "speed": "10000"},
                {"interface": "eth1", "label": "eth1", "busIdSlaves": "0000:00:04.0", "driver": "virtio_net",
                 "state": "up", "speed": "10000"},
            ],
            "ipmi": {"isSupported": False, "isConnected": False, "ip": ""},
        },
        {
            "hostname": "os-compute-1",
            "role": "compute",
            "status": "up",
            "management_ip": "192.0.2.21",
            "provider_ip": "198.51.100.21",
            "storage_ip": "10.0.0.21",
            "serial_number": "FAKE-SN-0002",
            "board_serial": "FAKE-BS-0002",
            "cpu_spec": "QEMU Virtual CPU version 2.5+",
            "is_virtual_ip_owner": False,
            "is_gpu_enabled": False,
            "uptime_seconds": 432000.5,
            "firmware": {"active": "CubeCOS 3.1.0 20260901-1200 TZ:+0800", "inactive": ""},
            "vcpu": {"totalCores": 64, "usedCores": 16, "usedPercent": 25, "freeCores": 48, "freePercent": 75},
            "memory": {"totalMiB": 262144, "usedMiB": 131072, "usedPercent": 50, "freeMiB": 131072, "freePercent": 50},
            "storage": {"totalMiB": 524288, "usedMiB": 131072, "usedPercent": 25, "freeMiB": 393216, "freePercent": 75},
            "network_interfaces": [
                {"interface": "eth0", "label": "eth0", "busIdSlaves": "0000:00:03.0", "driver": "virtio_net",
                 "state": "up", "speed": "10000"},
            ],
            "ipmi": {"isSupported": False, "isConnected": False, "ip": ""},
        },
        {
            "hostname": "os-compute-2",
            "role": "compute",
            "status": "up",
            "management_ip": "192.0.2.22",
            "provider_ip": "198.51.100.22",
            "storage_ip": "10.0.0.22",
            "serial_number": "FAKE-SN-0003",
            "board_serial": "FAKE-BS-0003",
            "cpu_spec": "QEMU Virtual CPU version 2.5+",
            "is_virtual_ip_owner": False,
            "is_gpu_enabled": False,
            "uptime_seconds": 432001.75,
            "firmware": {"active": "CubeCOS 3.1.0 20260901-1200 TZ:+0800", "inactive": ""},
            "vcpu": {"totalCores": 64, "usedCores": 0, "usedPercent": 0, "freeCores": 64, "freePercent": 100},
            "memory": {"totalMiB": 262144, "usedMiB": 0, "usedPercent": 0, "freeMiB": 262144, "freePercent": 100},
            "storage": {"totalMiB": 524288, "usedMiB": 0, "usedPercent": 0, "freeMiB": 524288, "freePercent": 100},
            "network_interfaces": [
                {"interface": "eth0", "label": "eth0", "busIdSlaves": "0000:00:03.0", "driver": "virtio_net",
                 "state": "up", "speed": "10000"},
            ],
            "ipmi": {"isSupported": False, "isConnected": False, "ip": ""},
        },
    ],
}

# internal/definition/v1/nodes/role.go:48-57
CLOUD_ROLES = ["control-converged", "control", "compute", "storage"]
EDGE_ROLES = ["edge-core", "moderator"]
# internal/apis/api.go:17（V1 = "/api/v1"）
V1 = "/api/v1"
DC = V1 + "/datacenters/:DataCenter"  # internal/runtime/router.go:116-122

LOCK = threading.Lock()
WRITE_CALLS: list[dict] = []
REQUESTS: list[dict] = []
MAX_REQUEST_LOG = 2000


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[fake-cubecos {ts}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# JSON 編碼：模仿 Go encoding/json
# ---------------------------------------------------------------------------
def go_float(x):
    """Go 的 float64 整數值會編成 `64` 而不是 `64.0`。"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0
    if f.is_integer() and abs(f) < 1e21:
        return int(f)
    return f


def go_json(obj) -> bytes:
    """bodies/resp.go:89-99 用 encoding/json.Marshal：預設把 < > & U+2028 U+2029 轉成 \\uXXXX、不留空白。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                    (chr(0x2028), "\\u2028"), (chr(0x2029), "\\u2029")):
        s = s.replace(ch, esc)
    return s.encode("utf-8")


def envelope(code: int, status: str, msg: str, data=None, with_data: bool = False) -> dict:
    """gin.H 是 map，encoding/json 會把 map 的鍵依字母排序：code、data、msg、status。

    data 為 nil 時不放 data 鍵：internal/apis/v1/bodies/resp.go:101-112、114-125；
    錯誤回應（SetBadRequest 以外）本來就沒有 data：resp.go:159-169、219-229。
    """
    out = {"code": code}
    if with_data or data is not None:
        out["data"] = data
    out["msg"] = msg
    out["status"] = status
    return out


# ---------------------------------------------------------------------------
# 設定與種子
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, args: argparse.Namespace):
        seed = json.loads(json.dumps(DEFAULT_SEED))
        if args.seed:
            with open(args.seed, encoding="utf-8") as fh:
                user_seed = json.load(fh)
            for key in ("datacenter", "token"):
                seed[key].update(user_seed.get(key, {}))
            for key in ("users", "nodes"):
                if key in user_seed:
                    seed[key] = user_seed[key]
        hostnames = os.environ.get("CUBECOS_NODE_HOSTNAMES", "").strip()
        if hostnames:
            # 非原始碼行為：用環境變數依序覆蓋種子節點的主機名稱，方便對上 OpenStack 測試靶的 Nova compute host。
            for node, name in zip(seed["nodes"], [h.strip() for h in hostnames.split(",")]):
                if name:
                    node["hostname"] = name
        ttl = os.environ.get("CUBECOS_ACCESS_TTL", "").strip()
        if ttl:
            seed["token"]["access_ttl"] = int(ttl)
        self.seed = seed
        self.dc = seed["datacenter"]
        self.users = {u["name"]: u["password"] for u in seed["users"]}
        self.unauth_mode = os.environ.get("CUBECOS_UNAUTH_MODE", "saml401").strip() or "saml401"
        if self.unauth_mode not in ("saml401", "redirect302", "html200"):
            raise SystemExit(f"CUBECOS_UNAUTH_MODE 不認得：{self.unauth_mode}")
        self.node_order = os.environ.get("CUBECOS_NODE_ORDER", "shuffle").strip() or "shuffle"
        if self.node_order not in ("shuffle", "seed"):
            raise SystemExit(f"CUBECOS_NODE_ORDER 不認得：{self.node_order}")
        prefix = os.environ.get("CUBECOS_PATH_PREFIX", "").strip().rstrip("/")
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix
        self.path_prefix = prefix
        self.keystone_url = os.environ.get("KEYSTONE_URL", "").strip().rstrip("/")
        self.keystone_service_token = os.environ.get("KEYSTONE_SERVICE_TOKEN", "").strip()
        self.keystone_insecure = os.environ.get("KEYSTONE_INSECURE", "") in ("1", "true", "yes")
        self.keystone_ca = os.environ.get("KEYSTONE_CA", "").strip()
        self.keystone_timeout = float(os.environ.get("KEYSTONE_TIMEOUT", "30"))
        self.accept_keystone = {t.strip() for t in os.environ.get("ACCEPT_KEYSTONE_TOKENS", "").split(",") if t.strip()}
        self.jwt_secret = secrets.token_bytes(32)
        # ServiceDiscoveryIdentity＝「dc-vip-oidc secret 前 8 碼」：internal/runtime/parse.go:31-36；
        # 真的 secret 拿不到，用固定字串代替。
        self.discovery_identity = f"{self.dc['name']}-{self.dc['virtual_ip']}-fakesecr"


CFG: Config | None = None


# ---------------------------------------------------------------------------
# 節點與資料中心的 JSON 形狀
# ---------------------------------------------------------------------------
def license_json(lic: dict | None) -> dict:
    """licenses.License：internal/definition/v1/licenses/license.go:44-73；status.License：status/status.go:127-130。

    沒裝授權時 GetHostLicense 回 {status:{current:"unlicense"}}：internal/cubecos/license.go:121-129。
    """
    lic = lic if lic is not None else {"status": {"current": "unlicense"}}
    out = {"name": lic.get("name", ""), "type": lic.get("type", "")}
    if lic.get("hostname"):  # omitzero
        out["hostname"] = lic["hostname"]
    if lic.get("hosts"):  # omitempty
        out["hosts"] = lic["hosts"]
    if lic.get("serial"):  # omitzero
        out["serial"] = lic["serial"]
    prod = lic.get("product", {})
    out["product"] = {"name": prod.get("name", ""), "feature": prod.get("feature", "")}
    issue = lic.get("issue", {})
    out["issue"] = {k: issue.get(k, "") for k in ("by", "to", "hardware", "date")}
    out["quantity"] = lic.get("quantity", "")
    out["supportPlan"] = lic.get("supportPlan", "")
    exp = lic.get("expiry", {})
    out["expiry"] = {"date": exp.get("date", ""), "days": int(exp.get("days", 0))}
    st = lic.get("status", {})
    status = {}
    if st.get("current"):  # omitempty
        status["current"] = st["current"]
    status["isExpiring"] = bool(st.get("isExpiring", False))
    out["status"] = status
    return out


def compute_json(m: dict | None) -> dict:
    # metric.Compute：internal/definition/v1/metric/metric.go:28-34
    m = m or {}
    return {k: go_float(m.get(k, 0)) for k in ("totalCores", "usedCores", "usedPercent", "freeCores", "freePercent")}


def space_json(m: dict | None) -> dict:
    # metric.Space：internal/definition/v1/metric/metric.go:36-42
    m = m or {}
    return {k: go_float(m.get(k, 0)) for k in ("totalMiB", "usedMiB", "usedPercent", "freeMiB", "freePercent")}


def node_json(n: dict) -> dict:
    """nodes.Node 的 json tag 順序：internal/definition/v1/nodes/node.go:45-75。

    兩種形狀：
    - 上線節點：來自 MDNS 註冊資料 nodes.New()（node.go:141-160），`ip` 是註冊 metadata 的 "ip"，
      也就是「管理網路 IP」（internal/runtime/parse.go:159），不是 provider IP。
    - 離線節點（status=down）：只有 /etc/settings.cluster.json 的資料（internal/cubecos/nodes.go:81-104），
      此時 `ip` 才是 provider IP；id／address／serial 等都是空字串（operators/v1/nodes/sync.go:127-137）。
    blockDevices 在清單與單一節點都不會填，一律是 []（internal/cubecos/nodes.go:258-268）。
    """
    assert CFG is not None
    down = n.get("status", "up") == "down"
    mgmt = n.get("management_ip", "")
    fw = n.get("firmware", {}) or {}
    firmware = {}
    if not down:
        # nodes.Firmware 兩個欄位都是 omitempty：internal/definition/v1/nodes/firmware.go:3-6
        if fw.get("active"):
            firmware["active"] = fw["active"]
        if fw.get("inactive"):
            firmware["inactive"] = fw["inactive"]
    ipmi = n.get("ipmi", {}) or {}
    out = {
        "id": "" if down else n.get("id") or f"{CFG.discovery_identity}-{uuid.uuid5(uuid.NAMESPACE_DNS, n['hostname'])}",
        "serialNumber": "" if down else n.get("serial_number", ""),
        "boardSerial": "" if down else n.get("board_serial", ""),
        "dataCenter": CFG.dc["name"],
        "hostname": n["hostname"],
        "role": n.get("role", ""),
    }
    if not down:
        # protocol 是 omitempty，值來自設定檔 kind（configs/cube-cos-api.yaml.template:1、runtime/parse.go:158）
        out["protocol"] = "http"
    # address＝管理 IP:listen port：internal/runtime/parse.go:182-196
    out["address"] = "" if down else f"{mgmt}:{CFG.dc.get('api_port', 8082)}"
    out["ip"] = n.get("provider_ip", "") if down else mgmt
    out["managementIP"] = mgmt
    out["storageIP"] = n.get("storage_ip", "")
    out["cpuSpec"] = "" if down else n.get("cpu_spec", "")
    # nodes.NetworkInterface：internal/definition/v1/nodes/infra.go:5-12
    out["networkInterfaces"] = [] if down else [
        {k: str(i.get(k, "")) for k in ("interface", "label", "busIdSlaves", "driver", "state", "speed")}
        for i in n.get("network_interfaces", [])
    ]
    out["blockDevices"] = []
    out["vcpu"] = compute_json(None if down else n.get("vcpu"))
    out["memory"] = space_json(None if down else n.get("memory"))
    out["storage"] = space_json(None if down else n.get("storage"))
    out["isVirtualIpOwner"] = bool(n.get("is_virtual_ip_owner", False))
    # IpmiEnablement：internal/definition/v1/nodes/ipmi.go:22-26
    out["ipmi"] = {"isSupported": bool(ipmi.get("isSupported", False)),
                   "isConnected": bool(ipmi.get("isConnected", False)),
                   "ip": "" if down else str(ipmi.get("ip", ""))}
    out["license"] = license_json(n.get("license"))
    out["firmware"] = firmware
    # 狀態空白時補成 syncing：internal/cubecos/nodes.go:270-276
    out["status"] = n.get("status") or "syncing"
    out["uptimeSeconds"] = 0 if down else go_float(n.get("uptime_seconds", 0))
    if not down:
        # labels 是 omitempty；上線節點固定有 isGpuEnabled：internal/definition/v1/nodes/node.go:156-158
        out["labels"] = {"isGpuEnabled": "true" if n.get("is_gpu_enabled") else "false"}
    return out


def all_nodes() -> list[dict]:
    """nodes.List() 的順序來自 map 走訪（operators/v1/nodes/sync.go:104-124 走訪 GetSourceNodeMap 的 map），
    在 Go 裡每次都不一樣。預設照樣打亂；CUBECOS_NODE_ORDER=seed 時照種子順序（非原始碼行為）。"""
    assert CFG is not None
    nodes = [node_json(n) for n in CFG.seed["nodes"]]
    if CFG.node_order == "shuffle":
        random.shuffle(nodes)
    return nodes


def datacenter_json() -> dict:
    """base.DataCenter：internal/definition/v1/base/datacenter.go:10-47；組裝：
    internal/apis/v1/handlers/datacenters/helper.go:39-62（getLocalDataCenter）。"""
    assert CFG is not None
    dc = CFG.dc
    nodes = [node_json(n) for n in CFG.seed["nodes"]]
    # datacenter.GetType：第一個屬於 cloud／edge 角色的節點決定（internal/definition/v1/datacenter/datacenter.go:18-30）
    dtype = "unknown"
    for n in nodes:
        if n["role"] in CLOUD_ROLES:
            dtype = "cloud"
            break
        if n["role"] in EDGE_ROLES:
            dtype = "edge"
            break
    roles = CLOUD_ROLES if dtype == "cloud" else EDGE_ROLES if dtype == "edge" else []
    # GetRegisteredRoles：照候選順序留下至少一個節點有的角色：internal/definition/v1/nodes/role.go:193-205
    registered = [r for r in roles if any(n["role"] == r for n in nodes)]
    # getNodeLicenseStatus：internal/apis/v1/handlers/datacenters/license.go:9-25
    lic = {"valid": 0, "expired": 0, "unlicense": 0}
    for n in nodes:
        cur = n["license"]["status"].get("current", "")
        if cur in lic:
            lic[cur] += 1
    version = dc["firmware_version"]
    out = {
        "type": dtype,
        "name": dc["name"],
        "roles": list(roles),
        "registeredRoles": registered,
        "version": version,
        "virtualIp": dc["virtual_ip"],
        "isLocal": True,
        "isHaEnabled": bool(dc.get("is_ha_enabled", False)),
    }
    if dc.get("utc_time_zone"):  # omitempty
        out["utcTimeZone"] = dc["utc_time_zone"]
    out["firmware"] = {"version": version, "updatedAt": dc.get("firmware_updated_at", "")}
    fp = dc.get("fixpack", {}) or {}
    out["fixpack"] = {"name": fp.get("name", ""), "version": fp.get("version", ""), "updatedAt": fp.get("updatedAt", "")}
    out["additional"] = {
        # internal/definition/v1/base/base.go:20
        "helpUrl": "https://www.bigstack.co/contact-us",
        # base.GenApiDocUrl：internal/definition/v1/base/base.go:113-119
        "v1ApiDocUrl": f"https://{dc['virtual_ip']}/api/v1/datacenters/{dc['name']}/apidocs/index.html",
        "nodeLicenseStatus": lic,
    }
    return out


# ---------------------------------------------------------------------------
# Token（Keycloak OIDC JWT 的替身）
# ---------------------------------------------------------------------------
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_jwt(username: str, ttl: int, typ: str) -> str:
    """真的 token 是 Keycloak 簽的 RS256 JWT（internal/cubecos/token.go:12-24 → gocloak Login）。
    這裡用 HS256 + 啟動時隨機金鑰簽一個外形相同的 JWT，只有本程式驗得過。"""
    assert CFG is not None
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "exp": now + ttl, "iat": now, "jti": str(uuid.uuid4()),
        "iss": f"https://{CFG.dc['virtual_ip']}:{CFG.dc['keycloak_port']}/auth/realms/master",
        "typ": typ, "azp": "cube-cos-api", "preferred_username": username,
    }
    signing = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
    sig = hmac.new(CFG.jwt_secret, signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{b64url(sig)}"


def verify_jwt(token: str) -> bool:
    """對應 oidc.VerifyToken：驗簽章與到期（internal/auths/oidc/oidc.go:26-46、66-76）。refresh token 不能當 access token 用。"""
    assert CFG is not None
    parts = token.split(".")
    if len(parts) != 3:
        return False
    signing = f"{parts[0]}.{parts[1]}"
    want = hmac.new(CFG.jwt_secret, signing.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(want, b64url_dec(parts[2])):
            return False
        payload = json.loads(b64url_dec(parts[1]))
    except (ValueError, json.JSONDecodeError):
        return False
    return payload.get("typ") == "Bearer" and int(payload.get("exp", 0)) > time.time()


def keystone_token_valid(token: str) -> bool:
    """isValidOpenstackToken：internal/runtime/router.go:450-459 → ValidToken
    （bigstack-dependency-go pkg/openstack/v2/credential.go:45-49）→ gophercloud v2.15.0 tokens.Get：
    GET {identity}/auth/tokens，X-Subject-Token＝被驗的 token，200／203 算有效。

    真的 cube-cos-api 用自己（服務帳號）的 token 當 X-Auth-Token；這裡沒有服務帳號，
    沒設 KEYSTONE_SERVICE_TOKEN 時就用被驗的 token 自己（Keystone 允許 token 驗自己）。
    沒設 KEYSTONE_URL 時改看 ACCEPT_KEYSTONE_TOKENS（非原始碼行為，只為單元測試）。
    """
    assert CFG is not None
    if not CFG.keystone_url:
        return token in CFG.accept_keystone
    req = urllib.request.Request(f"{CFG.keystone_url}/auth/tokens", method="GET", headers={
        "X-Auth-Token": CFG.keystone_service_token or token,
        "X-Subject-Token": token,
        "Accept": "application/json",
    })
    ctx = None
    if CFG.keystone_url.startswith("https"):
        if CFG.keystone_insecure:
            ctx = ssl._create_unverified_context()  # noqa: S323 — 測試靶專用
        else:
            ctx = ssl.create_default_context(cafile=CFG.keystone_ca or None)
    try:
        with urllib.request.urlopen(req, timeout=CFG.keystone_timeout, context=ctx) as resp:
            return resp.status in (200, 203)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 203)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log(f"keystone 驗證連不上：{exc}")
        return False


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class Reply:
    def __init__(self, status: int, body: bytes = b"", ctype: str = "application/json; charset=utf-8",
                 headers: list[tuple[str, str]] | None = None):
        self.status = status
        self.body = body
        self.ctype = ctype
        self.headers = headers or []


def json_reply(http_status: int, env: dict, headers=None) -> Reply:
    return Reply(http_status, go_json(env), headers=headers)


def ok(msg: str, data=None) -> Reply:  # bodies/resp.go:101-112
    return json_reply(200, envelope(200, "ok", msg, data))


def accepted(msg: str) -> Reply:  # bodies/resp.go:127-137
    return json_reply(202, envelope(202, "accepted", msg))


def bad_request(msg: str) -> Reply:  # bodies/resp.go:146-157（data 為 nil）
    return json_reply(400, envelope(400, "bad request", msg))


def internal_error(msg: str) -> Reply:  # bodies/resp.go:219-229
    return json_reply(500, envelope(500, "internal server error", msg))


def h_list_datacenters(req) -> Reply:
    # internal/apis/v1/handlers/datacenters/handlers.go:41-49
    return ok("fetch data center list successfully", [datacenter_json()])


def h_get_datacenter(req) -> Reply:
    # internal/apis/v1/handlers/datacenters/handlers.go:51-57（訊息字串原文就是 list）
    return ok("fetch data center list successfully", datacenter_json())


def jsoniter_decode_error(body: bytes) -> str:
    """json-iterator Decode 的錯誤字串（tokens/payload.go:13）。空內容是 io.EOF；其他情況只求外形相近。"""
    if not body.strip():
        return "EOF"
    snippet = body[:10].decode("utf-8", "replace")
    return f"ReadObjectCB: expect {{ or n, but found {snippet[:1]}, error found in #1 byte of ...|{snippet}|..."


def h_create_token(req) -> Reply:
    """internal/apis/v1/handlers/tokens/handlers.go:25-52 + payload.go:11-27 + resp.go:3-12。"""
    assert CFG is not None
    body = req.body
    try:
        user = json.loads(body.decode("utf-8")) if body.strip() else None
        if not body.strip():
            raise ValueError("EOF")
    except (ValueError, UnicodeDecodeError):
        return bad_request(jsoniter_decode_error(body))
    if user is None:
        # JSON `null` 會把 *auths.User 設成 nil，接著 user.IsNameEmpty() 解參照 nil 而 panic，
        # gin.Recovery（internal/runtime/router.go:87）回 500、沒有內容。
        return Reply(500, b"", ctype="text/plain; charset=utf-8")
    if not isinstance(user, dict) or not isinstance(user.get("name", ""), str) \
            or not isinstance(user.get("password", ""), str):
        return bad_request(jsoniter_decode_error(body))
    name, password = user.get("name", ""), user.get("password", "")
    if name == "":
        return bad_request("user name is empty")  # payload.go:18-20
    if password == "":
        return bad_request("user password is empty")  # payload.go:22-24
    if CFG.users.get(name) != password:
        # handlers.go:33-37 → SetUnauthorized(err)；err 是 gocloak APIError：
        # 「resp.Status(): error: error_description」（gocloak v13.9.0 client.go:120-134、errors.go:15-30），
        # Keycloak 帳密錯誤回 {"error":"invalid_grant","error_description":"Invalid user credentials"}。
        log(f"token：帳號 {name!r} 登入失敗")
        return json_reply(401, envelope(401, "unauthorized", "401 Unauthorized: invalid_grant: Invalid user credentials"))
    access_ttl = int(CFG.seed["token"]["access_ttl"])
    refresh_ttl = int(CFG.seed["token"]["refresh_ttl"])
    data = {
        "token": issue_jwt(name, access_ttl, "Bearer"),
        "refresh": issue_jwt(name, refresh_ttl, "Refresh"),
        "expires": {"access": access_ttl, "refresh": refresh_ttl},
    }
    log(f"token：發給帳號 {name!r}（有效 {access_ttl} 秒）")
    # SetCreated：HTTP 201，但內文 code 是 200：internal/apis/v1/bodies/resp.go:114-125
    return json_reply(201, envelope(200, "ok", "create token successfully", data))


def parse_watch(q: dict) -> str | None:
    # queries.GetWatch：internal/apis/v1/queries/watch.go:10-17（strconv.ParseBool）
    val = q.get("watch", ["false"])[0]
    if val not in ("1", "t", "T", "TRUE", "true", "True", "0", "f", "F", "FALSE", "false", "False"):
        return "watch parameter is invalid, it should be true or false if provided"
    return None


def parse_page(q: dict):
    """queries.GetPage：internal/apis/v1/queries/params.go:45-85。回傳 (page|None, 錯誤字串|None)。"""
    num = q.get("pageNum", [""])[0]
    size = q.get("pageSize", [""])[0]
    if num == "" and size == "":
        return None, None
    if num == "":
        return None, "pageNum should be provided if pageSize is provided"
    if size == "":
        return None, "pageSize should be provided if pageNum is provided"
    try:
        n = int(num, 10)
    except ValueError:
        return None, f"pageNum should be an integer: {num}"
    try:
        s = int(size, 10)
    except ValueError:
        return None, f"pageSize should be an integer: {size}"
    if n <= 0:
        return None, "pageNum should be greater than 0 if pageSize is provided"
    if s <= 0:
        return None, "pageSize should be greater than 0 if pageNum is provided"
    return (n, s), None


def normalize_keyword(s: str) -> str:
    # search.NormalizeKeyword：internal/definition/v1/search/search.go:18-33
    s = s.lower()
    for ch in " -_:.,@!#/%":
        s = s.replace(ch, "")
    return s


def keyword_match(node: dict, kw: str) -> bool:
    """近似 bleve 的 *keyword* 萬用字元查詢；可搜尋欄位見 internal/definition/v1/nodes/node.go:112-126
    （hostname 後面接 status）。"""
    fields = [
        normalize_keyword(node["hostname"]) + normalize_keyword(node["status"]),
        normalize_keyword(node["role"]), normalize_keyword(node["address"]), normalize_keyword(node["ip"]),
        normalize_keyword(node["storageIP"]), normalize_keyword(node["managementIP"]),
        normalize_keyword(node["license"]["expiry"]["date"]),
    ]
    return any(kw in f for f in fields if f)


def h_list_nodes(req) -> Reply:
    """internal/apis/v1/handlers/nodes/handlers.go:149-174；helper.go:94-103。

    注意原始碼的順序：先篩選、再分頁、最後只在「這一頁裡」依 hostname 排序（helper.go:95-98）。
    分頁前的順序是 map 走訪順序（見 all_nodes），所以要分頁的話每次看到的內容可能不同。
    """
    q = req.query
    page, err = parse_page(q)
    if err:
        return bad_request(err)  # handlers.go:150-155
    werr = parse_watch(q)
    if werr:
        return bad_request(werr)
    nodes = all_nodes()
    # filter.go:14-36：products → keyword → roles → licenseStatuses；陣列參數用重複的 key（c.QueryArray）
    products = [p.lower() for p in q.get("products", [])]
    if products:
        nodes = [n for n in nodes if n["license"]["product"]["name"].lower() in products]
    kw = q.get("keyword", [""])[0].lower()  # parse.go:91-94
    if kw:
        nkw = normalize_keyword(kw)
        nodes = [n for n in nodes if keyword_match(n, nkw)]
    roles = q.get("roles", [])
    if roles:
        nodes = [n for n in nodes if n["role"] in roles]  # filter.go:128-141
    lstat = q.get("licenseStatuses", [])
    if lstat:
        nodes = [n for n in nodes if n["license"]["status"].get("current", "") in lstat]
    total_items = len(nodes)
    if page:
        number, size = page
        left = min((number - 1) * size, total_items)  # paginate.go:10-18
        right = min(left + size, total_items)
        per_page = nodes[left:right]
        page_info = {"total": -(-total_items // size), "number": number, "size": size, "totalItemCount": total_items}
    else:
        per_page = nodes
        page_info = {"total": 1, "number": 1, "size": total_items, "totalItemCount": total_items}  # paginate.go:20-28
    per_page.sort(key=lambda n: n["hostname"])
    # nodePage：internal/apis/v1/handlers/nodes/resp.go:9-12
    return ok("fetch nodes list successfully", {"nodes": per_page, "page": page_info})


def h_get_node(req) -> Reply:
    """internal/apis/v1/handlers/nodes/handlers.go:176-201；找不到時是 500（不是 404），
    訊息來自 internal/cubecos/nodes.go:161-172。"""
    werr = parse_watch(req.query)
    if werr:
        return bad_request(werr)
    name = req.params["nodeName"]
    for n in all_nodes():
        if n["hostname"] == name:
            return ok("fetch node successfully", n)
    return internal_error(f"node {name} not found")


def stub_ok(msg: str, data=None):
    return lambda req: ok(msg, data)


def stub_accepted(msg: str):
    return lambda req: accepted(msg)


# (method, 路徑樣板, handler, 種類, 出處)；種類：read／write／token
ROUTES = [
    ("GET", V1 + "/datacenters", h_list_datacenters, "read", "handlers/datacenters/handlers.go:16-21"),
    ("GET", V1 + "/datacenters/:DataCenter", h_get_datacenter, "read", "handlers/datacenters/handlers.go:22-28"),
    ("POST", V1 + "/datacenters/:DataCenter/rollingReboot",
     stub_accepted("rollout data center by soft reboot successfully"), "write",
     "handlers/datacenters/handlers.go:29-35,59-70"),
    ("POST", DC + "/tokens", h_create_token, "token", "handlers/tokens/handlers.go:15-22"),
    ("GET", DC + "/nodes", h_list_nodes, "read", "handlers/nodes/handlers.go:22-27"),
    ("GET", DC + "/nodes/:nodeName", h_get_node, "read", "handlers/nodes/handlers.go:28-33"),
    # 以下是寫入路由：只記錄、回原始碼的成功外形，絕不能被 jt-ipam 呼叫。
    ("POST", DC + "/nodes/:nodeName/softReboot",
     stub_accepted("the request of rebooting node is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:34-39,203-221"),
    ("POST", DC + "/nodes/:nodeName/ipmi", stub_ok("the node ipmi setting is set successfully"), "write",
     "handlers/nodes/handlers.go:40-45,252-286"),
    ("POST", DC + "/nodes/:nodeName/ipmi/verify",
     stub_ok("the node ipmi is verified successfully", {
         # nodes.ImpiValidation：internal/definition/v1/nodes/infra.go:57-75
         "board": {"manufacturingDate": "", "manufacturer": "", "product": "", "serial": "", "partNumber": ""},
         "product": {"manufacturer": "", "name": "", "version": "", "serial": ""}}), "write",
     "handlers/nodes/handlers.go:46-51,223-250"),
    ("POST", DC + "/nodes/:nodeName/ipmi/:operation",
     stub_accepted("the request of ipmi operation is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:52-57,361-385"),
    ("DELETE", DC + "/nodes/:nodeName/ipmi/disconnect", stub_ok("the ipmi is successfully disconnected"), "write",
     "handlers/nodes/handlers.go:58-63,387-407"),
    ("POST", DC + "/nodes/drain", stub_ok("the node is successfully drained"), "write",
     "handlers/nodes/handlers.go:64-69,409-436"),
    ("PUT", DC + "/nodes/:nodeName/gpuCards/:gpuId", stub_ok("node GPU card updated successfully"), "write",
     "handlers/nodes/handlers.go:76-81,329-359"),
    ("POST", DC + "/nodes/:nodeName/devices",
     stub_accepted("the request of creating node device is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:94-99,464-488"),
    ("DELETE", DC + "/nodes/:nodeName/devices/:device",
     stub_accepted("the request of deleting node device is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:100-105,490-514"),
    ("PATCH", DC + "/nodes/:nodeName/devices/:device",
     stub_accepted("the request of update node device is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:106-111,516-545"),
    ("PATCH", DC + "/nodes/:nodeName/devices/tasks", stub_ok("update node device task successfully"), "write",
     "handlers/nodes/handlers.go:112-117,625-645"),
    ("POST", DC + "/nodes/:nodeName/osds/:id/restart",
     stub_accepted("the request of restarting node osd is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:118-123,547-571"),
    ("DELETE", DC + "/nodes/:nodeName/osds/:id",
     stub_accepted("the request of removing node osd is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:124-129,573-597"),
    ("PATCH", DC + "/nodes/:nodeName/osds/:id",
     stub_accepted("the request of patching node osd is accepted and under processing"), "write",
     "handlers/nodes/handlers.go:130-135,599-623"),
    ("PATCH", DC + "/nodes/:nodeName/osds/tasks", stub_ok("update node osd task successfully"), "write",
     "handlers/nodes/handlers.go:136-141,647-667"),
]


def match_route(method: str, path: str):
    """gin 的路由樹：每個 method 一棵樹、靜態段優先於 :param、:param 只吃一段且不能是空的。"""
    segs = path.split("/")
    best = None
    for r_method, tmpl, handler, kind, src in ROUTES:
        if r_method != method:
            continue
        tsegs = tmpl.split("/")
        if len(tsegs) != len(segs):
            continue
        params, score, okm = {}, [], True
        for t, s in zip(tsegs, segs):
            if t.startswith(":"):
                if s == "":
                    okm = False
                    break
                params[t[1:]] = s
                score.append(0)
            elif t == s:
                score.append(1)
            else:
                okm = False
                break
        if okm and (best is None or score > best[0]):
            best = (score, tmpl, handler, kind, src, params)
    return best


# ---------------------------------------------------------------------------
# 未通過驗證時的回應
# ---------------------------------------------------------------------------
def saml_redirect_url() -> tuple[str, str]:
    """saml.AuthRequest 產生的 IdP 導向網址：internal/auths/saml/saml.go:239-258（crewjam/saml Redirect binding，
    SignRequest=true 所以帶 SigAlg／Signature：saml.go:83）。IdP 是 Keycloak：https://<VIP>:10443/auth/realms/master/protocol/saml
    （internal/definition/v1/auths/auths.go:21-22、configs/cube-cos-api.yaml.template:24-28）。
    SAMLRequest 內容是編的，只求格式相同（deflate + base64）。"""
    assert CFG is not None
    dc = CFG.dc
    req_id = "id-" + secrets.token_hex(20)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sp = f"https://{dc['virtual_ip']}:443"
    xml = (f'<samlp:AuthnRequest xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
           f'xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" ID="{req_id}" Version="2.0" IssueInstant="{now}" '
           f'Destination="https://{dc["virtual_ip"]}:{dc["keycloak_port"]}/auth/realms/master/protocol/saml" '
           f'AssertionConsumerServiceURL="{sp}/saml/acs" '
           f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
           f'<saml:Issuer>{sp}/saml/metadata</saml:Issuer></samlp:AuthnRequest>')
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    saml_request = base64.b64encode(comp.compress(xml.encode()) + comp.flush()).decode()
    # crewjam/saml samlsp CookieRequestTracker.TrackRequest：index＝42 個隨機位元組的 RawURL base64
    relay = b64url(secrets.token_bytes(42))
    sig = base64.b64encode(secrets.token_bytes(256)).decode()
    url = (f"https://{dc['virtual_ip']}:{dc['keycloak_port']}/auth/realms/master/protocol/saml"
           f"?SAMLRequest={quote(saml_request, safe='')}&RelayState={quote(relay, safe='')}"
           f"&SigAlg={quote('http://www.w3.org/2001/04/xmldsig-more#rsa-sha256', safe='')}"
           f"&Signature={quote(sig, safe='')}")
    return url, relay


def unauth_reply() -> Reply:
    """conditionalSaml（internal/runtime/router.go:461-477）→ saml.AuthRequest（internal/auths/saml/saml.go:217-262）。

    v3.1.0 原始碼的實際結果：HTTP 401、JSON 外框 {code:401,status:"unauthorized",msg:<IdP 導向網址>}
    （saml.go:260 → bodies.SetUnauthorized，resp.go:159-169），外加 crewjam 的追蹤 cookie
    `saml_<RelayState>`（Path=/saml/acs、Max-Age=90、HttpOnly、Secure）。不是 302。

    CUBECOS_UNAUTH_MODE=redirect302／html200 是「非原始碼行為」：cube-cos-api 本身不會這樣回，
    但前面若有 nginx（/cos-api/）或其他代理改寫，客戶端可能看到；拿來測 jt-ipam 的容錯。
    """
    assert CFG is not None
    url, relay = saml_redirect_url()
    cookie = f"saml_{relay}={b64url(secrets.token_bytes(48))}; Path=/saml/acs; Max-Age=90; HttpOnly; Secure"
    if CFG.unauth_mode == "redirect302":
        body = f'<a href="{url.replace("&", "&amp;")}">Found</a>.\n\n'.encode()
        return Reply(302, body, ctype="text/html; charset=utf-8", headers=[("Location", url), ("Set-Cookie", cookie)])
    if CFG.unauth_mode == "html200":
        html = ("<!DOCTYPE html><html><head><title>Sign in to CubeCOS</title></head><body>"
                '<form id="kc-form-login" method="post" action="/auth/realms/master/login-actions/authenticate">'
                '<input name="username"><input name="password" type="password"><input type="submit" value="Sign In">'
                "</form></body></html>")
        return Reply(200, html.encode(), ctype="text/html; charset=utf-8", headers=[("Set-Cookie", cookie)])
    return json_reply(401, envelope(401, "unauthorized", url), headers=[("Set-Cookie", cookie)])


# ---------------------------------------------------------------------------
# HTTP 處理
# ---------------------------------------------------------------------------
class Request:
    def __init__(self, method, path, query, raw_query, headers, body):
        self.method, self.path, self.query, self.raw_query = method, path, query, raw_query
        self.headers, self.body, self.params = headers, body, {}


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-cubecos"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 用自己的 log，不印請求原文（可能含 token）
        return

    def send_response(self, code, message=None):
        # Go net/http 不送 Server 標頭，只送 Date
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def _send(self, reply: Reply) -> None:
        self.send_response(reply.status)
        if reply.body or reply.status not in (204, 304):
            self.send_header("Content-Type", reply.ctype)
        for k, v in reply.headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        if self.command != "HEAD" and reply.body:
            self.wfile.write(reply.body)

    def _handle(self) -> None:
        assert CFG is not None
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        parts = urlsplit(self.path)
        raw_path = parts.path
        method = self.command

        # ---- 測試專用端點（非原始碼行為；不需驗證、不算寫入）----
        if raw_path.startswith("/_test/"):
            self._send(self._test_endpoint(method, raw_path))
            return

        path = unquote(raw_path)  # gin 預設用解碼後的 URL.Path
        if CFG.path_prefix:
            if path == CFG.path_prefix or path.startswith(CFG.path_prefix + "/"):
                path = path[len(CFG.path_prefix):] or "/"
            else:
                self._finish(method, raw_path, None, "none", Reply(404, b"404 page not found", "text/plain"))
                return
        query = parse_qs(parts.query, keep_blank_values=True)
        req = Request(method, path, query, parts.query, self.headers, body)

        # filterReq：GET /saml/acs 一律 403、沒有內容（internal/runtime/filter.go:14-21、42-45）
        if method == "GET" and path == "/saml/acs":
            self._finish(method, raw_path, None, "none", Reply(403, b"", "text/plain"))
            return
        # /live 在驗證中介層之前註冊，任何 method 都回 ok（internal/runtime/router.go:91、354-358）
        if path == "/live":
            self._finish(method, raw_path, "/live", "none", ok("api is alive"))
            return
        # /saml/*any → saml.ServeAcs：沒有有效 assertion 時 302 到 redirect 路徑（internal/auths/saml/saml.go:185-215、
        # bodies/resp.go:139-144）。gin 的 c.Redirect 用 net/http Redirect，GET 時帶一小段 HTML。
        if path.startswith("/saml/"):
            loc = CFG.dc.get("redirect_path", "/home")
            rb = f'<a href="{loc}">Found</a>.\n\n'.encode() if method in ("GET", "HEAD") else b""
            self._finish(method, raw_path, "/saml/*any", "none",
                         Reply(302, rb, "text/html; charset=utf-8", [("Location", loc)]))
            return

        route = match_route(method, path)
        if route is None and len(path) > 1 and path.endswith("/") and match_route(method, path.rstrip("/")):
            # gin RedirectTrailingSlash（gin.New() 預設開）：GET 301、其他 307，在中介層之前發生。
            loc = CFG.path_prefix + path.rstrip("/") + (f"?{parts.query}" if parts.query else "")
            code = 301 if method == "GET" else 307
            rb = f'<a href="{loc}">Moved Permanently</a>.\n\n'.encode() if method == "GET" else b""
            self._finish(method, raw_path, None, "none", Reply(code, rb, "text/html; charset=utf-8", [("Location", loc)]))
            return

        tmpl = route[1] if route else None
        kind = route[3] if route else None
        if route:
            req.params = route[5]

        # ---- verifyAuthToken：internal/runtime/router.go:373-410 ----
        auth = "unauthenticated"
        if method == "GET" and tmpl == V1 + "/datacenters":
            auth = "free"  # authFreeRoutes：internal/runtime/filter.go:28-32
        else:
            # （1）node 間內部 token（Node 標頭）需要 OIDC client secret，這裡不模擬：router.go:383-390、412-429
            # （2）OpenStack／Keystone token：X-Auth-Token，router.go:392-398、450-459
            xat = self.headers.get("X-Auth-Token", "")
            if xat and keystone_token_valid(xat):
                auth = "openstack"
            else:
                # （3）OIDC：Authorization 必須是大小寫完全相同的 "Bearer " 開頭，router.go:400-406、431-443
                authz = self.headers.get("Authorization", "")
                if authz.startswith("Bearer ") and verify_jwt(authz[len("Bearer "):]):
                    auth = "oidc"

        is_write = kind == "write" or (route is None and method not in ("GET", "HEAD", "OPTIONS"))
        if is_write:
            self._record_write(method, raw_path, tmpl, auth)

        # ---- conditionalSaml：internal/runtime/router.go:461-482 ----
        if auth == "unauthenticated" and kind != "token":
            # gin 的全域中介層也套用在 404 上，所以不存在的路徑沒驗證時一樣回 SAML 401。
            self._finish(method, raw_path, tmpl, auth, unauth_reply(), is_write)
            return

        if route is None:
            # gin 預設 404：純文字 "404 page not found"（HandleMethodNotAllowed 預設關，method 不對也是 404）
            self._finish(method, raw_path, None, auth, Reply(404, b"404 page not found", "text/plain"), is_write)
            return

        if is_write:
            log(f"!!!!! 寫入路由被呼叫：{method} {raw_path}（{route[4]}），驗證＝{auth} !!!!!")
        try:
            reply = route[2](req)
        except Exception as exc:  # gin.Recovery：500、沒有內容
            log(f"handler 例外：{exc!r}")
            reply = Reply(500, b"", "text/plain")
        self._finish(method, raw_path, tmpl, auth, reply, is_write)

    def _record_write(self, method, path, tmpl, auth):
        entry = {"time": datetime.now(timezone.utc).isoformat(), "method": method, "path": path,
                 "route": tmpl or "(unknown)", "authenticated": auth not in ("unauthenticated",), "auth": auth}
        with LOCK:
            WRITE_CALLS.append(entry)
        log(f"!!!!! 寫入嘗試 #{len(WRITE_CALLS)}：{method} {path}（route={entry['route']}，auth={auth}）!!!!!")

    def _finish(self, method, path, tmpl, auth, reply: Reply, is_write: bool = False):
        with LOCK:
            REQUESTS.append({"time": datetime.now(timezone.utc).isoformat(), "method": method, "path": path,
                             "route": tmpl, "auth": auth, "status": reply.status, "write": is_write})
            if len(REQUESTS) > MAX_REQUEST_LOG:
                del REQUESTS[: len(REQUESTS) - MAX_REQUEST_LOG]
        log(f"{method} {path} → {reply.status}（auth={auth}）")
        self._send(reply)

    def _test_endpoint(self, method: str, path: str) -> Reply:
        if path == "/_test/write_calls" and method == "GET":
            with LOCK:
                data = {"count": len(WRITE_CALLS), "calls": list(WRITE_CALLS)}
            return Reply(200, json.dumps(data, ensure_ascii=False).encode())
        if path in ("/_test/write_calls", "/_test/reset") and method in ("DELETE", "POST"):
            with LOCK:
                WRITE_CALLS.clear()
                REQUESTS.clear()
            return Reply(200, b'{"reset":true}')
        if path == "/_test/requests" and method == "GET":
            with LOCK:
                data = {"count": len(REQUESTS), "requests": list(REQUESTS)}
            return Reply(200, json.dumps(data, ensure_ascii=False).encode())
        if path == "/_test/ca.pem" and method == "GET" and getattr(self.server, "ca_pem", None):
            return Reply(200, self.server.ca_pem, "application/x-pem-file")
        return Reply(404, b'{"error":"unknown test endpoint"}')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


# ---------------------------------------------------------------------------
# TLS：啟動時用 openssl 產生測試 CA 與伺服器憑證
# ---------------------------------------------------------------------------
def gen_certs(cert_dir: str, sans: list[str]) -> tuple[str, str, str]:
    """產生一張測試 CA 與由它簽發的伺服器憑證（CA 可給 jt-ipam 的 ca_cert 用，嚴格驗證也過）。"""
    os.makedirs(cert_dir, exist_ok=True)
    ca_key, ca_crt = os.path.join(cert_dir, "ca.key"), os.path.join(cert_dir, "ca.pem")
    key, csr, crt = (os.path.join(cert_dir, n) for n in ("server.key", "server.csr", "server.pem"))
    ext = os.path.join(cert_dir, "server.ext")
    san_items = []
    for s in sans:
        kind = "IP" if s.replace(".", "").isdigit() or ":" in s else "DNS"
        san_items.append(f"{kind}:{s}")

    def run(*cmd):
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca_crt,
        "-days", "30", "-subj", "/CN=fake-cubecos test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=fake-cubecos")
    with open(ext, "w", encoding="ascii") as fh:
        fh.write("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                 "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
                 f"subjectAltName={','.join(san_items)}\n")
    run("openssl", "x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial",
        "-out", crt, "-days", "30", "-sha256", "-extfile", ext)
    return ca_crt, crt, key


def main() -> None:
    global CFG
    ap = argparse.ArgumentParser(description="假的 Bigstack CubeCOS API（測試用）")
    ap.add_argument("--seed", default=os.environ.get("CUBECOS_SEED", ""), help="種子 JSON 檔（預設用內建資料）")
    ap.add_argument("--listen", default=os.environ.get("CUBECOS_LISTEN", "127.0.0.1"), help="監聽位址")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CUBECOS_PORT", "18443")), help="監聽埠")
    ap.add_argument("--http", action="store_true", default=os.environ.get("CUBECOS_HTTP", "") in ("1", "true"),
                    help="用純 HTTP（預設 HTTPS）")
    ap.add_argument("--cert-dir", default=os.environ.get("CUBECOS_CERT_DIR", ""),
                    help="憑證輸出目錄（預設暫存目錄）；會寫 ca.pem／server.pem／server.key")
    args = ap.parse_args()
    CFG = Config(args)

    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.daemon_threads = True
    server.ca_pem = None
    scheme = "http"
    if not args.http:
        cert_dir = args.cert_dir or tempfile.mkdtemp(prefix="fake-cubecos-tls-")
        sans = ["127.0.0.1", "localhost", "::1", CFG.dc["virtual_ip"]]
        if args.listen not in ("0.0.0.0", "::", "") and args.listen not in sans:
            sans.append(args.listen)
        sans += [s.strip() for s in os.environ.get("CUBECOS_TLS_SANS", "").split(",") if s.strip()]
        ca_crt, crt, key = gen_certs(cert_dir, sans)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(crt, key)
        # 交握延到處理執行緒裡做，避免慢的或講純 HTTP 的客戶端卡住 accept
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        with open(ca_crt, "rb") as fh:
            server.ca_pem = fh.read()
        scheme = "https"
        log(f"TLS 測試 CA：{ca_crt}（也可 GET /_test/ca.pem 取得）")
    log(f"監聽 {scheme}://{args.listen}:{server.server_address[1]}{CFG.path_prefix}  "
        f"資料中心＝{CFG.dc['name']}  節點＝{','.join(n['hostname'] for n in CFG.seed['nodes'])}")
    log(f"未驗證回應模式＝{CFG.unauth_mode}  節點順序＝{CFG.node_order}  "
        f"Keystone＝{CFG.keystone_url or ('ACCEPT_KEYSTONE_TOKENS（%d 個）' % len(CFG.accept_keystone))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
