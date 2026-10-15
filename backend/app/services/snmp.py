"""SNMP 收集的設定層：憑證加解密與驗證、收集者判定、交給代理的工作內容（0217）。

資料寫入（ARP／FDB／介面／鄰居）在 services/snmp_collect.py；這裡只回答三件事：

1. **憑證**：機密以 AES-GCM 加密（AAD 綁列 id），API 永遠不回傳。所有機密與帳號都**不可以有控制字元** ——
   代理把它們寫進暫存的 snmp.conf（一行一個設定），換行就等於讓人插入任意 net-snmp 設定。
2. **誰來收**（`resolve_many`）：auto＝管理位址所在子網路的負責代理（IP 記錄的子網路；覆寫位址時取包含它的最精確
   子網路，同樣精確卻指向不同代理＝不猜）、agent＝指定代理、server＝本機代理（is_local）。代理要有 SNMP 能力
   （工具盤點裡有 snmpbulkwalk、版本夠新）才會被指派；判定不出來時給明確的錯誤代碼，畫面上講得出原因。
3. **交給代理什麼**（`target_spec`／`job_params`）：解密後的憑證**只**放進給被指派代理的回應（既有的 HTTPS＋代理金鑰），
   不寫進資料庫的工作記錄（agent_probe_jobs.params 只存 id，領取時才展開）。
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.core.sqlin import in_values
from app.models.snmp import (
    AUTH_PROTOCOLS,
    DEFAULT_INTERVALS,
    MIN_INTERVALS,
    PRIV_PROTOCOLS,
    SECTIONS,
    SECURITY_LEVELS,
    SnmpCredential,
    SnmpTarget,
)

#: 支援 SNMP 收集的最低代理版本（工具盤點會回報 snmpbulkwalk 的版本）
MIN_AGENT_VERSION = (1, 19, 0)
#: 代理端的工具名稱（工具盤點 X-Agent-Tools 裡的 name）
AGENT_TOOL = "snmpbulkwalk"


def default_collect_flags(device_type: str | None) -> dict[str, bool]:
    """沒指定收集項目時依裝置類型給預設（與前端 utils/snmp.ts 的 defaultCollectFlags 同一套規則）：
    路由器／防火牆讀 ARP（補隔著它的網段的 MAC）＋介面；交換器讀 MAC 表＋鄰居＋介面；其他只讀最輕的介面。"""
    kind = (device_type or "").lower()
    if kind in ("router", "firewall"):
        return {"collect_arp": True, "collect_fdb": False, "collect_neighbors": False, "collect_interfaces": True}
    if kind == "switch":
        return {"collect_arp": False, "collect_fdb": True, "collect_neighbors": True, "collect_interfaces": True}
    return {"collect_arp": False, "collect_fdb": False, "collect_neighbors": False, "collect_interfaces": True}
#: 交給代理的負載上限（代理端會再夾一次）：逐台依序、每次 walk 有時間與列數上限、每台有總時間
BUDGET: dict[str, int] = {
    "target_seconds": 300,      # 一台設備一輪最多花多久（超過就把剩下的區段記成未完成）
    "walk_seconds": 120,        # 單一 walk 的時間上限（十萬列的 MAC 表、一次 10 筆要上萬個請求）
    "max_rows": 150_000,        # 單一 walk 的列數上限（超過就停、標未完成，不清舊資料）
    "max_repetitions": 10,      # GETBULK 一次要幾筆：小一點，設備 CPU 才不會被一個請求卡住
    "pause_ms": 200,            # walk 與 walk 之間的喘息
    "max_vlans": 64,            # Cisco 逐 VLAN 讀 FDB 的上限
}
#: 測試最多一次試幾組憑證（都對同一台設備；不對網段每台主機試）
MAX_TEST_CREDENTIALS = 5
SNMP_JOB_KINDS = ("snmp_test", "snmp_collect")
#: 測試／立即收集的工作：代理在線時一秒內就領走；給長一點是因為一台設備可能正在被排程收集（同一台不並行）
JOB_TTL = timedelta(minutes=3)


class SnmpConfigError(ValueError):
    """設定不合法：帶 ui_detail 代碼（422）。"""

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


# ─────────────────── 憑證 ───────────────────
_SECRET_FIELDS = ("community", "auth_passphrase", "priv_passphrase")


def _aad(cred_id: uuid.UUID, fieldname: str) -> bytes:
    return f"snmp_credential:{cred_id}:{fieldname}".encode()


def set_secret(cred: SnmpCredential, fieldname: str, value: str | None) -> None:
    """寫入（或清掉）一個機密欄位。`cred.id` 要先有值（AAD 綁 id）。"""
    if value:
        enc, nonce = encrypt_secret(value, aad=_aad(cred.id, fieldname))
    else:
        enc, nonce = None, None
    setattr(cred, f"{fieldname}_enc", enc)
    setattr(cred, f"{fieldname}_nonce", nonce)


def get_secret(cred: SnmpCredential, fieldname: str) -> str | None:
    enc = getattr(cred, f"{fieldname}_enc", None)
    nonce = getattr(cred, f"{fieldname}_nonce", None)
    if not enc or not nonce:
        return None
    return decrypt_secret(bytes(enc), bytes(nonce), aad=_aad(cred.id, fieldname)).decode("utf-8")


def has_secret(cred: SnmpCredential, fieldname: str) -> bool:
    return bool(getattr(cred, f"{fieldname}_enc", None))


def _bad_chars(value: str) -> bool:
    """控制字元（含換行、Tab、DEL）一律不收：代理把值寫進 snmp.conf，換行等於插入任意設定。"""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def validate_credential(cred: SnmpCredential, new_secrets: dict[str, str | None]) -> None:
    """套用變更後的整體檢查（建立與更新共用）。`new_secrets` 是這次有給的機密（None／空＝不變）。

    v2c：要有社群字串。v3：要帳號、安全等級、認證協定與密碼；authPriv 再要加密協定與密碼。
    USM 的密碼至少 8 個字元（RFC 3414；net-snmp 少於 8 個字直接拒絕，與其讓代理報錯不如這裡先講）。
    """
    for name, value in new_secrets.items():
        if value and _bad_chars(value):
            raise SnmpConfigError("snmp_cred_bad_chars", "憑證不可以包含換行或其他控制字元", field=name)
    if cred.username and (_bad_chars(cred.username) or " " in cred.username):
        raise SnmpConfigError("snmp_cred_bad_chars", "帳號不可以包含空白、換行或其他控制字元", field="username")

    def present(fieldname: str) -> bool:
        v = new_secrets.get(fieldname)
        return bool(v) or has_secret(cred, fieldname)

    if cred.version == "v2c":
        if not present("community"):
            raise SnmpConfigError("snmp_cred_community_required", "v2c 要填社群字串")
        return
    if not cred.username:
        raise SnmpConfigError("snmp_cred_username_required", "SNMPv3 要填帳號")
    if cred.security_level not in SECURITY_LEVELS:
        raise SnmpConfigError("snmp_cred_level_required", "SNMPv3 要選安全等級（authPriv 或 authNoPriv）")
    if cred.auth_protocol not in AUTH_PROTOCOLS or not present("auth_passphrase"):
        raise SnmpConfigError("snmp_cred_auth_required", "SNMPv3 要選認證協定並填認證密碼")
    for name in ("auth_passphrase", "priv_passphrase"):
        v = new_secrets.get(name)
        if v and len(v) < 8:
            raise SnmpConfigError("snmp_cred_passphrase_short", "SNMPv3 的密碼至少 8 個字元", field=name)
    if cred.security_level == "authPriv":
        if cred.priv_protocol not in PRIV_PROTOCOLS or not present("priv_passphrase"):
            raise SnmpConfigError("snmp_cred_priv_required", "authPriv 要選加密協定並填加密密碼")


def apply_secrets(cred: SnmpCredential, new_secrets: dict[str, str | None]) -> list[str]:
    """寫入這次有給的機密，並清掉這個版本／安全等級用不到的那些。回傳有異動的欄位名（稽核用，不含值）。"""
    changed: list[str] = []
    for name in _SECRET_FIELDS:
        v = new_secrets.get(name)
        if v:
            set_secret(cred, name, v)
            changed.append(name)
    # 用不到的一律清掉：v2c 不留 v3 密碼、v3 不留社群字串、authNoPriv 不留加密密碼
    unused: list[str] = []
    if cred.version == "v2c":
        unused = ["auth_passphrase", "priv_passphrase"]
        cred.username = cred.security_level = cred.auth_protocol = cred.priv_protocol = None
    else:
        unused = ["community"]
        if cred.security_level == "authNoPriv":
            unused.append("priv_passphrase")
            cred.priv_protocol = None
    for name in unused:
        if has_secret(cred, name):
            set_secret(cred, name, None)
            changed.append(name)
    return changed


def credential_spec(cred: SnmpCredential) -> dict[str, Any]:
    """交給代理的憑證（解密後）。只在給被指派代理的回應裡出現，不寫日誌、不存資料庫。"""
    spec: dict[str, Any] = {"credential_id": str(cred.id), "version": cred.version,
                            "timeout_s": int(cred.timeout_s or 5), "retries": int(cred.retries or 0)}
    if cred.version == "v2c":
        spec["community"] = get_secret(cred, "community") or ""
    else:
        spec.update({"username": cred.username, "security_level": cred.security_level,
                     "auth_protocol": cred.auth_protocol,
                     "auth_passphrase": get_secret(cred, "auth_passphrase") or ""})
        if cred.security_level == "authPriv":
            spec.update({"priv_protocol": cred.priv_protocol,
                         "priv_passphrase": get_secret(cred, "priv_passphrase") or ""})
    return spec


# ─────────────────── 收集目標 ───────────────────
def effective_intervals(target: SnmpTarget) -> dict[str, int]:
    """啟用中的區段 → 間隔（秒）。覆寫低於下限的一律拉回下限：不可以被設成每 10 秒讀一次路由器全表。"""
    raw = target.intervals if isinstance(target.intervals, dict) else {}
    out: dict[str, int] = {}
    for s in SECTIONS:
        if not getattr(target, f"collect_{s}", False):
            continue
        try:
            v = int(raw.get(s) or DEFAULT_INTERVALS[s])
        except (TypeError, ValueError):
            v = DEFAULT_INTERVALS[s]
        out[s] = max(MIN_INTERVALS[s], min(v, 86400))
    return out


def agent_version_tuple(v: str | None) -> tuple[int, ...]:
    out: list[int] = []
    for part in str(v or "").split(".")[:3]:
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def agent_snmp_tool(agent: Any) -> dict[str, Any] | None:
    """代理工具盤點裡的 snmpbulkwalk（有裝才回）。"""
    for t in agent.tools or []:
        if isinstance(t, dict) and t.get("name") == AGENT_TOOL and t.get("installed"):
            return t
    return None


def agent_snmp_capable(agent: Any) -> bool:
    """這台代理能不能做 SNMP 收集：版本夠新、而且工具盤點回報有 snmpbulkwalk（net-snmp 用戶端工具）。"""
    return (agent_version_tuple(agent.agent_version) >= MIN_AGENT_VERSION
            and agent_snmp_tool(agent) is not None)


@dataclass
class Resolution:
    """一個收集目標的判定結果：管理位址、收集者，或判定不出來的原因（代碼＋參數）。"""

    ip: str | None = None
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    capable: bool = False
    subnet_id: uuid.UUID | None = None
    subnet_cidr: str | None = None
    error_code: str | None = None
    error: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def fail(self, code: str, message: str, **params: Any) -> Resolution:
        self.error_code, self.error, self.params = code, message, params
        return self

    @property
    def ok(self) -> bool:
        return self.error_code is None and self.agent_id is not None and self.ip is not None

    def error_detail(self) -> dict[str, Any] | None:
        if self.error_code is None:
            return None
        return {"code": self.error_code, "params": self.params, "message": self.error}


@dataclass
class _Want:
    """resolve_many 的輸入：一個（可能還沒存檔的）收集目標要判定的欄位。"""

    key: Any
    device_id: uuid.UUID | None
    ip_override: str | None
    collector: str
    agent_id: uuid.UUID | None


def want_of(target: Any, key: Any = None) -> _Want:
    return _Want(key=key if key is not None else target.id, device_id=target.device_id,
                 ip_override=str(target.ip_override) if target.ip_override else None,
                 collector=target.collector or "auto", agent_id=target.agent_id)


async def resolve_many(session: AsyncSession, wants: list[_Want]) -> dict[Any, Resolution]:
    """整批判定（輪詢時每台代理都要算一次所有目標：查詢數不可以跟目標數成正比）。"""
    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.scan_agent import ScanAgent
    from app.models.subnet import Subnet

    out: dict[Any, Resolution] = {w.key: Resolution() for w in wants}
    if not wants:
        return out
    agents = {a.id: a for a in (await session.execute(select(ScanAgent))).scalars().all()}
    local = [a for a in agents.values() if a.is_local]

    # ── 1. 管理位址：覆寫 → 裝置主要 IP → 裝置唯一一筆連結的 IP ──
    dev_ids = {w.device_id for w in wants if w.device_id and not w.ip_override}
    prim: dict[uuid.UUID, tuple[str, uuid.UUID | None]] = {}
    if dev_ids:
        for did, ip, sid in (await session.execute(
            select(Device.id, IPAddress.ip, IPAddress.subnet_id)
            .join(IPAddress, IPAddress.id == Device.primary_ip_id)
            .where(in_values(Device.id, dev_ids)))).all():
            prim[did] = (str(ip).split("/")[0], sid)
        rest = dev_ids - set(prim)
        if rest:
            linked: dict[uuid.UUID, list[tuple[str, uuid.UUID | None]]] = {}
            for did, ip, sid in (await session.execute(
                select(IPAddress.device_id, IPAddress.ip, IPAddress.subnet_id)
                .where(in_values(IPAddress.device_id, rest)))).all():
                linked.setdefault(did, []).append((str(ip).split("/")[0], sid))
            for did, items in linked.items():
                if len(items) == 1:          # 好幾筆又沒設主要 IP：不猜哪一個是管理位址
                    prim[did] = items[0]
    sub_ids = {sid for _ip, sid in prim.values() if sid}
    subs: dict[uuid.UUID, tuple[str, uuid.UUID | None]] = {}
    if sub_ids:
        for sid, cidr, agent_id in (await session.execute(
                select(Subnet.id, Subnet.cidr, Subnet.scan_agent_id).where(in_values(Subnet.id, sub_ids)))).all():
            subs[sid] = (str(cidr), agent_id)

    for w in wants:
        r = out[w.key]
        sid: uuid.UUID | None = None
        if w.ip_override:
            r.ip = w.ip_override
        elif w.device_id in prim:
            r.ip, sid = prim[w.device_id]
        else:
            r.fail("snmp_no_mgmt_ip", "這台裝置沒有主要 IP（或連結了好幾筆 IP 卻沒設主要 IP）：請設定主要 IP 或填管理位址")
            continue

        # ── 2. 收集者 ──
        agent = None
        if w.collector == "agent":
            agent = agents.get(w.agent_id) if w.agent_id else None
            if agent is None:
                r.fail("snmp_agent_not_found", "指定的掃描代理不存在")
                continue
        elif w.collector == "server":
            if not local:
                r.fail("snmp_no_local_agent", "伺服器上沒有本機掃描代理：請改用自動或指定代理")
                continue
            agent = local[0]
        else:
            if sid is not None and sid in subs:
                r.subnet_id, (r.subnet_cidr, aid) = sid, subs[sid]
                agent = agents.get(aid) if aid else None
            else:
                hit = await _subnet_for_ip(session, r.ip)
                if hit is None:
                    r.fail("snmp_no_subnet", "管理位址不在任何子網路裡：請指定掃描代理", ip=r.ip)
                    continue
                if hit == "ambiguous":
                    r.fail("snmp_collector_ambiguous",
                           "管理位址落在好幾個同樣精確、由不同掃描代理負責的子網路（重疊網段）：請指定掃描代理", ip=r.ip)
                    continue
                r.subnet_id, r.subnet_cidr, aid = hit
                agent = agents.get(aid) if aid else None
            if agent is None:
                r.fail("snmp_no_collector", "管理位址所在的子網路沒有指派掃描代理：請指派代理或改成指定代理",
                       ip=r.ip, cidr=r.subnet_cidr or "")
                continue
        r.agent_id, r.agent_name = agent.id, agent.name
        r.capable = agent_snmp_capable(agent)
        if not agent.enabled:
            r.fail("snmp_agent_disabled", "負責的掃描代理已停用", agent=agent.name)
        elif not r.capable:
            r.fail("snmp_agent_no_snmp",
                   "負責的掃描代理還不能做 SNMP 收集：請更新代理到 1.19.0 以上並安裝 net-snmp 用戶端工具（apt install snmp）",
                   agent=agent.name)
    return out


async def _subnet_for_ip(session: AsyncSession, ip: str) -> tuple[uuid.UUID, str, uuid.UUID | None] | str | None:
    """包含這個位址、最精確的未封存子網路；同樣精確卻指向不同代理＝"ambiguous"。"""
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import INET

    from app.models.subnet import Subnet
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    rows = (await session.execute(
        select(Subnet.id, Subnet.cidr, Subnet.scan_agent_id, func.masklen(Subnet.cidr))
        .where(Subnet.cidr.op(">>=")(cast(ip, INET)), Subnet.archived_at.is_(None))
        .order_by(func.masklen(Subnet.cidr).desc()).limit(20))).all()
    if not rows:
        return None
    best = [r for r in rows if r[3] == rows[0][3]]
    agents = {r[2] for r in best}
    if len(agents) > 1:
        return "ambiguous"
    sid, cidr, aid, _m = best[0]
    return sid, str(cidr), aid


async def resolve(session: AsyncSession, target: Any) -> Resolution:
    return (await resolve_many(session, [want_of(target, key="one")]))["one"]


def target_spec(target: SnmpTarget, ip: str) -> dict[str, Any]:
    """交給代理的收集目標（不含憑證）。"""
    return {"target_id": str(target.id), "ip": ip, "port": int(target.port or 161),
            "sections": effective_intervals(target)}


async def poll_targets(session: AsyncSession, agent: Any) -> list[dict[str, Any]]:
    """這台代理要收集的目標（含解密後的憑證）。代理沒有 SNMP 能力就是空的（舊代理也不會看這個欄位）。"""
    if not agent_snmp_capable(agent) or not agent.enabled:
        return []
    targets = list((await session.execute(
        select(SnmpTarget).where(SnmpTarget.enabled.is_(True)))).scalars().all())
    if not targets:
        return []
    res = await resolve_many(session, [want_of(t) for t in targets])
    mine = [t for t in targets if res[t.id].ok and res[t.id].agent_id == agent.id]
    if not mine:
        return []
    creds = {c.id: c for c in (await session.execute(select(SnmpCredential).where(
        in_values(SnmpCredential.id, {t.credential_id for t in mine})))).scalars().all()}
    out: list[dict[str, Any]] = []
    for t in mine:
        cred = creds.get(t.credential_id)
        spec = target_spec(t, res[t.id].ip or "")
        if cred is None or not spec["sections"]:
            continue
        try:
            spec["credential"] = credential_spec(cred)
        except Exception as exc:  # 解不開（金鑰換了、資料損毀）：這台這輪不收，不要讓整個輪詢失敗
            import logging
            logging.getLogger(__name__).warning("snmp credential %s unreadable: %s", cred.id, type(exc).__name__)
            continue
        out.append(spec)
    return out


async def assigned_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """每台代理被指派幾個收集目標（掃描代理頁顯示用）。"""
    targets = list((await session.execute(
        select(SnmpTarget).where(SnmpTarget.enabled.is_(True)))).scalars().all())
    res = await resolve_many(session, [want_of(t) for t in targets])
    counts: dict[uuid.UUID, int] = {}
    for t in targets:
        aid = res[t.id].agent_id
        if aid is not None:
            counts[aid] = counts.get(aid, 0) + 1
    return counts


# ─────────────────── 測試／立即收集（代理工作通道）───────────────────
async def create_job(session: AsyncSession, *, agent_id: uuid.UUID, kind: str, target_id: uuid.UUID,
                     credential_ids: list[uuid.UUID] | None, requested_by: uuid.UUID | None) -> Any:
    """建立工作。**params 只存 id**：解密後的憑證在代理領取時才放進回應（job_params），資料庫裡沒有明文。"""
    from app.models.agent_probe_job import STATUS_PENDING, AgentProbeJob
    from app.services.agent_probe import MAX_PENDING_PER_AGENT, ProbeJobError

    if kind not in SNMP_JOB_KINDS:
        raise ProbeJobError(f"unsupported snmp job: {kind!r}")
    pending = list((await session.execute(select(AgentProbeJob.id).where(
        AgentProbeJob.agent_id == agent_id, AgentProbeJob.status == STATUS_PENDING,
        AgentProbeJob.expires_at > datetime.now(UTC)).limit(MAX_PENDING_PER_AGENT + 1))).scalars().all())
    if len(pending) >= MAX_PENDING_PER_AGENT:
        raise ProbeJobError("too many pending jobs for this agent")
    params: dict[str, Any] = {"target_id": str(target_id)}
    if credential_ids:
        params["credential_ids"] = [str(c) for c in credential_ids[:MAX_TEST_CREDENTIALS]]
    job = AgentProbeJob(agent_id=agent_id, kind=kind, params=params, status=STATUS_PENDING,
                        requested_by=requested_by, expires_at=datetime.now(UTC) + JOB_TTL)
    session.add(job)
    await session.flush()
    return job


async def job_params(session: AsyncSession, job: Any) -> dict[str, Any] | None:
    """代理領取時展開工作內容（含解密後的憑證）。目標不見了、或已經不歸這台代理收 → None（工作作廢）。"""
    raw = job.params if isinstance(job.params, dict) else {}
    try:
        target = await session.get(SnmpTarget, uuid.UUID(str(raw.get("target_id"))))
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    res = await resolve(session, target)
    if res.ip is None or res.agent_id != job.agent_id:
        return None
    spec = target_spec(target, res.ip)
    if job.kind == "snmp_collect":
        # 立即收集＝跑所有收集項目（不管排程到期了沒）
        spec["sections"] = effective_intervals(target)
    ids: list[uuid.UUID] = []
    for c in raw.get("credential_ids") or [str(target.credential_id)]:
        try:
            ids.append(uuid.UUID(str(c)))
        except ValueError:
            continue
    creds = {c.id: c for c in (await session.execute(select(SnmpCredential).where(
        in_values(SnmpCredential.id, ids)))).scalars().all()}
    specs = [credential_spec(creds[i]) for i in ids if i in creds]
    if not specs:
        return None
    if job.kind == "snmp_collect":
        spec["credential"] = specs[0]
    else:
        spec["credentials"] = specs[:MAX_TEST_CREDENTIALS]
    spec["budget"] = dict(BUDGET)
    return spec
