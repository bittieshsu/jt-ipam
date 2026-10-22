"""異常觀測（0224）：每一筆異常從出現到消失的期間。

異常偵測只保留最新一份報告（system_settings.anomaly_report），戰情室的「近幾小時依類型」時序
沒有資料可畫（2026-10-21）。每次存報告時呼叫 `record`：

- 指紋沿用通知去重的 `anomaly._item_fingerprint`（穩定欄位組合，不含每輪都會變的時間與天數）
- 新的一筆 → 新增一列（首次出現＝最後出現＝這次）
- 上一輪就在 → 只更新最後出現
- 這輪不見 → 記消失時間；之後再出現是新的一列（時序上是兩段，不會把中間空檔畫成持續）
- 消失超過 RETENTION_DAYS 的刪掉
- 表裡完全沒有記錄時（第一次記錄）的每一筆標 baseline：它們早就存在、只是這時才開始記，
  不可以畫成「新出現」（10-22 部署當下 149 筆全被畫成新出現）

規模：未授權 IP 一類就可能上千筆。每類一次查出「還在」的列，在記憶體比對，新增用批次。
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.anomaly_observation import AnomalyObservation

RETENTION_DAYS = 90
#: 指認一筆用的欄位（依序取第一個有值的；與通知裡舉例的欄位一致）
_LABEL_KEYS = ("ip", "ip_text", "address", "mac", "fqdn", "hostname", "name", "server_ip", "target",
               "cidr", "subnet_cidr", "device", "device_name", "rule", "firewall")


def _label(item: dict[str, Any]) -> str | None:
    for k in _LABEL_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:120]
    return None


def _ip(item: dict[str, Any]) -> str | None:
    for k in ("ip", "ip_text", "address", "server_ip"):
        v = item.get(k)
        if isinstance(v, str):
            try:
                return str(ipaddress.ip_address(v.strip()))
            except ValueError:
                continue
    return None


def _subnet_id(item: dict[str, Any]) -> Any:
    import uuid
    v = item.get("subnet_id")
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def categories_of(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """報告裡每一類的發現清單（只取「清單裡是字典」的鍵；總數之類的純量不算）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for k, v in report.items():
        if isinstance(v, list):
            items = [i for i in v if isinstance(i, dict)]
            if items or not v:
                out[k] = items
    return out


async def record(session: AsyncSession, report: dict[str, Any], *, at: datetime | None = None) -> dict[str, int]:
    """把這次偵測的結果併進觀測表。回傳 {new, ongoing, resolved}。"""
    from app.services.anomaly import _item_fingerprint

    now = at or datetime.now(UTC)
    cats = categories_of(report)
    stats = {"new": 0, "ongoing": 0, "resolved": 0}
    first_run = (await session.scalar(select(AnomalyObservation.id).limit(1))) is None

    # 這次報告裡沒有出現的類別（例如偵測換了版本少一類）也要把還開著的結掉：查所有還開著的類別
    open_cats = set((await session.execute(
        select(AnomalyObservation.category).where(AnomalyObservation.resolved_at.is_(None)).distinct()
    )).scalars().all())

    for cat in sorted(set(cats) | open_cats):
        items = cats.get(cat, [])
        current: dict[str, dict[str, Any]] = {}
        for it in items:
            current.setdefault(_item_fingerprint(it), it)
        open_rows = dict((await session.execute(
            select(AnomalyObservation.fingerprint, AnomalyObservation.id).where(
                AnomalyObservation.category == cat, AnomalyObservation.resolved_at.is_(None))
        )).all())

        ongoing = [open_rows[f] for f in current if f in open_rows]
        gone = [rid for f, rid in open_rows.items() if f not in current]
        fresh = [f for f in current if f not in open_rows]
        if ongoing:
            await session.execute(update(AnomalyObservation).where(
                in_values(AnomalyObservation.id, ongoing)).values(last_seen_at=now))
        if gone:
            await session.execute(update(AnomalyObservation).where(
                in_values(AnomalyObservation.id, gone)).values(resolved_at=now))
        if fresh:
            # 發現裡的 subnet_id 可能指向已刪除的子網路：先確認存在，否則外鍵錯誤會讓整份報告存不進去
            from app.models.subnet import Subnet
            wanted = {sid for f in fresh if (sid := _subnet_id(current[f])) is not None}
            known = set((await session.execute(select(Subnet.id).where(
                in_values(Subnet.id, list(wanted))))).scalars().all()) if wanted else set()
            session.add_all([AnomalyObservation(
                category=cat[:64], fingerprint=f, label=_label(current[f]), ip=_ip(current[f]),
                subnet_id=sid if (sid := _subnet_id(current[f])) in known else None,
                first_seen_at=now, last_seen_at=now, baseline=first_run) for f in fresh])
        stats["new"] += len(fresh)
        stats["ongoing"] += len(ongoing)
        stats["resolved"] += len(gone)

    await session.execute(delete(AnomalyObservation).where(
        AnomalyObservation.resolved_at.is_not(None),
        AnomalyObservation.resolved_at < now - timedelta(days=RETENTION_DAYS)))
    await session.flush()
    return stats
