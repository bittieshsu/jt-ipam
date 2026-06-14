"""憑證派送 agent 協定測試：check / bundle（含 scope）/ report + key 認證。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _make_cert(cn="svc.example.com", days=90):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()).decode())


async def _cert_with_version(client, auth_headers) -> tuple[str, str, str]:
    """建立憑證 + 上傳一版,回 (cert_id, name, fingerprint)。"""
    name = f"cert-{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/v1/certificates", headers=auth_headers, json={"name": name})
    cid = r.json()["id"]
    cert_pem, key_pem = _make_cert()
    rv = await client.post(f"/api/v1/certificates/{cid}/versions", headers=auth_headers, files={
        "cert_file": ("c.crt", cert_pem, "application/x-pem-file"),
        "key_file": ("c.key", key_pem, "application/x-pem-file"),
    })
    return cid, name, rv.json()["fingerprint_sha256"]


async def _make_agent(client, auth_headers, scope_ids) -> str:
    r = await client.post("/api/v1/cert-agents", headers=auth_headers,
                          json={"name": f"agent-{uuid.uuid4().hex[:6]}", "scope_cert_ids": scope_ids})
    assert r.status_code == 201, r.text
    return r.json()["enroll_key"]


async def test_check_and_bundle_in_scope(client, auth_headers):
    cid, name, fp = await _cert_with_version(client, auth_headers)
    key = await _make_agent(client, auth_headers, [cid])

    # check 回目前版本指紋
    rc = await client.get("/api/v1/cert-agents/check", headers={"X-Agent-Key": key})
    assert rc.status_code == 200, rc.text
    # 自我更新用：check 一律回 server 端 agent.py 的 sha256（64 hex）
    assert len(rc.json()["agent_sha"]) == 64
    certs = rc.json()["certificates"]
    assert len(certs) == 1
    assert certs[0]["cert"] == name
    assert certs[0]["fingerprint"] == fp

    # bundle 回 crt/key/chain（私鑰明文，僅 agent 認證後可取）
    rb = await client.get(f"/api/v1/cert-agents/bundle?cert={name}", headers={"X-Agent-Key": key})
    assert rb.status_code == 200, rb.text
    body = rb.json()
    assert "BEGIN CERTIFICATE" in body["cert_pem"]
    assert "PRIVATE KEY" in body["key_pem"]
    assert body["fingerprint"] == fp


async def test_bundle_out_of_scope_404(client, auth_headers):
    _cid, name, _ = await _cert_with_version(client, auth_headers)
    other_cid, _, _ = await _cert_with_version(client, auth_headers)
    key = await _make_agent(client, auth_headers, [other_cid])  # scope 只含別張
    rb = await client.get(f"/api/v1/cert-agents/bundle?cert={name}", headers={"X-Agent-Key": key})
    assert rb.status_code == 404  # 不在 scope 與不存在回相同 404


async def test_empty_scope_sees_nothing(client, auth_headers):
    await _cert_with_version(client, auth_headers)
    key = await _make_agent(client, auth_headers, [])  # deny-by-default
    rc = await client.get("/api/v1/cert-agents/check", headers={"X-Agent-Key": key})
    assert rc.json()["certificates"] == []


async def test_report_stored(client, auth_headers):
    cid, name, fp = await _cert_with_version(client, auth_headers)
    key = await _make_agent(client, auth_headers, [cid])
    rr = await client.post("/api/v1/cert-agents/report", headers={"X-Agent-Key": key}, json={
        "deployments": [{"cert": name, "profile": "nginx", "fingerprint": fp,
                         "status": "ok", "dry_run": False}],
    })
    assert rr.status_code == 200
    assert rr.json()["received"] == 1


async def test_list_reports_version_and_ip(client, auth_headers):
    """管理頁列表回傳 agent_version / server_agent_version / last_source_ip。"""
    cid, _name, _fp = await _cert_with_version(client, auth_headers)
    key = await _make_agent(client, auth_headers, [cid])
    # agent 帶版本 header poll 一次 → 記錄 version + source_ip
    await client.get("/api/v1/cert-agents/check",
                     headers={"X-Agent-Key": key, "X-Agent-Version": "0.0.1-test"})
    r = await client.get("/api/v1/cert-agents", headers=auth_headers)
    assert r.status_code == 200, r.text
    item = next(x for x in r.json()["items"] if x["agent_version"] == "0.0.1-test")
    assert item["server_agent_version"]  # server 端解析得到版本（非 None）
    assert item["server_agent_version"] != item["agent_version"]  # 落後 → UI 標可更新
    assert item["last_source_ip"]  # 來源 IP 有記錄


async def test_patch_and_rotate_agent_return_200(client, auth_headers):
    """PATCH 代理 / rotate-key 要回 200 — 回歸 commit 後 updated_at(onupdate)過期 500。"""
    r = await client.post("/api/v1/cert-agents", headers=auth_headers,
                          json={"name": f"agent-{uuid.uuid4().hex[:6]}", "scope_cert_ids": []})
    aid = r.json()["id"]
    rp = await client.patch(f"/api/v1/cert-agents/{aid}", headers=auth_headers,
                            json={"description": "edited"})
    assert rp.status_code == 200, rp.text
    assert rp.json()["description"] == "edited"
    rr = await client.post(f"/api/v1/cert-agents/{aid}/rotate-key", headers=auth_headers)
    assert rr.status_code == 200, rr.text
    assert rr.json()["enroll_key"]


async def test_bad_agent_key_401(client, auth_headers):
    await _cert_with_version(client, auth_headers)
    rc = await client.get("/api/v1/cert-agents/check", headers={"X-Agent-Key": "wrong"})
    assert rc.status_code == 401


async def test_agent_list_requires_admin(client):
    r = await client.get("/api/v1/cert-agents")
    assert r.status_code in (401, 403)


async def test_status_payload_and_global_read(client, auth_headers):
    cid, name, fp = await _cert_with_version(client, auth_headers)
    key = await _make_agent(client, auth_headers, [cid])
    await client.post("/api/v1/cert-agents/report", headers={"X-Agent-Key": key}, json={
        "deployments": [{"cert": name, "profile": "nginx", "fingerprint": fp,
                         "status": "ok", "dry_run": False}]})
    r = await client.get("/api/v1/cert-agents/status", headers=auth_headers)  # admin = global-read
    assert r.status_code == 200, r.text
    a = next(x for x in r.json()["agents"] if any(d["cert"] == name for d in x["deployments"]))
    dep = next(d for d in a["deployments"] if d["cert"] == name)
    assert dep["up_to_date"] is True
    assert dep["not_after"] is not None
    assert dep["days_remaining"] is not None


async def test_status_forbidden_for_non_global_read(client, db_session):
    import uuid as _uuid

    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    u = User(username=f"dep-{_uuid.uuid4().hex[:6]}", email=f"{_uuid.uuid4().hex[:6]}@t.local",
             display_name="dep", password_hash=hash_password("TestPassword2026!"),
             auth_provider="local", is_active=True, is_admin=False)
    db_session.add(u)
    await db_session.commit()
    token = issue_access_token(u)
    r = await client.get("/api/v1/cert-agents/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403  # 非 admin 且無萬用讀取 → 403
