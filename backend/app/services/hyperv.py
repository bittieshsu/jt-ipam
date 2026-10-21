"""Microsoft Hyper-V 同步（Beta）：WinRM＋PowerShell，唯讀。

兩種連法（`HyperVInstance.mode`）：
- `hosts`：逐台直連 Hyper-V 主機，每台跑一次 `_HOST_SCRIPT`（Get-VM、網卡、VLAN、KVP 客體資訊、主機網卡）。
  NTLM 不能二次跳轉，所以叢集的每個節點都要填，逐台連；叢集 VM 只出現在擁有它的節點上，以 VM GUID 去重。
- `scvmm`：連 System Center VMM 伺服器跑 `_SCVMM_SCRIPT`（VMM 模組問的是本機的 VMM 服務，不需要二次跳轉）。

規則照 Proxmox（同一份 IPAM 寫入函式 `proxmox._link_ip_to_ipam`，來源換成 `hyperv`）：
- 只有執行中的 VM 認領 IP（使用者 2026-10-13：「停用的不要抓」）
- MAC 走來源優先序（MacRun）、主機名稱走 HostnameRun（有 KVP／SCVMM 的客體 FQDN 用它，沒有用 VM 名稱）
- 所有主機都讀成功才刪不再出現的 VM、才清掉不再回報的主機名稱（叢集 VM 會在節點間移動）
- 全部主機都連不上＝硬失敗：寫 last_error 後往上拋（issue #44，否則作業顯示「成功、0 筆」）

安全（同 Windows DNS/DHCP）：出站位址在建構與連線當下各檢查一次（net_guard strict）；HTTP 5985 一律
NTLM 訊息加密、加密不了就失敗；腳本寫死在程式裡，不插入任何外部字串。

**沒有實機驗證過**（2026-10-20 開發時手邊沒有 Hyper-V／SCVMM）：腳本裡的屬性名稱照官方文件，讀不到的屬性
在 PowerShell 是 $null 而不是錯誤，所以欄位對不上時會是「空的」而不是同步失敗；「測試連線」會逐步回報。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.core.sqlin import in_values
from app.core.ui_error import UiError
from app.models.hyperv import HyperVInstance


class HyperVError(UiError):
    """對外可讀的錯誤（連線、認證、憑證、PowerShell 執行失敗）。"""


# ────────────────────────── 憑證 ──────────────────────────

def _aad(instance_id: Any) -> bytes:
    return f"hyperv_instance:{instance_id}:password".encode()


def encrypt_password(instance_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(instance_id))


def _decrypt_password(inst: HyperVInstance) -> str:
    return decrypt_secret(inst.password_enc, inst.password_nonce, aad=_aad(inst.id)).decode("utf-8")


def _check_address_safe(host: str) -> None:
    """A10：跟 HTTP 相同的出站規則（core/net_guard 的 strict）。"""
    from app.core.net_guard import check_target
    from app.core.safe_http import UnsafeOutboundURL
    try:
        check_target(host, policy="strict")
    except UnsafeOutboundURL as exc:
        raise HyperVError(str(exc), code="hyperv_connect", host=host) from exc


# ────────────────────────── PowerShell ──────────────────────────
# 列舉型別一律轉字串（ConvertTo-Json 會把 enum 寫成數字）；GUID 也轉成小寫字串。
# 整份結果用 -InputObject 輸出：走管線的話單一元素的陣列會被攤平。

_HOST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$o = [ordered]@{}
$cs = Get-CimInstance Win32_ComputerSystem
$o.host = $env:COMPUTERNAME
$o.fqdn = $(if ($cs.PartOfDomain -and $cs.Domain) { "$($cs.DNSHostName).$($cs.Domain)" } else { "$($cs.DNSHostName)" })
$o.cluster = $null
try { $o.cluster = "$((Get-Cluster -ErrorAction Stop).Name)" } catch { }
$kvp = @{}
$o.kvp_ok = $true
try {
  foreach ($c in @(Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_KvpExchangeComponent -ErrorAction Stop)) {
    $d = @{}
    foreach ($x in @($c.GuestIntrinsicExchangeItems)) {
      $p = ([xml]$x).INSTANCE.PROPERTY
      $n = ($p | Where-Object { $_.NAME -eq 'Name' }).VALUE
      $v = ($p | Where-Object { $_.NAME -eq 'Data' }).VALUE
      if ($n -eq 'FullyQualifiedDomainName' -or $n -eq 'OSName') { $d[$n] = "$v" }
    }
    $kvp["$($c.SystemName)".ToLower()] = $d
  }
} catch { $o.kvp_ok = $false }
$o.vms = @(foreach ($vm in @(Get-VM)) {
  $g = "$($vm.VMId)".ToLower()
  $k = $kvp[$g]
  [ordered]@{
    id = $g
    name = "$($vm.Name)"
    state = "$($vm.State)"
    cpu = $vm.ProcessorCount
    memory_mb = [int64]($vm.MemoryStartup / 1MB)
    disk_gb = $(try { $t = [int64]0; foreach ($d in @(Get-VMHardDiskDrive -VM $vm -ErrorAction Stop)) { if ($d.Path) { $t += (Get-VHD -Path $d.Path -ErrorAction Stop).Size } }; [int64]($t / 1GB) } catch { $null })
    notes = "$($vm.Notes)"
    fqdn = $(if ($k) { $k['FullyQualifiedDomainName'] } else { $null })
    os = $(if ($k) { $k['OSName'] } else { $null })
    nics = @(foreach ($n in @($vm.NetworkAdapters)) { [ordered]@{
      name = "$($n.Name)"
      mac = "$($n.MacAddress)"
      switch = "$($n.SwitchName)"
      ips = @(foreach ($a in @($n.IPAddresses)) { "$a" })
      vlan = $(if ($n.VlanSetting -and "$($n.VlanSetting.OperationMode)" -eq 'Access') { $n.VlanSetting.AccessVlanId } else { $null })
    } })
  }
})
$o.adapters_ok = $true
$o.adapters = @()
try {
  $o.adapters = @(foreach ($a in @(Get-NetAdapter -Physical -ErrorAction Stop)) {
    [ordered]@{ name = "$($a.Name)"; desc = "$($a.InterfaceDescription)"; mac = "$($a.MacAddress)"; up = ("$($a.Status)" -eq 'Up') } })
} catch { $o.adapters_ok = $false }
$o.switches = @()
try {
  $o.switches = @(foreach ($s in @(Get-VMSwitch -ErrorAction Stop)) {
    $m = @(@($s.NetAdapterInterfaceDescriptions) + @($s.NetAdapterInterfaceDescription) | Where-Object { $_ } | Select-Object -Unique)
    [ordered]@{ name = "$($s.Name)"; type = "$($s.SwitchType)"; members = @(foreach ($x in $m) { "$x" }) } })
} catch { $o.adapters_ok = $false }
$o.ips = @()
try {
  $o.ips = @(foreach ($i in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)) {
    if ("$($i.IPAddress)" -notlike '127.*' -and "$($i.IPAddress)" -notlike '169.254.*') {
      [ordered]@{ ip = "$($i.IPAddress)"; iface = "$($i.InterfaceAlias)" } } })
} catch { }
ConvertTo-Json -InputObject $o -Depth 6 -Compress
"""

_SCVMM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module virtualmachinemanager -ErrorAction Stop
$null = Get-SCVMMServer -ComputerName localhost
$o = [ordered]@{}
$o.server = $env:COMPUTERNAME
$o.hosts = @(foreach ($h in @(Get-SCVMHost)) {
  $hn = [ordered]@{
    name = $(if ($h.ComputerName) { "$($h.ComputerName)" } else { "$($h.Name)" })
    fqdn = "$($h.FQDN)"
    cluster = $(if ($h.HostCluster) { "$($h.HostCluster.Name)" } else { $null })
    adapters_ok = $true
    adapters = @()
    switches = @()
    ips = @()
  }
  try {
    $hn.adapters = @(foreach ($a in @(Get-SCVMHostNetworkAdapter -VMHost $h)) {
      [ordered]@{ name = $(if ($a.ConnectionName) { "$($a.ConnectionName)" } else { "$($a.Name)" }); desc = "$($a.Name)"
                  mac = "$($a.MacAddress)"; up = $true } })
    $hn.ips = @(foreach ($a in @(Get-SCVMHostNetworkAdapter -VMHost $h)) {
      foreach ($ip in @($a.IPAddresses)) { [ordered]@{ ip = "$ip"; iface = "$($a.ConnectionName)" } } })
  } catch { $hn.adapters_ok = $false }
  try {
    $hn.switches = @(foreach ($s in @(Get-SCVirtualNetwork -VMHost $h)) {
      [ordered]@{ name = "$($s.Name)"; type = 'External'
                  members = @(foreach ($x in @($s.VMHostNetworkAdapters)) { "$($x.Name)" }) } })
  } catch { $hn.adapters_ok = $false }
  $hn
})
$o.vms = @(foreach ($vm in @(Get-SCVirtualMachine)) {
  [ordered]@{
    id = "$($vm.VMId)".ToLower()
    name = "$($vm.Name)"
    state = "$($vm.Status)"
    host = $(if ($vm.VMHost) { $(if ($vm.VMHost.ComputerName) { "$($vm.VMHost.ComputerName)" } else { "$($vm.VMHost.Name)" }) } else { $null })
    cpu = $vm.CPUCount
    memory_mb = $vm.Memory
    disk_gb = $(try { [int64]((@(foreach ($d in @($vm.VirtualDiskDrives)) { $d.VirtualHardDisk.MaximumSize }) | Measure-Object -Sum).Sum / 1GB) } catch { $null })
    notes = "$($vm.Description)"
    fqdn = "$($vm.ComputerNameString)"
    os = $(if ($vm.OperatingSystem) { "$($vm.OperatingSystem.Name)" } else { $null })
    nics = @(foreach ($n in @($vm.VirtualNetworkAdapters)) { [ordered]@{
      name = "$($n.Name)"
      mac = "$($n.MACAddress)"
      switch = $(if ($n.VMNetwork) { "$($n.VMNetwork.Name)" } else { "$($n.VirtualNetwork)" })
      ips = @(foreach ($a in @(@($n.IPv4Addresses) + @($n.IPv6Addresses))) { if ($a) { "$a" } })
      vlan = $(if ($n.VLanEnabled) { $n.VLanID } else { $null })
    } })
  }
})
ConvertTo-Json -InputObject $o -Depth 7 -Compress
"""

_PING_SCRIPT = "ConvertTo-Json -InputObject ([ordered]@{ host = $env:COMPUTERNAME; ps = \"$($PSVersionTable.PSVersion)\" }) -Compress"
_HV_PROBE_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; $c = $null; try { $c = \"$((Get-Cluster -ErrorAction Stop).Name)\" } catch { }; "
    "ConvertTo-Json -InputObject ([ordered]@{ vms = @(Get-VM).Count; cluster = $c }) -Compress"
)
_VMM_PROBE_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; Import-Module virtualmachinemanager -ErrorAction Stop; "
    "$null = Get-SCVMMServer -ComputerName localhost; "
    "ConvertTo-Json -InputObject ([ordered]@{ hosts = @(Get-SCVMHost).Count; vms = @(Get-SCVirtualMachine).Count }) -Compress"
)


# ────────────────────────── WinRM ──────────────────────────

class HyperVClient:
    """對單一台 Windows 主機（Hyper-V 主機或 VMM 伺服器）的唯讀 WinRM 用戶端。"""

    def __init__(self, *, host: str, username: str, password: str, port: int = 5985,
                 use_ssl: bool = False, verify_tls: bool = True, timeout: float = 60.0,
                 auth_method: str = "ntlm", kerberos_realm: str | None = None,
                 kerberos_kdc: str | None = None) -> None:
        from app.services import winrm_kerberos as krb
        if not host:
            raise HyperVError("host is required", code="hyperv_connect", host="")
        _check_address_safe(host)
        self.host, self.username, self.password = host, username, password
        self.port, self.use_ssl, self.verify_tls, self.timeout = port, use_ssl, verify_tls, timeout
        self.auth_method = auth_method
        self.kerberos_realm = kerberos_realm
        self.kerberos_kdcs: list[str] = []
        if auth_method == "kerberos":
            try:
                krb.require_fqdn(host)           # 服務票是 HTTP/<主機名稱>：IP 拿不到票
                self.kerberos_kdcs = krb.parse_kdcs(kerberos_kdc)
            except krb.KerberosError as exc:
                raise HyperVError(str(exc), code=exc.code, **exc.params) from exc

    def _session(self, principal: str | None = None) -> Any:
        import winrm  # 放這裡：單元測試可以換成假的模組
        _check_address_safe(self.host)        # 連線當下再檢查一次（建構之後 DNS 可能換了答案）
        scheme = "https" if self.use_ssl else "http"
        return winrm.Session(
            target=f"{scheme}://{self.host}:{self.port}/wsman",
            # Kerberos：票已經在這次專用的快取裡（winrm_kerberos.ticket），帳號給主體名稱讓它挑對那張票
            auth=(principal or self.username, self.password),
            transport="kerberos" if principal else "ntlm",
            server_cert_validation="validate" if (self.use_ssl and self.verify_tls) else "ignore",
            # HTTP 5985 時內容與帳密一律以 NTLM 加密；加密不了就失敗，不退回明文（OWASP A02）
            message_encryption="auto" if self.use_ssl else "always",
            operation_timeout_sec=int(self.timeout),
            read_timeout_sec=int(self.timeout) + 5,
        )

    def run_ps(self, script: str) -> str:
        from app.services import winrm_kerberos as krb
        try:
            if self.auth_method == "kerberos":
                with krb.ticket(self.username, self.password, realm=self.kerberos_realm,
                                kdcs=self.kerberos_kdcs) as principal:
                    result = self._session(principal).run_ps(script)
            else:
                result = self._session().run_ps(script)
        except krb.KerberosError as exc:
            raise HyperVError(str(exc), code=exc.code, **{**exc.params, "host": self.host}) from exc
        except HyperVError:
            raise
        except Exception as exc:   # winrm/requests 的連線、認證、TLS、逾時都不是我們的例外型別
            raise _classify_transport_error(exc, host=self.host, port=self.port, use_ssl=self.use_ssl) from exc
        if result.status_code != 0:
            err = (result.std_err or b"").decode("utf-8", errors="replace")
            raise _classify_ps_error(err, host=self.host)
        return (result.std_out or b"").decode("utf-8", errors="replace")

    def run_json(self, script: str) -> dict[str, Any]:
        out = self.run_ps(script).strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError as exc:
            raise HyperVError(f"unexpected PowerShell output from {self.host}: {out[:200]}",
                              code="hyperv_error", host=self.host) from exc
        if not isinstance(data, dict):
            raise HyperVError(f"unexpected PowerShell output from {self.host}: {out[:200]}",
                              code="hyperv_error", host=self.host)
        return data


def _classify_transport_error(exc: Exception, *, host: str, port: int, use_ssl: bool) -> HyperVError:
    text = f"{exc.__class__.__name__}: {exc}"
    if re.search(r"401|InvalidCredentials|Unauthorized|credentials were rejected", text, re.I):
        return HyperVError(f"WinRM authentication failed on {host}: {text[-300:]}", code="hyperv_auth", host=host)
    if re.search(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed", text):
        return HyperVError(f"WinRM HTTPS certificate of {host} is not trusted: {text[-300:]}",
                           code="hyperv_cert", host=host)
    if use_ssl and re.search(r"ConnectTimeout|Max retries exceeded|Connection refused|NewConnectionError|timed out",
                             text):
        return HyperVError(f"WinRM HTTPS {host}:{port} is unreachable ({text[-200:]})",
                           code="hyperv_https_unreachable", host=host, port=port)
    return HyperVError(f"WinRM connection to {host}:{port} failed: {text[-300:]}", code="hyperv_connect",
                       host=host, port=port)


def _classify_ps_error(err: str, *, host: str) -> HyperVError:
    if re.search(r"Get-VM'? is not recognized|virtualmachinemanager.*(not loaded|was not found|not installed)|"
                 r"module .*virtualmachinemanager", err, re.I):
        return HyperVError(f"{host}: Hyper-V / VMM PowerShell module not available: {err[:300]}",
                           code="hyperv_no_module", host=host)
    if re.search(r"access is denied|unauthorized|permission", err, re.I):
        return HyperVError(f"{host}: access denied: {err[:300]}", code="hyperv_auth", host=host)
    return HyperVError(f"{host}: PowerShell error: {err[:300]}", code="hyperv_error", host=host)


def _client(inst: HyperVInstance, host: str) -> HyperVClient:
    return HyperVClient(host=host, username=inst.username, password=_decrypt_password(inst),
                        port=inst.port, use_ssl=inst.use_ssl, verify_tls=inst.verify_tls,
                        auth_method=inst.auth_method or "ntlm", kerberos_realm=inst.kerberos_realm,
                        kerberos_kdc=inst.kerberos_kdc)


# ────────────────────────── 正規化 ──────────────────────────

_STATE = {
    "running": "running",
    "off": "stopped", "poweroff": "stopped", "stopped": "stopped",
    "saved": "paused", "paused": "paused",
    "undermigration": "migrating", "migrating": "migrating",
}


def map_state(raw: Any) -> str:
    """Hyper-V 的 VMState／SCVMM 的 VMComputerSystemState → virtual_machines.status。"""
    return _STATE.get(re.sub(r"[^a-z]", "", str(raw or "").lower()), "unknown")


def norm_mac(raw: Any) -> str | None:
    """`00155D010203`／`00-15-5D-…`／`00:15:5D:…` → 冒號小寫；全零（關機的動態 MAC）、廣播一律不要。"""
    from app.services.arp_evidence import normalize
    h = re.sub(r"[^0-9a-fA-F]", "", str(raw or ""))
    if len(h) != 12:
        return None
    return normalize(":".join(h[i:i + 2] for i in range(0, 12, 2)).lower())


def _text(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def normalize_vm(raw: dict[str, Any], *, host: str | None) -> dict[str, Any] | None:
    vid = _text(raw.get("id"))
    if not vid or not re.fullmatch(r"[0-9a-f-]{32,36}", vid.lower()):
        return None
    vlan = _int(raw.get("vlan"))
    nics = []
    for n in _as_list(raw.get("nics")):
        if not isinstance(n, dict):
            continue
        v = _int(n.get("vlan"))
        nics.append({
            "name": (_text(n.get("name")) or "nic")[:64],
            "mac": norm_mac(n.get("mac")),
            "switch": _text(n.get("switch")),
            "vlan": v if v and 0 < v < 4095 else None,
            "ips": [str(x) for x in _as_list(n.get("ips")) if _text(x)],
        })
    return {
        "id": vid.lower(), "name": (_text(raw.get("name")) or vid)[:128],
        "node": _text(raw.get("host")) or host, "status": map_state(raw.get("state")),
        "vcpus": _int(raw.get("cpu")), "memory_mb": _int(raw.get("memory_mb")),
        # 虛擬磁碟大小合計（讀不到或 0 都是不知道；VHD 放在 SMB 共享時 WinRM 第二跳讀不到）
        "disk_gb": _int(raw.get("disk_gb")) or None,
        "notes": _text(raw.get("notes")), "fqdn": _text(raw.get("fqdn")),
        "os": (_text(raw.get("os")) or "")[:255] or None, "nics": nics, "vlan": vlan,
    }


def normalize_host(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _text(raw.get("host")) or _text(raw.get("name")) or "",
        "fqdn": _text(raw.get("fqdn")),
        "cluster": _text(raw.get("cluster")),
        "adapters_ok": bool(raw.get("adapters_ok", True)),
        "adapters": [a for a in _as_list(raw.get("adapters")) if isinstance(a, dict) and _text(a.get("name"))],
        "switches": [s for s in _as_list(raw.get("switches")) if isinstance(s, dict) and _text(s.get("name"))],
        "ips": [i for i in _as_list(raw.get("ips")) if isinstance(i, dict) and _text(i.get("ip"))],
    }


@dataclass
class Collected:
    hosts: list[dict[str, Any]] = field(default_factory=list)
    vms: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    kvp_complete: bool = True          # 每台主機的客體資訊都讀到了（主機名稱清除用）

    @property
    def complete(self) -> bool:
        return not self.errors


async def collect(inst: HyperVInstance) -> Collected:
    """讀取所有資料；全部主機都讀不到就 raise。"""
    out = Collected()
    seen: set[str] = set()
    hosts = inst.host_list()
    if inst.mode == "scvmm":
        last: HyperVError | None = None
        for h in hosts:                  # 備援 VMM 伺服器：依序試到通為止
            try:
                data = await asyncio.to_thread(_client(inst, h).run_json, _SCVMM_SCRIPT)
            except HyperVError as exc:
                last = exc
                continue
            for hr in _as_list(data.get("hosts")):
                if isinstance(hr, dict):
                    out.hosts.append(normalize_host(hr))
            for vr in _as_list(data.get("vms")):
                vm = normalize_vm(vr, host=None) if isinstance(vr, dict) else None
                if vm and vm["id"] not in seen:
                    seen.add(vm["id"])
                    out.vms.append(vm)
            return out
        assert last is not None
        raise last
    failures: list[HyperVError] = []
    for h in hosts:
        try:
            data = await asyncio.to_thread(_client(inst, h).run_json, _HOST_SCRIPT)
        except HyperVError as exc:
            failures.append(exc)
            out.errors.append(str(exc)[:300])
            continue
        host = normalize_host(data)
        out.hosts.append(host)
        if not data.get("kvp_ok", True):
            out.kvp_complete = False
        for vr in _as_list(data.get("vms")):
            vm = normalize_vm(vr, host=host["name"]) if isinstance(vr, dict) else None
            # 即時移轉中的 VM 可能同時出現在兩個節點：留第一個（執行中的優先）
            if vm is None:
                continue
            # 這台主機的 KVP 讀不到：客體名稱這一輪不知道（不是「沒有」），不報、也不清
            vm["name_known"] = bool(vm["fqdn"]) or bool(data.get("kvp_ok", True))
            if vm["id"] in seen:
                if vm["status"] == "running":
                    out.vms = [vm if x["id"] == vm["id"] else x for x in out.vms]
                continue
            seen.add(vm["id"])
            out.vms.append(vm)
    if failures and not out.hosts:
        raise failures[0]
    return out


# ────────────────────────── 同步 ──────────────────────────

def _scope_ids(inst: HyperVInstance) -> set[Any] | None:
    ids = {uuid.UUID(str(x)) for x in (inst.scope_subnet_ids or []) if x}
    return ids or None


async def _ensure_cluster(session: AsyncSession, inst: HyperVInstance, col: Collected) -> Any:
    """一個整合對映一筆 virt_cluster（type hyper-v）。叢集名稱：所有主機都屬同一個容錯移轉叢集時用它，否則用整合名稱。"""
    from app.models.virt import VirtCluster

    cluster = await session.get(VirtCluster, inst.cluster_id) if inst.cluster_id else None
    if cluster is not None and cluster.type != "hyper-v":
        cluster = None                    # 不可以拿別的平台的叢集來用（清除時會刪到別人的 VM）
    names = {h["cluster"] for h in col.hosts}
    wanted = next(iter(names)) if len(names) == 1 and None not in names and inst.mode == "hosts" else inst.name
    if cluster is None:
        for cand in (wanted, inst.name, f"{inst.name} (Hyper-V)"):
            cand = cand[:128]
            row = (await session.execute(select(VirtCluster).where(VirtCluster.name == cand).limit(1))).scalars().first()
            if row is None:
                cluster = VirtCluster(name=cand, type="hyper-v", description=inst.description)
                session.add(cluster)
                await session.flush()
                break
            if row.type == "hyper-v" and not (await _cluster_owned_by_other(session, row.id, inst.id)):
                cluster = row
                break
        if cluster is None:
            raise HyperVError(f"cluster name {wanted!r} is taken", code="hyperv_error")
    cluster.is_standalone = inst.mode == "hosts" and len(col.hosts) <= 1 and None in names
    inst.cluster_id = cluster.id
    return cluster


async def _cluster_owned_by_other(session: AsyncSession, cluster_id: Any, inst_id: Any) -> bool:
    return (await session.execute(select(HyperVInstance.id).where(
        HyperVInstance.cluster_id == cluster_id, HyperVInstance.id != inst_id).limit(1))).first() is not None


async def sync_instance(session: AsyncSession, inst: HyperVInstance) -> dict[str, Any]:
    """把 VM 清單鏡像進共用的虛擬化資料表，並把 IP／MAC／主機名稱接進 IPAM。"""
    from app.models.virt import VirtualMachine, VMInterface
    from app.services.arp_precedence import MacRun
    from app.services.esxi import pick_vm_ip
    from app.services.hostname_reports import HostnameRun, enabled_peers
    from app.services.ip_autocreate import addable_subnets
    from app.services.proxmox import _link_ip_to_ipam

    try:
        col = await collect(inst)
    except HyperVError as exc:
        inst.last_error = str(exc)[:2000]
        await session.commit()
        raise

    cluster = await _ensure_cluster(session, inst, col)
    scope = _scope_ids(inst)
    create_in = await addable_subnets(session, scope) if inst.auto_create_ips else None
    hn_run = HostnameRun(session, source="hyperv", origin=f"hyperv:{inst.id}",
                         peers=await enabled_peers(session, HyperVInstance))
    mac_run = MacRun(session, source="hyperv")
    summary: dict[str, Any] = {"cluster": cluster.name, "hosts": len(col.hosts), "vms": len(col.vms),
                               "ipam_linked": 0, "removed": 0, "ports": 0, "errors": list(col.errors)}

    # 主機本身：管理位址接進 IPAM（主機名稱用 FQDN），網卡與虛擬交換器建成裝置埠
    for h in col.hosts:
        hname = h["fqdn"] or h["name"]
        for ipr in h["ips"]:
            if await _link_ip_to_ipam(session, str(ipr["ip"]), None, hname, scope_ids=scope, create_in=create_in,
                                      hn_run=hn_run, mac_run=mac_run, source="hyperv"):
                summary["ipam_linked"] += 1
        try:
            summary["ports"] += await _sync_host_ports(session, inst, h, scope)
        except Exception as exc:          # 區段隔離：埠建不起來不影響 VM 同步
            summary["errors"].append(f"ports {h['name']}: {exc}"[:300])

    by_ext = {row.external_id: row for row in (await session.execute(
        select(VirtualMachine).where(VirtualMachine.cluster_id == cluster.id))).scalars()}
    # vm_interfaces.vlan_id 指向 VLAN 記錄：這個號碼只有一筆 VLAN 時才對應（好幾個 VLAN 網域都有同號碼就不猜，
    # 號碼寫在交換器名稱後面，資訊不丟）
    from app.models.vlan import VLAN
    numbers = {n["vlan"] for v in col.vms for n in v["nics"] if n["vlan"]}
    vlan_rows: dict[int, list[Any]] = {}
    if numbers:
        for vid, num in (await session.execute(select(VLAN.id, VLAN.number).where(
                in_values(VLAN.number, sorted(numbers))))).all():
            vlan_rows.setdefault(int(num), []).append(vid)
    seen: set[Any] = set()
    nics: list[VMInterface] = []
    for v in col.vms:
        row = by_ext.get(v["id"])
        if row is None:
            row = VirtualMachine(id=uuid.uuid4(), cluster_id=cluster.id, external_id=v["id"], name=v["name"])
            session.add(row)
            by_ext[v["id"]] = row
        row.name = v["name"]
        row.node = v["node"]
        row.kind = "vm"
        row.status = v["status"]
        row.vcpus = v["vcpus"]
        row.memory_mb = v["memory_mb"]
        if v["disk_gb"]:
            row.disk_gb = v["disk_gb"]        # 這次讀不到時保留上次的值
        row.is_template = False
        if v["os"]:
            row.os_name = v["os"]
        if v["notes"]:
            row.description = v["notes"]
        seen.add(row.id)
        claims = v["status"] == "running"
        name_known = v.get("name_known", True)
        reported = v["fqdn"] or v["name"]
        if not claims:
            hn_run.hold(reported)         # 關機的 VM 以前報過的名稱先保留
        if not name_known:
            hn_run.hold(v["name"])
        first_linked = None
        for n in v["nics"]:
            ip = pick_vm_ip(n["ips"])
            hits = vlan_rows.get(n["vlan"] or -1, [])
            bridge = n["switch"]
            if n["vlan"] and len(hits) != 1:
                bridge = f"{bridge or ''} (VLAN {n['vlan']})".strip()
            nics.append(VMInterface(vm_id=row.id, name=n["name"], mac=n["mac"], bridge=bridge,
                                    vlan_id=hits[0] if len(hits) == 1 else None, primary_ip=ip))
            if claims and (ip or n["mac"]):
                # 名稱不知道時連 HostnameRun 都不給：給了 None 會被當成「上游說沒有名稱」而清掉
                linked = await _link_ip_to_ipam(session, ip, n["mac"], reported if name_known else None,
                                                scope_ids=scope, create_in=create_in,
                                                hn_run=hn_run if name_known else None, mac_run=mac_run,
                                                source="hyperv")
                if linked is not None:
                    summary["ipam_linked"] += 1
                    first_linked = first_linked or linked.id
        if claims:
            row.primary_ip_id = first_linked
    await session.flush()                 # VM 先寫，網卡的外鍵才有對象
    if seen:
        await session.execute(VMInterface.__table__.delete().where(in_values(VMInterface.vm_id, seen)))
    # 同一台 VM 兩張網卡同名（Hyper-V 預設都叫「Network Adapter」）會撞 (vm_id, name) 唯一鍵：補序號
    used: dict[tuple[Any, str], int] = {}
    for n in nics:
        k = (n.vm_id, n.name)
        used[k] = used.get(k, 0) + 1
        if used[k] > 1:
            n.name = f"{n.name} #{used[k]}"[:64]
    session.add_all(nics)

    # 所有主機都讀成功才刪：叢集 VM 會在節點間移動，有一台沒讀到就可能誤刪
    if col.complete and seen:
        for row in list(by_ext.values()):
            if row.id not in seen:
                await session.delete(row)
                summary["removed"] += 1
    await mac_run.finish()
    hn = await hn_run.finish(complete=col.complete and col.kvp_complete)
    if hn["breaker"]:
        summary["errors"].append(f"hostname cleanup skipped: {hn['breaker']}")

    inst.last_sync_at = datetime.now(UTC)
    inst.last_error = "；".join(summary["errors"])[:2000] if summary["errors"] else None
    summary["errors"] = summary["errors"][:20]
    return summary


async def _sync_host_ports(session: AsyncSession, inst: HyperVInstance, h: dict[str, Any],
                           scope: set[Any] | None) -> int:
    """Hyper-V 主機的實體網卡與虛擬交換器 → 那台主機在 IPAM 裡的裝置的連接埠（照 Proxmox 的節點埠）。

    只掛到**已經存在**的裝置（用主機的位址或名稱找），不自動建裝置 —— 同 Proxmox。埠標記 `hyperv:<整合>`，
    這台主機完整讀到時，不再回報的埠由同步清掉；使用者自己建的埠（沒有標記）不動。
    """
    from sqlalchemy import func

    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.physical import DevicePort

    dev_id = None
    for ipr in h["ips"]:
        stmt = select(IPAddress).where(func.host(IPAddress.ip) == str(ipr["ip"]))
        if scope:
            stmt = stmt.where(in_values(IPAddress.subnet_id, scope))
        ipa = (await session.execute(stmt.limit(2))).scalars().all()
        if len(ipa) == 1:
            dev_id = ipa[0].device_id or (await session.execute(
                select(Device.id).where(Device.primary_ip_id == ipa[0].id).limit(1))).scalar_one_or_none()
            if dev_id:
                break
    if dev_id is None:
        for nm in sorted({x for x in (h["name"], h["fqdn"], (h["fqdn"] or "").split(".")[0]) if x}):
            hits = (await session.execute(select(Device.id).where(
                func.lower(Device.name) == nm.lower()).limit(2))).scalars().all()
            if len(hits) == 1:           # 同名好幾台：不猜
                dev_id = hits[0]
                break
    if dev_id is None:
        return 0

    origin = f"hyperv:{inst.id}"
    existing = {p.name: p for p in (await session.execute(
        select(DevicePort).where(DevicePort.device_id == dev_id))).scalars()}
    want: dict[str, dict[str, Any]] = {}
    by_desc: dict[str, str] = {}
    for a in h["adapters"]:
        name = str(a["name"])[:255]
        want[name] = {"type": "network", "mac": norm_mac(a.get("mac")),
                      "description": _text(a.get("desc")) or "Hyper-V host NIC"}
        if _text(a.get("desc")):
            by_desc[str(a["desc"])] = name
    for s in h["switches"]:
        name = f"vSwitch {s['name']}"[:255]
        members = [by_desc.get(str(m), str(m)) for m in _as_list(s.get("members")) if _text(m)]
        desc = f"Hyper-V {s.get('type') or ''} switch".replace("  ", " ")
        if members:
            desc += f" ← {', '.join(members)}"
        want[name] = {"type": "bridge", "mac": None, "description": desc, "members": members}
    created = 0
    for name, w in want.items():
        cur = existing.get(name)
        if cur is None:
            cur = DevicePort(device_id=dev_id, name=name, type=w["type"], description=w["description"],
                             mac_address=w["mac"], source_origin=origin)
            session.add(cur)
            existing[name] = cur
            created += 1
            continue
        if cur.source_origin in (None, origin):     # 使用者自己建的同名埠：只補空的欄位
            cur.type = w["type"] if cur.source_origin == origin else cur.type
            cur.description = cur.description or w["description"]
            cur.mac_address = cur.mac_address or w["mac"]
    await session.flush()
    # 只有一個成員的虛擬交換器 → 對應到那張實體網卡（纜線追蹤可以沿內部續走）
    for name, w in want.items():
        members = w.get("members") or []
        src, tgt = existing.get(name), existing.get(members[0]) if len(members) == 1 else None
        if src is not None and tgt is not None and src.id != tgt.id:
            src.peer_port_id = src.peer_port_id or tgt.id
            tgt.peer_port_id = tgt.peer_port_id or src.id
    if h["adapters_ok"]:
        for name, p in existing.items():
            if p.source_origin == origin and name not in want:
                await session.delete(p)
    return created


# ────────────────────────── 診斷 ──────────────────────────

async def diagnose(inst: HyperVInstance) -> list[dict[str, Any]]:
    """逐台、逐步回報：WinRM 連得上嗎、Hyper-V／VMM 模組讀得到嗎、有幾台 VM。卡在哪一步才是使用者要知道的。"""
    steps: list[dict[str, Any]] = []
    for h in inst.host_list():
        try:
            cli = _client(inst, h)
            info = await asyncio.to_thread(cli.run_json, _PING_SCRIPT)
            steps.append({"step": f"WinRM {h}", "ok": True,
                          "detail": f"{info.get('host') or h}, PowerShell {info.get('ps') or '?'}"})
        except HyperVError as exc:
            steps.append({"step": f"WinRM {h}", "ok": False, "detail": str(exc)[:300],
                          "code": exc.code, "params": exc.params})
            continue
        try:
            if inst.mode == "scvmm":
                r = await asyncio.to_thread(cli.run_json, _VMM_PROBE_SCRIPT)
                detail = f"VMM: {r.get('hosts')} hosts, {r.get('vms')} VMs"
            else:
                r = await asyncio.to_thread(cli.run_json, _HV_PROBE_SCRIPT)
                detail = f"Hyper-V: {r.get('vms')} VMs" + (f", cluster {r['cluster']}" if r.get("cluster") else "")
            steps.append({"step": f"{'VMM' if inst.mode == 'scvmm' else 'Hyper-V'} {h}", "ok": True, "detail": detail})
        except HyperVError as exc:
            steps.append({"step": f"{'VMM' if inst.mode == 'scvmm' else 'Hyper-V'} {h}", "ok": False,
                          "detail": str(exc)[:300], "code": exc.code, "params": exc.params})
        if inst.mode == "scvmm" and steps[-1]["ok"]:
            break                         # VMM：第一台通了就夠（其他是備援）
    return steps
