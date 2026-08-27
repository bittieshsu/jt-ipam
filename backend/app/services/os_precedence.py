"""OS 來源優先序。

OS 資訊有三個來源，各自存在不同地方：
  - scanner  : 掃描代理 nmap 偵測 → ip_addresses.os_guess
  - librenms : LibreNMS 裝置 → devices.os（IP 經 device_id 關聯）
  - wazuh    : Wazuh 代理 → wazuh_agents.os_platform / os_version（以 IP 對映）

依設定的順序取第一個有值的來源當作此 IP 的「有效 OS」。compute-on-read：不另存欄位，
由 `effective_os()` 即時彙整（OS 不常變，且免 migration / sync hook）。

排序與快取的共通機制在 `services/precedence.py`；來源性質登記在 `services/evidence.py`。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.os_fingerprint import normalize_os
from app.services.precedence import Precedence

OS_KEY = "os_precedence"
OS_SOURCES: list[str] = ["scanner", "librenms", "wazuh"]
DEFAULT_ORDER: list[str] = ["librenms", "wazuh", "scanner"]

# OS 沒有「停用個別來源」的需求，protected 留空即可（沒有 manual 這個來源）
_P = Precedence(key=OS_KEY, sources=tuple(OS_SOURCES),
                default_order=tuple(DEFAULT_ORDER), protected=frozenset())


def _bust() -> None:
    _P.bust()


async def get_order(session: AsyncSession) -> list[str]:
    return await _P.get_order(session)


async def set_order(
    session: AsyncSession, *, order: list[str], updated_by_user_id: uuid.UUID | None = None,
) -> list[str]:
    clean, _ = await _P.save(session, order=order, updated_by_user_id=updated_by_user_id)
    return clean


async def _candidates(session: AsyncSession, ip: Any) -> dict[str, str]:
    """彙整此 IP 各來源的原始 OS 字串（有值才放）。"""
    out: dict[str, str] = {}
    if ip.os_guess:
        out["scanner"] = ip.os_guess
    if ip.device_id:
        from app.models.device import Device
        dev = await session.get(Device, ip.device_id)
        if dev is not None and getattr(dev, "os", None):
            ver = getattr(dev, "version", None)
            out["librenms"] = f"{dev.os}{' ' + ver if ver else ''}"
    # Wazuh 代理以 IP 對映
    from app.models.wazuh import WazuhAgent
    wa = (await session.execute(
        select(WazuhAgent).where(WazuhAgent.ip == str(ip.ip)).limit(1)
    )).scalars().first()
    # 只比對 IP 不夠：DHCP 位址會被回收，失聯 agent 的舊登記會把別台機器的 OS 貼過來
    from app.services.wazuh import agent_represents_ip
    if wa is not None and not agent_represents_ip(wa, ip):
        wa = None
    if wa is not None and wa.os_platform:
        ver = wa.os_version
        out["wazuh"] = f"{wa.os_platform}{' ' + ver if ver else ''}"
    return out


async def effective_os(session: AsyncSession, ip: Any) -> dict[str, Any]:
    """依優先序回傳此 IP 的有效 OS：{os_guess, os_family, os_source}（皆可能為 None）。"""
    cand = await _candidates(session, ip)
    if not cand:
        return {"os_guess": None, "os_family": None, "os_source": None}
    order, disabled = await _P.load(session)
    src, raw = _P.pick(dict(cand), order, disabled)
    if not raw:
        return {"os_guess": None, "os_family": None, "os_source": None}
    return {"os_guess": raw, "os_family": normalize_os(raw), "os_source": src}
