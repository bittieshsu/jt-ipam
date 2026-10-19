"""LDAP / AD 認證。

phpIPAM 缺點：LDAP 設定散落、錯誤訊息不清、不支援 LDAPS+StartTLS 同時誤用。
jt-ipam：設定走 DB（管理區 UI）覆蓋 env；bind password 應用層加密；test endpoint。

OWASP 對應：
- A02：bind password 加密存 DB（AES-GCM）；TLS 預設啟用；不接受 verify=False
- A03：username 透過 ldap3.utils.conv.escape_filter_chars 跳脫 LDAP filter
- A07：認證失敗都回統一訊息；rate limit 沿用 /auth/login 的 auth bucket
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ldap3 import (
    ALL,
    SUBTREE,
    Connection,
    Server,
    Tls,
)
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.core.ui_error import UiError

if TYPE_CHECKING:
    from app.services.system_config import LdapConfig

log = logging.getLogger("ldap_auth")


class LDAPAuthError(UiError):
    pass


class LDAPNotConfigured(LDAPAuthError):
    pass


class LDAPInvalidCredentials(LDAPAuthError):
    pass


def _match_hostname(cert: dict[str, Any], hostname: str) -> None:
    """憑證名稱比對：IP 比對 IP SAN，其他交給 ldap3 原本的比對。

    Python 3.12 拿掉了 ssl.match_hostname，ldap3 退回自帶的舊版，只看 DNS 名稱 —— 伺服器欄位填 IP 時，
    憑證就算有 IP SAN 也永遠「名稱不符」（2026-10-19 用測試靶查到）。"""
    import ipaddress
    try:
        ip = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        _l3_match_hostname(cert, hostname)
        return
    for kind, value in cert.get("subjectAltName", ()):
        if kind == "IP Address":
            try:
                if ipaddress.ip_address(value.strip()) == ip:
                    return
            except ValueError:
                continue
    # 要丟 ldap3 自己的 CertificateError：它的 check_hostname 只攔這個類別，才會繼續試其他名稱、回報「名稱不符」
    raise _l3tls.CertificateError(f"hostname {hostname!r} doesn't match any IP address in the certificate")


import ldap3.core.tls as _l3tls  # noqa: E402

_l3_match_hostname = _l3tls.match_hostname
# ldap3 的 check_hostname 用模組層級的 match_hostname：換成會比對 IP SAN 的版本（只影響 LDAP 連線）
_l3tls.match_hostname = _match_hostname

#: 「允許較弱的憑證金鑰」打開時用的 OpenSSL 安全等級：1 接受 RSA 1024 位元（舊版 AD 網域控制站常見），
#: 憑證鏈與名稱照樣驗證。預設（等級 2）要求 2048 位元以上。
_WEAK_TLS_CIPHERS = "DEFAULT:@SECLEVEL=1"


def _translate(exc: BaseException, prefix: str = "LDAP") -> LDAPAuthError:
    """ldap3／ssl 的例外 → 帶代碼的 LDAPAuthError（前端翻譯成看得懂的說明，原文放在 reason）。

    以前只有 admin bind 有轉，搜尋使用者與使用者 bind 的例外直接穿到端點變成 500，
    畫面只剩「Internal Server Error」（客戶 2026-10-19）。
    """
    if isinstance(exc, LDAPAuthError):
        return exc
    reason = f"{exc.__class__.__name__}: {exc}"[:400]
    low = reason.lower()
    if "key too weak" in low or "ee_key_too_small" in low or "ca_key_too_small" in low:
        code = "ldap_tls_weak_key"
    elif ("unable to get local issuer" in low or "self-signed certificate" in low
          or "self signed certificate" in low or "unable to verify the first certificate" in low):
        code = "ldap_tls_untrusted"
    elif "doesn't match" in low or "hostname mismatch" in low or "not valid for" in low:
        code = "ldap_tls_hostname"
    else:
        code = "ldap_error"
    return LDAPAuthError(f"{prefix} error: {reason}", code=code, reason=reason)


@dataclass
class LDAPUserInfo:
    dn: str
    username: str
    email: str | None
    display_name: str | None
    is_admin: bool   # 透過 admin_groups 判定
    raw_attrs: dict[str, Any]


def _build_server(cfg: LdapConfig) -> Server:
    if not cfg.enabled or not cfg.server:
        raise LDAPNotConfigured("LDAP not configured")

    # 連線前檢查目標位址（core/net_guard）；TLS 要驗憑證名稱，所以 Server 仍給主機名稱
    from app.core.net_guard import check_target
    from app.core.safe_http import UnsafeOutboundURL
    try:
        check_target(cfg.server, cfg.port)
    except UnsafeOutboundURL as exc:
        raise LDAPAuthError(str(exc)) from exc
    # 企業內部 CA（AD 憑證服務）不在系統信任清單裡：有設定就只信任它（ca_certs_data 取代系統清單）
    tls = Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLS_CLIENT,
              ca_certs_data=cfg.ca_cert_pem or None,
              ciphers=_WEAK_TLS_CIPHERS if cfg.allow_weak_tls else None)
    return Server(
        host=cfg.server,
        port=cfg.port,
        use_ssl=cfg.use_ssl,
        tls=tls,
        get_info=ALL,
        connect_timeout=int(cfg.timeout),
    )


def _bind_admin_sync(cfg: LdapConfig) -> Connection:
    server = _build_server(cfg)
    try:
        return _bind_admin_conn(cfg, server)
    except LDAPAuthError:
        raise
    except LDAPException as exc:
        raise _translate(exc, "LDAP bind") from exc


def _bind_admin_conn(cfg: LdapConfig, server: Server) -> Connection:
    conn = Connection(
        server,
        user=cfg.bind_dn,
        password=cfg.bind_password,
        auto_bind=False,
        receive_timeout=int(cfg.timeout),
        raise_exceptions=True,
        # 不跟著轉介去連別台：那會繞過 net_guard 的目標檢查（AD 根目錄搜尋常帶轉介回來）
        auto_referrals=False,
    )
    try:
        if cfg.use_starttls and not cfg.use_ssl:
            conn.open()
            conn.start_tls()
        ok = conn.bind()
    except LDAPException as exc:
        # ldap3 在 raise_exceptions=True 時對 bind 失敗丟原生例外（含 AD 帳密錯誤）；轉成我們的型別
        msg = str(exc)
        if "invalidCredentials" in msg or "data 52e" in msg or "InvalidCredentials" in exc.__class__.__name__:
            raise LDAPInvalidCredentials(f"bind rejected（帳號或密碼錯誤）：{msg}",
                                         code="ldap_invalid_credentials",
                                         reason=msg[:200]) from exc
        raise _translate(exc, "LDAP bind") from exc
    if not ok:
        raise LDAPAuthError(f"LDAP admin bind failed: {conn.last_error}", code="ldap_error",
                            reason=f"admin bind: {conn.last_error}")
    return conn


def _authenticate_sync(cfg: LdapConfig, username: str, password: str) -> LDAPUserInfo:
    if not cfg.search_base:
        raise LDAPNotConfigured("LDAP search base not set")

    safe_username = escape_filter_chars(username)
    user_filter = cfg.user_filter.format(username=safe_username)

    # 1. admin bind 找出使用者 DN + 屬性
    conn = _bind_admin_sync(cfg)
    try:
        try:
            conn.search(
                search_base=cfg.search_base,
                search_filter=user_filter,
                search_scope=SUBTREE,
                attributes=[cfg.attr_email, cfg.attr_display_name,
                            cfg.attr_member_of, "cn"],
                time_limit=int(cfg.timeout),
            )
        except LDAPException as exc:
            raise _translate(exc, "LDAP search") from exc
        if not conn.entries:
            raise LDAPInvalidCredentials("user not found")
        if len(conn.entries) > 1:
            raise LDAPAuthError("multiple users matched filter")
        entry = conn.entries[0]
        user_dn = entry.entry_dn
        attrs = entry.entry_attributes_as_dict
    finally:
        try:
            conn.unbind()
        except LDAPException:
            pass

    # 2. 用 user DN + 密碼 bind 驗證
    server = _build_server(cfg)
    user_conn = Connection(
        server,
        user=user_dn,
        password=password,
        auto_bind=False,
        receive_timeout=int(cfg.timeout),
        raise_exceptions=False,
        auto_referrals=False,
    )
    try:
        if cfg.use_starttls and not cfg.use_ssl:
            user_conn.open()
            if not user_conn.start_tls():
                raise LDAPAuthError("LDAP user StartTLS failed", code="ldap_error",
                                    reason=f"user StartTLS: {user_conn.last_error}")
        bound = user_conn.bind()
    except LDAPException as exc:
        raise _translate(exc, "LDAP user bind") from exc
    try:
        if not bound:
            raise LDAPInvalidCredentials("invalid password")
    finally:
        try:
            user_conn.unbind()
        except LDAPException:
            pass

    # 3. 解析屬性
    email = (attrs.get(cfg.attr_email) or [None])[0]
    display_name = (attrs.get(cfg.attr_display_name) or [None])[0]
    groups = attrs.get(cfg.attr_member_of) or []
    is_admin = any(g in cfg.admin_groups for g in groups)

    return LDAPUserInfo(
        dn=user_dn,
        username=username,
        email=email,
        display_name=display_name,
        is_admin=is_admin,
        raw_attrs=attrs,
    )


async def authenticate(cfg: LdapConfig, username: str, password: str) -> LDAPUserInfo:
    """非同步入口；ldap3 同步呼叫包進 thread executor。"""
    if not cfg.enabled:
        raise LDAPNotConfigured("LDAP is disabled")
    return await asyncio.to_thread(_authenticate_sync, cfg, username, password)


async def test_connection(cfg: LdapConfig) -> dict[str, Any]:
    """admin bind 測試 — 不需要任何使用者密碼。"""
    if not cfg.enabled:
        raise LDAPNotConfigured("LDAP is disabled")

    def _go() -> dict[str, Any]:
        conn = _bind_admin_sync(cfg)
        try:
            return {
                "bound": True,
                "server": cfg.server,
                "port": cfg.port,
                "tls": "ssl" if cfg.use_ssl else "starttls" if cfg.use_starttls else "none",
                "who_am_i": conn.extend.standard.who_am_i(),
            }
        finally:
            try:
                conn.unbind()
            except LDAPException:
                pass

    return await asyncio.to_thread(_go)
