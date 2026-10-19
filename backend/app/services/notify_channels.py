"""通知管道發送器：Telegram / Slack / Teams / Nextcloud Talk / Zulip。

這些是「團隊頻道」型通知（一則事件推一次到設定好的頻道），與 Email（逐收件者）並行。
目標為管理者設定的外部端點（等同 SMTP 主機的信任模型：admin-only、直接連出、不套 SSRF 允許清單，
以支援自架 Nextcloud/Zulip 內網位址）。每個 send_* 成功回 None、失敗丟例外（供測試端點回報）；
broadcast_channels 逐管道 best-effort，單一失敗不影響其他管道或主流程。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets as _secrets
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import assert_url_safe, guarded_client

log = logging.getLogger("notify_channels")

# webhook 型通知的管道鍵（email 另外由 email_users 處理）
WEBHOOK_CHANNELS = ("telegram", "slack", "teams", "nextcloud", "zulip", "webhook")

_TIMEOUT = httpx.Timeout(12.0)


#: 連結的顯示文字。通知標題與內容本來就是中文（後端不知道收件者的語言），連結跟著用中文
LINK_LABEL = "前往檢視"


def _msg(subject: str, text: str | None, link: str | None = None) -> str:
    """純文字訊息（Telegram、Nextcloud Talk）：網址單獨一行，兩邊都會自動變成可點的連結。"""
    out = f"{subject}\n{text}" if text else subject
    return f"{out}\n{LINK_LABEL}：{link}" if link else out


def _md(subject: str, text: str | None, link: str | None, *, bold: str = "**") -> str:
    out = f"{bold}{subject}{bold}\n{text}" if text else f"{bold}{subject}{bold}"
    return f"{out}\n[{LINK_LABEL}]({link})" if link else out


def _public_base() -> str:
    from app.core.config import get_settings
    return str(get_settings().app_public_url)


def absolute_link(link: str | None) -> str | None:
    """站內路徑（/anomaly?tab=…）→ 外部收得到的完整網址（APP_PUBLIC_URL）；已經是完整網址就照用。

    沒登入的人點了會先到登入頁，登入後回到這一頁（前端 router 的 next 機制）。"""
    if not link:
        return None
    if link.startswith(("https://", "http://")):
        return link
    return _public_base().rstrip("/") + "/" + link.lstrip("/")


async def _post(url: str, *, json: dict | None = None, data: dict | None = None,
                headers: dict | None = None, auth: tuple[str, str] | None = None) -> None:
    """送出 webhook 型通知。

    SSRF：先過 `assert_url_safe`（與其他整合同一套檢查：擋 link-local / 雲端 metadata
    等，私有網段預設擋、`OUTBOUND_ALLOW_PRIVATE` 開啟才放行內網 webhook 接收端）。
    這一步很重要 —— 失敗時下面會把回應內容前 200 字放進錯誤訊息、而那訊息會顯示在
    設定頁與 last_error，等於可以拿任意 URL 的回應片段回來。
    這裡不用 `safe_request()` 只因為它不收 `data=` / `auth=`（表單編碼與 Basic Auth）；
    檢查用的是同一個函式。`follow_redirects=False`：避免以重導繞過檢查過的目標。
    """
    assert_url_safe(url)
    # 連線當下再檢查一次（DNS 可以在檢查後換答案）、不信任 HTTP_PROXY 等環境變數
    async with guarded_client(timeout=_TIMEOUT, http2=False) as client:
        r = await client.post(url, json=json, data=data, headers=headers, auth=auth)
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


async def send_telegram(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None) -> None:
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not (token and chat):
        raise RuntimeError("Telegram bot token / chat id not set")
    await _post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": _msg(subject, text, link), "disable_web_page_preview": True})


async def send_slack(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None) -> None:
    url = cfg.get("slack_webhook")
    if not url:
        raise RuntimeError("Slack webhook URL not set")
    body = f"*{subject}*\n{text}" if text else f"*{subject}*"
    if link:
        body += f"\n<{link}|{LINK_LABEL}>"      # Slack mrkdwn 的連結寫法
    await _post(url, json={"text": body})


async def send_teams(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None) -> None:
    url = cfg.get("teams_webhook")
    if not url:
        raise RuntimeError("Teams webhook URL not set")
    body = f"**{subject}**\n\n{text}" if text else f"**{subject}**"
    if link:
        body += f"\n\n[{LINK_LABEL}]({link})"
    try:
        # 舊版 Office365 connector：純 text
        await _post(url, json={"text": body})
    except Exception:
        # 新版 Teams「Workflows」incoming webhook 需要 Adaptive Card（connector 已被微軟淘汰）
        await _post(url, json={
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [{"type": "TextBlock", "text": body, "wrap": True}],
                    # 卡片底下一顆按鈕，比內文裡的連結好點
                    **({"actions": [{"type": "Action.OpenUrl", "title": LINK_LABEL, "url": link}]} if link else {}),
                },
            }],
        })


async def send_zulip(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None) -> None:
    site = (cfg.get("zulip_site") or "").rstrip("/")
    email, api_key = cfg.get("zulip_bot_email"), cfg.get("zulip_api_key")
    stream, topic = cfg.get("zulip_stream"), cfg.get("zulip_topic") or "jt-ipam"
    if not (site and email and api_key and stream):
        raise RuntimeError("Zulip site / bot email / API key / channel not set")
    # Zulip 9.0 起「stream」改名「channel」（畫面照新名稱）；API 的 type 仍送 "stream"：新版兩者都收，舊版只認 stream
    await _post(f"{site}/api/v1/messages", auth=(email, api_key),
                data={"type": "stream", "to": stream, "topic": topic,
                      "content": _md(subject, text, link)})


async def send_nextcloud(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None) -> None:
    # Nextcloud Talk bot：對話 token + bot 密鑰（HMAC-SHA256 簽章 random+message）
    site = (cfg.get("nextcloud_url") or "").rstrip("/")
    token, secret = cfg.get("nextcloud_token"), cfg.get("nextcloud_secret")
    if not (site and token and secret):
        raise RuntimeError("Nextcloud URL / conversation token / bot secret not set")
    message = _msg(subject, text, link)
    rnd = _secrets.token_hex(32)
    sig = hmac.new(secret.encode(), (rnd + message).encode(), hashlib.sha256).hexdigest()
    await _post(
        f"{site}/ocs/v2.php/apps/spreed/api/v1/bot/{token}/message",
        json={"message": message},
        headers={
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Nextcloud-Talk-Bot-Random": rnd,
            "X-Nextcloud-Talk-Bot-Signature": sig,
        },
    )


async def send_webhook(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None) -> None:
    # 通用 webhook：POST JSON 到自訂 URL；選填 Bearer token。方便串 n8n / 自寫端點等。
    url = cfg.get("webhook_url")
    if not url:
        raise RuntimeError("Webhook URL not set")
    headers = {}
    tok = cfg.get("webhook_token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    # 有連結才加 url 欄位：沒有時內容跟以前一模一樣（接收端可能有固定格式）
    await _post(url, json={"app": "jt-ipam", "subject": subject, "text": text or "", **({"url": link} if link else {})},
                headers=headers or None)


_SENDERS = {
    "telegram": send_telegram,
    "slack": send_slack,
    "teams": send_teams,
    "zulip": send_zulip,
    "nextcloud": send_nextcloud,
    "webhook": send_webhook,
}


async def send_one(cfg: dict[str, Any], channel: str, subject: str, text: str | None,
                   link: str | None = None) -> None:
    """發送到單一管道（供測試端點用）。失敗丟例外。"""
    fn = _SENDERS.get(channel)
    if fn is None:
        raise RuntimeError(f"unknown channel: {channel}")
    await fn(cfg, subject, text, link=link)


async def broadcast_channels(session: AsyncSession, *, subject: str, text: str | None = None,
                             link: str | None = None) -> None:
    """把一則事件推到所有『已啟用』的 webhook 型管道（各自 best-effort，不影響主流程）。

    `link` 是站內路徑（跟站內通知的 link 同一個值），這裡換成完整網址、各管道附上「前往檢視」。"""
    url = absolute_link(link)
    from app.services.system_config import get_notification_channels
    try:
        cfg = await get_notification_channels(session)
    except Exception:
        return
    enabled = [ch for ch in WEBHOOK_CHANNELS if cfg.get(f"{ch}_enabled")]
    if not enabled:
        return

    async def _one(ch: str) -> None:
        try:
            if url:
                await _SENDERS[ch](cfg, subject, text, link=url)
            else:
                await _SENDERS[ch](cfg, subject, text)
        except Exception as exc:
            log.warning("notify channel %s failed: %s: %s", ch, type(exc).__name__, exc)

    # 並行送出：最壞情況＝最慢的單一管道（~timeout），不是各管道相加，避免拖長主流程
    await asyncio.gather(*[_one(ch) for ch in enabled])
