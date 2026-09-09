"""異常偵測的通知可以逐類別決定。

原本整個異常偵測只有一列 `anomaly.detected`：要嘛十種發現全部通知、要嘛全部不通知。
實務上這十種的份量差很多 —— 「非法 DHCP 伺服器」是要立刻處理的事，
「失聯 IP」比較像每週整理一次的清單。混在一起的結果是使用者為了不被吵而整類關掉，
於是真正要緊的那幾種也一起消失。

守的重點是**升級不會改變既有行為**：已經把異常通知打開 Email 的站台，
升級之後每一類都要維持開著，而不是被重設成預設值。
"""
from __future__ import annotations

from app.services.system_config import ANOMALY_EVENTS


async def test_every_category_has_its_own_event(db_session):
    from app.services.system_config import get_notification_matrix

    m = await get_notification_matrix(db_session)
    for ev in ANOMALY_EVENTS:
        assert ev in m, f"{ev} 不在通知矩陣裡＝那一類永遠用不到自己的設定"


async def test_the_old_single_row_is_gone_from_the_ui_list(db_session):
    """留著一列講不出作用的總開關，只會讓人猜它與逐類別設定誰說了算。"""
    from app.services.system_config import NOTIFY_EVENTS

    keys = [k for k, _, _ in NOTIFY_EVENTS]
    assert "anomaly.detected" not in keys


async def test_existing_setting_carries_over(db_session):
    """升級前把異常通知的 Email 打開過 → 升級後十類都還是開著。"""
    from app.models.system_setting import SystemSetting
    from app.services.system_config import NOTIFY_MATRIX_KEY, get_notification_matrix

    # 模擬舊資料：只有 anomaly.detected，而且被改成 email=True
    db_session.add(SystemSetting(
        key=NOTIFY_MATRIX_KEY,
        value={"anomaly.detected": {"in_app": False, "email": True}},
    ))
    await db_session.flush()

    m = await get_notification_matrix(db_session)
    for ev in ANOMALY_EVENTS:
        assert m[ev] == {"in_app": False, "email": True}, f"{ev} 沒有沿用舊設定"


async def test_per_category_setting_wins_over_the_legacy_value(db_session):
    from app.models.system_setting import SystemSetting
    from app.services.system_config import NOTIFY_MATRIX_KEY, get_notification_matrix

    db_session.add(SystemSetting(
        key=NOTIFY_MATRIX_KEY,
        value={
            "anomaly.detected": {"in_app": True, "email": True},
            "anomaly.ghost_ips": {"in_app": False, "email": False},
        },
    ))
    await db_session.flush()
    m = await get_notification_matrix(db_session)
    assert m["anomaly.ghost_ips"] == {"in_app": False, "email": False}
    assert m["anomaly.ip_conflicts"] == {"in_app": True, "email": True}


async def test_notifications_respect_the_per_category_switch(db_session, admin_user):
    """關掉某一類就真的不發，其他類不受影響 —— 否則這個設定只是裝飾。"""
    from app.models.system_setting import SystemSetting
    from app.services.anomaly import notify_new_findings
    from app.services.system_config import NOTIFY_MATRIX_KEY

    db_session.add(SystemSetting(
        key=NOTIFY_MATRIX_KEY,
        value={"anomaly.ip_conflicts": {"in_app": False, "email": False},
               "anomaly.rogue_dhcp": {"in_app": True, "email": False}},
    ))
    await db_session.flush()

    sent = await notify_new_findings(db_session, {
        "ip_conflicts": [{"ip": "198.51.100.20"}],
        "rogue_dhcp": [{"server_ip": "198.51.100.30"}],
    })
    assert sent == 1, "只有沒被關掉的那一類該發"
