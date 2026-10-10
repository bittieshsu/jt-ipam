"""MFA 政策與復原碼（2026-10-09 起）。

以前 TOTP 是個人選用，管理員沒辦法要求任何人開；裝置遺失只能請管理員到資料庫清欄位。
- 政策（管理區「登入安全」）：不要求／管理員必須／所有人必須。被要求但還沒設定的帳號，
  登入時先完成 TOTP 設定才拿得到工作階段（不會被鎖在外面）
- SSO（OIDC／SAML）預設信任身分提供者自己的 MFA；勾選「SSO 登入也要求」才套用同一套
- 復原碼：啟用 TOTP 時產生 10 組，各用一次，只存 argon2 雜湊；可以重新產生（舊的全部作廢）
- 管理員可以重設某人的 MFA（清掉 TOTP 與復原碼、撤銷所有工作階段），留稽核

設定存 system_settings[auth_policy]。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_one_time_code, verify_password
from app.models.user import User
from app.models.user_session import UserRecoveryCode

POLICY_KEY = "auth_policy"
POLICIES = ("off", "admins", "all")
RECOVERY_COUNT = 10
#: 復原碼字母表：去掉容易看錯的 0/O、1/I/L
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_RECOVERY_RE = re.compile(r"^[A-Z0-9]{5}-?[A-Z0-9]{5}$")


@dataclass
class AuthPolicy:
    mfa_required: str = "off"           # off / admins / all
    mfa_apply_to_sso: bool = False


async def get_policy(session: AsyncSession) -> AuthPolicy:
    from app.models.system_setting import SystemSetting

    row = await session.get(SystemSetting, POLICY_KEY)
    v: dict[str, Any] = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    req = v.get("mfa_required")
    return AuthPolicy(mfa_required=req if req in POLICIES else "off",
                      mfa_apply_to_sso=bool(v.get("mfa_apply_to_sso", False)))


async def set_policy(session: AsyncSession, policy: AuthPolicy, *, updated_by: uuid.UUID | None) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting

    value = {"mfa_required": policy.mfa_required, "mfa_apply_to_sso": policy.mfa_apply_to_sso}
    row = await session.get(SystemSetting, POLICY_KEY)
    if row is None:
        session.add(SystemSetting(key=POLICY_KEY, value=value, updated_by=updated_by))
    else:
        row.value = value
        row.updated_by = updated_by
        flag_modified(row, "value")
    await session.flush()


def required_for(policy: AuthPolicy, user: User) -> bool:
    if policy.mfa_required == "all":
        return True
    return policy.mfa_required == "admins" and bool(user.is_admin)


def applies_to_method(policy: AuthPolicy, method: str) -> bool:
    """這種登入方式要不要走 jt-ipam 的 MFA（SSO 預設交給身分提供者）。"""
    return method not in ("oidc", "saml") or policy.mfa_apply_to_sso


def looks_like_recovery_code(code: str) -> bool:
    return bool(_RECOVERY_RE.match(code.strip().upper()))


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def _norm(code: str) -> str:
    return code.strip().upper().replace("-", "")


async def regenerate_recovery_codes(session: AsyncSession, user: User) -> list[str]:
    """產生新的 10 組（舊的全部作廢）；明文只回這一次。"""
    await session.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id))
    codes = [_new_code() for _ in range(RECOVERY_COUNT)]
    session.add_all(UserRecoveryCode(user_id=user.id, code_hash=hash_one_time_code(_norm(c))) for c in codes)
    await session.flush()
    return codes


async def use_recovery_code(session: AsyncSession, user: User, code: str) -> bool:
    """對上任何一組還沒用過的 → 標成已用、回 True。"""
    if not looks_like_recovery_code(code):
        return False
    rows = (await session.execute(
        select(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id, UserRecoveryCode.used_at.is_(None))
    )).scalars().all()
    norm = _norm(code)
    for r in rows:
        if verify_password(norm, r.code_hash):
            r.used_at = datetime.now(UTC)
            await session.flush()
            return True
    return False


async def remaining_recovery_codes(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int((await session.execute(
        select(func.count()).select_from(UserRecoveryCode)
        .where(UserRecoveryCode.user_id == user_id, UserRecoveryCode.used_at.is_(None)))).scalar_one())


async def clear_recovery_codes(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user_id))
