"""Hostname 多來源優先序解析（feature A）。

- 每個來源對一個 IP 各存一筆觀測（ip_hostname_observations）
- IPAddress.hostname = 解析後的有效值：
    1. 若該 IP 有 hostname_source_pin 且該來源有觀測 → 用它
    2. 否則依全域優先序（system_settings.hostname_precedence）取第一個有值的來源
- 有效值變動時，順手寫一筆 feature B 的 hostname_changed 異動記錄

全域優先序透過 set_precedence 改，存 system_settings；有 60s in-process cache。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ip_hostname import HOSTNAME_SOURCES, IPHostnameObservation
from app.services.ip_history import log_change
from app.services.precedence import Precedence

if TYPE_CHECKING:
    from app.models.address import IPAddress

HOSTNAME_KEY = "hostname_precedence"
# 預設：人工最優先，其次 DNS、LibreNMS、OPNsense、掃描、Proxmox
DEFAULT_ORDER: list[str] = ["manual", "dns", "librenms", "opnsense", "pfsense", "fortigate", "paloalto", "windows_dhcp", "scanner", "netbios", "mdns", "proxmox", "zabbix", "wazuh", "adguard"]

# 排序／停用／快取的共通機制在 services/precedence.py；
# 這裡只留 hostname 特有的部分：觀測表、pin、重算與異動記錄。
_P = Precedence(key=HOSTNAME_KEY, sources=tuple(HOSTNAME_SOURCES),
                default_order=tuple(DEFAULT_ORDER))


def _bust() -> None:
    _P.bust()


async def _load(session: AsyncSession) -> tuple[list[str], list[str]]:
    return await _P.load(session)


async def get_precedence(session: AsyncSession) -> list[str]:
    return await _P.get_order(session)


async def get_disabled(session: AsyncSession) -> list[str]:
    return await _P.get_disabled(session)


async def set_precedence(
    session: AsyncSession, *, order: list[str],
    disabled: list[str] | None = None, updated_by_user_id: uuid.UUID | None = None,
) -> tuple[list[str], list[str]]:
    return await _P.save(session, order=order, disabled=disabled,
                         updated_by_user_id=updated_by_user_id)


async def seed_observation(
    session: AsyncSession, *, ip: IPAddress, source: str, hostname: str | None,
) -> None:
    """IP 建立時用：hostname 已直接寫進 ip.hostname，這裡只補一筆觀測，不重算/不記異動。"""
    hostname = (hostname or "").strip() or None
    if hostname is None or source not in HOSTNAME_SOURCES:
        return
    from datetime import UTC, datetime
    session.add(IPHostnameObservation(
        ip_id=ip.id, source=source, hostname=hostname, observed_at=datetime.now(UTC),
    ))


def _resolve(observations: dict[str, str], pin: str | None, order: list[str]) -> str | None:
    """依 pin + 優先序挑出有效 hostname。"""
    if pin and observations.get(pin):
        return observations[pin]
    for src in order:
        v = observations.get(src)
        if v:
            return v
    return None


async def _observations_for(session: AsyncSession, ip_id) -> dict[str, str]:  # type: ignore[no-untyped-def]
    rows = (await session.execute(
        select(IPHostnameObservation.source, IPHostnameObservation.hostname)
        .where(IPHostnameObservation.ip_id == ip_id)
    )).all()
    return {src: hn for src, hn in rows}


async def recompute_effective(
    session: AsyncSession, *, ip: IPAddress, source: str | None = None,
    actor_user_id: str | None = None,
) -> bool:
    """依現有觀測重算 ip.hostname；有變就更新並寫異動記錄。回傳是否有變。"""
    obs = await _observations_for(session, ip.id)
    order, disabled = await _load(session)
    eff_order = [s for s in order if s not in disabled]   # 停用的來源不參與名稱比對
    new_hostname = _resolve(obs, ip.hostname_source_pin, eff_order)
    old_hostname = ip.hostname
    if (old_hostname or None) == (new_hostname or None):
        return False
    ip.hostname = new_hostname
    await log_change(
        session, ip=ip, event_type="hostname_changed", field="hostname",
        old=old_hostname, new=new_hostname,
        source=source or "system", actor_user_id=actor_user_id,
    )
    return True


async def apply_observation(
    session: AsyncSession, *, ip: IPAddress, source: str, hostname: str | None,
    actor_user_id: str | None = None, tiebreak_min: bool = False,
) -> bool:
    """記錄某來源對此 IP 的 hostname 觀測（None/空 → 清掉該來源），再重算有效值。

    回傳有效 hostname 是否因此變動。所有 sync / 人為編輯改 hostname 都走這裡。

    tiebreak_min：同一來源、同一 IP 已有不同主機名稱時，保留字典序較小者（穩定收斂）。
    給「多個來源實體可能指向同一 IP」的 sync 用（如多台 PVE guest 回報同一 IP），避免每次同步來回翻轉、洗版異動記錄。
    """
    if source not in HOSTNAME_SOURCES:
        source = "manual"
    hostname = (hostname or "").strip() or None

    existing = (await session.execute(
        select(IPHostnameObservation).where(
            IPHostnameObservation.ip_id == ip.id,
            IPHostnameObservation.source == source,
        )
    )).scalar_one_or_none()

    if hostname is None:
        if existing is not None:
            await session.execute(
                delete(IPHostnameObservation).where(IPHostnameObservation.id == existing.id)
            )
    elif existing is None:
        from datetime import UTC, datetime
        session.add(IPHostnameObservation(
            ip_id=ip.id, source=source, hostname=hostname, observed_at=datetime.now(UTC),
        ))
    elif existing.hostname != hostname:
        # 穩定收斂：多實體共用同一 IP 時，保留字典序較小者，避免每次同步來回翻轉
        if tiebreak_min and existing.hostname and hostname > existing.hostname:
            pass
        else:
            from datetime import UTC, datetime
            existing.hostname = hostname
            existing.observed_at = datetime.now(UTC)

    # observation 與下面 recompute 在同一交易；flush 讓上面的新增/刪除對 select 可見
    await session.flush()
    return await recompute_effective(
        session, ip=ip, source=source, actor_user_id=actor_user_id,
    )
