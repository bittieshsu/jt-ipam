"""ISOinsight 租約回應的解析與正規化（純函式：不做網路、不碰資料庫）。

資料契約（規格 §7，依客戶提供的截圖欄位）：

    {"dhcp_lease": [{"ip": "...", "mac": "...", "name": "...",
                     "start_time": "YYYY/MM/DD HH:mm:ss", "end_time": "YYYY/MM/DD HH:mm:ss"}]}

⚠️ 待真機驗證：時間格式與所屬時區、是否全量、有無分頁、是否含過期記錄、IPv6。這裡只認上面這個形狀，
其餘一律當錯誤或品質問題，**不猜**：
- 頂層不是物件、沒有 `dhcp_lease` 陣列、HTML、壞 JSON、204 → INVALID_RESPONSE（不等於空清單）
- 只有 `{"dhcp_lease": []}` 是合法的空清單（但仍不能據此判定來源是完整快照）
- 回應裡看得出「還沒取完」的分頁資訊 → INCOMPLETE_RESPONSE，不把第一頁當全量
- 超過筆數上限 → RESPONSE_LIMIT_EXCEEDED，不截斷當成功
- 時間無法解析（含「永久」之類的字串）→ 未知，不自行解讀成永久

租約狀態是「依時間推定」，不是來源的 binding state，也不代表設備上線。
"""
from __future__ import annotations

import codecs
import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from typing import Any
from zoneinfo import ZoneInfo

from app.services.isoinsight.errors import IsoError

TIME_FORMAT = "%Y/%m/%d %H:%M:%S"
_TIME_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

#: 讓本次結果至少為「部分成功」的品質標籤（其餘標籤只是資訊）
PROBLEM_TAGS = frozenset({"mac_empty", "mac_invalid", "time_unknown", "not_started", "invalid_period",
                          "mac_conflict"})
#: 時間不可信、不能推定占用狀態的租約狀態
TIME_PROBLEM_STATES = frozenset({"not_started", "invalid_period", "unknown"})

#: 頂層看得出總筆數的鍵（回應筆數比它少＝沒取完）
_TOTAL_KEYS = ("total", "total_count", "totalCount", "count", "recordsTotal", "record_count")
_MORE_KEYS = ("has_more", "hasMore", "more")
_NEXT_KEYS = ("next", "next_page", "nextPage", "next_url", "nextUrl")
_PAGE_PAIRS = (("page", "total_pages"), ("page", "pages"), ("page", "totalPages"),
               ("current_page", "last_page"))


@dataclass(slots=True, eq=False)
class Lease:
    ip: str
    version: int
    mac: str | None
    mac_key: str
    name: str | None
    start: datetime | None
    end: datetime | None
    start_raw: str | None
    end_raw: str | None
    state: str
    quality: set[str] = field(default_factory=set)
    raw_count: int = 1


@dataclass(slots=True)
class Parsed:
    leases: list[Lease]
    fetched: int
    invalid: int
    duplicates: int
    warnings: list[str]
    invalid_samples: list[dict[str, Any]]

    def quality_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for le in self.leases:
            for tag in le.quality:
                out[tag] = out.get(tag, 0) + 1
        return out

    def has_problems(self) -> bool:
        return self.invalid > 0 or any(le.quality & PROBLEM_TAGS for le in self.leases)


@dataclass(slots=True)
class Selection:
    """同一個 IP 的候選：`current` 是要套用到 IP 記錄的那一筆；衝突時為 None。"""
    current: Lease | None
    conflict: bool
    superseded: list[Lease]


# ── 欄位 ─────────────────────────────────────────────────────────────────────

def parse_ip(raw: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(raw, str):
        return None
    try:
        return ipaddress.ip_address(raw.strip())
    except ValueError:
        return None


def norm_mac(raw: Any) -> tuple[str | None, str | None]:
    """(正規化 MAC, 問題標籤)。空白＝mac_empty；格式不對、全 0、廣播＝mac_invalid。"""
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None, "mac_empty"
    hexs = "".join(ch for ch in s.lower() if ch in "0123456789abcdef")
    stripped = re.sub(r"[\s:\-.]", "", s.lower())
    if len(hexs) != 12 or stripped != hexs or hexs in ("000000000000", "ffffffffffff"):
        return None, "mac_invalid"
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2)), None


def clean_name(raw: Any) -> str | None:
    """來源主機名稱：攻擊者可控的文字，只當資料。去掉控制字元（PostgreSQL 也不收 NUL）、截短。"""
    if raw is None or isinstance(raw, dict | list):
        return None
    s = _CTRL.sub("", str(raw)).strip()
    return s[:255] or None


def parse_time(raw: Any, tz: ZoneInfo) -> datetime | None:
    """`YYYY/MM/DD HH:mm:ss`（來源時區）→ UTC。其他格式、數字、空值 → None。"""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not _TIME_RE.match(s):
        return None
    try:
        local = datetime.strptime(s, TIME_FORMAT)
    except ValueError:
        return None
    return local.replace(tzinfo=tz).astimezone(UTC)


def lease_state(start: datetime | None, end: datetime | None, now: datetime) -> str:
    """依時間推定的租約狀態（規格 §8）：active／expired／not_started／invalid_period／unknown。"""
    if start is not None and end is not None and end < start:
        return "invalid_period"
    if end is not None and end <= now:
        return "expired"
    if start is not None and start > now:
        return "not_started"
    if start is not None and end is not None and start <= now < end:
        return "active"
    return "unknown"


def _raw_text(raw: Any) -> str | None:
    if raw is None:
        return None
    s = _CTRL.sub("", str(raw)).strip()
    return s[:40] or None


# ── 回應 ─────────────────────────────────────────────────────────────────────

def _media_type(content_type: str | None) -> tuple[str, str | None]:
    ct = (content_type or "").lower()
    media = ct.split(";", 1)[0].strip()
    m = re.search(r"charset\s*=\s*\"?([a-z0-9_\-]+)", ct)
    return media, (m.group(1) if m else None)


def decode_json(body: bytes, content_type: str | None, *, stage: str = "validate") -> tuple[Any, list[str]]:
    """回應本文 → JSON。HTML 一律拒絕；Content-Type 不對但內容是 JSON → 相容性警告。"""
    media, charset = _media_type(content_type)
    warnings: list[str] = []
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if media in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype", b"<html", b"<")):
        raise IsoError("INVALID_RESPONSE", "the response is an HTML page, not lease JSON",
                       stage=stage, kind="html")
    enc = "utf-8-sig"
    if charset:
        try:
            codecs.lookup(charset)
            enc = charset if charset.replace("-", "").replace("_", "") != "utf8" else "utf-8-sig"
        except LookupError:
            warnings.append("unknown_charset")
    try:
        text = body.decode(enc)
    except UnicodeDecodeError as exc:
        raise IsoError("INVALID_RESPONSE", f"the response is not valid {enc} text ({exc.reason})",
                       stage=stage, kind="encoding") from exc
    if not text.strip():
        raise IsoError("INVALID_RESPONSE", "the response body is empty", stage=stage, kind="empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # 只帶解析錯誤的位置，不帶本文（本文是租約資料，留在記錄裡沒有必要）
        raise IsoError("INVALID_RESPONSE", f"not JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
                       stage=stage, kind="json") from exc
    if media not in ("application/json", "text/json") and not media.endswith("+json"):
        warnings.append("content_type_mismatch")
    return data, warnings


def _incomplete(top: dict[str, Any], n: int) -> str | None:
    """看得出來還沒取完的分頁資訊 → 原因；看不出來 → None。只看頂層、只認明確的值。"""
    for k in _TOTAL_KEYS:
        v = top.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > n:
            return f"{k}={v} but {n} rows returned"
    for k in _MORE_KEYS:
        if top.get(k) is True:
            return f"{k}=true"
    for k in _NEXT_KEYS:
        v = top.get(k)
        if isinstance(v, str) and v.strip():
            return f"{k} is set"
    for page_k, last_k in _PAGE_PAIRS:
        p, last = top.get(page_k), top.get(last_k)
        if (isinstance(p, int) and isinstance(last, int) and not isinstance(p, bool)
                and not isinstance(last, bool) and p < last):
            return f"{page_k}={p} of {last}"
    return None


def parse_leases(status: int, content_type: str | None, body: bytes, *, tz: str, now: datetime,
                 max_rows: int) -> Parsed:
    if status == 204:
        raise IsoError("INVALID_RESPONSE", "HTTP 204 is not a valid lease response", stage="validate",
                       http_status=204, kind="no_content")
    data, warnings = decode_json(body, content_type)
    if not isinstance(data, dict):
        raise IsoError("INVALID_RESPONSE", "the top level is not a JSON object", stage="validate",
                       kind="shape")
    rows = data.get("dhcp_lease")
    if not isinstance(rows, list):
        raise IsoError("INVALID_RESPONSE", "dhcp_lease is missing or is not an array", stage="validate",
                       kind="shape")
    if len(rows) > max_rows:
        raise IsoError("RESPONSE_LIMIT_EXCEEDED", f"{len(rows)} rows exceed the limit of {max_rows}",
                       stage="validate", rows=len(rows), max=max_rows)
    why = _incomplete(data, len(rows))
    if why:
        raise IsoError("INCOMPLETE_RESPONSE", f"the response looks paginated ({why})", stage="validate",
                       reason=why)

    zone = ZoneInfo(tz)
    invalid = 0
    samples: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], list[Lease]] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            invalid += 1
            if len(samples) < 20:
                samples.append({"index": i, "reason": "not_object"})
            continue
        addr = parse_ip(row.get("ip"))
        if addr is None:
            invalid += 1
            if len(samples) < 20:
                samples.append({"index": i, "reason": "ip_invalid"})
            continue
        quality: set[str] = set()
        mac, mac_problem = norm_mac(row.get("mac"))
        if mac_problem:
            quality.add(mac_problem)
        name = clean_name(row.get("name"))
        if name is None:
            quality.add("name_empty")
        start = parse_time(row.get("start_time"), zone)
        end = parse_time(row.get("end_time"), zone)
        if start is None or end is None:
            quality.add("time_unknown")
        state = lease_state(start, end, now)
        if state in ("not_started", "invalid_period"):
            quality.add(state)
        if addr.version == 6:
            quality.add("ipv6_unverified")
        le = Lease(ip=addr.compressed, version=addr.version, mac=mac,
                   mac_key=mac.replace(":", "") if mac else "", name=name, start=start, end=end,
                   start_raw=None if start else _raw_text(row.get("start_time")),
                   end_raw=None if end else _raw_text(row.get("end_time")),
                   state=state, quality=quality)
        by_key.setdefault((le.ip, le.mac_key), []).append(le)

    leases: list[Lease] = []
    duplicates = 0
    for group in by_key.values():
        if len(group) == 1:
            leases.append(group[0])
            continue
        # 同 IP 同 MAC 多筆：取最新的有效開始時間、再看結束時間；原始筆數留著
        best = max(group, key=lambda x: (x.start or datetime.min.replace(tzinfo=UTC),
                                         x.end or datetime.min.replace(tzinfo=UTC)))
        best.raw_count = len(group)
        best.quality.add("duplicate")
        duplicates += len(group) - 1
        leases.append(best)
    _flag_conflicts(leases)
    return Parsed(leases=leases, fetched=len(rows), invalid=invalid, duplicates=duplicates,
                  warnings=warnings, invalid_samples=samples)


def _overlap(a: Lease, b: Lease) -> bool:
    if a.start is None or a.end is None or b.start is None or b.end is None:
        return False
    if a.start >= a.end or b.start >= b.end:
        return False
    return a.start < b.end and b.start < a.end


def _flag_conflicts(leases: list[Lease]) -> None:
    """同 IP、不同的有效 MAC、租期重疊 → 兩筆都標 mac_conflict（沒有 MAC 的不算「不同 MAC」）。"""
    by_ip: dict[str, list[Lease]] = {}
    for le in leases:
        if le.mac_key:
            by_ip.setdefault(le.ip, []).append(le)
    for group in by_ip.values():
        if len(group) < 2:
            continue
        for a, b in combinations(group, 2):
            if a.mac_key != b.mac_key and _overlap(a, b):
                a.quality.add("mac_conflict")
                b.quality.add("mac_conflict")


def pick_current(leases: list[Lease]) -> dict[str, Selection]:
    """逐 IP 決定要套用到 IP 記錄的那一筆（只從租約期間內的挑）。

    - 有任何一筆衝突的租約在期間內 → 不挑（不任意選一個覆蓋 IP）
    - 期間內有帶 MAC 的 → 那一筆（去重後同 IP 同 MAC 只剩一筆；兩個不同 MAC 都在期間內必然重疊＝衝突）
    - 期間內只有沒帶有效 MAC 的 → 開始時間最新的那一筆（MAC 不套用）
    - 其餘期間內的（例如帶 MAC 那筆之外、沒有 MAC 的）→ superseded
    """
    by_ip: dict[str, list[Lease]] = {}
    for le in leases:
        by_ip.setdefault(le.ip, []).append(le)
    out: dict[str, Selection] = {}
    floor = datetime.min.replace(tzinfo=UTC)
    for ip, group in by_ip.items():
        active = [le for le in group if le.state == "active"]
        if any("mac_conflict" in le.quality for le in active):
            out[ip] = Selection(current=None, conflict=True, superseded=[])
            continue
        if not active:
            out[ip] = Selection(current=None, conflict=False, superseded=[])
            continue
        with_mac = [le for le in active if le.mac_key]
        pool = with_mac or active
        cur = max(pool, key=lambda x: (x.start or floor, x.end or floor))
        out[ip] = Selection(current=cur, conflict=False, superseded=[le for le in active if le is not cur])
    return out
