"""通知管道 senders 的 payload / headers 正確性，以及 broadcast 並行 + best-effort。

不打真網路：monkeypatch notify_channels._post 攔截送出的內容；Nextcloud 驗 HMAC 簽章。
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from app.services import notify_channels as nc


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    async def fake_post(url, *, json=None, data=None, headers=None, auth=None):
        calls.append({"url": url, "json": json, "data": data, "headers": headers, "auth": auth})

    monkeypatch.setattr(nc, "_post", fake_post)
    return calls


async def test_telegram(sent):
    await nc.send_telegram({"telegram_token": "T:tok", "telegram_chat_id": "-100"}, "Sub", "Body")
    c = sent[0]
    assert c["url"] == "https://api.telegram.org/botT:tok/sendMessage"
    assert c["json"]["chat_id"] == "-100"
    assert "Sub" in c["json"]["text"] and "Body" in c["json"]["text"]


async def test_slack(sent):
    await nc.send_slack({"slack_webhook": "https://hooks.slack/x"}, "Sub", "Body")
    assert sent[0]["url"] == "https://hooks.slack/x"
    assert sent[0]["json"]["text"].startswith("*Sub*")   # Slack mrkdwn 單星


async def test_teams_legacy_first(sent):
    await nc.send_teams({"teams_webhook": "https://teams/x"}, "Sub", "Body")
    # 先嘗試舊式純 text（雙星 markdown）；未失敗就只送一次
    assert sent[0]["json"]["text"].startswith("**Sub**")
    assert len(sent) == 1


async def test_teams_workflows_fallback(monkeypatch):
    calls: list[dict] = []
    first = {"n": 0}

    async def flaky_post(url, *, json=None, data=None, headers=None, auth=None):
        first["n"] += 1
        calls.append(json)
        if first["n"] == 1:
            raise RuntimeError("400 legacy rejected")   # 模擬 Workflows 拒收純 text

    monkeypatch.setattr(nc, "_post", flaky_post)
    await nc.send_teams({"teams_webhook": "https://teams/x"}, "Sub", "Body")
    assert len(calls) == 2  # legacy 失敗 → 改送 Adaptive Card
    assert calls[1]["type"] == "message"
    assert calls[1]["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


async def test_zulip(sent):
    await nc.send_zulip(
        {"zulip_site": "https://z/", "zulip_bot_email": "b@z", "zulip_api_key": "k",
         "zulip_stream": "alerts", "zulip_topic": "t"}, "Sub", "Body")
    c = sent[0]
    assert c["url"] == "https://z/api/v1/messages"
    assert c["auth"] == ("b@z", "k")
    assert c["data"]["type"] == "stream" and c["data"]["to"] == "alerts" and c["data"]["topic"] == "t"


async def test_nextcloud_hmac(sent):
    secret = "s3cr3t"
    await nc.send_nextcloud(
        {"nextcloud_url": "https://nc/", "nextcloud_token": "tok", "nextcloud_secret": secret},
        "Sub", "Body")
    c = sent[0]
    assert c["url"].endswith("/ocs/v2.php/apps/spreed/api/v1/bot/tok/message")
    rnd = c["headers"]["X-Nextcloud-Talk-Bot-Random"]
    msg = c["json"]["message"]
    expected = hmac.new(secret.encode(), (rnd + msg).encode(), hashlib.sha256).hexdigest()
    assert c["headers"]["X-Nextcloud-Talk-Bot-Signature"] == expected


async def test_generic_webhook(sent):
    await nc.send_webhook({"webhook_url": "https://ex/hook", "webhook_token": "abc"}, "Sub", "Body")
    c = sent[0]
    assert c["url"] == "https://ex/hook"
    assert c["json"] == {"app": "jt-ipam", "subject": "Sub", "text": "Body"}
    assert c["headers"]["Authorization"] == "Bearer abc"


async def test_generic_webhook_no_token(sent):
    await nc.send_webhook({"webhook_url": "https://ex/hook"}, "Sub", None)
    assert sent[0]["headers"] is None
    assert sent[0]["json"]["text"] == ""


async def test_missing_config_raises():
    for fn, cfg in [
        (nc.send_telegram, {}), (nc.send_slack, {}), (nc.send_teams, {}),
        (nc.send_zulip, {}), (nc.send_nextcloud, {}), (nc.send_webhook, {}),
    ]:
        with pytest.raises(RuntimeError):
            await fn(cfg, "s", "t")


async def test_broadcast_concurrent_best_effort(monkeypatch):
    ran: list[str] = []

    async def ok(cfg, s, t):
        ran.append("slack")

    async def boom(cfg, s, t):
        ran.append("telegram")
        raise RuntimeError("channel down")

    monkeypatch.setitem(nc._SENDERS, "slack", ok)
    monkeypatch.setitem(nc._SENDERS, "telegram", boom)

    async def fake_cfg(session):
        return {"slack_enabled": True, "telegram_enabled": True, "teams_enabled": False}

    monkeypatch.setattr("app.services.system_config.get_notification_channels", fake_cfg)
    # 一個管道丟例外不可中斷另一個；兩個啟用的都要被呼叫
    await nc.broadcast_channels(None, subject="s", text="t")
    assert set(ran) == {"slack", "telegram"}


# ── 「前往檢視」連結（使用者 2026-10-19：Zulip 收到「詳見異常偵測頁面」卻點不過去）──
URL = "https://ipam.example.com/anomaly?tab=conflicts"


async def test_every_channel_carries_the_link(sent):
    cfgs = {
        "telegram": {"telegram_token": "T:tok", "telegram_chat_id": "-100"},
        "slack": {"slack_webhook": "https://hooks.slack/x"},
        "teams": {"teams_webhook": "https://teams/x"},
        "zulip": {"zulip_site": "https://z.example", "zulip_bot_email": "b@z", "zulip_api_key": "k",
                  "zulip_stream": "s"},
        "nextcloud": {"nextcloud_url": "https://nc", "nextcloud_token": "t", "nextcloud_secret": "x"},
        "webhook": {"webhook_url": "https://hook"},
    }
    for ch, cfg in cfgs.items():
        sent.clear()
        await nc.send_one(cfg, ch, "Sub", "Body", link=URL)
        blob = str(sent[0])
        assert URL in blob, ch
    # 各管道用自己會顯示成可點連結的寫法
    sent.clear()
    await nc.send_slack(cfgs["slack"], "Sub", "Body", link=URL)
    assert f"<{URL}|前往檢視>" in sent[0]["json"]["text"]
    sent.clear()
    await nc.send_zulip(cfgs["zulip"], "Sub", "Body", link=URL)
    assert f"[前往檢視]({URL})" in sent[0]["data"]["content"]
    sent.clear()
    await nc.send_webhook(cfgs["webhook"], "Sub", "Body", link=URL)
    assert sent[0]["json"]["url"] == URL


async def test_teams_card_has_an_open_button(monkeypatch):
    calls: list[dict] = []

    async def flaky_post(url, *, json=None, data=None, headers=None, auth=None):
        calls.append(json)
        if len(calls) == 1:
            raise RuntimeError("400 legacy rejected")

    monkeypatch.setattr(nc, "_post", flaky_post)
    await nc.send_teams({"teams_webhook": "https://teams/x"}, "Sub", "Body", link=URL)
    assert f"[前往檢視]({URL})" in calls[0]["text"]
    card = calls[1]["attachments"][0]["content"]
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "前往檢視", "url": URL}]


async def test_no_link_keeps_the_old_message(sent):
    await nc.send_zulip({"zulip_site": "https://z", "zulip_bot_email": "b", "zulip_api_key": "k",
                         "zulip_stream": "s"}, "Sub", "Body")
    assert sent[0]["data"]["content"] == "**Sub**\nBody"


async def test_broadcast_turns_a_site_path_into_a_full_url(monkeypatch):
    got: list[str | None] = []

    async def ok(cfg, s, t, link=None):
        got.append(link)

    monkeypatch.setitem(nc._SENDERS, "slack", ok)

    async def fake_cfg(session):
        return {"slack_enabled": True}

    monkeypatch.setattr("app.services.system_config.get_notification_channels", fake_cfg)
    monkeypatch.setattr(nc, "_public_base", lambda: "https://ipam.example.com/")
    await nc.broadcast_channels(None, subject="s", text="t", link="/anomaly?tab=conflicts")
    await nc.broadcast_channels(None, subject="s", text="t", link="https://other.example/x")
    await nc.broadcast_channels(None, subject="s", text="t")
    assert got == [URL, "https://other.example/x", None]
