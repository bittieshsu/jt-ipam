"""會過期的存取憑證：到期前通知。

API 權杖一直都有到期日，但到期那天才會發現整合斷了；對外 MCP 金鑰與兩個公開端點的權杖
（Graylog DSV 查表、機櫃嵌入圖）2026-10-09 起也有到期日。這裡在到期前 14／7／1 天與到期當天
各通知一次（同一個門檻只通知一次，所以排程每輪都可以呼叫）：
- 系統層級的（MCP 金鑰、DSV、機櫃嵌入）→ 所有管理員
- API 權杖 → 權杖的擁有者

通知事件：`credential.expiring`（通知設定頁可以關、可以加 Email）。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import APIToken, User
from app.services.notification import push_notification

#: 到期前幾天通知（含 0＝到期當天／已過期）
THRESHOLDS = (14, 7, 1, 0)
EVENT = "credential.expiring"


@dataclass
class Credential:
    #: 去重用的穩定識別（換了一把新的就換一個）
    object_id: uuid.UUID
    kind: str                 # mcp_key / api_token / graylog_dsv / rack_embed
    name: str
    expires_at: datetime
    link: str
    recipients: list[User]


def bucket(expires_at: datetime, now: datetime) -> int | None:
    """落在哪個門檻；還沒到 14 天內回 None。"""
    days_left = math.ceil((expires_at - now).total_seconds() / 86400)
    if days_left > THRESHOLDS[0]:
        return None
    return min(t for t in THRESHOLDS if days_left <= t) if days_left > 0 else 0


def system_credential_id(kind: str, expires_at: datetime) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"jt-ipam:{kind}:{expires_at.isoformat()}")


#: 已通知過的門檻（{識別: 門檻}）存在設定表；只發 Email 不發站內通知時也要能去重
STATE_KEY = "credential_expiry_notified"


async def _load_state(session: AsyncSession) -> dict[str, int]:
    from app.models.system_setting import SystemSetting
    row = await session.get(SystemSetting, STATE_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


async def _save_state(session: AsyncSession, state: dict[str, int]) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting
    row = await session.get(SystemSetting, STATE_KEY)
    if row is None:
        session.add(SystemSetting(key=STATE_KEY, value=state))
    else:
        row.value = state
        flag_modified(row, "value")
    await session.flush()


async def _system_credentials(session: AsyncSession, admins: list[User]) -> list[Credential]:
    from app.services.system_config import get_graylog_dsv, get_llm_config, get_rack_embed

    out: list[Credential] = []
    cfg = await get_llm_config(session)
    if cfg.mcp_api_key and cfg.mcp_api_key_expires_at:
        out.append(Credential(system_credential_id("mcp_key", cfg.mcp_api_key_expires_at), "mcp_key",
                              "MCP API key", cfg.mcp_api_key_expires_at, "/llm", admins))
    for kind, getter, link, label in (("graylog_dsv", get_graylog_dsv, "/graylog-dsv", "Graylog DSV"),
                                      ("rack_embed", get_rack_embed, "/racks", "Rack embed")):
        v: dict[str, Any] = await getter(session)
        exp = v.get("token_expires_at")
        if v.get("enabled") and v.get("token_set", bool(v.get("token"))) and isinstance(exp, datetime):
            out.append(Credential(system_credential_id(kind, exp), kind, label, exp, link, admins))
    return out


async def check_credential_expiry(session: AsyncSession) -> dict[str, int]:
    """通知即將到期／已過期的憑證。回傳 {checked, notified}。"""
    from app.services.notification import email_users
    from app.services.system_config import get_notification_matrix

    matrix = await get_notification_matrix(session)
    ch = matrix.get(EVENT, {"in_app": True, "email": False})
    if not (ch.get("in_app") or ch.get("email")):
        return {"checked": 0, "notified": 0}
    now = datetime.now(UTC)
    admins = list((await session.execute(
        select(User).where(User.is_admin.is_(True), User.is_active.is_(True)))).scalars().all())

    creds = await _system_credentials(session, admins)
    tokens = (await session.execute(
        select(APIToken, User).join(User, User.id == APIToken.user_id)
        .where(APIToken.revoked_at.is_(None), User.is_active.is_(True))
    )).all()
    for tok, owner in tokens:
        creds.append(Credential(tok.id, "api_token", tok.name, tok.expires_at, "/account/api-tokens", [owner]))

    state = await _load_state(session)
    live: dict[str, int] = {}
    notified = 0
    for c in creds:
        if c.expires_at < now - timedelta(days=1):
            continue        # 早就過期的（例如很久以前的 API 權杖）不再補發，只在到期前後提醒
        bkt = bucket(c.expires_at, now)
        key = str(c.object_id)
        if key in state:
            live[key] = state[key]
        if bkt is None or not c.recipients or state.get(key) == bkt:
            continue
        live[key] = bkt
        expired = c.expires_at <= now
        days = max(math.ceil((c.expires_at - now).total_seconds() / 86400), 0)
        params = {"kind": c.kind, "name": c.name, "date": c.expires_at.date().isoformat(),
                  "days": days, "bucket": bkt}
        title = f"{'已過期' if expired else '即將到期'}：{c.name}"
        body = (f"{c.name} {'已於' if expired else '將於'} {params['date']} 到期"
                f"{'' if expired else f'（剩 {days} 天）'}，請到設定頁產生新的。")
        if ch.get("in_app"):
            for u in c.recipients:
                await push_notification(
                    session, user_id=u.id, title=title, body=body,
                    severity="error" if expired else "warning", link=c.link,
                    object_type="credential", object_id=c.object_id,
                    title_key="notif.credential_expired" if expired else "notif.credential_expiring",
                    body_key="notif.credential_expired_body" if expired else "notif.credential_expiring_body",
                    params=params)
        if ch.get("email"):
            await email_users(session, [u.email for u in c.recipients if u.email], f"[jt-ipam] {title}", body)
        notified += 1
    # 只留目前還存在的憑證（換新或刪掉的就不再佔位）
    if live != state:
        await _save_state(session, live)
    return {"checked": len(creds), "notified": notified}
