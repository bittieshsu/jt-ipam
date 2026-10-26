"""通知矩陣逐一勾選通訊軟體（使用者 10-25：「有啟用其它通知如 Zulip，在矩陣上也要可以勾選；預設跟第一欄通知一樣」）。

以前只要某事件勾了站內通知或 Email，就會發給**所有**已啟用的通訊軟體，沒辦法分開設定。
"""
from __future__ import annotations

from app.services import notify_channels
from app.services.system_config import (
    get_notification_matrix,
    set_notification_channels,
    set_notification_matrix,
)

EV = "firewall.rules_changed"          # 預設 站內開、Email 關


async def _enable(db_session, admin_user, *chs: str) -> None:
    data = {}
    for ch in chs:
        data[f"{ch}_enabled"] = True
    data.update({"telegram_bot_token": "1:x", "telegram_chat_id": "1", "zulip_url": "https://zulip.example.net",
                 "zulip_bot_email": "bot@example.net", "zulip_api_key": "k", "zulip_stream": "s", "zulip_topic": "t"})
    await set_notification_channels(db_session, data=data, updated_by_user_id=admin_user.id)
    await db_session.commit()


async def test_channel_columns_default_to_the_in_app_column(db_session, admin_user) -> None:
    mx = await get_notification_matrix(db_session)
    for ch in notify_channels.WEBHOOK_CHANNELS:
        assert mx[EV][ch] is mx[EV]["in_app"] is True
    # 只改站內那一欄、沒有存過通訊軟體的值：跟著站內走
    await set_notification_matrix(db_session, data={EV: {"in_app": False, "email": False}}, updated_by_user_id=admin_user.id)
    mx = await get_notification_matrix(db_session)
    assert mx[EV]["zulip"] is False and mx[EV]["telegram"] is False
    # 明確存過就照存的
    await set_notification_matrix(db_session, data={EV: {"in_app": False, "email": False, "zulip": True}},
                                  updated_by_user_id=admin_user.id)
    mx = await get_notification_matrix(db_session)
    assert mx[EV]["zulip"] is True and mx[EV]["telegram"] is False


async def test_broadcast_only_goes_to_channels_ticked_for_the_event(db_session, admin_user, monkeypatch) -> None:
    await _enable(db_session, admin_user, "telegram", "zulip")
    sent: list[str] = []

    async def fake(ch):
        async def f(cfg, title, text, link=None, rich=None):
            sent.append(ch)
        return f
    monkeypatch.setitem(notify_channels._SENDERS, "telegram", await fake("telegram"))
    monkeypatch.setitem(notify_channels._SENDERS, "zulip", await fake("zulip"))
    await set_notification_matrix(db_session, data={EV: {"in_app": True, "email": False, "telegram": True, "zulip": False}},
                                  updated_by_user_id=admin_user.id)
    await notify_channels.broadcast_channels(db_session, subject="規則有異動", event=EV)
    assert sent == ["telegram"]
    sent.clear()
    # 沒指定事件的呼叫照舊發給全部已啟用的
    await notify_channels.broadcast_channels(db_session, subject="x")
    assert sorted(sent) == ["telegram", "zulip"]


async def test_im_only_event_is_still_sent(db_session, admin_user, monkeypatch) -> None:
    """站內與 Email 都關、只勾 Zulip：仍要發到 Zulip（以前兩欄都關就整個不發）。"""
    from app.services.notification import notify_admins_event
    await _enable(db_session, admin_user, "zulip")
    sent: list[str] = []

    async def f(cfg, title, text, link=None, rich=None):
        sent.append(title)
    monkeypatch.setitem(notify_channels._SENDERS, "zulip", f)
    await set_notification_matrix(db_session, data={EV: {"in_app": False, "email": False, "zulip": True}},
                                  updated_by_user_id=admin_user.id)
    n = await notify_admins_event(db_session, event=EV, title="規則有異動")
    assert n == 0 and len(sent) == 1


async def test_api_round_trip(client, auth_headers) -> None:
    r = await client.put("/api/v1/system/notification-matrix", headers=auth_headers,
                         json={"matrix": {EV: {"in_app": True, "email": True, "slack": False}}})
    assert r.status_code == 200, r.text
    got = r.json()["matrix"][EV]
    assert got["email"] is True and got["slack"] is False and got["teams"] is True
    assert set(r.json()["channels"]) == set(notify_channels.WEBHOOK_CHANNELS)


def test_channel_lists_match() -> None:
    """矩陣的通訊軟體欄要跟實際的發送管道是同一份，新增管道時兩邊一起改。"""
    from app.services.system_config import NOTIFY_IM_CHANNELS
    assert tuple(NOTIFY_IM_CHANNELS) == tuple(notify_channels.WEBHOOK_CHANNELS)


async def test_anomaly_rows_follow_each_channels_ticks(db_session, admin_user, monkeypatch) -> None:
    """異常偵測一次合成多類：Telegram 只勾 IP 衝突、Zulip 兩類都勾 → Telegram 只收到一類。"""
    await _enable(db_session, admin_user, "telegram", "zulip")
    got: dict[str, list[str]] = {}

    async def fake_broadcast(session, *, subject, rows=None, only=None, **kw):
        for c in (only or []):
            got[c] = [r.label for r in rows or []]
    monkeypatch.setattr("app.services.notify_channels.broadcast_channels", fake_broadcast)
    await set_notification_matrix(db_session, data={
        "anomaly.ip_conflicts": {"in_app": False, "email": False, "telegram": True, "zulip": True},
        "anomaly.mac_drifts": {"in_app": False, "email": False, "telegram": False, "zulip": True},
    }, updated_by_user_id=admin_user.id)
    from app.services.anomaly import _notify_categories
    findings = {"ip_conflicts": [{"ip": "198.51.100.5"}], "mac_drifts": [{"mac": "02:00:00:00:00:01"}]}
    assert await _notify_categories(db_session, findings, only_new=False) == 2
    assert len(got["telegram"]) == 1 and len(got["zulip"]) == 2
