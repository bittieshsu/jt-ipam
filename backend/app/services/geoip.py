"""GeoIP：用 MaxMind GeoLite2 *web service*（免本地 DB）。

管理者在系統設定填 MaxMind account ID + license key（後者加密存）。
查詢時以 HTTP Basic 認證打 https://geolite.info/geoip/v2.1/city/{ip}。
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import UnsafeOutboundURL, safe_request
from app.core.security import decrypt_secret, encrypt_secret
from app.models.system_setting import SystemSetting

GEOIP_KEY = "geoip"
_HOST = "geolite.info"   # GeoLite2 web service（付費帳號可改 geoip.maxmind.com）


async def get_geoip_creds(session: AsyncSession) -> tuple[str | None, str | None]:
    """回傳 (account_id, license_key)；未設定回 (None, None)。"""
    row = await session.get(SystemSetting, GEOIP_KEY)
    if not row or not isinstance(row.value, dict):
        return None, None
    v = row.value
    acct = v.get("account_id") or None
    ct_b64 = v.get("key_ct")
    nonce_b64 = v.get("key_nonce")
    key: str | None = None
    if ct_b64 and nonce_b64:
        try:
            key = decrypt_secret(base64.b64decode(ct_b64), base64.b64decode(nonce_b64)).decode()
        except Exception:  # noqa: BLE001
            key = None
    return acct, key


async def set_geoip_creds(
    session: AsyncSession, *, account_id: str | None, license_key: str | None, updated_by=None,  # type: ignore[no-untyped-def]
) -> None:
    """設定 account_id；license_key 有給才更新（空字串/None 表示保留原本）。"""
    from sqlalchemy.orm.attributes import flag_modified
    row = await session.get(SystemSetting, GEOIP_KEY)
    if row is None:
        row = SystemSetting(key=GEOIP_KEY, value={}, updated_by=updated_by)
        session.add(row)
    cur: dict[str, Any] = dict(row.value or {})
    if account_id is not None:
        cur["account_id"] = account_id.strip()
    if license_key:
        ct, nonce = encrypt_secret(license_key.strip())
        cur["key_ct"] = base64.b64encode(ct).decode()
        cur["key_nonce"] = base64.b64encode(nonce).decode()
    row.value = cur
    row.updated_by = updated_by
    flag_modified(row, "value")
    await session.commit()


async def geoip_lookup(session: AsyncSession, ip: str) -> dict[str, Any]:
    acct, key = await get_geoip_creds(session)
    if not acct or not key:
        return {"ip": ip, "error": "not_configured"}
    auth = base64.b64encode(f"{acct}:{key}".encode()).decode()
    url = f"https://{_HOST}/geoip/v2.1/city/{ip}"
    try:
        resp = await safe_request("GET", url, headers={"Authorization": f"Basic {auth}"}, timeout=10.0)
    except (UnsafeOutboundURL, httpx.HTTPError) as exc:
        return {"ip": ip, "error": f"request_failed: {exc}"}
    if resp.status_code == 401:
        return {"ip": ip, "error": "auth_failed"}
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", "")
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        return {"ip": ip, "error": f"http_{resp.status_code}: {detail}"}
    d = resp.json()
    country = (d.get("country") or {}).get("names", {}).get("en")
    city = (d.get("city") or {}).get("names", {}).get("en")
    subs = [s.get("names", {}).get("en") for s in (d.get("subdivisions") or [])]
    loc = d.get("location") or {}
    traits = d.get("traits") or {}
    return {
        "ip": ip,
        "country": country,
        "country_iso": (d.get("country") or {}).get("iso_code"),
        "city": city,
        "subdivisions": [s for s in subs if s],
        "postal": (d.get("postal") or {}).get("code"),
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "time_zone": loc.get("time_zone"),
        "accuracy_radius": loc.get("accuracy_radius"),
        "network": traits.get("network"),
        "asn": traits.get("autonomous_system_number"),
        "as_org": traits.get("autonomous_system_organization"),
    }
