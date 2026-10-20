"""WinRM 的 Kerberos 驗證（共用；目前 Hyper-V 在用，Windows DNS/DHCP 之後可以接）。

pywinrm 的 Kerberos 傳輸（內建的 requests_kerberos＋pykerberos）只會從 Kerberos 憑證快取拿票，不收密碼。
所以每次連線前用 `kinit` 拿帳號密碼換一張 TGT，放在這次專用的暫存快取（0700 目錄，用完刪掉），
連線期間把 `KRB5CCNAME`／`KRB5_CONFIG` 指過去。這兩個是行程層級的環境變數，同一時間只能有一組，
所以整段（kinit 到 WinRM 呼叫結束）由一把鎖序列化；同步本來就是一台一台跑，不影響。

KDC 怎麼找：填了就用填的（寫進這次專用的 krb5.conf）；沒填就照 AD 的慣例查 DNS SRV（_kerberos._tcp.<領域>）。
主機一律寫 FQDN：Kerberos 的服務名稱是 HTTP/<主機名稱>，用 IP 位址拿不到票（AD 預設不替 IP 發票）。

2026-10-20 用 Samba AD DC 容器驗證過 kinit、TGT、HTTP/<主機> 服務票與 SPNEGO token（沒有 Windows 可測 WinRM 本身）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator

from app.core.ui_error import UiError

#: KRB5CCNAME／KRB5_CONFIG 是行程層級的環境變數 —— 拿票到 WinRM 呼叫結束整段序列化
_LOCK = threading.Lock()
KINIT_TIMEOUT = 20.0


class KerberosError(UiError):
    """Kerberos 這一段的錯誤（代碼 kerberos_*，畫面翻譯）。"""


def missing() -> list[str]:
    """伺服器上缺什麼（空清單＝可以用 Kerberos）：kinit（apt 的 krb5-user）、pykerberos（pip extra `kerberos`）。"""
    out = []
    if not shutil.which("kinit"):
        out.append("krb5-user")
    try:
        import importlib
        importlib.import_module("kerberos")                       # pykerberos（C 擴充，沒有型別資訊）
        importlib.import_module("winrm.vendor.requests_kerberos")
    except Exception:  # 沒裝或編不起來都算
        out.append("pykerberos")
    return out


def principal_for(username: str, realm: str | None) -> tuple[str, str]:
    """帳號 → (主體, 領域)。`user@corp.example` 取 @ 後面；`CORP\\user` 或只有帳號時要另外填領域
    （NetBIOS 網域名稱不一定等於 Kerberos 領域，不猜）。"""
    name = (username or "").strip()
    rlm = (realm or "").strip().upper()
    if "@" in name:
        user, _, dom = name.rpartition("@")
        rlm = rlm or dom.upper()
        name = user
    elif "\\" in name:
        name = name.split("\\", 1)[1]
    if name and not re.fullmatch(r"[A-Za-z0-9._$-]{1,256}", name):
        raise KerberosError(f"invalid account name {name!r}", code="kerberos_principal",
                            principal=name, reason="invalid characters")
    if not name or not rlm:
        raise KerberosError("Kerberos needs the realm: use user@domain or fill in the realm",
                            code="kerberos_realm", username=username)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,252}", rlm):
        raise KerberosError(f"invalid Kerberos realm {rlm!r}", code="kerberos_realm", username=username)
    return f"{name}@{rlm}", rlm


def require_fqdn(host: str) -> None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise KerberosError(f"{host}: Kerberos needs the full host name (FQDN)",
                                code="kerberos_needs_fqdn", host=host) from None
        return
    raise KerberosError(f"{host}: Kerberos needs the host name (FQDN), not an IP address",
                        code="kerberos_needs_fqdn", host=host)


def krb5_conf(realm: str, kdcs: list[str]) -> str:
    """這次專用的 krb5.conf。不用 DNS 反查、不把主機名稱正規化：服務名稱照使用者填的主機名稱組（HTTP/<主機>），
    否則 DNS 反解到別的名字時會拿到錯的票。強制走 TCP（AD 的票常比 UDP 封包大）。"""
    lines = [
        "[libdefaults]",
        f"    default_realm = {realm}",
        f"    dns_lookup_kdc = {'false' if kdcs else 'true'}",
        "    dns_lookup_realm = false",
        "    rdns = false",
        "    dns_canonicalize_hostname = false",
        "    udp_preference_limit = 1",
    ]
    if kdcs:
        lines += ["[realms]", f"    {realm} = {{", *(f"        kdc = {k}" for k in kdcs), "    }"]
    return "\n".join(lines) + "\n"


def parse_kdcs(raw: str | None) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"[\s,]+", raw or ""):
        tok = tok.strip()
        if not tok:
            continue
        if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,255}", tok):
            raise KerberosError(f"invalid KDC {tok!r}", code="kerberos_kdc", kdc=tok)
        if tok not in out:
            out.append(tok)
    return out


def _classify_kinit(err: str, principal: str) -> KerberosError:
    e = err.strip()
    if re.search(r"Password incorrect|Preauthentication failed|password has expired|revoked", e, re.I):
        return KerberosError(f"Kerberos rejected {principal}: {e[-300:]}", code="kerberos_auth",
                             principal=principal, reason=e[-300:])
    if re.search(r"not found in Kerberos database", e, re.I):
        return KerberosError(f"{principal} does not exist in the realm: {e[-300:]}", code="kerberos_principal",
                             principal=principal, reason=e[-300:])
    if re.search(r"Cannot (find|contact) (any )?KDC|Cannot resolve network address", e, re.I):
        return KerberosError(f"no KDC reachable for {principal}: {e[-300:]}", code="kerberos_kdc",
                             principal=principal, reason=e[-300:])
    if re.search(r"Clock skew", e, re.I):
        return KerberosError(f"clock skew with the KDC: {e[-300:]}", code="kerberos_clock",
                             principal=principal, reason=e[-300:])
    return KerberosError(f"kinit failed for {principal}: {e[-300:]}", code="kerberos_error",
                         principal=principal, reason=e[-300:])


@contextlib.contextmanager
def ticket(username: str, password: str, *, realm: str | None = None, kdcs: list[str] | None = None,
           ) -> Iterator[str]:
    """拿一張 TGT 放進這次專用的快取，期間把環境變數指過去；離開時還原並刪掉。產出主體名稱（給 WinRM 當帳號）。"""
    lack = missing()
    if lack:
        raise KerberosError(f"Kerberos is not available on this server (missing: {', '.join(lack)})",
                            code="kerberos_unavailable", missing=", ".join(lack))
    principal, rlm = principal_for(username, realm)
    with _LOCK:
        tmp = tempfile.mkdtemp(prefix="jt-ipam-krb-")
        os.chmod(tmp, 0o700)
        conf, cache = os.path.join(tmp, "krb5.conf"), f"FILE:{os.path.join(tmp, 'ccache')}"
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(krb5_conf(rlm, list(kdcs or [])))
        saved = {k: os.environ.get(k) for k in ("KRB5_CONFIG", "KRB5CCNAME")}
        try:
            env = {**os.environ, "KRB5_CONFIG": conf, "KRB5CCNAME": cache, "LC_ALL": "C"}
            kinit = shutil.which("kinit") or "kinit"
            try:
                # 固定 argv、主體名稱已經過允許清單（principal_for）、密碼走 stdin（不出現在 ps 看得到的參數裡）、非 shell
                r = subprocess.run([kinit, "-c", cache, principal], input=f"{password}\n", text=True,  # noqa: S603
                                   capture_output=True, env=env, timeout=KINIT_TIMEOUT, check=False)
            except subprocess.TimeoutExpired as exc:
                raise KerberosError(f"kinit timed out after {KINIT_TIMEOUT:.0f}s (KDC unreachable?)",
                                    code="kerberos_kdc", principal=principal,
                                    reason="timeout") from exc
            if r.returncode != 0:
                raise _classify_kinit(r.stderr or r.stdout, principal)
            os.environ["KRB5_CONFIG"], os.environ["KRB5CCNAME"] = conf, cache
            yield principal
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(tmp, ignore_errors=True)
