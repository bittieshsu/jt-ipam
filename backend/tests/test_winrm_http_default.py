"""WinRM 預設走 HTTP 5985（使用者 2026-10-09：「預設 5986 這個改掉，設定頁也是，預設走 5985 http，
並說因為 Windows Server 防火牆預設封鎖此 port」）。

- 新的 Windows DHCP／Windows DNS 連線預設 HTTP 5985
- 走 HTTP 時內容與帳密一律以 NTLM 加密（message_encryption=always），加密不了就連線失敗，不退回明文
- 既有的 Windows DNS 伺服器設定裡沒有記錄傳輸方式的（當時一律 HTTPS），升級時明確記成 HTTPS，不被切換
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _FakeSession:
    calls: list[dict] = []

    def __init__(self, **kw):
        _FakeSession.calls.append(kw)


def _fake_winrm(monkeypatch):
    _FakeSession.calls = []
    monkeypatch.setitem(sys.modules, "winrm", types.SimpleNamespace(Session=_FakeSession))


def test_windows_dhcp_defaults_to_http_5985() -> None:
    from app.models.windows_dhcp import WindowsDhcpServer
    from app.schemas.windows_dhcp import WindowsDhcpCreate
    body = WindowsDhcpCreate(name="dhcp", host="192.0.2.10", username="u", password="p")
    assert body.port == 5985 and body.use_ssl is False
    col = WindowsDhcpServer.__table__.c
    assert col.port.default.arg == 5985 and col.use_ssl.default.arg is False


def test_windows_dhcp_http_always_encrypts(monkeypatch) -> None:
    from app.services import windows_dhcp as wd
    monkeypatch.setattr(wd, "_check_address_safe", lambda _h: None)
    _fake_winrm(monkeypatch)
    wd.WindowsDhcpClient(host="192.0.2.10", username="u", password="p")._session()
    kw = _FakeSession.calls[-1]
    assert kw["target"] == "http://192.0.2.10:5985/wsman"
    assert kw["message_encryption"] == "always"
    wd.WindowsDhcpClient(host="192.0.2.10", username="u", password="p", port=5986, use_ssl=True)._session()
    kw = _FakeSession.calls[-1]
    assert kw["target"].startswith("https://") and kw["server_cert_validation"] == "validate"


async def test_windows_dns_without_a_transport_defaults_to_http(db_session, monkeypatch) -> None:
    from app.models.dns import DNSServer
    from app.services.dns import factory
    async def _no_secret(*_a, **_k):
        return "p"
    monkeypatch.setattr(factory, "_load_secret", _no_secret)
    srv = DNSServer(name="win", type="windows_dns", server_address="192.0.2.53",
                    extra_config=json.dumps({"username": "u"}))
    ad = await factory.get_adapter(db_session, srv)
    assert ad.port == 5985 and ad.use_ssl is False


def _migration():
    path = ROOT / "alembic" / "versions" / "0197_winrm_http_default.py"
    spec = importlib.util.spec_from_file_location("m0197", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_upgrade_pins_existing_windows_dns_to_https() -> None:
    pin = _migration().pin_legacy_https
    # 沒記錄傳輸方式的（當時一律 HTTPS）→ 明確記成 HTTPS
    assert json.loads(pin(json.dumps({"username": "u"}))) == {"username": "u", "use_ssl": True}
    assert json.loads(pin(None)) == {"use_ssl": True}
    assert json.loads(pin("")) == {"use_ssl": True}
    # 已經選過的不動
    assert pin(json.dumps({"use_ssl": False})) is None
    assert pin(json.dumps({"use_ssl": True, "winrm_port": 5986})) is None
    # 壞掉的 JSON 不碰（不要因為一筆壞資料讓升級失敗）
    assert pin("{not json") is None


async def test_saving_a_windows_dns_server_records_the_transport(client, auth_headers, db_session) -> None:
    """存檔時一律寫明 use_ssl：之後「沒記錄」就只會出現在 0197 之前的舊匯出檔（＝HTTPS）。"""
    r = await client.post("/api/v1/dns/servers", headers=auth_headers, json={
        "name": "win-dns-t", "type": "windows_dns", "server_address": "192.0.2.53",
        "extra_config": json.dumps({"username": "u"})})
    assert r.status_code == 201, r.text
    assert json.loads(r.json()["extra_config"])["use_ssl"] is False
    sid = r.json()["id"]
    r = await client.patch(f"/api/v1/dns/servers/{sid}", headers=auth_headers,
                           json={"extra_config": json.dumps({"username": "u", "use_ssl": True})})
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["extra_config"])["use_ssl"] is True


def test_importing_an_old_export_keeps_windows_dns_on_https() -> None:
    from app.models.dns import DNSServer
    from app.services.system_transfer.importer import _coerce
    table = DNSServer.__table__
    old = _coerce(table, {"name": "w", "type": "windows_dns", "extra_config": json.dumps({"username": "u"})})
    assert json.loads(old["extra_config"])["use_ssl"] is True
    new = _coerce(table, {"name": "w", "type": "windows_dns", "extra_config": json.dumps({"use_ssl": False})})
    assert json.loads(new["extra_config"])["use_ssl"] is False
    other = _coerce(table, {"name": "p", "type": "powerdns", "extra_config": None})
    assert other["extra_config"] is None
