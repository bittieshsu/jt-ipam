"""依 DNSServer.type 建出對應的 adapter。"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.models.dns import DNSServer
from app.models.encrypted_secret import EncryptedSecret
from app.services.dns.base import DNSAdapter, DNSAdapterError


def _aad(server_id: str, field: str) -> bytes:
    return f"dns_server:{server_id}:{field}".encode()


async def _load_secret(
    session: AsyncSession, server: DNSServer, field: str
) -> str | None:
    row = (
        await session.execute(
            select(EncryptedSecret).where(
                EncryptedSecret.object_type == "dns_server",
                EncryptedSecret.object_id == server.id,
                EncryptedSecret.field == field,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return decrypt_secret(row.ciphertext, row.nonce, aad=_aad(str(server.id), field)).decode("utf-8")


def parse_tsig(raw: str | None) -> tuple[str, str, str]:
    """把 `algorithm:keyname:base64key` 拆成三段（UI 就是這樣提示使用者輸入的）。

    以前只把整串當成密鑰、keyname 另外從 extra 讀 —— 但 UI 從來不寫 extra，於是
    keyname 永遠是空的、TSIG 形同沒設定，AXFR 被 BIND 拒絕而畫面上看不出原因。

    只給一段（沒有冒號）＝整串都是密鑰，維持舊行為。
    """
    if not raw:
        return ("", "", "")
    parts = raw.split(":", 2)
    if len(parts) == 3:
        return (parts[0].strip().lower(), parts[1].strip(), parts[2].strip())
    if len(parts) == 2:
        # keyname:secret（沒寫演算法）→ 用預設演算法
        return ("", parts[0].strip(), parts[1].strip())
    return ("", "", raw.strip())


def explicit_winrm_transport(server_type: str | None, extra_text: str | None) -> str | None:
    """Windows DNS 的設定一律明確記下 use_ssl（沒給＝HTTP 5985）。

    0197 之前沒記錄傳輸方式的伺服器一律走 HTTPS；之後沒記錄的意思變成 HTTP。存檔時把它寫明，
    系統匯入才分得出「舊版匯出檔裡沒記錄的（HTTPS）」與新版的設定。JSON 壞掉的不碰（讓既有驗證處理）。
    """
    if server_type != "windows_dns":
        return extra_text
    try:
        extra = json.loads(extra_text) if extra_text and extra_text.strip() else {}
    except ValueError:
        return extra_text
    if not isinstance(extra, dict) or "use_ssl" in extra:
        return extra_text
    return json.dumps({**extra, "use_ssl": False}, ensure_ascii=False)


async def get_adapter(session: AsyncSession, server: DNSServer) -> DNSAdapter:
    """主入口：給定 DNSServer 物件，回對應 adapter（密鑰已解密）。"""
    extra = json.loads(server.extra_config) if server.extra_config else {}

    if server.type == "powerdns":
        from app.services.dns.powerdns import PowerDNSAdapter
        api_key = await _load_secret(session, server, "api_key")
        if not server.api_url or not api_key:
            raise DNSAdapterError("PowerDNS requires api_url + api_key")
        return PowerDNSAdapter(
            api_url=server.api_url,
            api_key=api_key,
            server_id=str(extra.get("server_id", "localhost")),
        )

    if server.type == "bind9":
        from app.services.dns.bind9 import Bind9Adapter
        tsig_key = await _load_secret(session, server, "tsig_key")
        algo, keyname, secret = parse_tsig(tsig_key)
        zones = [str(z).strip() for z in extra.get("zones", []) if str(z).strip()]
        if not zones:
            # BIND 沒有「列出所有 zone」的標準協定，一定要明講要同步哪些。沒有的話
            # 同步會安靜地跑完、一筆記錄都不會有 —— 那看起來就像整合壞了。
            raise DNSAdapterError(
                "BIND 9 需要指定要同步的 zone（設定頁的「Zone 清單」），"
                "因為 DNS 協定沒有列舉所有 zone 的方法",
                code="dns_bind_zones_required",
            )
        return Bind9Adapter(
            server_address=server.server_address or "",
            tsig_keyname=keyname or str(extra.get("tsig_keyname", "")),
            tsig_secret=secret,
            tsig_algorithm=algo or str(extra.get("tsig_algorithm", "hmac-sha256")),
            zones=zones,
        )

    if server.type == "unbound_opnsense":
        from app.services.dns.unbound_opnsense import UnboundOPNsenseAdapter
        api_key = await _load_secret(session, server, "api_key")
        api_secret = await _load_secret(session, server, "api_secret")
        if not server.api_url or not api_key or not api_secret:
            raise DNSAdapterError(
                "OPNsense Unbound requires api_url + api_key + api_secret"
            )
        return UnboundOPNsenseAdapter(
            api_url=server.api_url,
            api_key=api_key,
            api_secret=api_secret,
        )

    if server.type == "windows_dns":
        from app.services.dns.windows_dns import WindowsDNSAdapter
        password = await _load_secret(session, server, "password")
        return WindowsDNSAdapter(
            host=server.server_address or "",
            username=str(extra.get("username", "")),
            password=password or "",
            # 沒記錄傳輸方式＝HTTP 5985（Windows Server 防火牆預設封鎖 5986）；0197 之前建立的
            # 伺服器當時一律 HTTPS，升級時已明確寫入 use_ssl=true，不會被切換
            port=int(extra.get("winrm_port") or (5986 if extra.get("use_ssl", False) else 5985)),
            use_ssl=bool(extra.get("use_ssl", False)),
            verify_tls=bool(extra.get("verify_tls", True)),
        )

    if server.type == "univention_ucs":
        from app.services.dns.ucs import UniventionUCSAdapter
        password = await _load_secret(session, server, "password")
        if not server.api_url or not password:
            raise DNSAdapterError("Univention UCS requires api_url + username + password")
        return UniventionUCSAdapter(
            api_url=server.api_url,
            username=str(extra.get("username", "")),
            password=password,
            verify_tls=bool(extra.get("verify_tls", True)),
        )

    if server.type == "technitium":
        from app.services.dns.technitium import TechnitiumDNSAdapter
        token = await _load_secret(session, server, "api_key")
        if not server.api_url or not token:
            raise DNSAdapterError("Technitium requires the API URL and an API token", code="dns_technitium_incomplete")
        return TechnitiumDNSAdapter(api_url=server.api_url, token=token,
                                    verify_tls=bool(extra.get("verify_tls", True)))

    raise DNSAdapterError(f"Unknown DNS server type: {server.type}")
