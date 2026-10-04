"""IP 詳細頁「探測」的結果摘要：由掃描代理回報的證據推出「這是什麼主機」。

探測本身在掃描代理上跑（agent/jt_ipam_agent.py 的 `_job_run_identify`）：服務版本、OS 指紋、
banner／TLS 憑證等唯讀資訊，加上反解、NetBIOS、mDNS 名稱。這裡只做**確定性的推論**
（規則表），每個結論都附上依據，畫面上看得到為什麼這樣判斷 —— 不交給 LLM 猜。

設備類型代碼（前端翻譯）：server／windows／router／switch／firewall／wireless_ap／printer／
camera／voip／hypervisor／storage／media／specialized／unknown。

有安裝 Recog 指紋庫（選用，services/recog.py）時，另外拿 banner、網頁標題、憑證、SMB 回的
OS 字串去比對：認得出 nmap 認不出的設備（預設憑證、管理介面標題）與更精確的 OS（OpenSSH 註解裡的
發行版）。Recog 的結論一樣列在依據裡。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.recog import Matcher

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
# 每條規則看（這個埠, 整台的背景）：單一個埠常常不夠 —— NAS 也會開 RTSP、Linux 的 Samba 也會開 445。
# ⚠️ 這終究是推測（畫面上會標明可能不準），規則要「寧可說不知道，也不要自信地說錯」。
_NAS_PRODUCT = re.compile(r"synology|diskstation|\bdsm\b|qnap|\bqts\b|asustor|terramaster|readynas|truenas|"
                          r"freenas|unraid|openmediavault", re.I)
_CAMERA_PRODUCT = re.compile(r"webcam|ip ?camera|network camera|hikvision|dahua|axis .*camera|vivotek|"
                             r"reolink|foscam", re.I)
# OUI 廠商：主力產品是 NAS 的（Synology 也做路由器，但少見；判錯時畫面上的依據看得出來）
_NAS_VENDORS = ("synology", "qnap", "asustor", "terramaster", "buffalo", "drobo")
_FILE_SHARING = ("nfs", "iscsi", "afp", "netbios-ssn", "microsoft-ds")


def _text(p: dict[str, Any]) -> str:
    return f"{p.get('product') or ''} {p.get('extrainfo') or ''}"


def _is_samba(p: dict[str, Any]) -> bool:
    return "samba" in _text(p).lower()


_SERVICE_RULES: list[tuple[str, Any]] = [
    ("hypervisor", lambda p, c: re.search(r"proxmox|vmware esxi|vmware authentication|xenserver|hyper-v",
                                          _text(p), re.I)
                                or p.get("port") == 8006),   # Proxmox VE 網頁介面；版本偵測常只認得 tcpwrapped
    # NAS：產品字樣、主力做 NAS 的廠牌＋檔案分享、或 iSCSI／NFS／AFP 這種儲存服務
    ("storage", lambda p, c: _NAS_PRODUCT.search(_text(p))
                             or (c["nas_vendor"] and p.get("service") in _FILE_SHARING)
                             or p.get("service") in ("iscsi", "nfs", "afp") or p.get("port") in (3260, 2049, 548)),
    # IPP 本身不代表印表機：Linux 的 CUPS 列印服務也開 631/ipp（實測 Ubuntu 開發機被判成印表機）
    ("printer", lambda p, c: p.get("service") in ("jetdirect", "printer") or p.get("port") == 9100
                             or (p.get("service") == "ipp" and "cups" not in str(p.get("product") or "").lower())),
    ("camera", lambda p, c: _CAMERA_PRODUCT.search(_text(p)) and not c["file_sharing"]),
    # 只有 RTSP 時：有檔案分享的不算（NAS、媒體伺服器都會開 RTSP）
    ("camera", lambda p, c: (p.get("service") == "rtsp" or p.get("port") == 554) and not c["file_sharing"]),
    ("voip", lambda p, c: p.get("service") in ("sip", "sip-tls") or p.get("port") in (5060, 5061)),
    # 445／139 在 Linux 上是 Samba，不是 Windows
    ("windows", lambda p, c: (p.get("service") in ("ms-wbt-server", "msrpc") or p.get("port") == 3389
                              or (p.get("service") == "microsoft-ds" and not _is_samba(p)))),
]

_MIN_OS_ACCURACY = 85
#: 第一名是設備類、但通用作業系統的猜測只差這麼多以內 → 分不出來，改用通用作業系統那筆
#: （nmap 常把新版 Linux 核心認成 HP P2000 G3 NAS，兩者只差 0~1 個百分點）
_AMBIGUOUS_GAP = 2


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


def _applications(ports: list[dict[str, Any]], recog_apps: dict[str, str] | None = None) -> list[str]:
    """這台跑了哪些軟體：nmap 認出來的產品＋版本；nmap 認不出、Recog 認得的用 Recog 的；
    都認不出、但 TLS 憑證的 CN 不是主機名稱時，CN 通常就是軟體名稱（例如 AnyDesk 的自簽憑證
    「AnyDesk Client」）。同一個軟體只列一次。"""
    apps: list[str] = []
    for p in ports:
        prod = " ".join(x for x in (p.get("product"), p.get("version")) if x).strip()
        if not prod and recog_apps:
            prod = recog_apps.get(f"{p.get('port')}/{p.get('proto') or 'tcp'}", "")
        if not prod:
            cn = _cert_subject_cn(p)
            if cn and not _FQDN_RE.match(cn) and not cn.startswith("*"):
                # 去掉結尾的 Client／Server／Service（不用 `\s+…$`：一長串空白會回溯到平方時間）
                words = cn.split()
                if len(words) > 1 and words[-1] in ("Client", "Server", "Service"):
                    words = words[:-1]
                prod = " ".join(words)
        if prod and prod not in apps:
            apps.append(prod)
    return apps


# ───────────────────────── Recog 指紋比對 ─────────────────────────

# Recog 的 hw.device／os.device → 設備類型。刻意不對應的：Desktop／Laptop／Mobile Phone（我們沒有這類）、
# Device／Appliance／Networking 這種太籠統的 —— 對不上就交給其他規則，不硬猜。
_RECOG_DEVICE: dict[str, str] = {
    **dict.fromkeys(("printer", "multifunction device", "print server", "copier", "fax server"), "printer"),
    **dict.fromkeys(("router", "broadband router", "adsl router", "cable modem", "docsis cable modem",
                     "dsl modem", "adsl modem", "sd-wan appliance", "remote access server"), "router"),
    **dict.fromkeys(("switch", "hub"), "switch"),
    **dict.fromkeys(("firewall", "security appliance", "ips", "ids", "vpn", "utm"), "firewall"),
    **dict.fromkeys(("wap", "wireless controller", "wlan repeater"), "wireless_ap"),
    **dict.fromkeys(("ip camera", "web cam", "dvr", "cloud network video recorder", "video encoder"), "camera"),
    **dict.fromkeys(("voip", "sip gateway", "sip device", "voip gateway", "voip server", "voip switch",
                     "voice appliance", "video conferencing"), "voip"),
    **dict.fromkeys(("nas", "storage", "storage appliance", "tape library"), "storage"),
    "hypervisor": "hypervisor",
    **dict.fromkeys(("media server", "media player", "smart tv", "iptv", "media receiver", "av receiver",
                     "network audio"), "media"),
    **dict.fromkeys(("lights out management", "management processor", "onboard administrator", "kvm",
                     "power device", "ups", "pdu", "plc", "hmi controller", "industrial control",
                     "building automation", "environment control", "access control", "alarm panel",
                     "sensor", "device server", "test instrument"), "specialized"),
}
_RECOG_MIN_CERTAINTY = 0.5        # 低於這個（例如 0.0＝「assert nothing」）的欄位一律不用
_RECOG_STRONG_OS = 0.75           # OS 到這個把握度才蓋過 nmap 的 OS 指紋
_MAX_RECOG_EVIDENCE = 6

_NMAP_ESC = re.compile(r"\\x([0-9A-Fa-f]{2})|\\\\")
# telnet 協商位元組：WILL/WONT/DO/DONT＋選項、子協商 SB … SE、其他單一指令。
# 子協商限長（CodeQL #39）：以前寫 `.*?`，每個沒結束的 SB 都掃到結尾＝二次方（40 KB 要十秒）；
# 子協商內容裡的 IAC 會寫成 \xff\xff，要允許
_TELNET_IAC = re.compile(rb"\xff[\xfb-\xfe].|\xff\xfa(?:[^\xff]|\xff\xff){0,256}\xff\xf0|\xff[\xf0-\xfa]", re.S)
#: smb-os-discovery 的「OS: …」那一行（只取整行，括號另外拆：CodeQL #40）
_SMB_OS_LINE = re.compile(r"^[ \t]*OS: ([^\n]+)$", re.M)
# nmap ssl-cert 的欄位名稱 → RFC 4514 的簡寫（Recog 的憑證範例是 CN=…,OU=…,O=…,L=…,ST=…,C=… 的順序）
_DN_ORDER = (("emailAddress", "emailAddress"), ("serialNumber", "SERIALNUMBER"), ("commonName", "CN"),
             ("organizationalUnitName", "OU"), ("organizationName", "O"), ("localityName", "L"),
             ("stateOrProvinceName", "ST"), ("countryName", "C"))


def _unescape(text: str, *, telnet: bool = False) -> str:
    """nmap 腳本輸出把不可列印的位元組寫成 \\xHH：還原後以 UTF-8 解（中文標題才讀得出來）。"""
    raw = bytearray()
    pos = 0
    for m in _NMAP_ESC.finditer(text):
        raw += text[pos:m.start()].encode("utf-8", errors="replace")
        raw += bytes([int(m.group(1), 16)]) if m.group(1) else b"\\"
        pos = m.end()
    raw += text[pos:].encode("utf-8", errors="replace")
    data = bytes(raw)
    if telnet:
        data = _TELNET_IAC.sub(b"", data)
    return data.decode("utf-8", errors="replace")


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _dn(fields: dict[str, Any]) -> str:
    """{commonName: …, organizationName: …} → 「CN=…,O=…」（值裡的逗號要跳脫）。"""
    parts = []
    for long_name, short in _DN_ORDER:
        v = fields.get(long_name)
        if isinstance(v, str) and v:
            parts.append(f"{short}={v.replace(',', chr(92) + ',')}")
    return ",".join(parts)


def _dn_from_text(line: str) -> str:
    """舊版代理只回文字：「commonName=x/organizationName=y/countryName=US」。"""
    fields: dict[str, str] = {}
    for part in line.split("/"):
        k, sep, v = part.partition("=")
        if sep:
            fields[k.strip()] = v.strip()
    return _dn(fields)


def _banner_observation(p: dict[str, Any], banner: str) -> tuple[str, str] | None:
    svc = str(p.get("service") or "").lower()
    port = p.get("port")
    line = _first_line(_unescape(banner))
    if line.startswith("SSH-") or svc == "ssh":
        return "ssh.banner", re.sub(r"^SSH-[\d.]+-", "", line)
    if svc == "ftp" or port == 21:
        return "ftp.banner", re.sub(r"^220[- ]", "", line)
    if svc in ("smtp", "submission") or port in (25, 587):
        return "smtp.banner", re.sub(r"^220[- ]", "", line)
    if svc == "pop3" or port == 110:
        return "pop3.banner", re.sub(r"^\+OK\s*", "", line)
    if svc == "imap" or port == 143:
        return "imap4.banner", re.sub(r"^\* OK\s*(?:\[[^\]]{0,400}\]\s*)?", "", line)
    if svc == "telnet" or port == 23:
        return "telnet_banners", _unescape(banner, telnet=True).strip()
    return None


def recog_observations(nmap: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """探測結果裡可以拿去比對 Recog 的文字 → [(指紋庫, 埠, 文字)]。"""
    obs: list[tuple[str, str | None, str]] = []
    for p in _open_ports(nmap):
        where = f"{p.get('port')}/{p.get('proto') or 'tcp'}"
        scripts = p.get("scripts") or {}
        data = p.get("script_data") or {}
        if scripts.get("banner"):
            b = _banner_observation(p, str(scripts["banner"]))
            if b and b[1]:
                obs.append((b[0], where, b[1]))
        for line in str(scripts.get("http-server-header") or "").splitlines():
            if line.strip():
                obs.append(("http_header.server", where, _unescape(line.strip())))
        title = _first_line(str(scripts.get("http-title") or ""))
        if title and not title.startswith(("Site doesn't have a title", "Did not follow redirect")):
            obs.append(("html_title", where, _unescape(title)))
        cert = data.get("ssl-cert") if isinstance(data.get("ssl-cert"), dict) else None
        text = str(scripts.get("ssl-cert") or "")
        for part, label in (("subject", "Subject:"), ("issuer", "Issuer:")):
            if cert and isinstance(cert.get(part), dict):
                dn = _dn(cert[part])
            else:
                line = next((ln.strip()[len(label):].strip() for ln in text.splitlines()
                             if ln.strip().startswith(label)), "")
                dn = _dn_from_text(line) if line else ""
            if dn:
                obs.append((f"x509.{part}", where, dn))
    # smb-os-discovery 是主機層腳本：「OS: Windows 10 Pro 19045 (Windows 10 Pro 6.3)」＝ native OS (LAN manager)
    smb = str((nmap.get("host_scripts") or {}).get("smb-os-discovery") or "")
    # 以前一條正規表示式同時拆括號（`(.+?)(?: \((.+)\))?\s*$`）：遇到「OS: a ( ( ( …」是二次方。
    # 現在先取整行、再用字串操作拆：第一個「 (」之前是 native OS，結尾的括號內是 LAN manager
    m = _SMB_OS_LINE.search(smb)
    rest = m.group(1).strip() if m else ""
    if rest:
        i = rest.find(" (", 1)
        if i != -1 and rest.endswith(")") and len(rest) - i > 3:
            obs.append(("smb.native_os", None, rest[:i]))
            obs.append(("smb.native_lm", None, rest[i + 2:-1]))
        else:
            obs.append(("smb.native_os", None, rest))
    return obs


def _certainty(match: dict[str, Any], ns: str) -> float:
    raw = match["params"].get(f"{ns}.certainty") or match.get("certainty")
    try:
        return float(raw) if raw is not None else 1.0
    except ValueError:
        return 1.0


def recog_matches(nmap: dict[str, Any], matcher: Matcher) -> list[dict[str, Any]]:
    """每段文字比對一次；比中了但 Recog 自己說「不下結論」（certainty 0）的也留著，只是不採用。"""
    out = []
    for key, where, text in recog_observations(nmap):
        m = matcher.match(key, text)
        if m:
            out.append({**m, "port": where, "input": text[:200]})
    return out


def _os_from_params(pr: dict[str, str]) -> str | None:
    product, vendor = pr.get("os.product"), pr.get("os.vendor")
    name = product or pr.get("os.family")
    if not name:
        return None
    if vendor and vendor.lower() not in name.lower():
        name = f"{vendor} {name}"
    for extra in (pr.get("os.edition"), pr.get("os.version")):
        if extra and extra.lower() not in name.lower():
            name = f"{name} {extra}"
    return name


def _service_name(pr: dict[str, str]) -> str | None:
    product = pr.get("service.product")
    if not product:
        return None
    family = pr.get("service.family")
    name = f"{family} {product}" if family and family.lower() not in product.lower() else product
    return f"{name} {pr['service.version']}" if pr.get("service.version") else name


def _recog_conclusions(matches: list[dict[str, Any]], matcher: Matcher,
                       known_ports: set[str]) -> dict[str, Any]:
    """比對結果 → 類型、OS（附把握度）、硬體廠牌、型號、每個埠的軟體。偏好度高的指紋庫先看。

    `known_ports`：nmap 已經認出產品的埠 —— 這些埠的軟體用 nmap 的，Recog 的就不算貢獻
    （不然依據裡會塞滿「nginx with version info」這種對結論沒有幫助的條目）。"""
    ranked = sorted(matches, key=lambda m: -matcher.preference(m["db"]))
    out: dict[str, Any] = {"device": None, "os": None, "vendor": None, "model": None, "apps": {}, "used": [],
                           "default_cert_ports": set()}
    for m in ranked:
        pr = m["params"]
        used = False
        hw_ok = _certainty(m, "hw") >= _RECOG_MIN_CERTAINTY
        os_ok = _certainty(m, "os") >= _RECOG_MIN_CERTAINTY
        # 比中的是設備出廠的預設憑證（Synology 的 CN=synology.com 這種）：上面的名稱是廠商的，不是這台的
        if m["db"] == "x509.subject" and m.get("port") and (
                (hw_ok and any(k.startswith("hw.") for k in pr)) or (os_ok and any(k.startswith("os.") for k in pr))):
            out["default_cert_ports"].add(m["port"])
        if out["device"] is None:
            for dev, ok in ((pr.get("hw.device"), hw_ok), (pr.get("os.device"), os_ok)):
                code = _RECOG_DEVICE.get((dev or "").lower()) if ok else None
                if code:
                    out["device"] = (code, m)
                    used = True
                    break
        if os_ok:
            name = _os_from_params(pr)
            cert = _certainty(m, "os")
            if name and (out["os"] is None or cert > out["os"][1]):
                out["os"] = (name, cert, m)
                used = True
        if hw_ok and out["vendor"] is None and pr.get("hw.vendor"):
            out["vendor"] = pr["hw.vendor"]
            used = True
        if hw_ok and out["model"] is None and (pr.get("hw.product") or pr.get("hw.family")):
            out["model"] = pr.get("hw.product") or pr.get("hw.family")
            used = True
        if (_certainty(m, "service") >= _RECOG_MIN_CERTAINTY and m.get("port")
                and m["port"] not in known_ports and m["port"] not in out["apps"]):
            svc = _service_name(pr)
            if svc:
                out["apps"][m["port"]] = svc
                used = True
        if used:
            out["used"].append(m)
    return out


def _recog_evidence(m: dict[str, Any]) -> str:
    where = f"{m['port']} " if m.get("port") else ""
    return f"recog:{m['description'] or m['db']} ({where}{m['db']})"


def summarize(result: dict[str, Any] | None, *, mac_vendor: str | None = None,
              recog: Matcher | None = None, virtual_guest: bool = False) -> dict[str, Any]:
    """代理回報的探測結果 → 摘要。`mac_vendor` 是 jt-ipam 依 IP 記錄的 MAC 查到的 OUI 廠商；
    `recog` 是已安裝的 Recog 指紋庫（沒安裝就是 None，摘要照常，只是少了這一層）；
    `virtual_guest`：已由虛擬化整合確認是虛擬機或容器 —— 虛擬化讓 TCP/IP 指紋失準，不拿它的類別判斷。"""
    result = result or {}
    nmap = result.get("nmap") or {}
    names_in = result.get("names") or {}
    ports = _open_ports(nmap)
    evidence: list[str] = []

    rc = (_recog_conclusions(recog_matches(nmap, recog), recog,
                             {f"{p.get('port')}/{p.get('proto') or 'tcp'}" for p in ports if p.get("product")})
          if recog else None)

    os_name = None
    os_list = [o for o in (nmap.get("os") or []) if isinstance(o, dict)]
    top = os_list[0] if os_list else None
    if top and str(top.get("type") or "").lower() not in ("", "general purpose"):
        gp = next((o for o in os_list if str(o.get("type") or "").lower() == "general purpose"), None)
        if gp and int(top.get("accuracy") or 0) - int(gp.get("accuracy") or 0) <= _AMBIGUOUS_GAP:
            top = gp
    if top and int(top.get("accuracy") or 0) >= _MIN_OS_ACCURACY:
        os_name = top.get("name")
        evidence.append(f"os:{top.get('name')} ({top.get('accuracy')}%)")
    # Recog 的 OS 來自服務自己講的話（OpenSSH 的註解、SMB 回的 OS 名稱），夠有把握時比 TCP/IP 指紋精確；
    # 把握度低的（例如「IIS 10 大概是 Windows」）只在 nmap 沒結論時才用
    # nmap 的指紋被推翻了沒有：Recog 有把握地講出另一個 OS 時，指紋的類別與廠牌也不再採信
    #（2026-10-02 PVE 的 LXC 容器：指紋說 HP NAS，OpenSSH 說 Ubuntu → 以前 OS 寫 Ubuntu、類型卻寫儲存設備）
    fingerprint_overruled = False
    if rc and rc["os"]:
        rname, rcert, _m = rc["os"]
        if rcert >= _RECOG_STRONG_OS or os_name is None:
            fingerprint_overruled = bool(top and os_name and rcert >= _RECOG_STRONG_OS and os_name != rname)
            os_name = rname
    trust_fingerprint = top is not None and not fingerprint_overruled and not virtual_guest

    ctx = {
        "file_sharing": any(p.get("service") in _FILE_SHARING or p.get("port") in (2049, 3260, 548)
                            for p in ports),
        "nas_vendor": any(v in (mac_vendor or nmap.get("mac_vendor") or "").lower() for v in _NAS_VENDORS),
    }
    device_type = "unknown"
    # Recog 比中的是特定產品的預設憑證、管理介面標題、banner —— 比「開了哪個埠」具體，先看
    if rc and rc["device"]:
        device_type = rc["device"][0]
    else:
        for code, rule in _SERVICE_RULES:
            hit = next((p for p in ports if rule(p, ctx)), None)
            if hit:
                device_type = code
                evidence.append(f"service:{_service_line(hit)}")
                break
    if device_type == "unknown" and trust_fingerprint and top and top.get("type"):
        mapped = _OSCLASS_TYPE.get(str(top["type"]).lower())
        if mapped and int(top.get("accuracy") or 0) >= _MIN_OS_ACCURACY:
            device_type = mapped
            evidence.append(f"osclass:{top['type']}")
    if device_type == "unknown" and fingerprint_overruled and os_name:
        # 服務自己講出的是一般作業系統（Ubuntu、Windows…）：那就是一台一般主機
        from app.core.os_fingerprint import normalize_os
        fam = normalize_os(os_name)
        if fam in ("linux", "windows", "bsd", "macos"):
            device_type = "windows" if fam == "windows" else "server"
            evidence.append(f"recog-os:{os_name}")
    if device_type == "unknown" and virtual_guest:
        # 虛擬機／容器沒有任何服務講出特定角色 → 一般主機（Windows 的話標 Windows）。
        # 不能停在「不明」：不明不會覆寫 IP 上舊的判讀，被指紋判錯的「儲存設備」就永遠改不過來
        from app.core.os_fingerprint import normalize_os
        device_type = "windows" if os_name and normalize_os(os_name) == "windows" else "server"
        evidence.append("virt:guest")

    vendor = (mac_vendor or nmap.get("mac_vendor") or (rc["vendor"] if rc else None)
              or ((top or {}).get("vendor") if trust_fingerprint else None) or None)
    if mac_vendor:
        evidence.append(f"oui:{mac_vendor}")
    if rc:
        evidence += [_recog_evidence(m) for m in rc["used"][:_MAX_RECOG_EVIDENCE]]

    names: list[str] = []
    skip_certs = rc["default_cert_ports"] if rc else set()
    for n in [names_in.get("rdns"), names_in.get("netbios"), names_in.get("mdns"),
              *(nmap.get("hostnames") or []),
              *[c for p in ports if f"{p.get('port')}/{p.get('proto') or 'tcp'}" not in skip_certs
                for c in _cert_names(p)]]:
        if n and n not in names:
            names.append(str(n))

    # 主機到底有沒有回應：-Pn 時 nmap 一律把主機當成「在線」，不能看它的狀態欄；
    # 要看實際的證據 —— 開著或關著的埠、區網內的 MAC 回應、OS 指紋、主機自己回的 NetBIOS／mDNS 名稱。
    # 反解是 DNS 回的，不算。全部沒有＝探測時沒有回應（多半是關機、離線，或防火牆擋掉所有探測）
    nmap_ok = bool(nmap.get("available", bool(nmap)))
    responded = bool(ports or int(nmap.get("closed") or 0) or nmap.get("mac") or top
                     or names_in.get("netbios") or names_in.get("mdns"))
    no_response = nmap_ok and not responded
    if no_response:
        device_type = "no_response"

    return {
        "device_type": device_type,
        "no_response": no_response,
        "os": os_name,
        "vendor": vendor,
        "model": rc["model"] if rc else None,
        "names": names,
        "applications": _applications(ports, rc["apps"] if rc else None),
        "services": [_service_line(p) for p in ports],
        "evidence": evidence,
        "nmap_available": bool(nmap.get("available", bool(nmap))),
        # 用了哪一版 Recog 指紋庫（沒裝是 None，畫面上會提示判斷較有限）
        "recog": recog.release if recog else None,
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
