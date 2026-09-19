"""OCS Inventory NG 同步服務 —— REST（/ocsapi/v1），**全程唯讀（只打 GET）**。

Phase 1 只做**資產身分**：主機名稱、OS、網卡 MAC、序號／型號／廠牌，以及最後盤點時間。
軟體清單、跨系統關聯（LibreNMS 位置、Wazuh 覆蓋、CVE）是後續階段。

2026-09-18 用官方映像檔 2.10／2.11 實機探測，因此有幾件事跟直覺不同（動這裡前先讀完）：

1. **比對只用 MAC，而且只比不建、多筆視為不明確不猜**（比照 Proxmox 的 guest 比對）。
   OCS 是資產系統不是掃描器 —— 它的 IP 只是「機器上次盤點時自己看到的」，拿來建 IP 記錄
   一定會髒；重疊網段（不同客戶都有 192.168.1.10）與複製 VM（同 MAC）也靠「多筆→不猜」擋掉。
2. **增量是版本能力**：sync 時實際探一次 `/computers/lastupdate` —— 200 就走增量（2.11），
   404 就全量分頁（2.10）。這樣 2.10→2.11 升級後不必重新診斷就會自動啟用增量。
   lastupdate 是 `LASTDATE > FROM_UNIXTIME(epoch)`（**嚴格大於**、參數是 epoch），
   所以游標要往前退一個重疊窗，免得邊界那一秒的異動被漏掉。
3. **`/computer/:id` 會把軟體送兩份**（`software` 與空字串 key 各一份）—— 別重複計。
4. **OCS 帳密選用**：REST 預設無驗證。診斷會主動測「沒帶憑證連不連得上」→ 連得上要警告。
5. **過期的 OCS 主機名稱不可壓過新鮮的掃描結果**：超過 `stale_after_days` 沒盤點的機器，
   `apply_observation` 仍會記（多源保存），但我們**不 stamp last_seen_ocs 為現在**，
   而且不覆寫已對到的 Device 序號（避免用一年前的資料蓋掉手填的正確值）。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import safe_client, safe_request, transport_detail
from app.core.security import decrypt_secret, encrypt_secret
from app.core.ui_error import UiError
from app.models.address import IPAddress
from app.models.device import Device
from app.models.ocs import OcsServer
from app.services import hostname as hostname_svc
from app.services.arp_precedence import normalize_mac

_PAGE = 200                 # /computers 分頁大小
_INCREMENTAL_OVERLAP = 900  # 增量游標往前退的重疊窗（秒）
_DIAG_TIMEOUT = 15.0
_SYNC_TIMEOUT = 60.0
_MAX_PAGES = 2000           # 安全上限：200×2000 = 40 萬台，遠超任何實際規模


class OcsError(UiError):
    """OCS 整合的錯誤，訊息會顯示給使用者。"""


# ─────────────────── 純函式（好測、不碰網路） ───────────────────

def parse_version(text: str | None) -> tuple[int, ...]:
    """把 "2.11.0" 這種字串解析成 (2, 11, 0)。認不得回 ()。"""
    if not text:
        return ()
    nums: list[int] = []
    for part in str(text).split("."):
        digits = "".join(c for c in part if c.isdigit())
        if not digits:
            break
        nums.append(int(digits))
    return tuple(nums)


def usable_nics(networks: Any) -> list[dict[str, Any]]:
    """從 OCS 的 networks 區段挑出「值得拿來比對」的實體網卡。

    丟掉的：虛擬介面（VIRTUALDEV=1，VPN／docker0／vSwitch）、全零 MAC、空 MAC。
    不過濾的話會把一堆假 MAC 灌進來 —— 實測筆電就有一張 VPN 介面掛著 00:00:00:00:00:00。
    """
    out: list[dict[str, Any]] = []
    for nic in networks or []:
        if not isinstance(nic, dict):
            continue
        if str(nic.get("VIRTUALDEV") or "0") in ("1", "true", "True"):
            continue
        mac = normalize_mac(nic.get("MACADDR"))
        if not mac or mac == "000000000000":
            continue
        out.append(nic)
    return out


# 修復亂碼。OCS 代理不論機器是哪國語系，回報給伺服器的都是 **UTF-8**；亂碼的成因是 OCS 的
# 資料庫不是 UTF-8（latin1／cp1252／SQL_ASCII 很常見）：UTF-8 位元組被當 8-bit 讀進去、再以
# UTF-8 端出來（雙重編碼）。所以還原是單一且無歧義的：把「解錯的字串」編回資料庫實際存的
# 位元組（latin-1 或 cp1252），再以 UTF-8 解讀 —— 繁中／簡中／日文／韓文一律適用（因為源頭
# 都是 UTF-8）。刻意不猜 Big5／GBK／Shift-JIS 這類 8-bit 母語編碼：同一串位元組在它們之間
# 合法但解出不同字，短字串連統計偵測都不可靠，硬猜只會製造另一種亂碼。
_RECODE_FROM = ("latin-1", "cp1252")
_RECODE_TO = ("utf-8",)


def _score_text(s: str) -> int:
    """越多可列印／CJK、越少替換字元與控制碼 → 分數越高。用來在候選解碼間挑最合理的。"""
    score = 0
    for ch in s:
        o = ord(ch)
        if ch == "�":                    # 替換字元＝解錯了
            score -= 5
        elif o < 0x20 and ch not in "\t\n\r":  # 控制碼
            score -= 3
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
            score += 2                         # CJK 統一漢字（含相容區）
        elif 0x3000 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7A3:
            score += 2                         # 日文假名／韓文
        elif o < 0x80:
            score += 1                         # ASCII
        elif 0x80 <= o <= 0xFF:
            score -= 1                         # latin-1 補充區：多半是 mojibake 的殘渣
    return score


def _repair_text(s: str | None) -> str | None:
    """盡力把任何編碼造成的亂碼還原成正常文字（見上方 _RECODE_* 說明）。

    正確存的中文（字元 > U+00FF）在 encode(latin-1) 會直接丟例外而原封不動；純 ASCII 重解
    後與原字串相同、分數不會更高，也不會被改。只有「重解後明顯更像正常文字」才採用。
    """
    if not s:
        return s
    best, best_score = s, _score_text(s)
    for enc_from in _RECODE_FROM:
        try:
            raw = s.encode(enc_from)
        except UnicodeEncodeError:
            continue
        for enc_to in _RECODE_TO:
            try:
                cand = raw.decode(enc_to)
            except (UnicodeDecodeError, LookupError):
                continue
            sc = _score_text(cand)
            if cand != s and sc > best_score:
                best, best_score = cand, sc
    return best


def hostname_of(hardware: dict[str, Any]) -> str | None:
    """OCS 的電腦名稱。空字串視為沒有。"""
    name = (hardware.get("NAME") or "").strip()
    return _repair_text(name) or None


def parse_os(hardware: dict[str, Any]) -> tuple[str | None, str | None]:
    """回 (os_guess 顯示字串, os_family 供前端配 icon)。

    OSCOMMENTS 是最好讀的（"Windows 10 Pro"／"Ubuntu 24.04.1 LTS"），OSNAME 是家族
    （"Windows"／"Linux"）。os_family 用 OSNAME 正規化。
    """
    comments = (hardware.get("OSCOMMENTS") or "").strip()
    osname = (hardware.get("OSNAME") or "").strip()
    osver = (hardware.get("OSVERSION") or "").strip()
    guess = _repair_text(comments or (f"{osname} {osver}".strip() if osname else None) or None)
    family = None
    low = osname.lower()
    if "windows" in low:
        family = "windows"
    elif "linux" in low or "ubuntu" in low or "debian" in low or "centos" in low:
        family = "linux"
    elif "mac" in low or "darwin" in low:
        family = "macos"
    elif osname:
        family = "other"
    return guess, family


# 主機板沒燒 DMI 時的佔位字串（拿來當序號會製造假資產）。有時真值前面還黏著佔位前綴
# （實機："To Be Filled By O.E.M. X570D4I-2T"）→ 去前綴、剩真值才留；整串就是佔位則視為沒有。
_DMI_JUNK = frozenset({
    "system manufacturer", "system product name", "to be filled by o.e.m.",
    "default string", "not specified", "not available", "none", "o.e.m.",
    "system serial number", "0", "n/a",
})
_DMI_JUNK_PREFIXES = ("to be filled by o.e.m.", "default string", "system manufacturer",
                      "system product name")


def _strip_dmi_placeholder(s: str | None) -> str | None:
    """去掉 DMI 佔位字串／前綴，回真值或 None。"""
    s = (s or "").strip()
    low = s.lower()
    for pre in _DMI_JUNK_PREFIXES:
        if low.startswith(pre):
            s = s[len(pre):].strip(" .-")
            low = s.lower()
    return None if (not s or low in _DMI_JUNK) else s


def _is_dmi_placeholder(s: str | None) -> bool:
    """既有 Device 欄位是不是佔位垃圾（給同步時判斷該不該覆寫）。"""
    return bool(s) and _strip_dmi_placeholder(s) != (s or "").strip()


def bios_asset(bios: Any) -> dict[str, str | None]:
    """從 bios 區段抽出 vendor / model / serial（去佔位字串、修亂碼）。

    bios 在清單回應裡是 list（0 或 1 筆），在 /computer/:id 也是 list。空的回全 None。
    """
    row = bios[0] if isinstance(bios, list) and bios else (bios if isinstance(bios, dict) else {})
    def clean(v: Any) -> str | None:
        return _strip_dmi_placeholder(_repair_text((str(v or "")).strip()))
    return {
        "vendor": clean(row.get("SMANUFACTURER")),
        "model": clean(row.get("SMODEL")),
        "serial": clean(row.get("SSN")),
    }


def ocs_tag_of(computer: dict[str, Any]) -> str | None:
    """資產標籤：OCS 放在 accountinfo（[{HARDWARE_ID, TAG}]）。空／預設值視為沒有。"""
    ai = computer.get("accountinfo")
    row = ai[0] if isinstance(ai, list) and ai else (ai if isinstance(ai, dict) else {})
    tag = _repair_text(str(row.get("TAG") or "").strip())
    if not tag or tag.lower() in {"na", "n/a", "none", "0"}:
        return None
    return tag[:128]


def ocs_agent_of(hardware: dict[str, Any]) -> str | None:
    """OCS 代理版本（hardware.USERAGENT，如 OCS-NG_unified_unix_agent_v2.10.0）。"""
    ua = str(hardware.get("USERAGENT") or "").strip()
    return ua[:128] or None


def ocs_notes_of(computer: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """最新幾筆備註（itmgmt_comments）。依 ID 由大到小取前 limit 筆；內容修復亂碼。"""
    rows = computer.get("itmgmt_comments")
    if not isinstance(rows, list):
        return []
    def key(r: Any) -> int:
        try:
            return int(r.get("ID") or 0)
        except (ValueError, TypeError):
            return 0
    out: list[dict[str, Any]] = []
    for r in sorted((r for r in rows if isinstance(r, dict)), key=key, reverse=True)[:limit]:
        out.append({
            "date": str(r.get("DATE_INSERT") or "").strip() or None,
            "user": str(r.get("USER_INSERT") or "").strip() or None,
            "comment": _repair_text(str(r.get("COMMENTS") or "").strip()) or None,
            "action": str(r.get("ACTION") or "").strip() or None,
        })
    return out


def lastdate_of(hardware: dict[str, Any]) -> datetime | None:
    """解析 LASTDATE（OCS 回 "2026-09-18 08:00:00" 的無時區字串，視為 UTC）。"""
    raw = (hardware.get("LASTDATE") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def is_stale(last: datetime | None, *, stale_after_days: int, now: datetime | None = None) -> bool:
    """這台機器的盤點是不是太舊，舊到不該拿它的資料去壓過新鮮來源。"""
    if last is None:
        return True
    now = now or datetime.now(UTC)
    return (now - last) > timedelta(days=max(0, stale_after_days))


def dedup_sections(computer: dict[str, Any]) -> dict[str, Any]:
    """/computer/:id 會把軟體同時放在 `software` 與空字串 key。統一只留具名區段。"""
    return {k: v for k, v in computer.items() if k != ""}


def decide_match(mac: str, ip_ids_by_mac: dict[str, list[uuid.UUID]]) -> uuid.UUID | None:
    """一張網卡的 MAC 對到哪個既有 IP。

    **只比不建**：查不到 → None（不新建 IP）。**多筆→不猜**：同一 MAC 對到多個 IP
    （複製 VM、重疊網段）→ None，視為不明確。只有唯一一筆才回那個 IP id。
    """
    ids = ip_ids_by_mac.get(normalize_mac(mac)) or []
    return ids[0] if len(ids) == 1 else None


# ─────────────────── REST 客戶端 ───────────────────

def _aad(server_id: uuid.UUID) -> bytes:
    """密碼加密的 AAD，格式與其他整合一致（系統匯出/匯入的 secrets registry 要對得上）。"""
    return f"ocs_server:{server_id}:api_password".encode()


def _auth(server: OcsServer) -> tuple[str, str] | None:
    if not server.api_username:
        return None
    if server.api_password_enc is None or server.api_password_nonce is None:
        return (server.api_username, "")
    pw = decrypt_secret(server.api_password_enc, server.api_password_nonce,
                        aad=_aad(server.id)).decode()
    return (server.api_username, pw)


def _auth_header(server: OcsServer) -> dict[str, str]:
    """OCS REST 的 Basic Auth 標頭。沒設帳密就回空 dict（OCS 預設無驗證）。

    `safe_request` 沒有 `auth=` 參數，Basic Auth 一律走 Authorization 標頭（比照其他整合）。
    """
    creds = _auth(server)
    if creds is None:
        return {}
    raw = f"{creds[0]}:{creds[1]}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _base(server: OcsServer) -> str:
    if not server.base_url:
        raise OcsError("這套 OCS 沒有設定網址", code="ocs_no_url")
    return server.base_url.rstrip("/") + "/ocsapi/v1"


async def _get_json(client: httpx.AsyncClient, url: str,
                    headers: dict[str, str], verify: bool) -> Any:
    resp = await safe_request("GET", url, client=client, headers={**headers, "Accept": "application/json"},
                              verify=verify)
    resp.raise_for_status()
    return _decode_json(resp.content)


def _decode_json(raw: bytes) -> Any:
    """把回應位元組解成 JSON，不倚賴 content-type 的 charset 宣告。

    OCS 站常把非 UTF-8 的中文標成 charset=UTF-8；若真的不是合法 UTF-8，直接 .json() 會炸或
    塞替換字元而掉資料。JSON 骨架本身一定是 ASCII，所以先試合法 UTF-8/16/32（json.loads 會
    自動偵測），失敗就退回 latin-1 保留原始位元組（骨架照樣可解），字串值裡的亂碼交給
    _repair_text 還原。真的不是 JSON 才拋，並帶上底層例外原文（見 FortiGate 那次教訓）。
    """
    try:
        return json.loads(raw)
    except UnicodeDecodeError:
        try:
            return json.loads(raw.decode("latin-1"))
        except json.JSONDecodeError as exc:
            raise OcsError(f"OCS 回應不是有效的 JSON：{exc}", code="ocs_bad_json") from exc
    except json.JSONDecodeError as exc:
        raise OcsError(f"OCS 回應不是有效的 JSON：{exc}", code="ocs_bad_json") from exc


# ─────────────────── 連線診斷 ───────────────────

async def diagnose(server: OcsServer) -> dict[str, Any]:
    """測連線：能不能連、要不要驗證、版本能不能增量、抓得到幾台。

    ⚠️ **主動測「沒帶憑證連不連得上」** —— OCS REST 預設無驗證，連得上就要提醒站台這是
    無認證對外開放。這是這個整合特有的一條診斷。
    """
    base = _base(server)
    hdr = _auth_header(server)
    out: dict[str, Any] = {"base_url": server.base_url, "source_type": server.source_type}
    async with safe_client(timeout=_DIAG_TIMEOUT, verify=server.verify_tls) as client:
        # 1) 基本可達性 + 版本能力（lastupdate 在不在）
        try:
            listid = await _get_json(client, f"{base}/computers/listID", hdr, server.verify_tls)
            out["reachable"] = True
            out["computer_count"] = len(listid) if isinstance(listid, list) else None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise OcsError("OCS 需要驗證，但帳密不正確或未提供",
                               code="ocs_auth_required", reason=f"HTTP {code}") from exc
            raise OcsError("連得上網址，但 /ocsapi 沒有正確回應（REST API 可能沒開）",
                           code="ocs_rest_unavailable", reason=f"HTTP {code}") from exc
        except Exception as exc:
            raise OcsError("連不到這套 OCS", code="ocs_unreachable",
                           reason=transport_detail(exc)) from exc

        # 2) 增量能力
        try:
            r = await safe_request("GET", f"{base}/computers/lastupdate", client=client,
                                   headers=hdr, verify=server.verify_tls)
            out["incremental"] = r.status_code == 200
        except Exception:
            out["incremental"] = False

        # 3) 無認證檢查：故意不帶憑證再打一次
        no_auth_ok = False
        try:
            r = await safe_request("GET", f"{base}/computers/listID", client=client,
                                   headers={"Accept": "application/json"}, verify=server.verify_tls)
            no_auth_ok = r.status_code == 200
        except Exception:
            no_auth_ok = False
        out["auth_required"] = not no_auth_ok
        out["unauthenticated_access"] = no_auth_ok  # True = 這套 OCS 沒帶憑證也讀得到 → 警告

    return out


# ─────────────────── 整批同步 ───────────────────

async def _incremental_ids(client, base, hdr, verify, since_epoch: int) -> list[int]:
    data = await _get_json(client, f"{base}/computers/lastupdate/{since_epoch}", hdr, verify)
    return [int(r["ID"]) for r in data if isinstance(r, dict) and "ID" in r]


async def _fetch_computer(client, base, hdr, verify, cid: int) -> dict[str, Any] | None:
    data = await _get_json(client, f"{base}/computer/{cid}", hdr, verify)
    if not isinstance(data, dict):
        return None
    body = data.get(str(cid)) or next(iter(data.values()), None)
    return dedup_sections(body) if isinstance(body, dict) else None


async def _iter_full(client, base, hdr, verify):
    """分頁抓完所有電腦。務必抓到空頁為止 —— 只拿第一頁是本專案踩過的坑。"""
    for page in range(_MAX_PAGES):
        data = await _get_json(
            client, f"{base}/computers?limit={_PAGE}&start={page * _PAGE}", hdr, verify)
        if not isinstance(data, dict) or not data:
            return
        for cid, body in data.items():
            if isinstance(body, dict):
                yield int(cid) if str(cid).isdigit() else cid, dedup_sections(body)
        if len(data) < _PAGE:
            return


async def _apply_computer(
    session: AsyncSession, server: OcsServer, computer: dict[str, Any],
    ip_ids_by_mac: dict[str, list[uuid.UUID]], now: datetime,
    ocs_id: int | None = None,
) -> dict[str, int]:
    """把一台 OCS 電腦的資料落到對到的既有 IP（只比不建）。回傳這台命中的計數。"""
    hw = computer.get("hardware") or {}
    hn = hostname_of(hw)
    os_guess = parse_os(hw)[0]
    last = lastdate_of(hw)
    stale = is_stale(last, stale_after_days=server.stale_after_days, now=now)
    asset = bios_asset(computer.get("bios")) if server.sync_bios else {}
    tag = ocs_tag_of(computer)
    agent = ocs_agent_of(hw)
    notes = ocs_notes_of(computer)
    counts = {"matched": 0}

    for nic in usable_nics(computer.get("networks")):
        mac = nic.get("MACADDR")
        ip_id = decide_match(mac, ip_ids_by_mac)
        if ip_id is None:
            continue
        ip = await session.get(IPAddress, ip_id)
        if ip is None:
            continue
        counts["matched"] += 1

        # 記下 OCS 的 systemid／標籤／代理版本／備註，供裝置明細卡片顯示與深連結。
        # 這些是「目前狀態」的識別/描述資訊，非優先序來源，過期與否都更新。
        if isinstance(ocs_id, int):
            ip.ocs_id = ocs_id
        ip.ocs_tag = tag
        ip.ocs_agent = agent
        ip.ocs_notes = notes or None

        # 主機名稱：過期的也記（多源保存），但由優先序決定要不要當有效值。
        if hn:
            await hostname_svc.apply_observation(
                session, ip=ip, source="ocs", hostname=hn, tiebreak_min=True)
        # OS：寫進 OCS 專屬欄位（不污染掃描代理的 os_guess）；有效值由 os_precedence
        # 決定（ocs 排在 scanner 之上，agent 回報的 OS 蓋過 nmap 指紋猜測）。過期不寫。
        if os_guess and not stale:
            ip.os_ocs = os_guess[:160]
        # MAC 不用寫：我們是**用這張網卡的 MAC 比對到這個 IP 的**，兩邊已定義相等。
        # 盤點時間：只有不算過期時才 stamp 成這次盤點的時間
        if last and not stale:
            ip.last_seen_ocs = last

        # 序號／型號／廠牌落到對到的 Device —— 只補空值，不覆寫（OCS 非權威、可能過期）
        if asset and ip.device_id and not stale:
            dev = await session.get(Device, ip.device_id)
            if dev is not None:
                # 空值或先前存進去的 DMI 佔位垃圾（舊版同步留下的 "To Be Filled By O.E.M. …"）
                # → 換成 OCS 的乾淨值；OCS 也沒有乾淨值時就清掉垃圾（設 None）。使用者手填的
                # 真值一律不動。
                def _pick(cur: str | None, new: str | None) -> str | None:
                    return new if (not cur or _is_dmi_placeholder(cur)) else cur
                dev.serial = _pick(dev.serial, asset.get("serial"))
                dev.model = _pick(dev.model, asset.get("model"))
                dev.vendor = _pick(dev.vendor, asset.get("vendor"))
    return counts


async def sync_instance(session: AsyncSession, server: OcsServer) -> dict[str, Any]:
    """同步一套 OCS。全量或增量由版本能力決定。不 commit（由呼叫端負責）。"""
    if server.source_type != "rest":
        raise OcsError("目前只支援 REST 來源（DB 直讀是第二階段）", code="ocs_db_not_impl")
    base = _base(server)
    hdr = _auth_header(server)
    now = datetime.now(UTC)
    t0 = time.monotonic()

    # 建 MAC → 既有 IP id 的索引（一次撈，不逐台查 DB）
    ip_ids_by_mac: dict[str, list[uuid.UUID]] = {}
    rows = (await session.execute(
        select(IPAddress.id, IPAddress.mac).where(IPAddress.mac.isnot(None))
    )).all()
    for ip_id, mac in rows:
        ip_ids_by_mac.setdefault(normalize_mac(mac), []).append(ip_id)

    seen = matched = 0
    async with safe_client(timeout=_SYNC_TIMEOUT, verify=server.verify_tls) as client:
        # 增量能力：實際探一次，不信任儲存的版本（升級後自動啟用）
        incremental = False
        try:
            r = await safe_request("GET", f"{base}/computers/lastupdate", client=client,
                                   headers=hdr, verify=server.verify_tls)
            incremental = r.status_code == 200
        except Exception:
            incremental = False
        # 順便記下偵測到的能力（清單/版本頁顯示）；用能力字串，不假裝知道確切小版號
        server.detected_version = "2.11+" if incremental else "2.10"

        use_incremental = incremental and server.last_incremental_epoch is not None
        mode = "incremental" if use_incremental else "full"

        if use_incremental:
            since = max(0, int(server.last_incremental_epoch) - _INCREMENTAL_OVERLAP)
            ids = await _incremental_ids(client, base, hdr, server.verify_tls, since)
            for cid in ids:
                comp = await _fetch_computer(client, base, hdr, server.verify_tls, cid)
                if comp is None:
                    continue
                seen += 1
                matched += (await _apply_computer(
                    session, server, comp, ip_ids_by_mac, now,
                    ocs_id=cid if isinstance(cid, int) else None))["matched"]
        else:
            async for _cid, comp in _iter_full(client, base, hdr, server.verify_tls):
                seen += 1
                matched += (await _apply_computer(
                    session, server, comp, ip_ids_by_mac, now,
                    ocs_id=_cid if isinstance(_cid, int) else None))["matched"]

    # 增量游標推進到這次同步的當下（epoch）。因為 lastupdate 是嚴格大於、下次會退重疊窗。
    if incremental:
        server.last_incremental_epoch = int(now.timestamp())
    server.last_sync_at = now
    server.last_success_at = now
    server.last_error = None
    server.last_cost = {
        "mode": mode, "computers": seen, "matched_ips": matched,
        "seconds": round(time.monotonic() - t0, 2),
    }
    return server.last_cost


# ─────────────────── 憑證 helper（給 API 存密碼用） ───────────────────

def set_api_password(server: OcsServer, password: str | None) -> None:
    """把 REST 密碼寫進實例（AES-GCM，AAD 綁 id）。None/空 → 清掉。"""
    if not password:
        server.api_password_enc = None
        server.api_password_nonce = None
        return
    enc, nonce = encrypt_secret(password, aad=_aad(server.id))
    server.api_password_enc = enc
    server.api_password_nonce = nonce
