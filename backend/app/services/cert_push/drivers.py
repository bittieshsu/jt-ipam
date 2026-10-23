"""各家設備的憑證推送 driver。只透過 Http 跟設備說話（base.Http），換上之前一定先記下原本的設定，
回傳的 rollback 會把設備恢復成原狀。

FortiGate（FortiOS 7.x REST，API 權杖要有 system 與 vpn 的寫入權限）
  options: {"bind": ["admin", "sslvpn"], "vdom": "root"}
  1. 讀目前管理介面（system global admin-server-cert）與 SSL VPN（vpn.ssl settings servercert）的憑證
  2. POST /api/v2/monitor/vpn-certificate/local/import（type=regular，憑證含中繼鏈、私鑰都是 PEM 的 base64，scope=global）
     名稱帶指紋前 8 碼：同一版重推會撞名 → 當成已經匯入
  3. 依 bind 改 admin-server-cert／servercert；bind 為空＝只匯入，交給管理員自己選
  舊的 jtipam- 憑證不自動刪（換回去要用；FortiGate 刪憑證前必須沒有任何設定引用它）

OPNsense（23.7 以後的 Trust API，API 金鑰的權限要能寫 System: Certificates 與重啟服務）
  options: {"cert_uuid": "<要替換的既有憑證>", "restart_webgui": true}
  直接替換一張既有憑證的內容（POST /api/trust/cert/set/{uuid}，action=import）：refid 不變，
  網頁管理介面、OpenVPN、HAProxy…凡是引用這張的都跟著換，不必改各服務的設定。
  Trust API 存檔不會重啟服務 → restart_webgui 時 POST /api/core/service/restart/webgui
  （重啟當下連線可能被切斷，這一步的連線錯誤不算失敗，由引擎連過去確認）。

Palo Alto（PAN-OS XML API；API 金鑰的角色要能匯入憑證、改設定、commit）
  options: {"profile": "<SSL/TLS 服務設定檔名稱>", "commit": true}
  1. 私鑰用隨機密碼加密成 PKCS#8 後與憑證一起匯入（type=import&category=keypair&format=pem；PAN-OS 要求私鑰加密）
     中繼憑證另外匯入（category=certificate），名稱同前綴加 -ca1、-ca2…
  2. 讀設定檔目前的憑證 → 改成新的（/config/shared/ssl-tls-service-profile/entry[@name=…]/certificate）
  3. commit，輪詢工作直到 FIN；結果不是 OK 就失敗（換回去也要 commit）
  設定檔被 GlobalProtect 入口、管理介面等引用，換掉設定檔的憑證＝它們一起換。

pfSense（pfSense-pkg-RESTAPI v2；X-API-Key 的權限要能建立憑證與改 webConfigurator）
  options: {"bind_webgui": true}
  1. 憑證清單裡已有同名（jtipam-…-指紋）就沿用，否則 POST /api/v2/system/certificate 建一張
  2. bind_webgui 時讀 /api/v2/system/webgui/settings 的 sslcertref → PATCH 成新憑證的 refid
"""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from defusedxml import ElementTree as DefusedET

from app.services.cert_push.base import Bundle, Http, PushError, Rollback, Steps, b64


def _body(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class FortiGateDriver:
    kind = "fortigate"

    def __init__(self, http: Http, options: dict[str, Any]) -> None:
        self.http, self.opt = http, options or {}

    def _ok(self, step: str, resp: Any) -> dict[str, Any]:
        body = _body(resp)
        if resp.status_code >= 400 or body.get("status") == "error":
            raise PushError(step, "certpush_device_error", resp.text)
        return body

    async def push(self, bundle: Bundle, steps: Steps) -> Rollback | None:
        bind = [b for b in (self.opt.get("bind") or []) if b in ("admin", "sslvpn")]
        vdom = str(self.opt.get("vdom") or "root")
        name = bundle.device_name(35)
        old: dict[str, str] = {}
        if "admin" in bind:
            r = self._ok("read_admin", await self.http("GET", "/api/v2/cmdb/system/global"))
            old["admin"] = str((r.get("results") or {}).get("admin-server-cert") or "")
        if "sslvpn" in bind:
            r = self._ok("read_sslvpn", await self.http("GET", "/api/v2/cmdb/vpn.ssl/settings", params={"vdom": vdom}))
            old["sslvpn"] = str((r.get("results") or {}).get("servercert") or "")
        steps.ok("read", ", ".join(f"{k}={v}" for k, v in old.items()))

        resp = await self.http("POST", "/api/v2/monitor/vpn-certificate/local/import", json={
            "type": "regular", "certname": name, "scope": "global",
            "file_content": b64(bundle.fullchain), "key_file_content": b64(bundle.key_pem), "password": "",
        })
        body = _body(resp)
        if resp.status_code >= 400 or body.get("status") == "error":
            if body.get("error") == -5 or "exist" in resp.text.lower():     # 同一版已經匯入過
                steps.ok("import", f"{name} (already present)")
            else:
                raise PushError("import", "certpush_device_error", resp.text)
        else:
            steps.ok("import", name)

        done: list[str] = []

        async def rollback() -> None:
            for what in reversed(done):
                if what == "admin":
                    await self.http("PUT", "/api/v2/cmdb/system/global", json={"admin-server-cert": old["admin"]})
                else:
                    await self.http("PUT", "/api/v2/cmdb/vpn.ssl/settings", params={"vdom": vdom},
                                    json={"servercert": old["sslvpn"]})

        try:
            if "admin" in bind:
                self._ok("bind_admin", await self.http("PUT", "/api/v2/cmdb/system/global",
                                                       json={"admin-server-cert": name}))
                done.append("admin")
                steps.ok("bind_admin", name)
            if "sslvpn" in bind:
                self._ok("bind_sslvpn", await self.http("PUT", "/api/v2/cmdb/vpn.ssl/settings", params={"vdom": vdom},
                                                        json={"servercert": name}))
                done.append("sslvpn")
                steps.ok("bind_sslvpn", name)
        except PushError:
            await rollback()                         # 綁到一半：已經換掉的先換回去
            raise
        return rollback if done else None


class OPNsenseDriver:
    kind = "opnsense"

    def __init__(self, http: Http, options: dict[str, Any]) -> None:
        self.http, self.opt = http, options or {}

    async def list_device_certs(self) -> list[dict[str, str]]:
        """給畫面選「要替換哪一張」。"""
        resp = await self.http("GET", "/api/trust/cert/search")
        if resp.status_code >= 400:
            raise PushError("list", "certpush_device_error", resp.text)
        return [{"id": str(r.get("uuid")), "name": str(r.get("descr") or ""), "subject": str(r.get("commonname") or "")}
                for r in (_body(resp).get("rows") or []) if r.get("uuid")]

    async def _set(self, uuid: str, descr: str, crt: str, prv: str, step: str) -> None:
        resp = await self.http("POST", f"/api/trust/cert/set/{uuid}", json={"cert": {
            "action": "import", "descr": descr, "crt_payload": crt, "prv_payload": prv}})
        body = _body(resp)
        if resp.status_code >= 400 or body.get("result") != "saved":
            raise PushError(step, "certpush_device_error", str(body.get("validations") or resp.text))

    async def _restart(self, steps: Steps) -> None:
        try:
            resp = await self.http("POST", "/api/core/service/restart/webgui", timeout=20.0)
            steps.ok("restart_webgui", str(resp.status_code))
        except Exception as exc:
            steps.ok("restart_webgui", f"connection closed during restart ({type(exc).__name__})")

    async def push(self, bundle: Bundle, steps: Steps) -> Rollback | None:
        uuid = str(self.opt.get("cert_uuid") or "").strip()
        if not uuid:
            raise PushError("read", "certpush_opnsense_no_target")
        resp = await self.http("GET", f"/api/trust/cert/get/{uuid}")
        cur = _body(resp).get("cert")
        if resp.status_code >= 400 or not isinstance(cur, dict):
            raise PushError("read", "certpush_opnsense_cert_missing", resp.text)
        descr = str(cur.get("descr") or bundle.cert_name)

        def _dec(v: Any) -> str:
            try:
                return base64.b64decode(str(v or "")).decode()
            except Exception:
                return ""
        old_crt, old_prv = _dec(cur.get("crt")), _dec(cur.get("prv"))
        steps.ok("read", descr)

        await self._set(uuid, descr, bundle.fullchain, bundle.key_pem, "replace")
        steps.ok("replace", descr)
        restart = self.opt.get("restart_webgui", True) is not False
        if restart:
            await self._restart(steps)

        async def rollback() -> None:
            if not (old_crt and old_prv):
                raise PushError("rollback", "certpush_no_rollback_data")
            await self._set(uuid, descr, old_crt, old_prv, "rollback")
            if restart:
                await self._restart(Steps())
        return rollback


def _split_pems(text: str) -> list[str]:
    return re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text or "", re.S)


def _encrypt_key(key_pem: str, passphrase: str) -> str:
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.BestAvailableEncryption(passphrase.encode())).decode()


class PaloAltoDriver:
    kind = "paloalto"

    def __init__(self, http: Http, options: dict[str, Any]) -> None:
        self.http, self.opt = http, options or {}

    async def _xml(self, step: str, params: dict[str, Any], *, content: bytes | None = None,
                   headers: dict[str, str] | None = None, method: str = "GET") -> Any:
        resp = await self.http(method, "/api/", params=params, content=content, headers=headers)
        try:
            root = DefusedET.fromstring(resp.text)
        except Exception as exc:
            raise PushError(step, "certpush_device_error", resp.text) from exc
        if resp.status_code >= 400 or root.get("status") != "success":
            msg = " ".join(t.strip() for t in root.itertext() if t.strip())
            raise PushError(step, "certpush_device_error", msg or resp.text)
        return root

    async def _import(self, step: str, name: str, category: str, pem: str, passphrase: str | None = None) -> None:
        boundary = "jtipam" + secrets.token_hex(12)
        content = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}.pem\"\r\n"
                   f"Content-Type: application/x-pem-file\r\n\r\n{pem}\r\n--{boundary}--\r\n").encode()
        params: dict[str, Any] = {"type": "import", "category": category, "certificate-name": name, "format": "pem"}
        if passphrase:
            params["passphrase"] = passphrase
        await self._xml(step, params, content=content, method="POST",
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    async def _commit(self, step: str) -> None:
        root = await self._xml(step, {"type": "commit", "cmd": "<commit></commit>"})
        job = root.findtext(".//job")
        if not job:                                  # 「沒有可以 commit 的變更」
            return
        for _ in range(120):
            r = await self._xml(step, {"type": "op", "cmd": f"<show><jobs><id>{int(job)}</id></jobs></show>"})
            if (r.findtext(".//job/status") or "") == "FIN":
                if (r.findtext(".//job/result") or "") != "OK":
                    lines = " ".join((e.text or "").strip() for e in r.iter("line"))
                    raise PushError(step, "certpush_commit_failed", lines)
                return
            await asyncio.sleep(2)
        raise PushError(step, "certpush_commit_timeout")

    async def push(self, bundle: Bundle, steps: Steps) -> Rollback | None:
        profile = str(self.opt.get("profile") or "").strip()
        if not profile:
            raise PushError("read", "certpush_paloalto_no_profile")
        xpath = f"/config/shared/ssl-tls-service-profile/entry[@name='{profile}']"
        root = await self._xml("read", {"type": "config", "action": "get", "xpath": f"{xpath}/certificate"})
        old = (root.findtext(".//certificate") or "").strip()
        steps.ok("read", old)

        name = bundle.device_name(31)
        for i, ca in enumerate(_split_pems(bundle.chain_pem), 1):
            await self._import("import_chain", f"{name[:27]}-ca{i}", "certificate", ca + "\n")
        passphrase = secrets.token_urlsafe(24)
        await self._import("import", name, "keypair",
                           bundle.cert_pem.strip() + "\n" + _encrypt_key(bundle.key_pem, passphrase), passphrase)
        steps.ok("import", name)

        await self._xml("set_profile", {"type": "config", "action": "set", "xpath": xpath,
                                        "element": f"<certificate>{_xml_escape(name)}</certificate>"})
        steps.ok("set_profile", profile)
        commit = self.opt.get("commit", True) is not False
        if commit:
            await self._commit("commit")
            steps.ok("commit")

        async def rollback() -> None:
            if not old:
                raise PushError("rollback", "certpush_no_rollback_data")
            await self._xml("rollback", {"type": "config", "action": "set", "xpath": xpath,
                                         "element": f"<certificate>{_xml_escape(old)}</certificate>"})
            if commit:
                await self._commit("rollback")
        return rollback


class PfSenseDriver:
    kind = "pfsense"

    def __init__(self, http: Http, options: dict[str, Any]) -> None:
        self.http, self.opt = http, options or {}

    def _ok(self, step: str, resp: Any) -> dict[str, Any]:
        body = _body(resp)
        if resp.status_code >= 400 or (body.get("status") not in (None, "ok")):
            raise PushError(step, "certpush_device_error", str(body.get("message") or resp.text))
        return body

    async def push(self, bundle: Bundle, steps: Steps) -> Rollback | None:
        name = bundle.device_name(40)
        rows = self._ok("read", await self.http("GET", "/api/v2/system/certificates")).get("data") or []
        refid = next((str(r.get("refid")) for r in rows if isinstance(r, dict) and r.get("descr") == name), "")
        if refid:
            steps.ok("import", f"{name} (already present)")
        else:
            data = self._ok("import", await self.http("POST", "/api/v2/system/certificate", json={
                "descr": name, "crt": bundle.fullchain, "prv": bundle.key_pem})).get("data") or {}
            refid = str(data.get("refid") or "")
            if not refid:
                raise PushError("import", "certpush_device_error", "no refid in response")
            steps.ok("import", name)

        if not self.opt.get("bind_webgui", True):
            return None
        cur = self._ok("read_webgui", await self.http("GET", "/api/v2/system/webgui/settings")).get("data") or {}
        old = str(cur.get("sslcertref") or "")
        self._ok("bind_webgui", await self.http("PATCH", "/api/v2/system/webgui/settings", json={"sslcertref": refid}))
        steps.ok("bind_webgui", refid)

        async def rollback() -> None:
            if not old:
                raise PushError("rollback", "certpush_no_rollback_data")
            self._ok("rollback", await self.http("PATCH", "/api/v2/system/webgui/settings", json={"sslcertref": old}))
        return rollback


DRIVERS: dict[str, type] = {"fortigate": FortiGateDriver, "opnsense": OPNsenseDriver,
                            "paloalto": PaloAltoDriver, "pfsense": PfSenseDriver}
