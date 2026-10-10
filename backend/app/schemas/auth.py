"""認證 schemas。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.schemas.base import StrictModel


class LoginRequest(StrictModel):
    username: Annotated[str, Field(min_length=1, max_length=128)]
    password: Annotated[str, Field(min_length=1, max_length=256)]
    realm: Annotated[str, Field(max_length=32)] = "local"


class TokenResponse(StrictModel):
    """登入 / refresh 成功的回應；若 user 有開 TOTP 則回傳 mfa_required + mfa_token。

    更新權杖不在回應本文裡：它只放在 HttpOnly Cookie（`jt_refresh`，JavaScript 讀不到）。
    `refresh_token` 欄位留著是為了相容，一律是 null。"""

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 — OAuth token_type 字面值，非密碼
    expires_in: int | None = None  # seconds

    # MFA 挑戰（僅 login 第一步成功且 user 有 TOTP 時設定）
    mfa_required: bool = False
    mfa_token: str | None = None
    # 政策要求 MFA 但這個帳號還沒設定：用 mfa_token 走 /auth/mfa/setup/* 設定完才發工作階段
    mfa_setup_required: bool = False
    # 剛設定好 TOTP 時的復原碼（明文只出現這一次）
    recovery_codes: list[str] | None = None


class SessionRead(StrictModel):
    """自己的登入工作階段（「登入中的裝置」）。"""

    id: str
    created_at: str
    last_used_at: str
    expires_at: str
    ip: str | None
    user_agent: str | None
    method: str
    mfa: bool
    current: bool


class TotpDisableRequest(StrictModel):
    """停用 TOTP 需要「升級驗證」（step-up）—— 光有 session 不夠。

    本機帳號給 `password`；外部認證帳號（LDAP / OIDC / SAML，本地沒有密碼雜湊）
    改給當前的 6 位數 `code`。兩者任一驗過即可，但不能兩者都不給。
    """

    password: Annotated[str | None, Field(default=None, max_length=256)] = None
    code: Annotated[str | None, Field(default=None, min_length=6, max_length=6)] = None


class ReauthRequest(TotpDisableRequest):
    """需要升級驗證的操作（重新產生復原碼）：同 TotpDisableRequest。"""


class ChangePasswordRequest(StrictModel):
    """本機帳號自助變更密碼（外部 IdP / LDAP 帳號不適用）。"""

    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Annotated[str, Field(min_length=12, max_length=256)]
