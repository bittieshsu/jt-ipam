"""Technitium DNS Server adapter（DNSServer.type = technitium）。

API（實機 15.6 確認）：
  POST {base}/api/zones/list                               zone 清單（name、type、disabled）
  POST {base}/api/zones/records/get  domain=<zone> zone=<zone> listZone=true   整個 zone 的紀錄
  POST {base}/api/user/session/get                         版本、帳號與權限（測試連線用）
token 放 POST 表單（services/technitium.py），不進網址。

只讀：jt-ipam 不寫回 Technitium（其他 DNS 類型的寫入路徑也沒有任何呼叫端），所以帳號只要「檢視」權限。
Technitium 的 DNS 紀錄另有逐個 zone 的權限，新建的 zone 預設只給管理員群組 —— 讀不到的 zone 丟錯，
pull_server 記成問題、不清它的舊紀錄；測試連線列出讀不到的 zone。
"""

from __future__ import annotations

from app.services.dns.base import DNSAdapter, DNSAdapterError, DNSRecordOp, DNSZoneInfo
from app.services.technitium import TechnitiumClient, TechnitiumError

#: 自己有位址紀錄的 zone 類型；Stub（只有 NS）、Catalog（成員清單）不讀
_RECORD_ZONE_TYPES = frozenset({"primary", "secondary", "forwarder", "secondaryforwarder"})
#: 測試連線時最多逐一確認幾個 zone 讀不讀得到（每個只讀 zone 頂點那幾筆，不讀整個 zone）
HEALTH_ZONE_PROBE_MAX = 200


def _name(v: object) -> str:
    return str(v or "").strip().rstrip(".").lower()


def _err(exc: TechnitiumError, **extra: object) -> DNSAdapterError:
    return DNSAdapterError(str(exc), code=exc.code, **{**exc.params, **extra})


class TechnitiumDNSAdapter(DNSAdapter):
    type = "technitium"

    def __init__(self, *, api_url: str, token: str, verify_tls: bool = True) -> None:
        self.client = TechnitiumClient(api_url=api_url, token=token, verify_tls=verify_tls)

    async def _zones(self) -> list[DNSZoneInfo]:
        try:
            data = await self.client.call("/api/zones/list")
        except TechnitiumError as exc:
            raise _err(exc) from exc
        out: list[DNSZoneInfo] = []
        for z in data.get("zones") or []:
            if not isinstance(z, dict) or z.get("disabled"):
                continue
            if str(z.get("type") or "").lower() not in _RECORD_ZONE_TYPES:
                continue
            name = _name(z.get("name"))
            if not name:
                continue
            kind = "reverse" if name.endswith((".in-addr.arpa", ".ip6.arpa")) else "forward"
            out.append(DNSZoneInfo(name=name, kind=kind))
        return out

    async def healthcheck(self) -> dict[str, object]:
        try:
            info = await self.client.session()
        except TechnitiumError as exc:
            raise _err(exc) from exc
        zones = await self._zones()
        unreadable: list[str] = []
        for z in zones[:HEALTH_ZONE_PROBE_MAX]:
            try:
                await self.client.call("/api/zones/records/get", domain=z.name, zone=z.name, listZone="false")
            except TechnitiumError as exc:
                if exc.code != "technitium_denied":
                    raise _err(exc, zone=z.name) from exc
                unreadable.append(z.name)
        zp = info["zones"]
        return {"ok": True, "version": info["version"], "server": info["server"], "user": info["user"],
                "zones": len(zones), "unreadable_zones": unreadable,
                "probed_zones": min(len(zones), HEALTH_ZONE_PROBE_MAX),
                "can_modify": bool(zp.get("modify")) if isinstance(zp, dict) else False}

    async def list_zones(self) -> list[DNSZoneInfo]:
        return await self._zones()

    async def list_records(self, zone_name: str) -> list[DNSRecordOp]:
        zone = _name(zone_name)
        try:
            data = await self.client.call("/api/zones/records/get", domain=zone, zone=zone, listZone="true")
        except TechnitiumError as exc:
            raise _err(exc, zone=zone) from exc
        out: list[DNSRecordOp] = []
        for r in data.get("records") or []:
            if not isinstance(r, dict) or r.get("disabled"):
                continue
            typ = str(r.get("type") or "").upper()
            rd = r.get("rData") if isinstance(r.get("rData"), dict) else {}
            if typ in ("A", "AAAA"):
                value = str(rd.get("ipAddress") or "").strip()
            elif typ == "PTR":
                value = _name(rd.get("ptrName"))
            elif typ == "CNAME":
                value = _name(rd.get("cname"))
            else:
                continue
            name = _name(r.get("name"))
            if not name or not value:
                continue
            try:
                ttl = int(r.get("ttl") or 3600)
            except (TypeError, ValueError):
                ttl = 3600
            out.append(DNSRecordOp(name=name, type=typ, value=value, ttl=ttl))
        return out

    async def upsert_record(self, zone_name: str, op: DNSRecordOp) -> None:
        raise DNSAdapterError("Technitium is read only in jt-ipam", code="dns_technitium_read_only")

    async def delete_record(self, zone_name: str, op: DNSRecordOp) -> None:
        raise DNSAdapterError("Technitium is read only in jt-ipam", code="dns_technitium_read_only")
