"""對外開放的埠上跑的是什麼服務：照證據強弱分三層（使用者 2026-10-19 定案，第一版只用現有資料）。

起因：AI 分析把 Jitsi Videobridge 的 UDP 10000 講成「Webmin 管理介面對外」—— 只看埠號就下結論。

- confirmed（已確認）：「IP 探測」連到那個內部位址與內部埠，服務自己報出來的（nmap 探到的產品／版本、banner、
  網頁標題、TLS 憑證）。只採位址換手（device_since）之後的探測。nmap 只靠埠號表猜的（method=table）不算。
- named（依名稱）：規則名稱、說明、主機名稱、FQDN 裡認得出的服務名稱（管理員的意圖，通常對，可能過時）。
- port_guess（依埠號推測）：埠號＋協定的常見用途，只是提示。

「可能是管理介面／敏感服務」只採前兩層；只有埠號推測的另外列成「未確認」。
名稱與實際不符（規則寫 Jitsi，探測到的卻是 SSH）本身就是發現。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

LEVELS = ("confirmed", "named", "port_guess")
#: 算「管理介面／敏感服務」的類別
SENSITIVE_CATEGORIES = frozenset({"mgmt", "remote", "db", "file"})

#: 名稱裡認得出的服務：(樣式, 服務, 類別)。樣式比對規則名稱、說明、主機名稱、FQDN（不分大小寫）
_NAME_RULES: list[tuple[re.Pattern[str], str, str]] = [(re.compile(p, re.I), n, c) for p, n, c in (
    (r"jitsi|videobridge|\bjvb\b", "Jitsi Meet", "media"),
    (r"openvpn", "OpenVPN", "vpn"),
    (r"wireguard|\bwg\d*\b", "WireGuard", "vpn"),
    (r"ipsec|l2tp|\bike\b", "IPsec VPN", "vpn"),
    (r"rustdesk", "RustDesk", "remote"),
    (r"reverse.?proxy|revproxy|haproxy|traefik|\bcaddy\b|\bnginx\b", "Reverse proxy", "web"),
    (r"proxmox|\bpve\b|\bpmg\b|\bpbs\b", "Proxmox", "mgmt"),
    (r"webmin", "Webmin", "mgmt"),
    (r"winbox", "MikroTik Winbox", "mgmt"),
    (r"synology|\bdsm\b", "Synology DSM", "mgmt"),
    (r"qnap", "QNAP", "mgmt"),
    (r"\bipmi\b|\bidrac\b|\bilo\b|\bbmc\b", "BMC", "mgmt"),
    (r"zabbix|grafana|librenms|prometheus|portainer", "Monitoring / admin UI", "mgmt"),
    (r"\bssh\b|sftp", "SSH", "remote"),
    (r"\brdp\b|remote.?desktop|遠端桌面|terminal.?server", "RDP", "remote"),
    (r"\bvnc\b", "VNC", "remote"),
    (r"telnet", "Telnet", "remote"),
    (r"mysql|mariadb|postgres|mssql|sql.?server|redis|mongo|elastic", "Database", "db"),
    (r"\bftp\b", "FTP", "file"),
    (r"\bsmb\b|samba|cifs", "SMB", "file"),
    (r"z-?push|activesync|exchange|zimbra|postfix|\bsmtp\b|\bimap\b|\bpop3\b|\bmail\b", "Mail", "mail"),
    (r"nextcloud|owncloud", "Nextcloud", "web"),
    (r"gitlab|gitea", "Git", "web"),
    (r"\bdns\b|bind9|unbound", "DNS", "dns"),
    (r"\bsip\b|asterisk|freepbx|voip", "VoIP", "media"),
)]

#: nmap 服務名稱／產品 → 類別（探測到的才用）
_CONFIRMED_CATEGORY: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.I), c) for p, c in (
    (r"\bssh\b|openssh|dropbear", "remote"),
    (r"ms-wbt-server|\brdp\b|terminal services", "remote"),
    (r"\bvnc\b|rfb", "remote"),
    (r"telnet", "remote"),
    (r"miniserv|webmin|proxmox|pve-|winbox|ipmi|idrac|ilo\b|synology|qnap", "mgmt"),
    (r"mysql|mariadb|postgres|ms-sql|redis|mongodb|elasticsearch|memcache", "db"),
    (r"\bftp\b|vsftpd|proftpd|pure-ftpd|microsoft-ds|netbios|samba|\bsmb\b", "file"),
    (r"\bsnmp\b", "mgmt"),
    (r"smtp|imap|pop3|postfix|exim|dovecot|exchange", "mail"),
    (r"openvpn|wireguard|isakmp|ipsec", "vpn"),
    (r"https?|nginx|apache|httpd|iis|lighttpd|caddy|traefik|haproxy", "web"),
)]

#: 埠號推測用的常見用途（協定要對；敏感的那些在 attack_surface_ai.SENSITIVE_PORTS）
_COMMON_PORTS: dict[int, tuple[str, frozenset[str]]] = {
    25: ("SMTP", frozenset({"tcp"})), 53: ("DNS", frozenset({"tcp", "udp"})), 80: ("HTTP", frozenset({"tcp"})),
    110: ("POP3", frozenset({"tcp"})), 143: ("IMAP", frozenset({"tcp"})), 443: ("HTTPS", frozenset({"tcp", "udp"})),
    465: ("SMTPS", frozenset({"tcp"})), 587: ("SMTP", frozenset({"tcp"})), 993: ("IMAPS", frozenset({"tcp"})),
    995: ("POP3S", frozenset({"tcp"})), 1194: ("OpenVPN", frozenset({"tcp", "udp"})),
    1723: ("PPTP", frozenset({"tcp"})), 5060: ("SIP", frozenset({"tcp", "udp"})),
    8080: ("HTTP", frozenset({"tcp"})), 8443: ("HTTPS", frozenset({"tcp"})), 51820: ("WireGuard", frozenset({"udp"})),
}


def _proto_ok(proto: Any, allowed: frozenset[str]) -> bool:
    p = str(proto or "").lower()
    return p in ("", "any", "tcp/udp") or p in allowed


def from_name(item: dict[str, Any]) -> tuple[str, str, str] | None:
    """(服務, 類別, 依據)：依序看規則名稱、說明、主機名稱、FQDN。"""
    ident = item.get("identity") or {}
    for label, value in (("規則名稱", item.get("name")), ("說明", item.get("descr")),
                         ("主機名稱", ident.get("hostname")), ("FQDN", " ".join(ident.get("fqdns") or []))):
        text = str(value or "")
        if not text:
            continue
        for pat, name, cat in _NAME_RULES:
            if pat.search(text):
                return name, cat, f"{label}「{text[:80]}」"
    return None


def from_probe(port_entry: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """(服務, 類別, 依據)：探測結果裡這個埠的條目。只靠埠號表猜的不算確認。"""
    if not port_entry:
        return None
    product = " ".join(x for x in (port_entry.get("product"), port_entry.get("version")) if x).strip()
    service = str(port_entry.get("service") or "")
    scripts = port_entry.get("scripts") or {}
    detail = ""
    if isinstance(scripts, dict):
        for k in ("http-title", "banner", "ssl-cert"):
            if scripts.get(k):
                detail = str(scripts[k]).splitlines()[0][:80]
                break
    probed = str(port_entry.get("method") or "") == "probed" or bool(product) or bool(detail)
    if not probed:
        return None
    label = product or service or "?"
    text = f"{service} {product} {detail}"
    cat = next((c for pat, c in _CONFIRMED_CATEGORY if pat.search(text)), "other")
    ev = f"IP 探測：{service or '?'}" + (f" {product}" if product else "") + (f"（{detail}）" if detail else "")
    return label, cat, ev


def from_port(port: int | None, proto: Any) -> tuple[str, str, str] | None:
    from app.services.attack_surface_ai import sensitive_service
    svc = sensitive_service(port, proto)
    if svc:
        name_cat = next((c for pat, c in _CONFIRMED_CATEGORY if pat.search(svc)), "mgmt")
        return svc, name_cat, f"依埠號推測：{port}/{str(proto or 'any').lower()}"
    hit = _COMMON_PORTS.get(port) if port is not None else None
    if hit and _proto_ok(proto, hit[1]):
        return hit[0], "other", f"依埠號推測：{port}/{str(proto or 'any').lower()}"
    return None


def identify(item: dict[str, Any], probe_ports: list[dict[str, Any]] | None) -> dict[str, Any]:
    """一列的服務判定。`probe_ports`：這個目標最近一次（換手之後）IP 探測看到的開放埠。"""
    try:
        port = int(str(item.get("port")).strip())
    except (TypeError, ValueError):
        port = None
    proto = str(item.get("protocol") or "").lower()
    entry = None
    for p in probe_ports or []:
        if p.get("port") == port and (not proto or proto in ("any", "tcp/udp") or (p.get("proto") or "tcp") == proto):
            entry = p
            break
    conf, named, guess = from_probe(entry), from_name(item), from_port(port, proto)
    out: dict[str, Any] = {"name": None, "level": None, "evidence": [], "sensitive": False,
                           "unverified_sensitive": False, "mismatch": None}
    best = conf or named or guess
    if best is None:
        return out
    out["name"] = best[0]
    out["level"] = "confirmed" if conf else "named" if named else "port_guess"
    out["evidence"] = [x[2] for x in (conf, named, guess) if x]
    if conf:
        out["sensitive"] = conf[1] in SENSITIVE_CATEGORIES
        # 名稱說是一種、實際探到的是敏感服務 —— 這是要查的
        if named and conf[1] in SENSITIVE_CATEGORIES and named[1] != conf[1]:
            out["mismatch"] = f"名稱看起來是 {named[0]}，實際探測到 {conf[0]}"
    elif named:
        out["sensitive"] = named[1] in SENSITIVE_CATEGORIES
    elif guess:
        out["unverified_sensitive"] = guess[1] in SENSITIVE_CATEGORIES
    return out


async def probe_ports_by_ip(session: AsyncSession, ips: set[str]) -> dict[str, list[dict[str, Any]]]:
    """每個 IP 最近一次成功的 IP 探測（位址換手之後）看到的開放埠。"""
    from sqlalchemy import Text, func, select

    from app.core.sqlin import in_values
    from app.models.address import IPAddress
    from app.models.agent_probe_job import STATUS_DONE, AgentProbeJob
    if not ips:
        return {}
    since: dict[str, Any] = {}
    for ip, ds in (await session.execute(select(func.host(IPAddress.ip), IPAddress.device_since).where(
            in_values(func.host(IPAddress.ip), sorted(ips), type_=Text())))).all():
        if ds is not None and (ip not in since or (since[ip] is not None and ds > since[ip])):
            since[ip] = ds
    target = AgentProbeJob.params["targets"][0].astext
    rows = (await session.execute(select(target, AgentProbeJob.result, AgentProbeJob.created_at).where(
        AgentProbeJob.kind == "identify", AgentProbeJob.status == STATUS_DONE,
        in_values(target, sorted(ips), type_=Text())).order_by(AgentProbeJob.created_at.desc()))).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for ip, result, created in rows:
        if ip in out or not isinstance(result, dict):
            continue
        if since.get(ip) is not None and created < since[ip]:
            continue                       # 換手之前的探測屬於上一台
        nmap = result.get("nmap") if isinstance(result.get("nmap"), dict) else {}
        out[ip] = [p for p in (nmap.get("ports") or []) if isinstance(p, dict)]
    return out


async def enrich(session: AsyncSession, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每列加上 `service`（判定結果）。"""
    ips = {str((i.get("identity") or {}).get("ip") or "").split("/")[0] for i in items}
    probes = await probe_ports_by_ip(session, {x for x in ips if x})
    for it in items:
        ip = str((it.get("identity") or {}).get("ip") or "").split("/")[0]
        it["service"] = identify(it, probes.get(ip))
    return items
