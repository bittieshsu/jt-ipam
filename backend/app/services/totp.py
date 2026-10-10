"""TOTP MFA：enroll、confirm、verify、disable。

OWASP A04 / A07：
- secret 用 AES-256-GCM 加密儲存（aad 綁定 user.id）
- 啟用 / 停用都寫 audit
- 防重放：記下最後一次用過的時間步（users.totp_last_step），同一組（或更早的）驗證碼不能再用
  （2026-10-09 之前這行只是註解，實際上 90 秒內同一組碼可以重複使用）
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import UTC, datetime

import pyotp
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.models.user import User

_ISSUER = "jt-ipam"


def _aad(user_id: uuid.UUID) -> bytes:
    return f"user:{user_id}:totp".encode()


def is_enabled(user: User) -> bool:
    return user.totp_secret_enc is not None and user.totp_nonce is not None


def begin_enrollment() -> str:
    """產生新的 base32 secret（呼叫者尚未持久化，需 confirm）。"""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=_ISSUER)


async def confirm_enrollment(
    session: AsyncSession,
    *,
    user: User,
    secret: str,
    code: str,
) -> bool:
    """用 user 提交的 6-digit code 驗證 secret 正確；正確才寫入 DB。

    回傳 True 表成功（已 commit）；False 表 code 錯誤（未寫入）。
    """
    step = match_step(secret, code)
    if step is None:
        return False

    ciphertext, nonce = encrypt_secret(secret, aad=_aad(user.id))
    user.totp_secret_enc = ciphertext
    user.totp_nonce = nonce
    user.totp_last_step = step
    await session.commit()
    return True


async def disable(session: AsyncSession, *, user: User) -> None:
    user.totp_secret_enc = None
    user.totp_nonce = None
    user.totp_last_step = None
    from app.services.mfa import clear_recovery_codes
    await clear_recovery_codes(session, user.id)
    await session.commit()


def match_step(secret: str, code: str, now: datetime | None = None) -> int | None:
    """驗證碼對上哪一個時間步（容許前後各一步的時鐘誤差）；對不上回 None。"""
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return None
    t = pyotp.TOTP(secret)
    base = t.timecode(now or datetime.now(UTC))
    for off in (0, -1, 1):
        if hmac.compare_digest(t.generate_otp(base + off), code):
            return base + off
    return None


async def verify_code(user: User, code: str) -> bool:
    """以使用者持久化的 secret 驗證 code，並記下用過的時間步（呼叫端負責 commit）。

    同一個時間步或更早的碼一律拒絕：被側錄的驗證碼在有效的 90 秒內不能拿來再登入一次。"""
    if not is_enabled(user):
        return False
    secret = decrypt_secret(
        user.totp_secret_enc,  # type: ignore[arg-type]
        user.totp_nonce,        # type: ignore[arg-type]
        aad=_aad(user.id),
    ).decode("utf-8")
    step = match_step(secret, code)
    if step is None:
        return False
    if user.totp_last_step is not None and step <= user.totp_last_step:
        return False
    user.totp_last_step = step
    return True


def issue_mfa_challenge(user: User, *, purpose: str = "mfa_challenge", method: str = "local") -> str:
    """登入第一步成功後發給 client 的短期 token。

    - `mfa_challenge`（5 分鐘）：已啟用 TOTP → 第二步送驗證碼或復原碼
    - `mfa_setup`（10 分鐘）：政策要求 MFA 但還沒設定 → 先完成 TOTP 設定才發工作階段
    `method` 是第一步的登入方式（local／ldap／radius／oidc／saml），建立工作階段時記下來。
    """
    from app.core.security import create_access_token

    return create_access_token(
        subject=str(user.id),
        extra_claims={"type": purpose, "method": method, "jti": secrets.token_urlsafe(8)},
        expires_in_minutes=10 if purpose == "mfa_setup" else 5,
    )
