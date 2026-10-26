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
from dataclasses import dataclass, field
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

#: 聊天型管道的標題前綴（使用者 2026-10-19：通知美觀一點、用點 emoji）。依嚴重度；呼叫端可以另給 emoji
SEVERITY_EMOJI = {"critical": "🚨", "error": "🚨", "warning": "⚠️", "info": "ℹ️", "success": "✅"}


def decorate(subject: str, *, severity: str | None = None, emoji: str | None = None) -> str:
    e = emoji or SEVERITY_EMOJI.get(str(severity or "").lower())
    return f"{e} {subject}" if e else subject


def _msg(subject: str, text: str | None, link: str | None = None) -> str:
    """純文字訊息（Telegram、Nextcloud Talk）：網址單獨一行，兩邊都會自動變成可點的連結。"""
    out = f"{subject}\n{text}" if text else subject
    return f"{out}\n{LINK_LABEL}：{link}" if link else out


def _md(subject: str, text: str | None, link: str | None, *, bold: str = "**") -> str:
    out = f"{bold}{subject}{bold}\n{text}" if text else f"{bold}{subject}{bold}"
    return f"{out}\n[{LINK_LABEL}]({link})" if link else out


# ── 版面（使用者 2026-10-19／10-20）────────────────────────────────────────
# 第二版太花（emoji 五個、時間和主機名稱每則都重複、Zulip 的時間標記又多一個時鐘），一次偵測還洗出四則。
# 現在：emoji 只在標題（代表嚴重度），內容是一般段落，多筆事情用條列（每列的名稱就是連結、後面接實例），
# 最後一行「前往檢視」（Slack／Teams 是按鈕）。聊天軟體本來就有訊息時間，不再重複。

#: Teams 卡片標題區的底色
_TEAMS_STYLE = {"critical": "attention", "error": "attention", "warning": "warning", "success": "good",
                "info": "accent"}


@dataclass
class Row:
    """條列的一列：名稱（可連結）、說明、實例（程式碼樣式）。"""
    label: str
    detail: str | None = None
    codes: list[str] = field(default_factory=list)
    link: str | None = None            # 完整網址
    more: bool = False                 # 實例沒列完（後面接一個「…」）


@dataclass
class Rich:
    """一則通知的結構（標題已含 emoji）。各管道照自己的格式排版。"""
    title: str
    text: str | None = None
    bullets: list[str] = field(default_factory=list)   # 單一事件的實例（例：衝突的 IP）
    rows: list[Row] = field(default_factory=list)      # 一則裡的多件事（例：異常偵測各分類）
    link: str | None = None            # 完整網址
    severity: str | None = None


def make_rich(subject: str, text: str | None = None, *, link: str | None = None, severity: str | None = None,
              emoji: str | None = None, bullets: list[str] | None = None,
              rows: list[Row] | None = None) -> Rich:
    return Rich(title=decorate(subject, severity=severity, emoji=emoji), text=text or None,
                bullets=[b for b in (bullets or []) if b], rows=list(rows or []), link=absolute_link(link),
                severity=severity)


def _code(s: str) -> str:
    return "`" + s.replace("`", "'") + "`"


def _md_link(label: str, url: str | None) -> str:
    return f"[{label}]({url})" if url else label


def rich_markdown(r: Rich, *, zulip: bool = False) -> str:
    """Zulip／Nextcloud Talk／Teams（舊版 connector）共用的 Markdown 版面。`zulip` 留著給之後的差異。"""
    lines = [f"**{r.title}**"]
    if r.text:
        lines.append(r.text)
    if r.bullets:
        lines.append("　".join(_code(b) for b in r.bullets))
    for row in r.rows:
        tail = "　".join(_code(c) for c in row.codes) + (" …" if row.more else "")
        lines.append(f"- {_md_link(row.label, row.link)}" + (f"：{row.detail}" if row.detail else "")
                     + (f"　{tail}" if tail else ""))
    if r.link:
        if r.rows:
            lines.append("")               # 條列之後空一行，連結才不會被當成最後一列的延續
        lines.append(f"[{LINK_LABEL}]({r.link})")
    return "\n".join(lines)


def rich_telegram(r: Rich) -> str:
    from html import escape

    def a(label: str, url: str | None) -> str:
        return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>' if url else escape(label)
    parts = [f"<b>{escape(r.title)}</b>"]
    if r.text:
        parts.append(escape(r.text))
    if r.bullets:
        parts.append("　".join(f"<code>{escape(b)}</code>" for b in r.bullets))
    for row in r.rows:
        tail = "　".join(f"<code>{escape(c)}</code>" for c in row.codes) + (" …" if row.more else "")
        parts.append(f"• {a(row.label, row.link)}" + (f"：{escape(row.detail)}" if row.detail else "")
                     + (f"　{tail}" if tail else ""))
    if r.link:
        parts.append(a(LINK_LABEL, r.link))
    return "\n".join(parts)


def rich_slack(r: Rich) -> dict[str, Any]:
    """Slack Block Kit：粗體標題＋內容一個區塊、按鈕一個區塊；`text` 是通知預覽用的純文字。"""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = [f"*{esc(r.title)}*"]
    if r.text:
        lines.append(esc(r.text))
    if r.bullets:
        lines.append("　".join(_code(b) for b in r.bullets))
    for row in r.rows:
        name = f"<{row.link}|{esc(row.label)}>" if row.link else esc(row.label)
        tail = "　".join(_code(c) for c in row.codes) + (" …" if row.more else "")
        lines.append(f"• {name}" + (f"：{esc(row.detail)}" if row.detail else "") + (f"　{tail}" if tail else ""))
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}}]
    if r.link:
        blocks.append({"type": "actions", "elements": [{"type": "button", "url": r.link,
                       "text": {"type": "plain_text", "text": LINK_LABEL}}]})
    return {"text": r.title, "blocks": blocks}


def rich_teams_card(r: Rich) -> dict[str, Any]:
    """Teams（Workflows）的 Adaptive Card：有底色的標題區、內容、條列、按鈕。"""
    body: list[dict[str, Any]] = [{
        "type": "Container", "style": _TEAMS_STYLE.get(str(r.severity or "").lower(), "emphasis"), "bleed": True,
        "items": [{"type": "TextBlock", "text": r.title, "weight": "Bolder", "size": "Medium", "wrap": True}],
    }]
    if r.text:
        body.append({"type": "TextBlock", "text": r.text, "wrap": True})
    if r.bullets:
        body.append({"type": "TextBlock", "text": "　".join(r.bullets), "wrap": True, "fontType": "Monospace"})
    if r.rows:
        body.append({"type": "TextBlock", "wrap": True, "text": "\r".join(
            f"- {_md_link(row.label, row.link)}" + (f"：{row.detail}" if row.detail else "")
            + (f"　{' '.join(row.codes)}" if row.codes else "") + (" …" if row.more else "") for row in r.rows)})
    card: dict[str, Any] = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard", "version": "1.4", "body": body}
    if r.link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": LINK_LABEL, "url": r.link}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                 "content": card}]}


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


async def send_telegram(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None,
                        *, rich: Rich | None = None) -> None:
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not (token and chat):
        raise RuntimeError("Telegram bot token / chat id not set")
    payload: dict[str, Any] = {"chat_id": chat, "disable_web_page_preview": True}
    if rich is not None:
        payload.update(text=rich_telegram(rich), parse_mode="HTML")
    else:
        payload["text"] = _msg(subject, text, link)
    await _post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)


async def send_slack(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None,
                     *, rich: Rich | None = None) -> None:
    url = cfg.get("slack_webhook")
    if not url:
        raise RuntimeError("Slack webhook URL not set")
    if rich is not None:
        await _post(url, json=rich_slack(rich))
        return
    body = f"*{subject}*\n{text}" if text else f"*{subject}*"
    if link:
        body += f"\n<{link}|{LINK_LABEL}>"      # Slack mrkdwn 的連結寫法
    await _post(url, json={"text": body})


async def send_teams(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None,
                     *, rich: Rich | None = None) -> None:
    url = cfg.get("teams_webhook")
    if not url:
        raise RuntimeError("Teams webhook URL not set")
    if rich is not None:
        try:   # 舊版 connector 只收 text（Markdown 段落以空行分開）；新版 Workflows 收卡片
            await _post(url, json={"text": rich_markdown(rich).replace("\n", "\n\n")})
        except Exception:
            await _post(url, json=rich_teams_card(rich))
        return
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


async def send_zulip(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None,
                     *, rich: Rich | None = None) -> None:
    site = (cfg.get("zulip_site") or "").rstrip("/")
    email, api_key = cfg.get("zulip_bot_email"), cfg.get("zulip_api_key")
    stream, topic = cfg.get("zulip_stream"), cfg.get("zulip_topic") or "jt-ipam"
    if not (site and email and api_key and stream):
        raise RuntimeError("Zulip site / bot email / API key / channel not set")
    # Zulip 9.0 起「stream」改名「channel」（畫面照新名稱）；API 的 type 仍送 "stream"：新版兩者都收，舊版只認 stream
    await _post(f"{site}/api/v1/messages", auth=(email, api_key),
                data={"type": "stream", "to": stream, "topic": topic,
                      "content": rich_markdown(rich, zulip=True) if rich is not None
                      else _md(subject, text, link)})


async def send_nextcloud(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None,
                         *, rich: Rich | None = None) -> None:
    # Nextcloud Talk bot：對話 token + bot 密鑰（HMAC-SHA256 簽章 random+message）
    site = (cfg.get("nextcloud_url") or "").rstrip("/")
    token, secret = cfg.get("nextcloud_token"), cfg.get("nextcloud_secret")
    if not (site and token and secret):
        raise RuntimeError("Nextcloud URL / conversation token / bot secret not set")
    message = rich_markdown(rich) if rich is not None else _msg(subject, text, link)
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


async def send_webhook(cfg: dict[str, Any], subject: str, text: str | None, link: str | None = None,
                       severity: str | None = None, bullets: list[str] | None = None) -> None:
    # 通用 webhook：POST JSON 到自訂 URL；選填 Bearer token。方便串 n8n / 自寫端點等。
    url = cfg.get("webhook_url")
    if not url:
        raise RuntimeError("Webhook URL not set")
    headers = {}
    tok = cfg.get("webhook_token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    # 有連結才加 url 欄位：沒有時內容跟以前一模一樣（接收端可能有固定格式）
    # 通用 webhook 的接收端多半是程式：標題不加 emoji，嚴重度另給欄位（有才給，沒有時內容跟以前一樣）
    await _post(url, json={"app": "jt-ipam", "subject": subject, "text": text or "", **({"url": link} if link else {}),
                           **({"severity": severity} if severity else {}),
                           **({"items": bullets} if bullets else {})},
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
                   link: str | None = None, *, rich: Rich | None = None) -> None:
    """發送到單一管道（供測試端點用）。失敗丟例外。"""
    fn = _SENDERS.get(channel)
    if fn is None:
        raise RuntimeError(f"unknown channel: {channel}")
    if rich is not None and channel != "webhook":
        await fn(cfg, subject, text, link=link, rich=rich)
    else:
        await fn(cfg, subject, text, link=link)


async def broadcast_channels(session: AsyncSession, *, subject: str, text: str | None = None,
                             link: str | None = None, severity: str | None = None,
                             emoji: str | None = None, bullets: list[str] | None = None,
                             rows: list[Row] | None = None, event: str | None = None,
                             only: set[str] | None = None) -> None:
    """把一則事件推到『已啟用』的 webhook 型管道（各自 best-effort，不影響主流程）。

    `event`：通知矩陣的事件鍵，只發給那一列有勾的通訊軟體；`only`：限定這幾個管道（異常偵測依類別分組送）。

    `link` 是站內路徑（跟站內通知的 link 同一個值），這裡換成完整網址、各管道附上「前往檢視」。
    `severity`／`emoji`：聊天型管道的標題前綴（🚨 ⚠️ ℹ️ ✅ 或呼叫端指定的）；通用 webhook 只收 severity 欄位。"""
    url = absolute_link(link)
    rich = make_rich(subject, text, link=link, severity=severity, emoji=emoji, bullets=bullets, rows=rows)
    from app.services.system_config import get_notification_channels
    try:
        cfg = await get_notification_channels(session)
    except Exception:
        return
    enabled = [ch for ch in WEBHOOK_CHANNELS if cfg.get(f"{ch}_enabled")]
    if only is not None:
        enabled = [ch for ch in enabled if ch in only]
    if event and enabled:
        # 通知矩陣逐一勾選的通訊軟體（10-25）；沒指定事件的呼叫照舊發給全部已啟用的
        from app.services.system_config import get_notification_matrix
        row_ch = (await get_notification_matrix(session)).get(event)
        if row_ch is not None:
            enabled = [ch for ch in enabled if row_ch.get(ch, row_ch.get("in_app", True))]
    if not enabled:
        return

    async def _one(ch: str) -> None:
        try:
            if ch == "webhook":
                await send_webhook(cfg, subject, text, link=url, severity=severity, bullets=bullets)
            else:
                await _SENDERS[ch](cfg, rich.title, text, link=url, rich=rich)
        except Exception as exc:
            log.warning("notify channel %s failed: %s: %s", ch, type(exc).__name__, exc)

    # 並行送出：最壞情況＝最慢的單一管道（~timeout），不是各管道相加，避免拖長主流程
    await asyncio.gather(*[_one(ch) for ch in enabled])
