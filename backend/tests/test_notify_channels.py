"""通知管道 senders 的 payload / headers 正確性，以及 broadcast 並行 + best-effort。

不打真網路：monkeypatch notify_channels._post 攔截送出的內容；Nextcloud 驗 HMAC 簽章。
"""
from __future__ import annotations

import hashlib
from typing import Any
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

    async def ok(cfg, s, t, **kw):
        ran.append("slack")

    async def boom(cfg, s, t, **kw):
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

    async def ok(cfg, s, t, link=None, **kw):
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


# ── 美觀：emoji 前綴與連結圖示（使用者 2026-10-19）──
async def test_chat_channels_get_an_emoji_but_the_webhook_gets_a_severity_field(monkeypatch):
    got: dict[str, Any] = {}

    async def zulip(cfg, s, t, link=None, rich=None):
        got["zulip"] = nc.rich_markdown(rich, zulip=True)

    async def hook(url, *, json=None, data=None, headers=None, auth=None):
        got["webhook"] = json

    monkeypatch.setitem(nc._SENDERS, "zulip", zulip)
    monkeypatch.setattr(nc, "_post", hook)

    async def fake_cfg(session):
        return {"zulip_enabled": True, "webhook_enabled": True, "webhook_url": "https://hook"}

    monkeypatch.setattr("app.services.system_config.get_notification_channels", fake_cfg)
    monkeypatch.setattr(nc, "_public_base", lambda: "https://ipam.example.com/")
    await nc.broadcast_channels(None, subject="疑似登入權杖被盜用", text="t", link="/audit", severity="error")
    assert got["zulip"].startswith("**🚨 疑似登入權杖被盜用**")
    assert got["webhook"]["subject"] == "疑似登入權杖被盜用"
    assert got["webhook"]["severity"] == "error"
    await nc.broadcast_channels(None, subject="IP 申請", text="t", link="/requests", emoji="📝")
    assert got["zulip"].startswith("**📝 IP 申請**")
    assert "severity" not in got["webhook"]


async def test_link_line_has_an_icon(sent):
    await nc.send_zulip({"zulip_site": "https://z", "zulip_bot_email": "b", "zulip_api_key": "k",
                         "zulip_stream": "s"}, "⚠️ Sub", "Body", link=URL)
    assert sent[0]["data"]["content"] == f"**⚠️ Sub**\nBody\n🔗 [前往檢視]({URL})"


def test_anomaly_notifications_name_a_few_examples():
    from app.services.anomaly import _notify_examples
    items = [{"ip": "192.0.2.1"}, {"ip": "192.0.2.2"}, {"mac": "00:00:5e:00:53:01"}, {"ip": "192.0.2.4"}]
    assert _notify_examples(items) == ["192.0.2.1", "192.0.2.2", "00:00:5e:00:53:01"]
    assert _notify_examples([{"other": 1}]) == []



def _rich(**k: Any):
    from datetime import UTC, datetime
    base = dict(title="⚠️ IP 衝突：新增 3 筆（共 12 筆）", bullets=["192.0.2.13", "192.0.2.20"], link=URL,
                severity="warning", when=datetime(2026, 10, 19, 9, 5, tzinfo=UTC), source="ipam.example.com")
    base.update(k)
    return nc.Rich(**base)


def test_rich_markdown_layout() -> None:
    md = nc.rich_markdown(_rich(), zulip=True)
    assert md.splitlines() == [
        "**⚠️ IP 衝突：新增 3 筆（共 12 筆）**", "> • `192.0.2.13`", "> • `192.0.2.20`", "",
        "🟠 警告　·　🕒 <time:2026-10-19T09:05:00+00:00>　·　🖥️ ipam.example.com", f"🔗 [前往檢視]({URL})"]


def test_rich_telegram_escapes_and_uses_html() -> None:
    out = nc.rich_telegram(_rich(text="<script>x</script>"))
    assert "<b>⚠️ IP 衝突" in out
    assert "&lt;script&gt;" in out
    assert "<code>192.0.2.13</code>" in out
    assert f'<a href="{URL}">' in out


def test_rich_slack_blocks_and_teams_card() -> None:
    sl = nc.rich_slack(_rich())
    assert [b["type"] for b in sl["blocks"]] == ["header", "section", "context", "actions"]
    assert sl["blocks"][-1]["elements"][0]["url"] == URL
    card = nc.rich_teams_card(_rich(severity="error"))["attachments"][0]["content"]
    assert card["body"][0]["style"] == "attention"
    assert card["actions"][0]["url"] == URL


async def test_broadcast_sends_rich_to_every_chat_channel(monkeypatch, sent) -> None:
    async def fake_cfg(session):
        return {"telegram_enabled": True, "telegram_token": "T:t", "telegram_chat_id": "-1",
                "slack_enabled": True, "slack_webhook": "https://hooks.slack/x"}
    monkeypatch.setattr("app.services.system_config.get_notification_channels", fake_cfg)
    monkeypatch.setattr(nc, "_public_base", lambda: "https://ipam.example.com/")
    await nc.broadcast_channels(None, subject="IP 衝突", link="/anomaly", severity="warning", bullets=["192.0.2.1"])
    tg = next(c for c in sent if "telegram" in c["url"])
    sl = next(c for c in sent if "slack" in c["url"])
    assert tg["json"]["parse_mode"] == "HTML"
    assert "blocks" in sl["json"]
