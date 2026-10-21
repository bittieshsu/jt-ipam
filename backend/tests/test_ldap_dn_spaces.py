"""LDAP／AD：DN 逗號後面有空白（客戶 2026-10-21）。

AD 的「識別名稱」常被寫成 `CN=svc, CN=Users, DC=corp, DC=example`（從工具複製、或手打時習慣加空白）。
AD 本身接受，所以「測試連線」（只做服務帳號 bind）會過；但「用帳密測試」要搜尋使用者，ldap3 會先檢查
搜尋起點的格式，報 `LDAPInvalidDnError: character ' ' not allowed in attribute type`。

這裡守的是：
1. 用到 DN 的地方（服務帳號、搜尋起點、管理員群組）一律先正規化：拿掉逗號與等號兩邊的空白，
   值裡面的空白（`CN=John Smith`）與跳脫的逗號（`CN=Smith\\, John`）照原樣。
2. 管理員群組比對不受空白與大小寫影響（AD 回來的 memberOf 不會有逗號後的空白）。
3. 篩選條件是 OpenLDAP 的 `(uid=...)` 而找不到人時，提示 AD 要用 sAMAccountName。
"""
from __future__ import annotations

from typing import Any

import pytest
from app.services import ldap_auth
from app.services.system_config import LdapConfig

pytestmark = pytest.mark.anyio


def _cfg(**k: Any) -> LdapConfig:
    base: dict[str, Any] = dict(
        enabled=True, server="dc1.corp.example", port=389, use_ssl=False, use_starttls=False,
        bind_dn="CN=svc-ipam, CN=Users, DC=corp, DC=example", bind_password="pw",
        search_base="DC=corp, DC=example", user_filter="(sAMAccountName={username})",
        attr_email="mail", attr_display_name="displayName", attr_member_of="memberOf",
        admin_groups=["CN=IPAM-Admins, OU=Groups, DC=corp, DC=example"], timeout=5.0)
    base.update(k)
    return LdapConfig(**base)


@pytest.mark.parametrize(("raw", "want"), [
    ("CN=svc, CN=Users, DC=corp, DC=example", "CN=svc,CN=Users,DC=corp,DC=example"),
    (" DC = corp ,DC= example ", "DC=corp,DC=example"),
    ("CN=John Smith, OU=Staff, DC=corp", "CN=John Smith,OU=Staff,DC=corp"),          # 值裡的空白保留
    ("CN=Smith\\, John, OU=Staff", "CN=Smith\\, John,OU=Staff"),                      # 跳脫的逗號不切
    ("CN=a + UID=b, DC=corp", "CN=a+UID=b,DC=corp"),                                  # 多值 RDN
    ("", ""),
])
def test_normalize_dn(raw: str, want: str) -> None:
    assert ldap_auth.normalize_dn(raw) == want


def test_search_and_bind_use_the_normalized_dns(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Conn:
        entries: list = []

        def search(self, *a, **k):
            seen["search_base"] = k["search_base"]

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    with pytest.raises(ldap_auth.LDAPInvalidCredentials):
        ldap_auth._authenticate_sync(_cfg(), "user01", "pw")
    assert seen["search_base"] == "DC=corp,DC=example"

    class _Fake:
        def __init__(self, *a, **k):
            seen["user"] = k["user"]
            raise ldap_auth.LDAPException("stop here")
    monkeypatch.setattr(ldap_auth, "Connection", _Fake)
    with pytest.raises(ldap_auth.LDAPException):
        ldap_auth._bind_admin_conn(_cfg(), None)
    assert seen["user"] == "CN=svc-ipam,CN=Users,DC=corp,DC=example"


def test_admin_group_match_ignores_spaces_and_case(monkeypatch) -> None:
    class _Entry:
        entry_dn = "CN=user01,CN=Users,DC=corp,DC=example"
        entry_attributes_as_dict = {"memberOf": ["cn=ipam-admins,ou=Groups,dc=corp,dc=example"]}

    class _Conn:
        entries = [_Entry()]

        def search(self, *a, **k):
            pass

        def unbind(self):
            pass

    class _UserConn:
        def __init__(self, *a, **k):
            pass

        def bind(self):
            return True

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    monkeypatch.setattr(ldap_auth, "Connection", _UserConn)
    monkeypatch.setattr(ldap_auth, "_build_server", lambda cfg: None)
    info = ldap_auth._authenticate_sync(_cfg(), "user01", "pw")
    assert info.is_admin is True


async def test_uid_filter_on_ad_gets_a_hint(client, auth_headers, monkeypatch) -> None:
    async def not_found(cfg, u, p):
        raise ldap_auth.LDAPInvalidCredentials("user not found")
    monkeypatch.setattr(ldap_auth, "authenticate", not_found)

    async def cfg_uid(session):
        return _cfg(user_filter="(uid={username})")
    monkeypatch.setattr("app.api.v1.endpoints.ldap_admin.get_ldap_config", cfg_uid)
    r = await client.post("/api/v1/system/ldap/test-auth", json={"username": "user01", "password": "x"},
                          headers=auth_headers)
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "ldap_user_not_found_uid_filter"


async def test_test_connection_warns_about_uid_filter_on_ad_and_checks_the_search_base(monkeypatch) -> None:
    """「測試連線」以前只做服務帳號 bind，所以搜尋起點寫錯、AD 卻用 OpenLDAP 的 (uid=…) 都會顯示成功，
    要到「用帳密測試」才爆（客戶 2026-10-21 兩個都踩到）。現在 bind 之後再讀一次搜尋起點，並依伺服器的
    身分格式（AD 是 u:網域\\帳號）提醒篩選條件。"""
    seen: dict[str, Any] = {}

    class _Ext:
        class standard:
            @staticmethod
            def who_am_i():
                return "u:CORP\\svc-ipam"

    class _Info:
        other = {"supportedCapabilities": ["1.2.840.113556.1.4.800", "1.2.840.113556.1.4.1670"]}

    class _Server:
        info = _Info()

    class _Conn:
        extend = _Ext()
        server = _Server()

        def search(self, *a, **k):
            seen["base"] = k["search_base"]
            seen["scope"] = k["search_scope"]
            return True

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    r = await ldap_auth.test_connection(_cfg(user_filter="(uid={username})"))
    assert r["directory"] == "ad"
    assert seen["base"] == "DC=corp,DC=example"
    assert seen["scope"] == ldap_auth.BASE
    assert [w["code"] for w in r["warnings"]] == ["ldap_warn_uid_filter_on_ad"]

    r2 = await ldap_auth.test_connection(_cfg())                     # sAMAccountName：沒有警告
    assert r2["warnings"] == []

    class _BadBase(_Conn):
        def search(self, *a, **k):
            raise ldap_auth.LDAPException("noSuchObject")
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _BadBase())
    r3 = await ldap_auth.test_connection(_cfg())
    assert [w["code"] for w in r3["warnings"]] == ["ldap_warn_search_base"]
    assert "noSuchObject" in r3["warnings"][0]["params"]["reason"]


@pytest.mark.parametrize(("raw", "want"), [
    ("(sAMAccountName={username})", "(sAMAccountName={username})"),
    (" ( sAMAccountName = {username} ) ", "(sAMAccountName={username})"),
    ("(sAMAccountName={ username })", "(sAMAccountName={username})"),
    ("(& (objectClass=user) (sAMAccountName={username}) )", "(&(objectClass=user)(sAMAccountName={username}))"),
    ("(cn=John Smith)", "(cn=John Smith)"),                                           # 值裡的空白保留
    ("(&(memberOf=CN=IPAM Users, OU=Groups, DC=corp, DC=example)(sAMAccountName={username}))",
     "(&(memberOf=CN=IPAM Users,OU=Groups,DC=corp,DC=example)(sAMAccountName={username}))"),  # DN 值照 DN 規則
    ("(!(userAccountControl:1.2.840.113556.1.4.803:=2))", "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"),
    ("(| (mail={username}) (sAMAccountName={username}) )", "(|(mail={username})(sAMAccountName={username}))"),
])
def test_normalize_filter(raw: str, want: str) -> None:
    assert ldap_auth.normalize_filter(raw) == want


def test_search_uses_the_normalized_filter(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Conn:
        entries: list = []

        def search(self, *a, **k):
            seen["filter"] = k["search_filter"]

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    with pytest.raises(ldap_auth.LDAPInvalidCredentials):
        ldap_auth._authenticate_sync(_cfg(user_filter=" ( sAMAccountName = { username } ) "), "user01", "pw")
    assert seen["filter"] == "(sAMAccountName=user01)"


async def test_saving_cleans_up_spaces(client, auth_headers) -> None:
    """存檔時就整理好（畫面上看得到結果）；已經存在的舊設定則在使用時再正規化一次（上面幾個測試）。"""
    body = {"enabled": False, "server": "dc1.corp.example", "port": 389, "use_ssl": False, "use_starttls": False,
            "bind_dn": " CN=svc-ipam, CN=Users, DC=corp, DC=example ", "search_base": "DC=corp, DC=example",
            "user_filter": " ( sAMAccountName = {username} ) ",
            "admin_groups": ["CN=IPAM-Admins, OU=Groups, DC=corp, DC=example", "  ", ""]}
    r = await client.put("/api/v1/system/ldap", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    got = (await client.get("/api/v1/system/ldap", headers=auth_headers)).json()
    assert got["bind_dn"] == "CN=svc-ipam,CN=Users,DC=corp,DC=example"
    assert got["search_base"] == "DC=corp,DC=example"
    assert got["user_filter"] == "(sAMAccountName={username})"
    assert got["admin_groups"] == ["CN=IPAM-Admins,OU=Groups,DC=corp,DC=example"]


@pytest.mark.parametrize(("flt", "code"), [
    ("(sAMAccountName=username)", "ldap_filter_no_username"),        # 客戶 2026-10-21：漏了大括號
    ("(sAMAccountName=username{})", "ldap_filter_no_username"),      # 客戶第三次：以前是 IndexError（str.format）
    ("(sAMAccountName={username}", "ldap_filter_invalid"),           # 括號不成對
])
async def test_saving_rejects_a_filter_that_can_never_match(client, auth_headers, flt: str, code: str) -> None:
    body = {"enabled": False, "server": "dc1.corp.example", "port": 389, "use_ssl": False, "use_starttls": False,
            "search_base": "DC=corp,DC=example", "user_filter": flt}
    r = await client.put("/api/v1/system/ldap", json=body, headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == code



async def test_test_connection_survives_a_server_without_who_am_i(monkeypatch) -> None:
    """Samba AD 不支援 Who am I 擴充操作（真的 AD 支援）：以前整個測試連線因此失敗。AD 改由 rootDSE 的
    supportedCapabilities（1.2.840.113556.1.4.800）判斷，Who am I 只是附加資訊。"""
    class _Ext:
        class standard:
            @staticmethod
            def who_am_i():
                raise ldap_auth.LDAPException("extension not in DSA list of supported extensions")

    class _Info:
        # ldap3 對 Samba AD 實際讀到的 rootDSE：沒有 supportedCapabilities，但有 AD 專屬的屬性
        other = {"domainControllerFunctionality": ["7"], "forestFunctionality": ["7"],
                 "defaultNamingContext": ["DC=corp,DC=example"]}

    class _Server:
        info = _Info()

    class _Conn:
        extend = _Ext()
        server = _Server()

        def search(self, *a, **k):
            return True

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    r = await ldap_auth.test_connection(_cfg(user_filter="(uid={username})"))
    assert r["bound"] is True
    assert r["who_am_i"] is None
    assert r["directory"] == "ad"
    assert [w["code"] for w in r["warnings"]] == ["ldap_warn_uid_filter_on_ad"]



async def test_openldap_is_not_mistaken_for_ad(monkeypatch) -> None:
    class _Ext:
        class standard:
            @staticmethod
            def who_am_i():
                return "dn:cn=admin,dc=example,dc=org"

    class _Info:
        other = {"configContext": ["cn=config"], "namingContexts": ["dc=example,dc=org"]}

    class _Server:
        info = _Info()

    class _Conn:
        extend = _Ext()
        server = _Server()

        def search(self, *a, **k):
            return True

        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_bind_admin_sync", lambda cfg: _Conn())
    r = await ldap_auth.test_connection(_cfg(user_filter="(uid={username})"))
    assert r["directory"] == "ldap"
    assert r["warnings"] == []                                         # OpenLDAP 用 uid 是對的
