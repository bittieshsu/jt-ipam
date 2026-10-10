"""TOTP MFA schemas。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.schemas.base import StrictModel


class EnrollResponse(StrictModel):
    """啟用 TOTP 第一步：回傳 secret 與 otpauth:// URI 給 user 掃 QR code。

    secret 也回給 user 是因為 enrollment 還未確認；確認 code 對才會寫進 DB。
    """

    secret: str
    otpauth_uri: str


class ConfirmRequest(StrictModel):
    secret: Annotated[str, Field(min_length=16, max_length=64)]  # 在 enroll 拿到的 secret
    code: Annotated[str, Field(pattern=r"^\d{6}$")]


class VerifyRequest(StrictModel):
    """登入第二步使用：mfa_token（從 login 回的 challenge）+ 6 位數驗證碼或一組復原碼（XXXXX-XXXXX）。"""

    mfa_token: Annotated[str, Field(min_length=8, max_length=4096)]
    code: Annotated[str, Field(pattern=r"^(\d{6}|[A-Za-z0-9]{5}-?[A-Za-z0-9]{5})$")]


class MfaTokenRequest(StrictModel):
    """政策要求 MFA、還沒設定的帳號：用登入第一步拿到的 mfa_token 開始設定。"""

    mfa_token: Annotated[str, Field(min_length=8, max_length=4096)]


class MfaSetupConfirmRequest(StrictModel):
    mfa_token: Annotated[str, Field(min_length=8, max_length=4096)]
    secret: Annotated[str, Field(min_length=16, max_length=64)]
    code: Annotated[str, Field(pattern=r"^\d{6}$")]


class RecoveryCodesResponse(StrictModel):
    recovery_codes: list[str]
