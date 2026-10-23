"""推送引擎：載入目前版本 → 交給 driver 推上去並換上 → 連過去確認對方出示的憑證就是這一版 → 不是就換回去。

確認的規則（推錯憑證可能讓管理介面連不上，所以這段寧可保守）：
- 推之前先連一次確認位址（verify_endpoint，預設是 api_url 的主機與埠）。
- 推完輪詢最多 VERIFY_TRIES 次：出示的指紋等於新版 → ok。
- 推之前連得到、推完一直不是新版（或連不到）→ 呼叫 driver 的 rollback 換回原本的憑證 → rolled_back。
- 推之前就連不到（例如 SSL VPN 埠只開在外網）→ 沒辦法確認，不換回 → unverified。
- 只匯入、不換上（driver 回 rollback=None）→ unverified（設備上有了，但還沒有服務在用）。
每次推送都留稽核（不含私鑰），失敗發 cert.push_failed 通知，成功發 cert.deployed。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import UnsafeOutboundURL, safe_request, transport_detail
from app.core.security import decrypt_secret, encrypt_secret
from app.models.cert_push_target import CertPushTarget
from app.models.certificate import Certificate, CertVersion
from app.models.encrypted_secret import EncryptedSecret
from app.services.cert_push.base import Bundle, PushError, Steps
from app.services.cert_push.drivers import DRIVERS

VERIFY_TRIES = 10
VERIFY_INTERVAL_S = 3.0
#: 自動推送失敗後，隔多久才再試（避免每輪同步都去敲設備）
RETRY_AFTER = timedelta(hours=1)
SECRET_TYPE = "cert_push_target"  # noqa: S105 -- encrypted_secrets 的物件類型名稱，不是密碼


def _aad(target_id: Any) -> bytes:
    return f"cert_push_target:{target_id}:secret".encode()


async def save_secret(session: AsyncSession, target_id: Any, secret: dict[str, str]) -> None:
    enc, nonce = encrypt_secret(json.dumps(secret), aad=_aad(target_id))
    row = (await session.execute(select(EncryptedSecret).where(
        EncryptedSecret.object_type == SECRET_TYPE, EncryptedSecret.object_id == target_id,
        EncryptedSecret.field == "secret"))).scalar_one_or_none()
    if row is None:
        session.add(EncryptedSecret(object_type=SECRET_TYPE, object_id=target_id, field="secret",
                                    ciphertext=enc, nonce=nonce))
    else:
        row.ciphertext, row.nonce = enc, nonce


async def load_secret(session: AsyncSession, target_id: Any) -> dict[str, str]:
    row = (await session.execute(select(EncryptedSecret).where(
        EncryptedSecret.object_type == SECRET_TYPE, EncryptedSecret.object_id == target_id,
        EncryptedSecret.field == "secret"))).scalar_one_or_none()
    if row is None:
        return {}
    return json.loads(decrypt_secret(row.ciphertext, row.nonce, aad=_aad(target_id)).decode())


async def delete_secret(session: AsyncSession, target_id: Any) -> None:
    from sqlalchemy import delete
    await session.execute(delete(EncryptedSecret).where(
        EncryptedSecret.object_type == SECRET_TYPE, EncryptedSecret.object_id == target_id))


def _auth_headers(kind: str, secret: dict[str, str]) -> dict[str, str]:
    if kind == "fortigate":
        return {"Authorization": f"Bearer {secret.get('api_key', '')}"}
    if kind == "opnsense":
        import base64
        raw = f"{secret.get('api_key', '')}:{secret.get('api_secret', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if kind == "paloalto":
        return {"X-PAN-KEY": secret.get("api_key", "")}
    if kind == "pfsense":
        return {"X-API-Key": secret.get("api_key", "")}
    return {}


def make_http(target: CertPushTarget, secret: dict[str, str]):
    """driver 用的 Http：所有請求都走 safe_request（SSRF 檢查、轉址不帶認證到別台）。"""
    base = target.api_url.rstrip("/")
    auth = _auth_headers(target.kind, secret)

    async def call(method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                   content: bytes | None = None, headers: dict[str, str] | None = None,
                   timeout: float = 30.0) -> httpx.Response:
        try:
            return await safe_request(method, base + path, headers={**auth, **(headers or {})}, params=params,
                                      json=json, content=content, timeout=timeout, verify=target.verify_tls,
                                      follow_redirects=False)
        except UnsafeOutboundURL as exc:
            raise PushError("connect", "certpush_ssrf", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise PushError("connect", "certpush_transport", transport_detail(exc)) from exc
    return call


def verify_address(target: CertPushTarget) -> tuple[str, int]:
    raw = (target.verify_endpoint or "").strip()
    if raw:
        u = urlsplit(raw if "://" in raw else f"https://{raw}")
    else:
        u = urlsplit(target.api_url)
    return (u.hostname or ""), (u.port or 443)


async def presented_fingerprint(host: str, port: int, timeout: float = 8.0) -> str | None:
    """連過去讀對方出示的憑證，回 SHA-256 指紋（小寫十六進位，與 CertVersion 相同）；連不到回 None。
    只讀不驗（自簽、名稱不符照樣讀得到）—— 這裡要知道的是「它現在用的是哪一張」，不是「它可不可信」。"""
    # 先解析並檢查位址（core/net_guard），再連「檢查過的那個位址」：不然 DNS 在檢查與連線之間換掉就繞過了
    from app.core import net_guard
    try:
        addrs = await net_guard.acheck_target(host, port)
    except UnsafeOutboundURL:
        return None
    if not addrs:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(str(addrs[0]), port, ssl=ctx, server_hostname=host), timeout)
    except (TimeoutError, OSError, ssl.SSLError):
        return None
    try:
        der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        w.close()
    return hashlib.sha256(der).hexdigest() if der else None


async def _current(session: AsyncSession, cert_id: Any) -> CertVersion | None:
    return (await session.execute(select(CertVersion).where(
        CertVersion.certificate_id == cert_id, CertVersion.is_current.is_(True)))).scalar_one_or_none()


async def _bundle(session: AsyncSession, cert: Certificate, ver: CertVersion) -> Bundle:
    from app.services.cert_fetch import _key_aad
    key = decrypt_secret(ver.key_enc, ver.key_nonce, aad=_key_aad(cert.id, ver.fingerprint_sha256)).decode()
    return Bundle(cert_name=cert.name, fingerprint=ver.fingerprint_sha256, cert_pem=ver.cert_pem,
                  chain_pem=ver.chain_pem or "", key_pem=key)


async def push_target(session: AsyncSession, target: CertPushTarget, *, actor_user_id: Any = None,
                      request_id: str | None = None, trigger: str = "manual",
                      probe=presented_fingerprint) -> dict[str, Any]:
    """推一個目標並記錄結果。回 {status, fingerprint, steps, error}。呼叫端負責 commit。"""
    steps = Steps()
    status, error, fp = "error", None, None
    cert = await session.get(Certificate, target.certificate_id)
    ver = await _current(session, target.certificate_id) if cert else None
    try:
        if cert is None or ver is None:
            raise PushError("load", "certpush_no_version")
        fp = ver.fingerprint_sha256
        bundle = await _bundle(session, cert, ver)
        driver_cls = DRIVERS.get(target.kind)
        if driver_cls is None:
            raise PushError("load", "certpush_unknown_kind", target.kind)
        host, port = verify_address(target)
        verify = (target.options or {}).get("verify", True) is not False
        before = await probe(host, port) if verify else None
        steps.ok("probe_before", f"{host}:{port} {'reachable' if before else 'unreachable'}")
        driver = driver_cls(make_http(target, await load_secret(session, target.id)), target.options or {})
        rollback = await driver.push(bundle, steps)
        if rollback is None or not verify:
            status = "unverified"
        else:
            seen = None
            for i in range(VERIFY_TRIES):
                seen = await probe(host, port)
                if seen == fp:
                    break
                if i < VERIFY_TRIES - 1:
                    await asyncio.sleep(VERIFY_INTERVAL_S)
            if seen == fp:
                steps.ok("verify", f"{host}:{port} presents {fp[:16]}")
                status = "ok"
            elif before is None and seen is None:
                steps.ok("verify", f"{host}:{port} unreachable before and after; not verified")
                status = "unverified"
            else:
                steps.fail("verify", "certpush_verify_failed", f"{host}:{port} presents {(seen or 'nothing')[:16]}")
                try:
                    await rollback()
                    steps.ok("rollback")
                    status, error = "rolled_back", "certpush_verify_failed"
                except PushError as exc:
                    steps.fail("rollback", exc.code, exc.detail)
                    status, error = "error", "certpush_rollback_failed"
    except PushError as exc:
        steps.fail(exc.step, exc.code, exc.detail)
        status, error = "error", exc.code

    target.last_push_at = datetime.now(UTC)
    target.last_status = status
    target.last_error = error
    target.last_steps = steps.items
    if status in ("ok", "unverified"):
        target.last_fingerprint = fp
    await _audit_and_notify(session, target, cert, status, fp, error, actor_user_id, request_id, trigger)
    return {"status": status, "fingerprint": fp, "error": error, "steps": steps.items}


async def _audit_and_notify(session: AsyncSession, target: CertPushTarget, cert: Certificate | None, status: str,
                            fp: str | None, error: str | None, actor_user_id: Any, request_id: str | None,
                            trigger: str) -> None:
    from app.core.audit import append_audit
    from app.services.notification import notify_admins_event
    await append_audit(
        session, actor_user_id=str(actor_user_id) if actor_user_id else None, actor_ip=None,
        actor_user_agent=f"cert-push/{trigger}", object_type="cert_push_target", object_id=str(target.id),
        action="cert_push", diff={"target": target.name, "kind": target.kind, "certificate": cert.name if cert else None,
                                  "fingerprint": fp, "status": status, "error": error},
        request_id=request_id)
    if status in ("error", "rolled_back"):
        await notify_admins_event(
            session, event="cert.push_failed", severity="warning",
            title=f"憑證推送失敗：{target.name}", body=f"{cert.name if cert else ''} → {target.kind} {target.api_url}（{error}）",
            link="/certificates", title_key="notif.cert_push_failed",
            params={"target": target.name, "certificate": cert.name if cert else "", "error": error or ""})
    elif status == "ok":
        await notify_admins_event(
            session, event="cert.deployed", severity="info",
            title=f"憑證已推送：{target.name}", body=f"{cert.name if cert else ''} → {target.kind} {target.api_url}",
            link="/certificates", title_key="notif.cert_pushed",
            params={"target": target.name, "certificate": cert.name if cert else ""})


async def due_targets(session: AsyncSession, now: datetime | None = None) -> list[CertPushTarget]:
    """自動推送：目標上的版本不是目前版本；上次失敗的隔 RETRY_AFTER 才再試。"""
    now = now or datetime.now(UTC)
    rows = (await session.execute(select(CertPushTarget, CertVersion.fingerprint_sha256).join(
        CertVersion, (CertVersion.certificate_id == CertPushTarget.certificate_id) & CertVersion.is_current.is_(True)
    ).where(CertPushTarget.enabled.is_(True), CertPushTarget.auto_push.is_(True)))).all()
    out = []
    for t, fp in rows:
        if t.last_fingerprint == fp:
            continue
        if t.last_status in ("error", "rolled_back") and t.last_push_at and now - t.last_push_at < RETRY_AFTER:
            continue
        out.append(t)
    return out
