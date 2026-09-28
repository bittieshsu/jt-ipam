"""IP 詳細頁「探測」的結果摘要：由掃描代理回報的證據推出「這是什麼主機」。

探測本身在掃描代理上跑（agent/jt_ipam_agent.py 的 `_job_run_identify`）：服務版本、OS 指紋、
banner／TLS 憑證等唯讀資訊，加上反解、NetBIOS、mDNS 名稱。這裡只做**確定性的推論**
（規則表），每個結論都附上依據，畫面上看得到為什麼這樣判斷 —— 不交給 LLM 猜。

設備類型代碼（前端翻譯）：server／windows／router／switch／firewall／wireless_ap／printer／
camera／voip／hypervisor／storage／media／specialized／unknown。
"""
from __future__ import annotations

import re
from typing import Any

# nmap osclass 的 type → 設備類型
_OSCLASS_TYPE = {
    "general purpose": "server",
    "router": "router",
    "switch": "switch",
    "firewall": "firewall",
    "wap": "wireless_ap",
    "printer": "printer",
    "print server": "printer",
    "webcam": "camera",
    "phone": "voip",
    "voip phone": "voip",
    "voip adapter": "voip",
    "pbx": "voip",
    "storage-misc": "storage",
    "media device": "media",
    "specialized": "specialized",
    "power-device": "specialized",
    "remote management": "specialized",
}

# 服務特徵 → 設備類型（比 OS 指紋可靠：服務是實際回應的內容）。依序比對，第一個命中為準。
_SERVICE_RULES: list[tuple[str, Any]] = [
    ("hypervisor", lambda p: re.search(r"proxmox|vmware esxi|vmware authentication|xenserver|hyper-v",
                                       f"{p.get('product', '')} {p.get('extrainfo', '')}", re.I)),
    # IPP 本身不代表印表機：Linux 的 CUPS 列印服務也開 631/ipp（實測 Ubuntu 開發機被判成印表機）
    ("printer", lambda p: p.get("service") in ("jetdirect", "printer") or p.get("port") == 9100
                          or (p.get("service") == "ipp" and "cups" not in str(p.get("product") or "").lower())),
    ("camera", lambda p: p.get("service") == "rtsp" or p.get("port") == 554),
    ("voip", lambda p: p.get("service") in ("sip", "sip-tls") or p.get("port") in (5060, 5061)),
    ("windows", lambda p: p.get("service") in ("ms-wbt-server", "microsoft-ds", "msrpc")
                          or p.get("port") == 3389),
]

_MIN_OS_ACCURACY = 85


def _open_ports(nmap: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in (nmap.get("ports") or []) if isinstance(p, dict)
            and (p.get("state") or "open") == "open"]


def _service_line(p: dict[str, Any]) -> str:
    parts = [f"{p.get('port')}/{p.get('proto') or 'tcp'}", p.get("service") or "?"]
    prod = " ".join(x for x in (p.get("product"), p.get("version")) if x)
    if prod:
        parts.append(prod)
    return " ".join(str(x) for x in parts)


_MAX_CERT_NAMES = 3


def _cert_names(p: dict[str, Any]) -> list[str]:
    """TLS 憑證上的主機名稱：只看 Subject 與 SAN（Issuer 的 CN 是簽發機構，例如 Let's Encrypt 的
    「YR2」），略過萬用名稱（*.example.net 說明不了這是哪一台），每張憑證最多取幾個 ——
    一張多網域憑證可能列上幾十個名稱。"""
    out = (p.get("scripts") or {}).get("ssl-cert") or ""
    found: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Subject:"):
            found += re.findall(r"commonName=([^/\s,]+)", line)
        elif line.startswith("Subject Alternative Name:"):
            found += re.findall(r"DNS:([^\s,]+)", line)
    names: list[str] = []
    for n in found:
        if _FQDN_RE.match(n) and n not in names:
            names.append(n)
    return names[:_MAX_CERT_NAMES]


# 憑證上的名稱要長得像網域名稱才算主機名稱：實測 7070 埠的憑證 CN 是「AnyDesk Client」
# （軟體自簽的憑證，CN 放的是軟體名稱），被當成主機名稱「AnyDesk」。萬用名稱（*.）也不算。
_FQDN_RE = re.compile(r"^(?=.{4,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$")


def _cert_subject_cn(p: dict[str, Any]) -> str | None:
    out = (p.get("scripts") or {}).get("ssl-cert") or ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Subject:"):
            m = re.search(r"commonName=([^/,]+)", line)
            return m.group(1).strip() if m else None
    return None


def _applications(ports: list[dict[str, Any]]) -> list[str]:
    """這台跑了哪些軟體：nmap 認出來的產品＋版本；認不出產品、但 TLS 憑證的 CN 不是主機名稱時，
    CN 通常就是軟體名稱（例如 AnyDesk 的自簽憑證「AnyDesk Client」）。同一個軟體只列一次。"""
    apps: list[str] = []
    for p in ports:
        prod = " ".join(x for x in (p.get("product"), p.get("version")) if x).strip()
        if not prod:
            cn = _cert_subject_cn(p)
            if cn and not _FQDN_RE.match(cn) and not cn.startswith("*"):
                prod = re.sub(r"\s+(Client|Server|Service)$", "", cn).strip()
        if prod and prod not in apps:
            apps.append(prod)
    return apps


def summarize(result: dict[str, Any] | None, *, mac_vendor: str | None = None) -> dict[str, Any]:
    """代理回報的探測結果 → 摘要。`mac_vendor` 是 jt-ipam 依 IP 記錄的 MAC 查到的 OUI 廠商。"""
    result = result or {}
    nmap = result.get("nmap") or {}
    names_in = result.get("names") or {}
    ports = _open_ports(nmap)
    evidence: list[str] = []

    os_name = None
    top = next((o for o in (nmap.get("os") or []) if isinstance(o, dict)), None)
    if top and int(top.get("accuracy") or 0) >= _MIN_OS_ACCURACY:
        os_name = top.get("name")
        evidence.append(f"os:{top.get('name')} ({top.get('accuracy')}%)")

    device_type = "unknown"
    for code, rule in _SERVICE_RULES:
        hit = next((p for p in ports if rule(p)), None)
        if hit:
            device_type = code
            evidence.append(f"service:{_service_line(hit)}")
            break
    if device_type == "unknown" and top and top.get("type"):
        mapped = _OSCLASS_TYPE.get(str(top["type"]).lower())
        if mapped and int(top.get("accuracy") or 0) >= _MIN_OS_ACCURACY:
            device_type = mapped
            evidence.append(f"osclass:{top['type']}")

    vendor = mac_vendor or nmap.get("mac_vendor") or (top or {}).get("vendor") or None
    if mac_vendor:
        evidence.append(f"oui:{mac_vendor}")

    names: list[str] = []
    for n in [names_in.get("rdns"), names_in.get("netbios"), names_in.get("mdns"),
              *(nmap.get("hostnames") or []), *[c for p in ports for c in _cert_names(p)]]:
        if n and n not in names:
            names.append(str(n))

    return {
        "device_type": device_type,
        "os": os_name,
        "vendor": vendor,
        "names": names,
        "applications": _applications(ports),
        "services": [_service_line(p) for p in ports],
        "evidence": evidence,
        "nmap_available": bool(nmap.get("available", bool(nmap))),
    }


def _port_key(p: dict[str, Any]) -> str:
    return f"{p.get('port')}/{p.get('proto') or 'tcp'}"


def _port_product(p: dict[str, Any]) -> str:
    return " ".join(x for x in (p.get("product"), p.get("version")) if x).strip()


def changes_between(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any]:
    """兩次探測之間的差異：新開的埠、關掉的埠、產品／版本變了的服務。"""
    before = {_port_key(p): p for p in _open_ports((previous or {}).get("nmap") or {})}
    after = {_port_key(p): p for p in _open_ports((current or {}).get("nmap") or {})}
    changed = []
    for k in after:
        if k in before:
            a, b = _port_product(before[k]), _port_product(after[k])
            if a != b and a and b:
                changed.append({"port": k, "before": a, "after": b})
    return {
        "opened": [k for k in after if k not in before],
        "closed": [k for k in before if k not in after],
        "changed": changed,
    }
