"""戰情室設定與電視用的顯示權杖（system_settings 的 `warroom` 鍵）。

顯示權杖（使用者 2026-10-21 決定要做）：牆上的電視不登入也能看。沿用機櫃嵌入 SVG 的系統權杖作法
（加密保存並綁定用途、常數時間比對、會到期、到期前由 credential_expiry 通知），另外：
- 預設關閉；只能讀戰情室的端點（app/api/v1/endpoints/warroom.py 的 warroom_principal）
- 看到的資料＝設定裡指定帳號（principal_user_id）的可見範圍；帳號停用或刪除就失效
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting

KEY = "warroom"
TOKEN_AAD = b"setting:warroom:display_token"

#: 主題（順序就是預設的播放順序）
PAGES = ("overview", "capacity", "topology", "services", "anomalies", "assets", "collection",
         "stats", "relations", "availability", "ai_audit")
READY_PAGES = PAGES
#: 升級後自動加進播放清單的主題。08–11（統計、物件關係、可用性、AI 巡檢；2026-10-23）不自動加入，
#: 由管理員在戰情室設定自己勾（規格：不必讓所有主題預設進入每一份播放清單）
AUTO_JOIN_PAGES = PAGES[:7]
#: 第一批（M1）完成的主題：這之前存的設定沒有 ready_seen，視為當時只看過這三頁
M1_READY = ("overview", "capacity", "collection")
GROUP_BY = ("location", "section", "customer")
AI_MODES = ("off", "template", "llm")
LANGS = ("", "zh-TW", "en-US", "ja-JP")
#: 可用性主題的時間範圍（availability.WINDOWS 的鍵）；歷史不夠長時畫面會自動退到涵蓋得到的最大範圍
AVAILABILITY_WINDOWS = ("24h", "7d", "30d", "90d")
MAX_SCOPE = 500                      # 每一種最多選幾個

DEFAULTS: dict[str, Any] = {
    "enabled": True,                 # 登入的帳號都看得到（升級後自動可用）；顯示權杖另外開
    "pages": list(AUTO_JOIN_PAGES),
    "dwell_seconds": 20,
    "max_subpages": 2,               # 每個主題最多輪播幾個資料子頁：大型站台不會長期霸佔輪播
    "group_by": "location",
    "capacity_warn": 80,
    "capacity_crit": 90,
    "language": "",                  # 空＝跟著瀏覽器
    "reduce_motion": False,
    "availability_window": "7d",
    "ai_summary": "off",
    "display_enabled": False,
    "principal_user_id": None,
    # 顯示範圍：只顯示這些單位／區段／子網路（聯集；全空＝全部）。見 common.scope_subnet_ids
    "scope": {"customers": [], "sections": [], "subnets": []},
    # 存設定當下已完成的主題：之後新完成的主題自動加進播放清單（升級後自動可用），
    # 但管理員看過、自己拿掉的不會被加回來
    "ready_seen": list(READY_PAGES),
}


def _clean(v: dict[str, Any]) -> dict[str, Any]:
    """把存的值整理成合法的設定（舊版本少的鍵補預設、亂掉的值退回預設）。"""
    out = {**DEFAULTS, **{k: v[k] for k in DEFAULTS if k in v}}
    seen = v.get("ready_seen") if isinstance(v.get("ready_seen"), list) else list(M1_READY)
    pages = [p for p in (out.get("pages") or []) if p in PAGES]
    if v.get("pages") is not None:
        # 新主題插在預設順序裡它前一個（已在清單中的）主題後面；清單其餘照管理員排的順序
        # （播放清單可拖拉排序，整個重排回預設順序會蓋掉自訂順序，10-22）
        for p in [p for p in AUTO_JOIN_PAGES if p not in seen and p not in pages]:
            before = [q for q in PAGES[:PAGES.index(p)] if q in pages]
            pages.insert(pages.index(before[-1]) + 1 if before else 0, p)
    out["pages"] = list(dict.fromkeys(pages)) or list(AUTO_JOIN_PAGES)
    out["ready_seen"] = list(READY_PAGES)
    sc = out.get("scope") if isinstance(out.get("scope"), dict) else {}
    clean_scope: dict[str, list[str]] = {}
    for kind in ("customers", "sections", "subnets"):
        ids: list[str] = []
        for x in (sc.get(kind) or [])[:MAX_SCOPE]:
            try:
                ids.append(str(uuid.UUID(str(x))))
            except ValueError:
                continue
        clean_scope[kind] = list(dict.fromkeys(ids))
    out["scope"] = clean_scope
    out["dwell_seconds"] = min(max(int(out.get("dwell_seconds") or 20), 5), 600)
    out["max_subpages"] = min(max(int(out.get("max_subpages") or 2), 1), 50)
    out["group_by"] = out["group_by"] if out.get("group_by") in GROUP_BY else "location"
    warn = min(max(int(out.get("capacity_warn") or 80), 1), 99)
    crit = min(max(int(out.get("capacity_crit") or 90), 2), 100)
    out["capacity_warn"], out["capacity_crit"] = (warn, crit) if warn < crit else (80, 90)
    out["language"] = out["language"] if out.get("language") in LANGS else ""
    out["reduce_motion"] = bool(out.get("reduce_motion"))
    if out.get("availability_window") not in AVAILABILITY_WINDOWS:
        out["availability_window"] = "7d"
    out["ai_summary"] = out["ai_summary"] if out.get("ai_summary") in AI_MODES else "off"
    out["enabled"] = bool(out.get("enabled"))
    out["display_enabled"] = bool(out.get("display_enabled"))
    try:
        out["principal_user_id"] = str(uuid.UUID(str(out["principal_user_id"]))) if out.get("principal_user_id") else None
    except ValueError:
        out["principal_user_id"] = None
    return out


async def _row(session: AsyncSession) -> SystemSetting | None:
    return await session.get(SystemSetting, KEY)


async def get_warroom(session: AsyncSession) -> dict[str, Any]:
    """設定＋顯示權杖狀態（token 是明文，只給程序內用；回給畫面前要拿掉）。"""
    from app.services.system_config import _public_token

    row = await _row(session)
    raw = dict(row.value) if (row and isinstance(row.value, dict)) else {}
    cfg = _clean(raw)
    tok, exp = _public_token(raw, TOKEN_AAD)
    cfg.update({"token": tok, "token_set": bool(tok), "token_expires_at": exp})
    return cfg


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    """給設定頁：不含權杖本身。"""
    return {k: v for k, v in cfg.items() if k != "token"}


async def set_warroom(session: AsyncSession, data: dict[str, Any], *,
                      updated_by_user_id: uuid.UUID | None = None) -> dict[str, Any]:
    from sqlalchemy.orm.attributes import flag_modified

    row = await _row(session)
    if row is None:
        row = SystemSetting(key=KEY, value={}, updated_by=updated_by_user_id)
        session.add(row)
    cur = dict(row.value or {})
    cur.update(_clean({**_clean(cur), **{k: v for k, v in data.items() if k in DEFAULTS}}))
    row.value = cur
    row.updated_by = updated_by_user_id
    flag_modified(row, "value")
    await session.flush()
    return await get_warroom(session)


async def rotate_display_token(session: AsyncSession, *, days: int,
                               updated_by_user_id: uuid.UUID | None = None) -> dict[str, Any]:
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.system_config import _new_public_token

    row = await _row(session)
    if row is None:
        row = SystemSetting(key=KEY, value={}, updated_by=updated_by_user_id)
        session.add(row)
    cur = dict(row.value or {})
    _new_public_token(cur, TOKEN_AAD, days, 32)
    row.value = cur
    row.updated_by = updated_by_user_id
    flag_modified(row, "value")
    await session.flush()
    return await get_warroom(session)


async def revoke_display_token(session: AsyncSession, *, updated_by_user_id: uuid.UUID | None = None) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    row = await _row(session)
    if row is None:
        return
    cur = dict(row.value or {})
    for k in ("token_enc", "token", "token_expires_at"):
        cur.pop(k, None)
    cur["display_enabled"] = False
    row.value = cur
    row.updated_by = updated_by_user_id
    flag_modified(row, "value")
    await session.flush()


async def get_display_credential(session: AsyncSession) -> dict[str, Any]:
    """給 credential_expiry：與機櫃嵌入同樣的形狀 {enabled, token_set, token_expires_at}。"""
    cfg = await get_warroom(session)
    exp = cfg.get("token_expires_at")
    return {"enabled": cfg["display_enabled"], "token_set": cfg["token_set"],
            "token_expires_at": exp if isinstance(exp, datetime) else None}
