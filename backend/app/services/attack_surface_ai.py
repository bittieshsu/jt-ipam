"""對外開放服務清單的 AI 分析（使用者 2026-10-19）。

分層照其他資安 AI 功能（project_security_ai_features）：**事實由程式算**——總數、各防火牆的開口數、
未登錄的目標、管理埠對外、目標已離線卻還開著、沒有 Wazuh 代理、開出去的是什麼設備；
模型只把這些寫成判讀、排出優先順序、給下一步。主機名稱、規則名稱、說明是外面的人改得到的文字，
一律 fence() 加 <data> 定界才進提示詞。
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

#: 一次分析最多幾列（畫面篩選後送回的 key 數）。再多就請使用者先篩選；全部都送也不會超過清單上限
MAX_KEYS = 5000
#: 提示詞裡逐列列出的上限（其餘只進統計）—— 模型的上下文有限，統計才是重點
_LIST_MAX = 60

#: 對外開放就值得特別看的連接埠（管理介面、遠端桌面、資料庫、不加密的老協定）：埠 → (常見用途, 協定)。
#: 要看協定：UDP 10000 是 Jitsi Videobridge，不是 Webmin（TCP 10000）—— 只看埠號曾讓 AI 把視訊會議講成
#: 「Webmin 管理介面對外」（使用者 2026-10-19）。這只是依埠號的推測，規則名稱說的才算數（提示詞有寫）。
_T, _U, _TU = frozenset({"tcp"}), frozenset({"udp"}), frozenset({"tcp", "udp"})
SENSITIVE_PORTS: dict[int, tuple[str, frozenset[str]]] = {
    21: ("FTP", _T), 22: ("SSH", _T), 23: ("Telnet", _T), 69: ("TFTP", _U), 111: ("RPC", _TU),
    135: ("MS-RPC", _T), 139: ("NetBIOS", _T), 161: ("SNMP", _U), 389: ("LDAP", _TU), 445: ("SMB", _T),
    623: ("IPMI", _U), 1433: ("MSSQL", _T), 1521: ("Oracle", _T), 2049: ("NFS", _TU),
    2375: ("Docker API", _T), 2376: ("Docker API", _T), 3306: ("MySQL", _T), 3389: ("RDP", _T),
    5432: ("PostgreSQL", _T), 5900: ("VNC", _T), 5901: ("VNC", _T), 5985: ("WinRM", _T), 5986: ("WinRM", _T),
    6379: ("Redis", _T), 8006: ("Proxmox", _T), 8291: ("MikroTik Winbox", _T), 9200: ("Elasticsearch", _T),
    10000: ("Webmin", _T), 11211: ("Memcached", _TU), 27017: ("MongoDB", _T),
}


def sensitive_service(port: int | None, protocol: Any) -> str | None:
    """這個埠＋協定的常見敏感用途；協定不明（any、沒寫）時照埠號算（寧可多看一眼）。"""
    hit = SENSITIVE_PORTS.get(port) if port is not None else None
    if hit is None:
        return None
    proto = str(protocol or "").lower()
    return hit[0] if proto in ("", "any", "tcp/udp") or proto in hit[1] else None


def surface_key(item: dict[str, Any]) -> str:
    """一列的穩定識別（同一份清單重算時不變）：前端送回這個，後端照它挑出畫面上的那些列。"""
    ident = item.get("identity") or {}
    raw = "|".join(str(x or "") for x in (
        item.get("via"), item.get("source"), item.get("firewall"), ident.get("ip"),
        item.get("port"), str(item.get("protocol") or "").lower(), item.get("name"), item.get("descr")))
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def _port(item: dict[str, Any]) -> int | None:
    try:
        return int(str(item.get("port")).strip())
    except (TypeError, ValueError):
        return None


def _online(ident: dict[str, Any]) -> bool | None:
    st = str(ident.get("status") or "").lower()
    if not st:
        return None
    return st.startswith("online")


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    ident = item.get("identity") or {}
    svc = item.get("service") or {}
    return {"ip": ident.get("ip"), "port": item.get("port"), "protocol": item.get("protocol"),
            "name": item.get("name"), "firewall": item.get("firewall"), "hostname": ident.get("hostname"),
            "service": svc.get("name"), "level": svc.get("level"),
            "evidence": (svc.get("evidence") or [None])[0]}


def surface_facts(items: list[dict[str, Any]]) -> dict[str, Any]:
    """確定性的統計與分類（畫面與提示詞共用；給模型的只是這份的文字版）。"""
    from app.services.surface_service import identify
    targets = {(i.get("identity") or {}).get("ip") or f"?{n}" for n, i in enumerate(items)}
    sensitive, unverified, mismatch, unregistered, offline, no_wazuh = [], [], [], [], [], []
    seen_off: set[str] = set()
    seen_nw: set[str] = set()
    kinds: Counter[str] = Counter()
    for it in items:
        ident = it.get("identity") or {}
        if "service" not in it:            # 端點會先用 surface_service.enrich 帶探測結果；沒有就只看名稱與埠號
            it = {**it, "service": identify(it, None)}
        svc = it["service"]
        if svc.get("sensitive"):
            sensitive.append(_brief(it))
        elif svc.get("unverified_sensitive"):
            unverified.append(_brief(it))
        if svc.get("mismatch"):
            mismatch.append({**_brief(it), "mismatch": svc["mismatch"]})
        if not ident.get("registered"):
            unregistered.append(_brief(it))
            continue
        ip = str(ident.get("ip") or "")
        if _online(ident) is False and ip not in seen_off:
            seen_off.add(ip)
            offline.append(_brief(it))
        if not ident.get("wazuh") and ip not in seen_nw:
            seen_nw.add(ip)
            no_wazuh.append(_brief(it))
        if ident.get("device_kind"):
            kinds[str(ident["device_kind"])] += 1
    return {
        "total": len(items),
        "targets": len(targets),
        "by_via": dict(Counter(str(i.get("via") or "") for i in items)),
        "by_firewall": dict(Counter(str(i.get("firewall") or "—") for i in items)),
        "by_protocol": dict(Counter(str(i.get("protocol") or "any").upper() for i in items)),
        "unregistered": unregistered,
        "sensitive": sensitive,
        "unverified": unverified,
        "mismatch": mismatch,
        "offline": offline,
        "no_wazuh": no_wazuh,
        "device_kinds": dict(kinds),
    }


_LEVEL_ZH = {"confirmed": "已確認", "named": "依名稱", "port_guess": "依埠號推測"}


def _line(e: dict[str, Any], fence: Any) -> str:
    svc = ""
    if e.get("service"):
        svc = f"（{_LEVEL_ZH.get(e.get('level') or '', '')}：{e['service']}；依據 <data>{fence(e.get('evidence'))}</data>）"
    if e.get("mismatch"):
        svc += f" 不符：<data>{fence(e['mismatch'])}</data>"
    return (f"- {e.get('ip') or '?'}:{e.get('port') or '—'}/{str(e.get('protocol') or 'any').lower()}{svc} "
            f"防火牆 <data>{fence(e.get('firewall'))}</data> 規則 <data>{fence(e.get('name'))}</data> "
            f"主機名稱 <data>{fence(e.get('hostname'))}</data>")


def build_prompt(facts: dict[str, Any], items: list[dict[str, Any]]) -> str:
    from app.services.ip_triage import fence
    from app.services.prompt_safety import DATA_RULE_ZH

    def block(title: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return f"{title}：無"
        more = f"\n（另有 {len(rows) - _LIST_MAX} 筆未列出）" if len(rows) > _LIST_MAX else ""
        return f"{title}（{len(rows)} 筆）：\n" + "\n".join(_line(r, fence) for r in rows[:_LIST_MAX]) + more

    def kv(d: dict[str, Any]) -> str:
        return "、".join(f"<data>{fence(k)}</data> {v}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "無"

    others = [_brief(i) for i in items][:_LIST_MAX]
    return f"""你是網路資安分析師，要替一份「對外開放服務清單」寫判讀。統計與分類已經由系統算好，是事實；你負責解讀與排優先順序。

規則：
- {DATA_RULE_ZH}
- 只根據下面列出的資料，缺資料就說「無法判斷」，不可以編造位址、埠或設備。
- 「未登錄」代表對外開口指向 IPAM 沒有的主機。
- 每個服務標了證據等級：「已確認」是 IP 探測實際看到的；「依名稱」是規則名稱、主機名稱、FQDN 寫的；
  「依埠號推測」只是該連接埠與協定的常見用途，不是事實。依埠號推測的項目不可以列為主要風險，只能說「若是 X 則……」；
  名稱顯示其他用途時（例如 Jitsi、Videobridge、VPN）以名稱為準。
- 「名稱與實際服務不符」是值得優先查的發現（規則寫的用途和實際在跑的不一樣）。

總覽：共 {facts['total']} 個對外開口、{facts['targets']} 個目標位址。
來源：{kv(facts['by_via'])}
各防火牆：{kv(facts['by_firewall'])}
協定：{kv(facts['by_protocol'])}
開出去的設備類型：{kv(facts['device_kinds'])}

{block('管理介面／敏感服務（已確認或依名稱）', facts['sensitive'])}

{block('名稱與實際服務不符', facts['mismatch'])}

{block('只依埠號推測、未確認的敏感埠', facts['unverified'])}

{block('指向未登錄主機', facts['unregistered'])}

{block('目標已離線卻仍開著', facts['offline'])}

{block('已登錄但沒有 Wazuh 代理', facts['no_wazuh'])}

{block('清單內容（節錄）', others)}

請用 Markdown 輸出（不超過 450 字）：
## 摘要
兩三句說明整體暴露程度。
## 主要風險
依嚴重程度排序，每點寫出是哪些位址／埠、為什麼危險。
## 建議
具體的下一步（關閉、限制來源、改走 VPN、補登錄、裝代理……）。
## 資料限制
這份分析看不到什麼（例如目的是別名或網段的規則沒有展開）。"""


async def run_analysis(session: Any, prompt: str, *, on_chunk: Any) -> tuple[str, str]:
    """把提示詞送進「AI 判讀」模型（邊寫邊回 on_chunk）。提示詞由 build_prompt 組，資料一律定界
    （prompt_safety.DATA_RULE_ZH）；呼叫留在這個模組，送進 LLM 的地方才看得到那條規則。"""
    from app.services import ai as ai_mod
    return await ai_mod.interpret_chat(session, prompt, timeout=180.0, max_output_tokens="auto",
                                       no_thinking=True, on_chunk=on_chunk)
