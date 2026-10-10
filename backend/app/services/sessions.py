"""登入工作階段：建立、換發、撤銷（2026-10-09 起；資料表見 models/user_session）。

以前更新權杖是不受追蹤的 JWT：14 天內一直有效，登出什麼都不做，改密碼、停用帳號也收不回。
現在：
- 更新權杖是 `<sid>.<隨機字串>`，只放在 HttpOnly Cookie（JavaScript 讀不到），資料庫只存雜湊
- 存取權杖帶 `sid`；每個請求都確認那個工作階段還在（dependencies.get_current_user）
- 每次換發都換一把新的更新權杖；上一把 60 秒內還能用（多個分頁同時換發），超過還拿舊的來
  ＝被偷用了 → 整個工作階段撤銷並留稽核
- 登出撤銷這一個；改密碼撤銷其他的；強制登出／停用／管理員重設密碼撤銷全部，並設
  `users.tokens_valid_after`（連不帶 sid 的舊存取權杖一起失效）
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user import User
from app.models.user_session import UserSession

#: Cookie 名稱與路徑：只送到認證端點（換發、登出），其他 API 不帶
REFRESH_COOKIE = "jt_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"
#: 多個分頁同時換發時，上一把更新權杖的寬限秒數
ROTATION_GRACE = timedelta(seconds=60)
#: 用 Cookie 換發／登出必須帶的標頭（SameSite=Strict 之外再一道 CSRF 防護）
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "jt-ipam"


class SessionInvalid(Exception):
    """更新權杖無效、過期、被撤銷，或偵測到重用。`reason` 寫進稽核。"""

    def __init__(self, reason: str, *, user_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _lifetime() -> timedelta:
    return timedelta(days=get_settings().refresh_token_expire_days)


def _client(request: Any) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    ip = request.client.host if getattr(request, "client", None) else None
    ua = (request.headers.get("user-agent") or "")[:300] or None
    return ip, ua


@dataclass
class Issued:
    session: UserSession
    refresh_token: str      # 給 Cookie 用的明文（只此一次）


async def create_session(session: AsyncSession, user: User, request: Any, *, method: str,
                         mfa: bool) -> Issued:
    secret = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    ip, ua = _client(request)
    row = UserSession(user_id=user.id, refresh_hash=_hash(secret), created_at=now, last_used_at=now,
                      expires_at=now + _lifetime(), method=method[:16], mfa=mfa, ip=ip, user_agent=ua)
    session.add(row)
    await session.flush()
    return Issued(row, f"{row.id}.{secret}")


def _parse(raw: str | None) -> tuple[uuid.UUID, str]:
    if not raw or "." not in raw:
        raise SessionInvalid("malformed")
    sid, secret = raw.split(".", 1)
    try:
        return uuid.UUID(sid), secret
    except ValueError as exc:
        raise SessionInvalid("malformed") from exc


async def rotate(session: AsyncSession, raw: str | None, request: Any) -> tuple[User, Issued]:
    """用更新權杖換一把新的。重用（超過寬限還拿舊的來）→ 撤銷整個工作階段後丟 SessionInvalid。"""
    sid, secret = _parse(raw)
    row = await session.get(UserSession, sid, with_for_update=True)
    if row is None:
        raise SessionInvalid("unknown")
    now = datetime.now(UTC)
    if row.revoked_at is not None:
        raise SessionInvalid("revoked", user_id=row.user_id)
    if row.expires_at <= now:
        raise SessionInvalid("expired", user_id=row.user_id)
    h = _hash(secret)
    if h == row.refresh_hash:
        pass
    elif (h == row.prev_refresh_hash and row.rotated_at is not None
          and now - row.rotated_at <= ROTATION_GRACE):
        pass          # 另一個分頁同時換發：寬限內照樣換（兩邊最後都會拿到最新的 Cookie）
    else:
        row.revoked_at = now
        row.revoked_reason = "refresh_reuse"
        await session.flush()
        raise SessionInvalid("reuse", user_id=row.user_id)
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise SessionInvalid("inactive", user_id=row.user_id)
    new_secret = secrets.token_urlsafe(32)
    # 上一把＝換發前的「目前」那一把：兩個分頁同時換發時，瀏覽器最後存下的可能是任何一個回應
    # 給的 Cookie，兩把都要能用
    row.prev_refresh_hash = row.refresh_hash
    row.refresh_hash = _hash(new_secret)
    row.rotated_at = now
    row.last_used_at = now
    row.expires_at = now + _lifetime()
    ip, ua = _client(request)
    row.ip, row.user_agent = ip or row.ip, ua or row.user_agent
    await session.flush()
    return user, Issued(row, f"{row.id}.{new_secret}")


async def session_alive(session: AsyncSession, user: User, sid: Any) -> bool:
    """存取權杖帶的 sid 是否仍是這個使用者有效的工作階段。"""
    try:
        sid_uuid = sid if isinstance(sid, uuid.UUID) else uuid.UUID(str(sid))
    except ValueError:
        return False
    row = (await session.execute(
        select(UserSession.user_id, UserSession.revoked_at, UserSession.expires_at)
        .where(UserSession.id == sid_uuid))).first()
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        return False
    return row.expires_at > datetime.now(UTC)


def token_issued_too_early(user: User, iat: Any) -> bool:
    """存取權杖的簽發時間早於 users.tokens_valid_after（強制登出等）→ True。"""
    cutoff = user.tokens_valid_after
    if cutoff is None:
        return False
    try:
        return int(iat) < int(cutoff.timestamp())
    except (TypeError, ValueError):
        return True


async def revoke(session: AsyncSession, sid: Any, *, reason: str, user_id: uuid.UUID | None = None) -> bool:
    try:
        sid_uuid = sid if isinstance(sid, uuid.UUID) else uuid.UUID(str(sid))
    except ValueError:
        return False
    stmt = (update(UserSession).where(UserSession.id == sid_uuid, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revoked_reason=reason[:64]))
    if user_id is not None:
        stmt = stmt.where(UserSession.user_id == user_id)
    res = await session.execute(stmt)
    return bool(res.rowcount)


async def revoke_all(session: AsyncSession, user: User, *, reason: str, keep_sid: Any = None,
                     cutoff: bool = False) -> int:
    """撤銷這個使用者的工作階段（`keep_sid` 那一個除外）。`cutoff=True` 時連舊的存取權杖一起失效。"""
    stmt = (update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revoked_reason=reason[:64]))
    if keep_sid is not None:
        stmt = stmt.where(UserSession.id != uuid.UUID(str(keep_sid)))
    res = await session.execute(stmt)
    if cutoff:
        user.tokens_valid_after = datetime.now(UTC)
    return int(res.rowcount or 0)


async def cookie_matches(session: AsyncSession, raw: str | None) -> bool:
    """Cookie 裡的更新權杖是否對得上它聲稱的工作階段（目前或寬限內的上一把）。"""
    try:
        sid, secret = _parse(raw)
    except SessionInvalid:
        return False
    row = await session.get(UserSession, sid)
    if row is None:
        return False
    h = _hash(secret)
    return h == row.refresh_hash or h == row.prev_refresh_hash


async def owner_of(session: AsyncSession, sid: Any) -> uuid.UUID | None:
    try:
        row = await session.get(UserSession, uuid.UUID(str(sid)))
    except ValueError:
        return None
    return row.user_id if row is not None else None


async def list_active(session: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
    now = datetime.now(UTC)
    return list((await session.execute(
        select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
                                  UserSession.expires_at > now)
        .order_by(UserSession.last_used_at.desc()))).scalars().all())


def set_refresh_cookie(response: Any, issued: Issued) -> None:
    response.set_cookie(
        REFRESH_COOKIE, issued.refresh_token, max_age=int(_lifetime().total_seconds()),
        path=REFRESH_COOKIE_PATH, secure=True, httponly=True, samesite="strict")


def clear_refresh_cookie(response: Any) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, secure=True, httponly=True,
                           samesite="strict")
