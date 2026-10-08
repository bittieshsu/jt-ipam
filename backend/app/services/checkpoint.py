"""Check Point 管理伺服器整合（第一階段：Management API，唯讀）。使用者 2026-10-08，R81.20（API 1.9）。

Management API：`POST <base>/web_api/<指令>`，JSON；`login` 回 `sid`，之後每個請求帶 `X-chkp-sid`。
- 用 API key（R80.40 起）或帳密登入，一律帶 `read-only: true`：不鎖任何物件
- 每輪登入一次、**結束一定登出**（finally）—— 不登出的 session 會留到逾時，佔滿管理伺服器的連線數
- Multi-Domain 每個網域各自登入（`domain`）
- 清單分頁：`offset`／`limit`（最多 500），以回應的 `to`／`total` 往下翻
- 存取規則：政策套件 → 存取層 → `show-access-rulebase`（段落攤平；`Apply Layer` 的內嵌層也抓）；
  `use-object-dictionary` 讓欄位是 uid、名稱在 `objects-dictionary`，一頁解析一次
- NAT：`show-nat-rulebase`，只收目的地轉換（對外開放），寫進共用的 nat_translations

**不把主機拖慢**：依序抓、每頁之間喘息、每類有上限（超過就停並在摘要標出來，不清舊資料）。
**每個區段各自隔離**：某一段讀不到不影響其他段；讀不到的那一段不清舊資料。

實機驗收待 VM（使用者準備中）；回應形狀照官方 API 文件，測試用 tests/checkpoint_mock.py。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import UnsafeOutboundURL, safe_request, transport_detail
from app.core.security import decrypt_secret, encrypt_secret
from app.core.sqlin import in_values
from app.core.ui_error import UiError

SOURCE = "checkpoint"
PAGE = 500                 # Management API 一頁的上限
PAUSE = 0.05               # 每頁之間喘息（秒）
MAX_OBJECTS = 200_000      # 每一類物件
MAX_RULES = 100_000        # 每個存取層
MAX_INLINE_DEPTH = 4
TIMEOUT = 60.0
MAX_BYTES = 128 * 1024 * 1024

OBJECT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("show-hosts", "host"), ("show-networks", "network"), ("show-address-ranges", "address-range"),
    ("show-groups", "group"), ("show-groups-with-exclusion", "group-with-exclusion"),
)


class CheckPointError(UiError):
    """呼叫 Management API 失敗。訊息只放指令、狀態與伺服器的錯誤碼／訊息，絕不放 API key、密碼或 sid。"""


def _aad(server_id: Any) -> bytes:
    return f"checkpoint_server:{server_id}:secret".encode()


def encrypt_secret_for(server_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(server_id))


def _secret(inst: Any) -> str:
    if not inst.secret_enc or not inst.secret_nonce:
        raise CheckPointError("no API key or password configured", code="cp_no_secret")
    return decrypt_secret(inst.secret_enc, inst.secret_nonce, aad=_aad(inst.id)).decode("utf-8")


def api_base(api_url: str) -> str:
    base = api_url.rstrip("/")
    return base if base.endswith("/web_api") else f"{base}/web_api"


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


class Session:
    """一次登入。`async with Session(inst, domain) as s:`，離開時一定登出。"""

    def __init__(self, inst: Any, domain: str | None = None, *, timeout: float = TIMEOUT) -> None:
        self.inst, self.domain, self.timeout = inst, domain or None, timeout
        self.base = api_base(inst.api_url)
        self.sid: str | None = None
        self.version: str | None = None
        # uid → 名稱：群組成員是 uid 字串時拿來換名稱（閘道、各類物件同步時都會填）
        self.names: dict[str, str] = {}

    async def _post(self, cmd: str, body: dict[str, Any], *, login: bool = False) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.sid:
            headers["X-chkp-sid"] = self.sid
        try:
            resp = await safe_request("POST", f"{self.base}/{cmd}", content=json.dumps(body).encode(), headers=headers,
                                      timeout=self.timeout, verify=self.inst.verify_tls, max_bytes=MAX_BYTES,
                                      follow_redirects=False)
        except UnsafeOutboundURL as exc:
            raise CheckPointError(f"blocked by the outbound connection policy: {exc}", code="cp_ssrf",
                                  command=cmd, reason=str(exc)[:200]) from exc
        except httpx.HTTPError as exc:
            reason = transport_detail(exc)
            raise CheckPointError(f"{cmd}: {reason}", code="cp_transport", command=cmd, reason=reason) from exc
        if resp.is_redirect:
            loc = _strip_query(str(resp.headers.get("location") or ""))
            raise CheckPointError(f"{cmd}: redirected to {loc}", code="cp_redirect", command=cmd, location=loc)
        try:
            data = resp.json()
        except ValueError as exc:
            raise CheckPointError(f"{cmd}: HTTP {resp.status_code}, the reply is not JSON", code="cp_not_json",
                                  command=cmd, status=resp.status_code) from exc
        if resp.status_code != 200:
            err = str(data.get("code") or "") if isinstance(data, dict) else ""
            msg = str(data.get("message") or "")[:300] if isinstance(data, dict) else ""
            if login and (resp.status_code in (400, 401, 403) or "login" in err):
                raise CheckPointError(f"login failed: {err or resp.status_code} {msg}".strip(),
                                      code="cp_login_failed", status=resp.status_code, reason=msg or err)
            if resp.status_code in (401, 403):
                raise CheckPointError(f"{cmd}: {err or resp.status_code} {msg}".strip(), code="cp_denied",
                                      command=cmd, status=resp.status_code, reason=msg or err)
            raise CheckPointError(f"{cmd}: HTTP {resp.status_code} {err} {msg}".strip(), code="cp_api_error",
                                  command=cmd, status=resp.status_code, reason=(msg or err)[:300])
        if not isinstance(data, dict):
            raise CheckPointError(f"{cmd}: unexpected reply", code="cp_not_json", command=cmd, status=resp.status_code)
        return data

    async def __aenter__(self) -> Session:
        # 實機（R81.20）：唯讀登入帶 session-name／session-description 會 400（generic_err_invalid_parameter），
        # 整個整合就登不進去 → 只帶 read-only
        body: dict[str, Any] = {"read-only": True}
        if (self.inst.auth_mode or "api_key") == "password":
            body.update({"user": self.inst.username or "", "password": _secret(self.inst)})
        else:
            body["api-key"] = _secret(self.inst)
        if self.domain:
            body["domain"] = self.domain
        data = await self._post("login", body, login=True)
        self.sid = str(data.get("sid") or "") or None
        if not self.sid:
            raise CheckPointError("login: no session id in the reply", code="cp_login_failed", reason="no sid")
        self.version = str(data.get("api-server-version") or "") or None
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if not self.sid:
            return
        try:
            await self._post("logout", {})
        except CheckPointError:
            pass                    # 登出失敗不蓋掉原本的錯誤；session 會自己逾時
        finally:
            self.sid = None

    async def call(self, cmd: str, **body: Any) -> dict[str, Any]:
        return await self._post(cmd, body)

    async def pages(self, cmd: str, key: str, *, limit: int = MAX_OBJECTS, **body: Any) -> tuple[list[Any], bool]:
        """翻完一個清單。回 (項目, 是否被上限截斷)。"""
        out: list[Any] = []
        offset = 0
        while True:
            page = await self.call(cmd, offset=offset, limit=PAGE, **body)
            items = page.get(key) or []
            out.extend(items)
            to, total = int(page.get("to") or 0), int(page.get("total") or 0)
            if not items or to >= total or to <= offset:
                return out, False
            if len(out) >= limit:
                return out[:limit], True
            offset = to
            await asyncio.sleep(PAUSE)

    async def rulebase(self, cmd: str, ident: dict[str, Any], *, limit: int = MAX_RULES,
                       **extra: Any) -> tuple[list[tuple[str | None, dict[str, Any]]], dict[str, dict[str, Any]], bool]:
        """翻完一個規則庫：回 ([(段落名稱, 規則)], uid → 物件字典, 是否截斷)。段落裡的規則攤平。"""
        rules: list[tuple[str | None, dict[str, Any]]] = []
        dic: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = await self.call(cmd, **ident, offset=offset, limit=PAGE, **{"details-level": "standard",
                                                                              "use-object-dictionary": True}, **extra)
            for o in page.get("objects-dictionary") or []:
                if isinstance(o, dict) and o.get("uid"):
                    dic[str(o["uid"])] = o
            items = page.get("rulebase") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                if str(it.get("type") or "").endswith("-section"):
                    rules.extend((it.get("name"), r) for r in it.get("rulebase") or [] if isinstance(r, dict))
                else:
                    rules.append((None, it))
            to, total = int(page.get("to") or 0), int(page.get("total") or 0)
            if not items or to >= total or to <= offset:
                return rules, dic, False
            if len(rules) >= limit:
                return rules[:limit], dic, True
            offset = to
            await asyncio.sleep(PAUSE)


# ── 解析 ──────────────────────────────────────────────────────────────────────

def _ref_name(v: Any, dic: dict[str, dict[str, Any]]) -> str:
    o = dic.get(v) if isinstance(v, str) else v if isinstance(v, dict) else None
    if not o:
        return str(v or "")
    if o.get("type") == "CpmiAnyObject" or str(o.get("name") or "") == "Any":
        return "any"
    return str(o.get("name") or o.get("uid") or "")


def _names(v: Any, dic: dict[str, dict[str, Any]]) -> str:
    vals = v if isinstance(v, list) else ([v] if v else [])
    return ", ".join(n for n in (_ref_name(x, dic) for x in vals) if n)


def _object_value(o: dict[str, Any]) -> str | None:
    t = o.get("type")
    if t == "host":
        return o.get("ipv4-address") or o.get("ipv6-address") or None
    if t == "network":
        if o.get("subnet4"):
            return f"{o['subnet4']}/{o.get('mask-length4')}"
        if o.get("subnet6"):
            return f"{o['subnet6']}/{o.get('mask-length6')}"
        return None
    if t == "address-range":
        a, b = o.get("ipv4-address-first") or o.get("ipv6-address-first"), o.get("ipv4-address-last") or o.get("ipv6-address-last")
        return f"{a}-{b}" if a and b else None
    if t == "group-with-exclusion":
        inc, exc = o.get("include") or {}, o.get("except") or {}
        return f"{inc.get('name', '')} − {exc.get('name', '')}"
    return None


def _when(v: Any) -> datetime | None:
    s = (v or {}).get("iso-8601") if isinstance(v, dict) else v
    if not s:
        return None
    try:
        s = str(s)
        if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":       # +0800 → +08:00
            s = f"{s[:-2]}:{s[-2:]}"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _ip(v: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(v or "").strip()))
    except ValueError:
        return None


# ── 各區段 ────────────────────────────────────────────────────────────────────

async def sync_gateways(session: AsyncSession, s: Session, inst: Any, domain: str, now: datetime) -> int:
    from app.models.checkpoint import CheckPointGateway
    items, _trunc = await s.pages("show-gateways-and-servers", "objects", **{"details-level": "full"})
    rows = {g.uid: g for g in (await session.execute(select(CheckPointGateway).where(
        CheckPointGateway.server_id == inst.id, CheckPointGateway.domain == domain))).scalars().all()}
    seen: set[str] = set()
    for o in items:
        uid = str(o.get("uid") or "")
        if not uid:
            continue
        seen.add(uid)
        s.names[uid] = str(o.get("name") or uid)
        g = rows.get(uid) or CheckPointGateway(server_id=inst.id, domain=domain, uid=uid, name="")
        g.name = str(o.get("name") or uid)[:255]
        g.gw_type = str(o.get("type") or "")[:64] or None
        g.ipv4_address = _ip(o.get("ipv4-address"))
        g.version = str(o.get("version") or "")[:32] or None
        g.last_sync_at = now
        session.add(g)
    gone = [u for u in rows if u not in seen]
    if gone:
        await session.execute(delete(CheckPointGateway).where(
            CheckPointGateway.server_id == inst.id, CheckPointGateway.domain == domain, in_values(CheckPointGateway.uid, gone)))
    return len(seen)


async def sync_objects(session: AsyncSession, s: Session, inst: Any, domain: str, now: datetime) -> tuple[int, bool]:
    from app.models.checkpoint import CheckPointObject
    fetched: list[tuple[str, dict[str, Any]]] = []
    truncated = False
    for cmd, typ in OBJECT_COMMANDS:
        extra: dict[str, Any] = {"details-level": "full"}
        if cmd == "show-groups":
            # 實機：不加這個時 members 是 uid 字串；加了才是帶名稱的物件
            extra["dereference-group-members"] = True
        items, trunc = await s.pages(cmd, "objects", **extra)
        truncated = truncated or trunc
        fetched.extend((typ, o) for o in items if isinstance(o, dict))
        await asyncio.sleep(PAUSE)
    for _typ, o in fetched:
        if o.get("uid"):
            s.names[str(o["uid"])] = str(o.get("name") or o["uid"])
    rows = {o.uid: o for o in (await session.execute(select(CheckPointObject).where(
        CheckPointObject.server_id == inst.id, CheckPointObject.domain == domain))).scalars().all()}
    seen: set[str] = set()
    for typ, o in fetched:
        uid = str(o.get("uid") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        row = rows.get(uid) or CheckPointObject(server_id=inst.id, domain=domain, uid=uid, name="", obj_type=typ)
        row.name, row.obj_type = str(o.get("name") or uid)[:255], typ
        row.value = _object_value({**o, "type": typ})
        row.members = ([str(m.get("name") or m.get("uid")) if isinstance(m, dict) else s.names.get(str(m), str(m))
                        for m in o.get("members") or []] if typ == "group" else None)
        row.comments = str(o.get("comments") or "") or None
        row.last_sync_at = now
        session.add(row)
    if not truncated:
        gone = [u for u in rows if u not in seen]
        if gone:
            await session.execute(delete(CheckPointObject).where(
                CheckPointObject.server_id == inst.id, CheckPointObject.domain == domain, in_values(CheckPointObject.uid, gone)))
    return len(seen), truncated


async def _packages(s: Session, inst: Any) -> list[dict[str, Any]]:
    items, _t = await s.pages("show-packages", "packages", **{"details-level": "full"})
    wanted = {p.strip() for p in inst.packages or [] if p.strip()}
    return [p for p in items if isinstance(p, dict) and (not wanted or str(p.get("name") or "") in wanted)]


async def sync_policies(session: AsyncSession, s: Session, inst: Any, domain: str, now: datetime,
                        packages: list[dict[str, Any]]) -> tuple[int, bool]:
    from app.models.checkpoint import CheckPointRule
    # (政策套件, 層的顯示名稱, 指令識別, 深度)；同一個層被多個套件共用時只抓一次
    queue: list[tuple[str, str, dict[str, Any], int]] = []
    done: set[str] = set()
    for p in packages:
        if p.get("access") is False:
            continue
        for lay in p.get("access-layers") or []:
            key = str(lay.get("uid") or lay.get("name") or "")
            if key and key not in done:
                done.add(key)
                queue.append((str(p.get("name") or ""), str(lay.get("name") or key), {"name": lay.get("name")}
                              if lay.get("name") else {"uid": key}, 0))
    fetched: list[tuple[str, str, str | None, dict[str, Any], dict[str, dict[str, Any]]]] = []
    truncated = False
    while queue:
        pkg, label, ident, depth = queue.pop(0)
        rules, dic, trunc = await s.rulebase("show-access-rulebase", ident, **{"show-hits": True})
        truncated = truncated or trunc
        for section, r in rules:
            fetched.append((pkg, label, section, r, dic))
            inline = r.get("inline-layer")
            iuid = str(inline.get("uid") if isinstance(inline, dict) else inline or "")
            if iuid and iuid not in done and depth < MAX_INLINE_DEPTH:
                done.add(iuid)
                queue.append((pkg, f"{label} › {_ref_name(inline, dic) or iuid}", {"uid": iuid}, depth + 1))
        await asyncio.sleep(PAUSE)
    rows = {(r.layer, r.uid): r for r in (await session.execute(select(CheckPointRule).where(
        CheckPointRule.server_id == inst.id, CheckPointRule.domain == domain))).scalars().all()}
    seen: set[tuple[str, str]] = set()
    for pkg, label, section, r, dic in fetched:
        uid = str(r.get("uid") or "")
        if not uid or (label[:512], uid) in seen:
            continue
        key = (label[:512], uid)
        seen.add(key)
        row = rows.get(key) or CheckPointRule(server_id=inst.id, domain=domain, layer=label[:512], uid=uid, package="")
        row.package, row.section = pkg[:255], (str(section)[:255] if section else None)
        row.rule_number = int(r["rule-number"]) if str(r.get("rule-number") or "").isdigit() else None
        row.name = str(r.get("name") or "")[:255] or None
        row.action = _ref_name(r.get("action"), dic)[:64] or None
        row.enabled = r.get("enabled") is not False
        row.source, row.destination = _names(r.get("source"), dic), _names(r.get("destination"), dic)
        row.service = _names(r.get("service"), dic)
        row.source_negate, row.destination_negate = bool(r.get("source-negate")), bool(r.get("destination-negate"))
        row.install_on = _names(r.get("install-on"), dic) or None
        row.comments = str(r.get("comments") or "") or None
        hits = r.get("hits") if isinstance(r.get("hits"), dict) else {}
        row.hits = int(hits["value"]) if str(hits.get("value", "")).isdigit() else None
        row.last_hit_at = _when(hits.get("last-date"))
        row.last_sync_at = now
        session.add(row)
    if not truncated:
        gone = [k for k in rows if k not in seen]
        for k in gone:
            await session.delete(rows[k])
    return len(seen), truncated


async def sync_nat(session: AsyncSession, s: Session, inst: Any, domain: str, now: datetime,
                   packages: list[dict[str, Any]], seen: set[str]) -> tuple[int, bool]:
    """目的地轉換（靜態 NAT、port forward）→ nat_translations；hide NAT 是出向，不收。"""
    from app.models.nat import NATTranslation
    from app.services.ip_autocreate import match_existing_many
    origin = f"{SOURCE}:{inst.id}"
    existing = {n.external_id: n for n in (await session.execute(select(NATTranslation).where(
        NATTranslation.source_origin == origin))).scalars().all()}
    picked: list[tuple[str, dict[str, Any], dict[str, dict[str, Any]], str]] = []
    truncated = False
    for p in packages:
        if p.get("nat-policy") is False:
            continue
        pkg = str(p.get("name") or "")
        rules, dic, trunc = await s.rulebase("show-nat-rulebase", {"package": pkg})
        truncated = truncated or trunc
        for _section, r in rules:
            td = r.get("translated-destination")
            if _ref_name(td, dic) in ("", "Original"):
                continue
            picked.append((pkg, r, dic, str(r.get("uid") or "")))
        await asyncio.sleep(PAUSE)

    def obj_ip(v: Any, dic: dict[str, dict[str, Any]]) -> str | None:
        o = dic.get(v) if isinstance(v, str) else v
        return _ip((o or {}).get("ipv4-address") or (o or {}).get("ipv6-address")) if isinstance(o, dict) else None

    # 主機的自動靜態 NAT（實機才發現）：規則的原始目的地與轉換後目的地是同一台主機，對外位址只在
    # 主機的 nat-settings 裡（物件字典是精簡版、沒有它）→ 逐台問一次，同一台只問一次
    nat_public: dict[str, str | None] = {}

    async def public_ip(uid: str) -> str | None:
        if uid not in nat_public:
            try:
                o = (await s.call("show-object", uid=uid, **{"details-level": "full"})).get("object") or {}
            except CheckPointError:
                o = {}
            ns = o.get("nat-settings") if isinstance(o.get("nat-settings"), dict) else {}
            nat_public[uid] = _ip(ns.get("ipv4-address") or ns.get("ipv6-address"))
        return nat_public[uid]

    def same_object(r: dict[str, Any]) -> str | None:
        od, td = r.get("original-destination"), r.get("translated-destination")
        od = od.get("uid") if isinstance(od, dict) else od
        td = td.get("uid") if isinstance(td, dict) else td
        return str(od) if od and od == td else None

    targets = {ip for _p, r, dic, _u in picked if (ip := obj_ip(r.get("translated-destination"), dic))}
    scope = list(inst.scope_subnet_ids) if inst.scope_subnet_ids else None
    matches = await match_existing_many(session, targets, scope) if targets else {}
    n = 0
    for pkg, r, dic, uid in picked:
        if not uid:
            continue
        ext = f"{domain}:{pkg}:{uid}"[:200]
        seen.add(ext)
        row = existing.get(ext) or NATTranslation(source_origin=origin, external_id=ext)
        orig_name, trans_name = _ref_name(r.get("original-destination"), dic), _ref_name(r.get("translated-destination"), dic)
        orig_ip, trans_ip = obj_ip(r.get("original-destination"), dic), obj_ip(r.get("translated-destination"), dic)
        if (same := same_object(r)) is not None:
            orig_ip = await public_ip(same) or orig_ip
        row.name = (str(r.get("name") or "") or f"{pkg} #{r.get('rule-number', '')}")[:255]
        # 跟 Palo Alto 一樣一律記成 port_forward：「對外開放服務」只列這一類；靜態 NAT 在說明裡寫明
        row.type = "port_forward"
        row.protocol = "any"
        hit = matches.get(trans_ip) if trans_ip else None
        row.dst_ip_id = hit[0].id if hit and hit[0] is not None else None
        row.disabled = r.get("enabled") is False
        row.description = (f"Check Point {pkg} #{r.get('rule-number', '')} ({r.get('method') or 'nat'}): "
                           f"{orig_name} {orig_ip or ''} → "
                           f"{trans_name} {trans_ip or ''}").strip()[:500]
        row.updated_at = now
        session.add(row)
        n += 1
    return n, truncated


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def _section(errors: dict[str, str], key: str, coro: Any) -> Any:
    """跑一個區段；失敗只記錯誤、回 None（其他區段照跑）。"""
    try:
        return await coro
    except CheckPointError as exc:
        errors[key] = str(exc)[:200]
        return None


async def sync_instance(session: AsyncSession, inst: Any) -> dict[str, Any]:
    """依序同步每個網域（單一管理伺服器就是一個空網域）。全部網域都登入失敗才往上拋（作業顯示失敗）。"""
    now = datetime.now(UTC)
    domains = [d.strip() for d in inst.domains or [] if d.strip()] or [""]
    counts: dict[str, Any] = {"gateways": 0, "objects": 0, "rules": 0, "nat": 0}
    errors: dict[str, str] = {}
    truncated: list[str] = []
    nat_seen: set[str] = set()
    nat_complete = True
    login_failures: list[CheckPointError] = []

    for dom in domains:
        tag = f"{dom}:" if len(domains) > 1 or dom else ""
        try:
            async with Session(inst, dom or None) as s:
                if s.version and inst.api_version != s.version:
                    inst.api_version = s.version

                got = await _section(errors, f"{tag}gateways", sync_gateways(session, s, inst, dom, now))
                counts["gateways"] += got or 0
                if inst.sync_objects:
                    got = await _section(errors, f"{tag}objects", sync_objects(session, s, inst, dom, now))
                    if got:
                        counts["objects"] += got[0]
                        if got[1]:
                            truncated.append(f"{tag}objects")
                if inst.sync_policies or inst.sync_nat:
                    pkgs = await _section(errors, f"{tag}packages", _packages(s, inst))
                    if pkgs is not None:
                        if inst.sync_policies:
                            got = await _section(errors, f"{tag}policies", sync_policies(session, s, inst, dom, now, pkgs))
                            if got:
                                counts["rules"] += got[0]
                                if got[1]:
                                    truncated.append(f"{tag}policies")
                        if inst.sync_nat:
                            got = await _section(errors, f"{tag}nat", sync_nat(session, s, inst, dom, now, pkgs, nat_seen))
                            if got is None:
                                nat_complete = False
                            else:
                                counts["nat"] += got[0]
                                nat_complete = nat_complete and not got[1]
                    else:
                        nat_complete = False
        except CheckPointError as exc:
            login_failures.append(exc)
            errors[f"{tag}login"] = str(exc)[:200]
            nat_complete = False
    if login_failures and len(login_failures) == len(domains):
        inst.last_error = str(login_failures[0])
        await session.commit()
        raise login_failures[0]

    # NAT 是全部網域一起比對歸屬（source_origin 一個實例一個），全部讀到才清掉這次沒看到的
    if inst.sync_nat and nat_complete:
        from app.models.nat import NATTranslation
        stale = (await session.execute(select(NATTranslation).where(
            NATTranslation.source_origin == f"{SOURCE}:{inst.id}"))).scalars().all()
        for n in stale:
            if n.external_id not in nat_seen:
                await session.delete(n)
    if inst.sync_policies and not any(k.endswith("policies") or k.endswith("login") for k in errors):
        from app.services.fw_review import run_sentinel
        await run_sentinel(session, source_type=SOURCE, instance=inst)

    counts["domains"] = len(domains)
    if truncated:
        counts["truncated"] = truncated
    if errors:
        counts["errors"] = errors
    inst.last_summary = {**counts, "api_version": inst.api_version}
    inst.last_sync_at = datetime.now(UTC)
    inst.last_error = ("部分區段失敗：" + "；".join(f"{k}: {v}" for k, v in errors.items())) if errors else None
    return counts


async def diagnose(inst: Any) -> dict[str, Any]:
    """測試連線：每個網域登入一次，回版本與各類數量（只讀第一頁的 total，不抓全部）。"""
    domains = [d.strip() for d in inst.domains or [] if d.strip()] or [""]
    out: dict[str, Any] = {"domains": []}
    for dom in domains:
        entry: dict[str, Any] = {"domain": dom}
        async with Session(inst, dom or None, timeout=20.0) as s:
            entry["version"] = s.version
            for key, cmd in (("gateways", "show-gateways-and-servers"), ("hosts", "show-hosts"),
                             ("networks", "show-networks"), ("groups", "show-groups"), ("packages", "show-packages")):
                try:
                    page = await s.call(cmd, offset=0, limit=1)
                    entry[key] = int(page.get("total") or 0)
                except CheckPointError as exc:
                    entry[key] = None
                    entry.setdefault("errors", {})[key] = str(exc)[:200]
        out["domains"].append(entry)
    out["version"] = next((d.get("version") for d in out["domains"] if d.get("version")), None)
    return out


__all__ = ["CheckPointError", "Session", "api_base", "diagnose", "encrypt_secret_for", "sync_instance", "uuid"]
