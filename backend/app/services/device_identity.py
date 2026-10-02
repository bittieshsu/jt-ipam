"""掃描代理定期偵測的判讀結果寫回 IP 記錄（2026-10-01，Recog 的其他用途 ①）。

代理 1.14.0 起，定期 OS 偵測連同 nmap 的結構化結果（服務 banner、網頁標題、伺服器標頭、憑證）
一起回報；這裡用 IP 探測同一套判讀（`ip_identify.summarize`，含 Recog 指紋庫）推出 OS、設備類型
與廠牌型號。以前只有代理在本機從 nmap 文字推的一行 OS（NAS 常被判成攝影機、OS 只給
「Linux 4.15-5.8」）。

OS 家族或設備類型**從一個確定的值變成另一個**時寫一筆 IP 異動記錄（os_changed／kind_changed）：
同一個位址突然從印表機變成 Linux 主機，可能是 IP 被別台機器拿去用了 —— 異常偵測據此提出來。
從「不知道」變成知道不算變更。判讀不出來（unknown）時保留上一次的結果，不會清掉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress

#: 會寫進 IP 記錄的設備類型（ip_identify 的 device_type；unknown／no_response 不寫）
KINDS = frozenset({"router", "switch", "firewall", "wireless_ap", "printer", "camera", "voip",
                   "storage", "hypervisor", "media", "specialized", "server", "windows"})


def model_text(summary: dict[str, Any]) -> str | None:
    vendor = (summary.get("vendor") or "").strip()
    model = (summary.get("model") or "").strip()
    if model and vendor and model.lower().startswith(vendor.lower()):
        vendor = ""
    text = " ".join(x for x in (vendor, model) if x)
    return text[:120] or None


async def apply_summary(session: AsyncSession, ipa: IPAddress, summary: dict[str, Any], *,
                        fallback_os: str | None = None, source: str = "scanner") -> None:
    """把一次判讀結果寫進 IP 記錄。`fallback_os`：判讀沒有 OS 時用代理自己推的那行（舊行為）。"""
    from app.core.os_fingerprint import normalize_os
    from app.services.ip_history import log_change

    os_text = summary.get("os") or fallback_os
    if os_text:
        new_family = normalize_os(os_text)
        old_family = ipa.os_family
        ipa.os_guess = str(os_text)[:160]
        ipa.os_family = new_family
        if old_family and new_family and old_family != new_family:
            await log_change(session, ip=ipa, event_type="os_changed", field="os_family",
                             old=old_family, new=new_family, source=source,
                             note=str(os_text)[:200])

    kind = summary.get("device_type")
    if kind in KINDS:
        old_kind = ipa.device_kind
        ipa.device_kind = kind
        # 同一類型、這次沒帶型號 → 保留原型號（定期偵測常常沒有）；類型換了 → 舊型號屬於上一次的判讀，
        # 不可沿用（PVE 的 LXC 從「儲存設備 · HP」改判成伺服器時，留著 HP 就變成「伺服器 · HP」）
        new_model = model_text(summary)
        ipa.device_model = new_model if (new_model or old_kind != kind) else ipa.device_model
        ipa.device_identified_at = datetime.now(UTC)
        if old_kind and old_kind != kind:
            evidence = ", ".join((summary.get("evidence") or [])[:3])
            await log_change(session, ip=ipa, event_type="kind_changed", field="device_kind",
                             old=old_kind, new=kind, source=source, note=evidence[:200] or None)
