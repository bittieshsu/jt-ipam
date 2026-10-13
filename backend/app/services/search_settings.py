"""全站搜尋的設定（使用者 2026-10-13）：隨打即找、搜哪些類型與欄位、結果的排列順序。

只管畫面上方的全站搜尋框；權限照舊（看不到的照樣看不到），AI 對話的搜尋工具不受影響（一律全搜）。
類型與欄位是寫死的允許清單：設定只能從裡面挑，SQL 只會用到這裡列出的欄位。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ui_error import UiError
from app.models.system_setting import SystemSetting

KEY = "global_search"

#: 類型 → 可比對的欄位（順序＝預設的排列順序與畫面上的欄位順序）
CATALOG: dict[str, tuple[str, ...]] = {
    "section": ("name", "description"),
    "subnet": ("cidr", "description"),
    "vlan": ("number", "name"),
    "vm": ("name", "vmid"),
    "ip_address": ("ip", "hostname", "mac", "description", "owner", "note"),
    "device": ("name", "serial", "description"),
    "vpn": ("name", "peer"),
    "customer": ("name", "title"),
    "rack": ("name",),
    "location": ("name",),
    "nat": ("name",),
    "dns_record": ("name", "value"),
    "firewall": ("name",),
    "ip_request": ("ip", "hostname"),
}


def defaults() -> dict[str, Any]:
    return {"instant": True,
            "types": [{"type": t, "enabled": True, "fields": list(f)} for t, f in CATALOG.items()]}


def normalize(raw: Any) -> dict[str, Any]:
    """存的值 → 完整的設定：不認得的類型與欄位丟掉、重複的只留第一個，
    沒列到的類型（升級後新增的）照預設接在最後面，欄位照允許清單的順序。"""
    out: dict[str, Any] = {"instant": True, "types": []}
    if isinstance(raw, dict):
        if isinstance(raw.get("instant"), bool):
            out["instant"] = raw["instant"]
        seen: set[str] = set()
        for item in raw.get("types") or []:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t not in CATALOG or t in seen:
                continue
            seen.add(t)
            want = {f for f in (item.get("fields") or []) if isinstance(f, str)}
            out["types"].append({"type": t, "enabled": bool(item.get("enabled", True)),
                                 "fields": [f for f in CATALOG[t] if f in want]})
    have = {x["type"] for x in out["types"]}
    out["types"] += [{"type": t, "enabled": True, "fields": list(f)} for t, f in CATALOG.items() if t not in have]
    return out


def validate(settings: dict[str, Any]) -> None:
    """至少要有一個類型開著；開著的類型至少要勾一個欄位（否則它等於關著，畫面卻說開著）。"""
    on = [x for x in settings["types"] if x["enabled"]]
    if not on:
        raise UiError("至少要開啟一個搜尋類型", code="search_settings_no_type")
    empty = [x["type"] for x in on if not x["fields"]]
    if empty:
        raise UiError("開啟的類型至少要勾一個欄位", code="search_settings_no_field", types=", ".join(empty))


async def get_search_settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(SystemSetting, KEY)
    return normalize(row.value if row is not None else None)


async def set_search_settings(session: AsyncSession, raw: Any, *,
                              updated_by_user_id: uuid.UUID | None = None) -> dict[str, Any]:
    settings = normalize(raw)
    validate(settings)
    row = await session.get(SystemSetting, KEY)
    if row is None:
        row = SystemSetting(key=KEY, value=settings, updated_by=updated_by_user_id)
        session.add(row)
    else:
        row.value = settings
        row.updated_by = updated_by_user_id
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "value")
    return settings


def enabled_fields(settings: dict[str, Any] | None) -> dict[str, set[str]]:
    """開著的類型 → 開著的欄位（None＝全部，給 AI 工具等不受畫面設定限制的呼叫端）。"""
    s = settings or defaults()
    return {x["type"]: set(x["fields"]) for x in s["types"] if x["enabled"] and x["fields"]}


def order(settings: dict[str, Any] | None) -> list[str]:
    s = settings or defaults()
    return [x["type"] for x in s["types"] if x["enabled"] and x["fields"]]
