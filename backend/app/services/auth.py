"""認證服務：密碼登入、帳號鎖定、JWT。

OWASP 對應：
- A02：密碼用 argon2id（core.security.hash_password），自動 rehash
- A07：失敗計數 + 暫時鎖定（MFA 驗證碼錯誤也算）、存取權杖 15 分鐘、更新權杖每次換發都換新
  （伺服器端工作階段，見 services/sessions）
- A09：所有 login 嘗試（成功/失敗）寫入 audit log
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import append_audit
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.models.user import User
from app.services import ldap_auth, radius_auth
from app.services.system_config import get_ldap_config

# A07：lockout 政策
_MAX_FAILED_ATTEMPTS: Final[int] = 5
_LOCK_DURATION: Final[timedelta] = timedelta(minutes=15)


class AuthError(Exception):
    """所有 auth 失敗的基底例外；endpoint 層轉成 401。

    刻意統一訊息以避免 user enumeration（A07）。
    """

    public_message: str = "Invalid credentials"


class InvalidCredentials(AuthError):
    pass


class AccountLocked(AuthError):
    public_message = "Account temporarily locked"


class AccountInactive(AuthError):
    public_message = "Account is not active"


class TokenInvalid(AuthError):
    public_message = "Invalid or expired token"


async def _would_orphan_admins(session: AsyncSession, user: User) -> bool:
    """把這個人降權之後，系統會不會一個有效的管理員都不剩。

    PATCH 與 DELETE 早就有這道保護，**登入這條路徑沒有**。設了群組對應之後，
    最後一位管理員只要哪天掉出那個群組（群組改名、打錯字、目錄異動），
    下次登入就被降權 —— 從此沒有人進得了管理區，只能到伺服器上跑 CLI 救。

    目錄是唯一真相沒錯，但「一個管理員都不剩」是不可逆的鎖死；這個專案在另外兩條
    路徑上早就選了同一個答案，這裡跟著一致。
    """
    from app.models.user import User as _User

    if not (user.is_admin and user.is_active):
        return False
    count = (await session.execute(
        select(func.count()).select_from(_User).where(
            _User.is_admin.is_(True), _User.is_active.is_(True),
        )
    )).scalar_one()
    return count <= 1


async def authenticate(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    realm: str = "local",
    actor_ip: str | None,
    actor_user_agent: str | None,
    request_id: str | None,
) -> User:
    """以使用者名 / Email + 密碼驗證。

    驗證順序（A07）：
      1. 找本機 user → auth_provider 決定走哪個 backend
      2. 若 settings.ldap_enabled 且 jt-ipam 沒這個 user，嘗試 LDAP；成功則 auto-provision
      3. 若 settings.radius_enabled 且 user.auth_provider=='radius'，走 Radius

    無論成功失敗皆寫 audit；失敗的 reason 不對外顯示（防 enumeration）。
    """
    realm = (realm or "local").strip().lower()
    now = datetime.now(UTC)
    _DUMMY = ("$argon2id$v=19$m=65536,t=3,p=4$00000000000000000000000000000000$"
              "00000000000000000000000000000000000000000000")

    async def _audit(action: str, *, success: bool, reason: str | None,
                     target_user: User | None) -> None:
        await append_audit(
            session,
            actor_user_id=str(target_user.id) if target_user else None,
            actor_ip=actor_ip, actor_user_agent=actor_user_agent,
            object_type="auth",
            object_id=str(target_user.id) if target_user else None,
            action=action,
            diff={"username": username, "realm": realm, "success": success, "reason": reason},
            request_id=request_id,
        )

    async def _ensure_active(u: User) -> None:
        if not u.is_active:
            await _audit("login_failed", success=False, reason="inactive", target_user=u)
            await session.commit()
            raise AccountInactive
        if u.locked_until is not None and u.locked_until > now:
            await _audit("login_failed", success=False, reason="locked", target_user=u)
            await session.commit()
            raise AccountLocked

    async def _reject(reason: str) -> None:
        verify_password(password, _DUMMY)  # 抗 timing
        await _audit("login_failed", success=False, reason=reason, target_user=None)
        await session.commit()
        raise InvalidCredentials

    async def _bump_lock(u: User) -> None:
        await register_failure(session, u, actor_ip=actor_ip)

    # ───────────── LDAP / AD realm ─────────────
    if realm == "ldap":
        ldap_cfg = await get_ldap_config(session)
        if not ldap_cfg.enabled:
            await _reject("ldap_disabled")
        account = username.split("@")[0].strip()       # 容錯：使用者多打 @領域時去掉
        stored = f"{account}@ldap"
        # email 允許重複（同一人常同時有本機與 LDAP 帳號），所以 email 這一路
        # 必須限定在 LDAP 帳號內，否則會撈到同 email 的本機帳號去改它；
        # 且不可用 scalar_one_or_none（多筆同 email 會炸 MultipleResultsFound）
        user = (await session.execute(
            select(User).where(
                (User.username == stored)
                | ((User.email == username) & (User.auth_provider == "ldap")),
            ).order_by((User.username == stored).desc()).limit(1)   # 帳號精準命中優先
        )).scalars().first()
        if user is not None:
            await _ensure_active(user)
        try:
            info = await ldap_auth.authenticate(ldap_cfg, account, password)
        except ldap_auth.LDAPInvalidCredentials as exc:
            if user is not None:
                await _bump_lock(user)
            await _audit("login_failed", success=False, reason="ldap_invalid", target_user=user)
            await session.commit()
            raise InvalidCredentials from exc
        except (ldap_auth.LDAPNotConfigured, ldap_auth.LDAPAuthError) as exc:
            await _audit("login_failed", success=False, reason="ldap_error", target_user=user)
            await session.commit()
            raise InvalidCredentials from exc
        if user is None:
            # email 唯一：若 LDAP 回的 email 已被別的帳號（例如同名本機帳號）佔用，
            # 改用領域命名空間的 email，避免自動建帳號因 unique 衝突而失敗
            prov_email = info.email or f"{account}@ldap.local"
            if prov_email:
                taken = (await session.execute(
                    select(User.id).where(User.email == prov_email))).first()
                if taken:
                    # 依序退到領域命名空間的 email；仍撞號就加短隨機碼，
                    # 保證自動建帳號不會因 unique 衝突而整個登入失敗
                    import uuid as _uuid4

                    for cand in (f"{account}@ldap.local", f"{stored}@ldap.local",
                                 f"{account}-{_uuid4.uuid4().hex[:8]}@ldap.local"):
                        if not (await session.execute(
                                select(User.id).where(User.email == cand))).first():
                            prov_email = cand
                            break
            user = User(
                username=stored, email=prov_email,
                display_name=info.display_name, auth_provider="ldap",
                external_subject=info.dn, is_active=True, is_admin=info.is_admin,
            )
            session.add(user)
            await session.flush()
            if ldap_cfg.default_group_id:   # 預設角色（自動建立帳號）
                import uuid as _uuid

                from app.models.user import UserGroupMember
                session.add(UserGroupMember(
                    user_id=user.id, group_id=_uuid.UUID(str(ldap_cfg.default_group_id))))
            await _audit("ldap_auto_provision", success=True, reason=None, target_user=user)
        else:
        # 只有在「管理員群組對應」真的設定過時，才由目錄決定管理員身分。
        #
        # 沒設定就把 is_admin 寫成 False 是**從「沒有設定」推出「不是管理員」**：
        # 空清單的 `any(...)` 恆為 False，於是本機管理員在介面上開的權限，
        # 下一次登入就被安靜地關掉 —— 使用者看到的是「開了又自己關掉」，
        # 而且完全不知道為什麼（客戶回報）。
        #
        # 有設定 → 目錄是唯一真相（那正是設定它的用意），照舊覆寫。
            if ldap_cfg.admin_groups and not (
                user.is_admin and not info.is_admin
                and await _would_orphan_admins(session, user)
            ):
                user.is_admin = info.is_admin
            if info.display_name:
                user.display_name = info.display_name
            # email 是唯一鍵：LDAP 回的 email 常與同一人的本機帳號重複，
            # 硬寫進去會在 commit 時撞 unique 而讓整個登入回 500（帳密其實是對的）。
            # 撞號就保留原本 email，登入照常放行。
            if info.email and info.email != user.email:
                clash = (await session.execute(select(User.id).where(
                    User.email == info.email, User.id != user.id))).first()
                if not clash:
                    user.email = info.email
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        user.last_login_ip = actor_ip
        await _audit("login_success", success=True, reason="ldap", target_user=user)
        await session.commit()
        return user

    # ───────────── 本機 realm（預設）─────────────
    # 同上：email 可重複 → 以 email 登入時排除 LDAP 帳號（那要走 ldap 領域），
    # 並以帳號精準命中優先、取一筆，避免 MultipleResultsFound
    stmt = (select(User).where(
        (User.username == username)
        | ((User.email == username) & (User.auth_provider != "ldap")),
    ).order_by((User.username == username).desc()).limit(1))
    user = (await session.execute(stmt)).scalars().first()
    if user is None:
        await _reject("no_user")
    await _ensure_active(user)

    # legacy：Radius 帳號（無獨立 realm，沿用既有 provider 判定）
    if user.auth_provider == "radius":
        settings = get_settings()
        if not settings.radius_enabled:
            await _audit("login_failed", success=False, reason="radius_disabled", target_user=user)
            await session.commit()
            raise InvalidCredentials
        try:
            await radius_auth.authenticate(username, password)
        except (radius_auth.RadiusInvalidCredentials, radius_auth.RadiusAuthError) as exc:
            await _bump_lock(user)
            await _audit("login_failed", success=False, reason="radius_reject", target_user=user)
            await session.commit()
            raise InvalidCredentials from exc
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        user.last_login_ip = actor_ip
        await _audit("login_success", success=True, reason="radius", target_user=user)
        await session.commit()
        return user

    # 外部帳號（ldap 等）不可走本機 realm
    if user.auth_provider != "local":
        await _reject("wrong_realm")

    target_hash = user.password_hash or _DUMMY
    if not verify_password(password, target_hash):
        await _bump_lock(user)
        await _audit("login_failed", success=False, reason="invalid_password", target_user=user)
        await session.commit()
        raise InvalidCredentials
    if password_needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    user.last_login_ip = actor_ip
    await _audit("login_success", success=True, reason="local", target_user=user)
    await session.commit()
    return user


async def register_failure(session: AsyncSession, user: User, *, actor_ip: str | None) -> None:
    """密碼或 MFA 驗證碼錯一次：累計失敗次數，達門檻就鎖定（並留稽核）。

    MFA 驗證碼錯誤以前不算進來：知道密碼的人可以對 6 位數驗證碼無限次嘗試（只受每分鐘限流）。"""
    now = datetime.now(UTC)
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= _MAX_FAILED_ATTEMPTS:
        user.locked_until = now + _LOCK_DURATION
        # 鎖定原本只改欄位就 commit —— 「這個帳號什麼時候被鎖過、從哪個位址打的」
        # 事後完全查不到。鎖定是資安事件，一定要留下記錄。
        from app.services.security_alert import audit_lockout
        await audit_lockout(session, user=user, actor_ip=actor_ip, until=user.locked_until)


def is_locked(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > datetime.now(UTC)


def issue_access_token(user: User, *, sid: object | None = None) -> str:
    """存取權杖。正式的登入流程一律帶 `sid`（工作階段），撤銷工作階段就立即失效；
    不帶 sid 的只有測試在用（守門：tests/test_session_revocation.py 檢查 app/ 裡每個呼叫都帶 sid）。"""
    claims: dict[str, object] = {"username": user.username, "is_admin": user.is_admin, "type": "access"}
    if sid is not None:
        claims["sid"] = str(sid)
    return create_access_token(subject=str(user.id), extra_claims=claims)


def decode_token(token: str, *, expected_type: str) -> dict[str, object]:
    try:
        payload = decode_access_token(token)
    except Exception as exc:
        raise TokenInvalid from exc
    if payload.get("type") != expected_type:
        raise TokenInvalid
    return payload
