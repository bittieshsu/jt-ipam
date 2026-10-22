"""異常觀測（0224）：每一筆異常出現、持續、消失的時間。

異常偵測以前只留最新一份報告，戰情室要畫「近幾小時依類型」的時序就沒有資料（2026-10-21 使用者確認新增）。
守的是：
1. 新的一筆記首次出現；下一輪還在只更新最後出現；不見了記消失時間；消失後再出現是新的一列。
2. 每次存報告（排程與手動）都會記錄，不必另外呼叫。
3. 消失超過保留天數的清掉，表不會無限長大。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.anomaly_observation import AnomalyObservation
from app.services import anomaly_history

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


def _report(*items: tuple[str, dict]) -> dict:
    out: dict = {"unauthorized_total": 3}
    for cat, it in items:
        out.setdefault(cat, []).append(it)
    return out


async def _rows(db) -> list[AnomalyObservation]:
    return list((await db.execute(select(AnomalyObservation).order_by(AnomalyObservation.first_seen_at))).scalars())


async def test_new_ongoing_resolved_and_reappeared(db_session) -> None:
    a = ("ip_conflicts", {"ip": "198.51.100.10", "macs": ["00:00:5e:00:53:01", "00:00:5e:00:53:02"]})
    b = ("unauthorized", {"ip": "198.51.100.20", "mac": "00:00:5e:00:53:20"})
    await anomaly_history.record(db_session, _report(a, b), at=T0)
    rows = await _rows(db_session)
    assert {r.category for r in rows} == {"ip_conflicts", "unauthorized"}
    assert all(r.first_seen_at == T0 and r.last_seen_at == T0 and r.resolved_at is None for r in rows)
    assert {r.label for r in rows} == {"198.51.100.10", "198.51.100.20"}
    assert {str(r.ip) for r in rows} == {"198.51.100.10", "198.51.100.20"}

    # 第二輪：a 還在、b 不見、多一筆 c
    c = ("dns_mismatches", {"fqdn": "app-07.example.com"})
    t1 = T0 + timedelta(hours=1)
    await anomaly_history.record(db_session, _report(a, c), at=t1)
    rows = {r.category: r for r in await _rows(db_session)}
    assert rows["ip_conflicts"].first_seen_at == T0
    assert rows["ip_conflicts"].last_seen_at == t1
    assert rows["unauthorized"].resolved_at == t1
    assert rows["dns_mismatches"].first_seen_at == t1
    assert rows["dns_mismatches"].label == "app-07.example.com"

    # 第三輪：b 又出現 → 新的一列（舊的那段保持已消失）
    t2 = T0 + timedelta(hours=2)
    await anomaly_history.record(db_session, _report(a, b, c), at=t2)
    unauth = [r for r in await _rows(db_session) if r.category == "unauthorized"]
    assert len(unauth) == 2
    assert unauth[0].resolved_at == t1
    assert unauth[1].first_seen_at == t2
    assert unauth[1].resolved_at is None


async def test_items_without_identity_still_count_once(db_session) -> None:
    """沒有可辨識欄位的發現，指紋退回整包內容：同一份內容兩輪之間是同一筆。"""
    weird = ("fw_rule_rot", {"kind": "unused", "detail": "x"})
    await anomaly_history.record(db_session, _report(weird), at=T0)
    await anomaly_history.record(db_session, _report(weird), at=T0 + timedelta(hours=1))
    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].resolved_at is None


async def test_saving_a_report_records_observations(db_session) -> None:
    from app.services.system_config import set_anomaly_report
    await set_anomaly_report(db_session, _report(("unauthorized", {"ip": "198.51.100.30"})), trigger="manual")
    rows = await _rows(db_session)
    assert [r.label for r in rows] == ["198.51.100.30"]


async def test_old_resolved_rows_are_pruned(db_session) -> None:
    old = T0 - timedelta(days=anomaly_history.RETENTION_DAYS + 1)
    db_session.add(AnomalyObservation(category="unauthorized", fingerprint="f" * 16, label="old",
                                      first_seen_at=old, last_seen_at=old, resolved_at=old))
    db_session.add(AnomalyObservation(category="unauthorized", fingerprint="e" * 16, label="recent",
                                      first_seen_at=T0, last_seen_at=T0, resolved_at=T0))
    await db_session.flush()
    await anomaly_history.record(db_session, _report(), at=T0 + timedelta(hours=1))
    assert [r.label for r in await _rows(db_session)] == ["recent"]


async def test_unknown_subnet_id_does_not_break_saving(db_session) -> None:
    import uuid
    from app.services.system_config import get_anomaly_report, set_anomaly_report
    bogus = str(uuid.uuid4())
    await set_anomaly_report(db_session, _report(("ip_conflicts", {"ip": "198.51.100.40", "subnet_id": bogus})),
                             trigger="manual")
    assert (await get_anomaly_report(db_session))["report"]["ip_conflicts"][0]["ip"] == "198.51.100.40"
    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].subnet_id is None


async def test_first_run_marks_existing_findings_as_baseline(db_session) -> None:
    """觀測表第一次記錄時，報告裡的每一筆早就存在了，只是這時才開始記（10-22 使用者：「怎麼前面都沒資料、
    忽然又有資料」，部署當下 149 筆全被畫成「新出現」）。這些標 baseline，之後才出現的才是真的新出現。"""
    a = ("unauthorized", {"ip": "198.51.100.20", "mac": "00:00:5e:00:53:20"})
    b = ("unauthorized", {"ip": "198.51.100.21", "mac": "00:00:5e:00:53:21"})
    await anomaly_history.record(db_session, _report(a), at=T0)
    await anomaly_history.record(db_session, _report(a, b), at=T0 + timedelta(minutes=10))
    rows = {r.label: r for r in await _rows(db_session)}
    assert rows["198.51.100.20"].baseline is True
    assert rows["198.51.100.21"].baseline is False


async def test_baseline_only_on_the_very_first_run(db_session) -> None:
    """表裡已經有記錄（就算都已經消失）就不是第一次：再出現的是新的。"""
    a = ("unauthorized", {"ip": "198.51.100.20", "mac": "00:00:5e:00:53:20"})
    await anomaly_history.record(db_session, _report(a), at=T0)
    await anomaly_history.record(db_session, _report(), at=T0 + timedelta(minutes=10))
    await anomaly_history.record(db_session, _report(a), at=T0 + timedelta(minutes=20))
    rows = await _rows(db_session)
    assert [r.baseline for r in rows] == [True, False]
