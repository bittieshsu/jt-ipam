"""加密的機密一律綁定用途（AES-GCM 的 AAD）。

沒綁定的密文可以被搬到別的欄位照樣解開（例如把一個整合的密碼密文貼到另一個整合的欄位，
用那個整合的功能把它送出去）。綁定之後換了位置就解不開。2026-10-09 合規核對找到三處沒綁：
GeoIP 授權金鑰、phpIPAM 搬移的 SSH 私鑰、系統匯入時重加密 phpIPAM 私鑰的那一段。
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import pathlib

import pytest

from app.core.security import decrypt_secret, encrypt_secret

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
_FUNCS = {"encrypt_secret", "decrypt_secret", "envelope_encrypt", "envelope_decrypt"}


def _calls():
    for p in APP.rglob("*.py"):
        if p.name == "security.py" and p.parent.name == "core":
            continue          # 定義本身
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in _FUNCS:
                yield p, node


def test_every_encrypt_and_decrypt_binds_an_aad() -> None:
    found, missing = 0, []
    for p, node in _calls():
        found += 1
        kw = {k.arg: k.value for k in node.keywords}
        aad = kw.get("aad")
        if aad is None or (isinstance(aad, ast.Constant) and aad.value is None):
            missing.append(f"{p.relative_to(APP)}:{node.lineno}")
    assert found > 30, f"只找到 {found} 處加解密呼叫，偵測方式可能失效了"
    assert not missing, f"這些加解密沒有綁定用途（aad=）：{missing}"


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic/versions/0198_settings_secret_aad.py"
    spec = importlib.util.spec_from_file_location("m0198", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _legacy(plain: str) -> dict:
    ct, nonce = encrypt_secret(plain)    # 0198 之前的存法（沒有 AAD）
    return {"key_ct": base64.b64encode(ct).decode(), "key_nonce": base64.b64encode(nonce).decode(),
            "account_id": "12345"}


def test_migration_rewraps_legacy_values_and_is_idempotent() -> None:
    from app.services.geoip import KEY_AAD

    m = _migration()
    old = _legacy("LICENSE-KEY")
    new = m.rewrap(old, "key_ct", "key_nonce", from_aad=None, to_aad=KEY_AAD)
    assert new is not None and new["account_id"] == "12345"
    got = decrypt_secret(base64.b64decode(new["key_ct"]), base64.b64decode(new["key_nonce"]), aad=KEY_AAD)
    assert got == b"LICENSE-KEY"
    # 已經是新格式 → 不動（重跑 migration 安全）
    assert m.rewrap(new, "key_ct", "key_nonce", from_aad=None, to_aad=KEY_AAD) is None
    # 解不開的（金鑰不對／損壞）→ 不動
    broken = {**old, "key_ct": base64.b64encode(b"x" * 32).decode()}
    assert m.rewrap(broken, "key_ct", "key_nonce", from_aad=None, to_aad=KEY_AAD) is None
    # 與程式使用的 AAD 一致
    targets = {t[0]: t[3] for t in m.TARGETS}
    from app.api.v1.endpoints.migration import KEY_AAD as MIG_AAD
    assert targets["geoip"] == KEY_AAD and targets["phpipam_migration"] == MIG_AAD


@pytest.mark.anyio
async def test_geoip_key_is_bound_and_cannot_be_moved(db_session) -> None:
    from app.services.geoip import get_geoip_creds, set_geoip_config

    await set_geoip_config(db_session, account_id="1", license_key="ABC-123")
    acct, key = await get_geoip_creds(db_session)
    assert (acct, key) == ("1", "ABC-123")
    # 換成沒綁定的密文（舊格式或從別處搬來的）→ 解不開
    from app.models.system_setting import SystemSetting
    row = await db_session.get(SystemSetting, "geoip")
    row.value = {**row.value, **{k: v for k, v in _legacy("ABC-123").items() if k != "account_id"}}
    await db_session.flush()
    from app.services import system_config
    system_config._bust() if hasattr(system_config, "_bust") else None
    _acct, key2 = await get_geoip_creds(db_session)
    assert key2 is None


def test_system_transfer_carries_geoip_key_across_keys() -> None:
    """GeoIP 金鑰以前不在搬移清單：匯出帶的是來源主機的密文，到別台解不開。"""
    from app.services.geoip import KEY_AAD
    from app.services.system_transfer.secrets import transform_settings_in, transform_settings_out

    ct, nonce = encrypt_secret("LIC", aad=KEY_AAD)
    stored = {"account_id": "9", "key_ct": base64.b64encode(ct).decode(),
              "key_nonce": base64.b64encode(nonce).decode()}
    out = transform_settings_out("geoip", stored)
    assert out["key_ct"] == {"__plain__": "LIC"} and "key_nonce" not in out
    back = transform_settings_in("geoip", out)
    got = decrypt_secret(base64.b64decode(back["key_ct"]), base64.b64decode(back["key_nonce"]), aad=KEY_AAD)
    assert got == b"LIC"
