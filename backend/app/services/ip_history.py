"""IP 異動記錄寫入 helper（feature B）。

所有對 IPAddress 的「有意義變更」都應呼叫這裡留痕：人為編輯、sync 來的
hostname/mac/arp 變更、上下線。查詢端在 endpoints/ip_changes.py。

設計：呼叫方在自己的交易內呼叫，不自行 commit（與 append_audit 一致）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ip_change_log import IPChangeLog

if TYPE_CHECKING:
    from app.models.address import IPAddress

# 人為編輯時：欄位 → 事件類型（沒列到的歸 "edited"）
_FIELD_EVENT = {
    "hostname": "hostname_changed",
    "mac": "mac_changed",
    "state": "state_changed",
}

# 會留痕的可編輯欄位（其它如 custom_fields blob 不逐欄記）
_TRACKED_FIELDS = (
    "hostname", "mac", "state", "description", "owner", "switch_port",
    "device_id", "note", "customer_id",
)


def _s(v: Any) -> str | None:
    """值轉成記錄用字串；None 保持 None。"""
    if v is None:
        return None
    return str(v)


# 翻動摺疊的時間窗：同一輪同步（含同一交易內）互相覆寫都落在這個範圍內
_FLAP_WINDOW = timedelta(minutes=10)


async def log_change(
    session: AsyncSession,
    *,
    ip: IPAddress,
    event_type: str,
    field: str | None = None,
    old: Any = None,
    new: Any = None,
    source: str = "system",
    actor_user_id: str | uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    """寫一筆 IP 異動記錄。

    **翻動摺疊**：若上一筆同 IP 同欄位剛好是這次的反向（A→B 之後 B→A）且發生在
    `_FLAP_WINDOW` 之內，就把那一筆刪掉、這一筆也不寫 —— 淨效果是零，記兩筆只是噪音。
    來由：兩個 Wazuh 代理登記同一個 IP，每輪同步互相覆寫，實機十天洗出 620 筆，
    把真正有意義的人為編輯完全埋掉。根因在各 sync 端用 tiebreak 收斂，這裡是最後一道防線。
    """
    if field is not None:
        prev = (await session.execute(
            select(IPChangeLog).where(
                IPChangeLog.ip_id == ip.id, IPChangeLog.field == field,
            ).order_by(IPChangeLog.created_at.desc()).limit(1)
        )).scalars().first()
        if (prev is not None
                and prev.old_value == _s(new) and prev.new_value == _s(old)
                and prev.created_at is not None
                and datetime.now(UTC) - prev.created_at <= _FLAP_WINDOW):
            await session.delete(prev)
            return
    session.add(
        IPChangeLog(
            ip_id=ip.id,
            subnet_id=ip.subnet_id,
            ip_text=str(ip.ip),
            event_type=event_type,
            field=field,
            old_value=_s(old),
            new_value=_s(new),
            source=source,
            actor_user_id=uuid.UUID(str(actor_user_id)) if actor_user_id else None,
            note=note,
        )
    )


async def log_field_diffs(
    session: AsyncSession,
    *,
    ip: IPAddress,
    before: dict[str, Any],
    changes: dict[str, Any],
    source: str = "manual",
    actor_user_id: str | uuid.UUID | None = None,
) -> int:
    """比對 before vs changes，對每個真的有變的可追蹤欄位各寫一筆。回傳寫入筆數。

    before：變更前的值快照（key 為欄位名）。
    changes：本次要套用的變更（通常是 payload.model_dump(exclude_unset=True)）。
    """
    n = 0
    for field in _TRACKED_FIELDS:
        if field not in changes:
            continue
        old = before.get(field)
        new = changes.get(field)
        if _s(old) == _s(new):
            continue
        await log_change(
            session, ip=ip,
            event_type=_FIELD_EVENT.get(field, "edited"),
            field=field, old=old, new=new,
            source=source, actor_user_id=actor_user_id,
        )
        n += 1
    return n
