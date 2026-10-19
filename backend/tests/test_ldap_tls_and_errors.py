"""LDAP／AD：加密連線與錯誤回報（客戶 2026-10-19）。

1. 選 StartTLS／LDAPS 時出現 `EE certificate key too weak`：AD 網域控制站的憑證是 RSA 1024 位元，
   OpenSSL 預設安全等級拒絕。就算過了這關，企業內部 CA 也不在系統信任清單裡，下一個錯誤一定是
   「找不到簽發者」—— 以前設定頁沒有地方放 CA 憑證，加密連 AD 幾乎做不到。
2. 不加密時「用帳密測試」回 500：搜尋使用者與使用者 bind 階段的 ldap3 例外沒有轉成我們的錯誤型別，
   直接穿到端點變成 Internal Server Error（登入也一樣），看不到真正原因。
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from ldap3.core.exceptions import LDAPException, LDAPStartTLSError

from app.services import ldap_auth
from app.services.system_config import LdapConfig

pytestmark = pytest.mark.anyio


def _cfg(**k: Any) -> LdapConfig:
    base: dict[str, Any] = dict(
        enabled=True, server="ldap.example.com", port=389, use_ssl=False, use_starttls=True,
        bind_dn="cn=svc,dc=example,dc=com", bind_password="pw", search_base="dc=example,dc=com",
        user_filter="(sAMAccountName={username})", attr_email="mail", attr_display_name="displayName",
        attr_member_of="memberOf", admin_groups=[], timeout=5.0)
    base.update(k)
    return LdapConfig(**base)


def _pem() -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example Root CA")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def no_guard(monkeypatch):
    monkeypatch.setattr("app.core.net_guard.check_target", lambda *a, **k: None)


def test_tls_defaults_stay_strict(no_guard) -> None:
    tls = ldap_auth._build_server(_cfg()).tls
    assert tls.ca_certs_data is None
    assert not tls.ciphers


def test_tls_uses_the_ca_and_the_weak_key_opt_in(no_guard) -> None:
    pem = _pem()
    tls = ldap_auth._build_server(_cfg(ca_cert_pem=pem, allow_weak_tls=True)).tls
    assert tls.ca_certs_data == pem
    assert "SECLEVEL=1" in tls.ciphers
    import ssl
    assert tls.validate == ssl.CERT_REQUIRED          # 放寬金鑰長度，仍然要驗憑證


@pytest.mark.parametrize(("text", "code"), [
    ("wrap socket error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
     "EE certificate key too weak (_ssl.c:1081)", "ldap_tls_weak_key"),
    ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate",
     "ldap_tls_untrusted"),
    ("certificate verify failed: self-signed certificate in certificate chain", "ldap_tls_untrusted"),
    ("socket ssl wrapping error: hostname 'dc01' doesn't match", "ldap_tls_hostname"),
    ("invalid server address", "ldap_error"),
])
def test_errors_get_a_code_and_keep_the_original_text(text: str, code: str) -> None:
    err = ldap_auth._translate(LDAPStartTLSError(text))
    assert isinstance(err, ldap_auth.LDAPAuthError)
    assert err.code == code
    assert text[:60] in err.params["reason"]


def test_search_failures_are_ldap_errors_not_500(no_guard, monkeypatch) -> None:
    class _Conn:
        entries: list = []

        def search(self, *a, **k):
            raise LDAPException("search went wrong")

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    with pytest.raises(ldap_auth.LDAPAuthError) as ei:
        ldap_auth._authenticate_sync(_cfg(use_starttls=False), "user01", "pw")
    assert "search went wrong" in ei.value.params["reason"]


def test_connections_do_not_follow_referrals(no_guard, monkeypatch) -> None:
    """跟著轉介去連別台主機會繞過 net_guard 的目標檢查；AD 的根目錄搜尋也會帶轉介回來。"""
    seen: list[dict] = []

    class _Fake:
        def __init__(self, *a, **k):
            seen.append(k)
            raise LDAPException("stop here")
    monkeypatch.setattr(ldap_auth, "Connection", _Fake)
    with pytest.raises(ldap_auth.LDAPAuthError):
        ldap_auth._bind_admin_sync(_cfg())
    assert seen
    assert seen[0]["auto_referrals"] is False


async def test_test_auth_never_returns_500(client, auth_headers, monkeypatch) -> None:
    async def boom(cfg, u, p):
        raise RuntimeError("unexpected thing")
    monkeypatch.setattr(ldap_auth, "authenticate", boom)
    r = await client.post("/api/v1/system/ldap/test-auth", json={"username": "user01", "password": "x"},
                          headers=auth_headers)
    assert r.status_code == 502, r.text
    d = r.json()["detail"]
    assert d["code"] == "ldap_error"
    assert "RuntimeError" in d["params"]["reason"]

    async def weak(cfg, u, p):
        raise ldap_auth._translate(LDAPStartTLSError("certificate verify failed: EE certificate key too weak"))
    monkeypatch.setattr(ldap_auth, "authenticate", weak)
    r = await client.post("/api/v1/system/ldap/test-auth", json={"username": "user01", "password": "x"},
                          headers=auth_headers)
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "ldap_tls_weak_key"


async def test_ca_and_weak_key_settings_round_trip(client, auth_headers) -> None:
    pem = _pem()
    body = {"enabled": False, "server": "dc01.example.com", "port": 389, "use_ssl": False, "use_starttls": True,
            "ca_cert_pem": pem, "allow_weak_tls": True}
    r = await client.put("/api/v1/system/ldap", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    got = (await client.get("/api/v1/system/ldap", headers=auth_headers)).json()
    assert got["ca_cert_pem"].strip() == pem.strip()
    assert got["allow_weak_tls"] is True
    assert "Example Root CA" in (got.get("ca_cert_subjects") or [""])[0]

    bad = {**body, "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nnot a cert\n-----END CERTIFICATE-----"}
    r = await client.put("/api/v1/system/ldap", json=bad, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ldap_ca_invalid"

    r = await client.put("/api/v1/system/ldap", json={**body, "ca_cert_pem": "", "allow_weak_tls": False},
                         headers=auth_headers)
    got = r.json()
    assert not got["ca_cert_pem"]
    assert got["allow_weak_tls"] is False


def test_hostname_check_accepts_an_ip_san() -> None:
    """Python 3.12 拿掉 ssl.match_hostname，ldap3 退回只看 DNS 名稱的舊版：用 IP 連線時，憑證明明有
    IP SAN 也永遠「名稱不符」。"""
    import ldap3.core.tls as l3tls
    cert = {"subject": ((("commonName", "dc01.example.com"),),),
            "subjectAltName": (("DNS", "dc01.example.com"), ("IP Address", "192.0.2.10"))}
    ldap_auth._match_hostname(cert, "192.0.2.10")
    ldap_auth._match_hostname(cert, "dc01.example.com")
    with pytest.raises(l3tls.CertificateError):
        ldap_auth._match_hostname(cert, "192.0.2.11")
    with pytest.raises(l3tls.CertificateError):
        ldap_auth._match_hostname(cert, "dc02.example.com")
    assert l3tls.match_hostname is ldap_auth._match_hostname
