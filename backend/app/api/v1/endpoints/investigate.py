"""調查模式端點：一個位址的完整線索，外加（選用的）模型敘述。

**事實與推測分開回傳**：`dossier` 是查得到的事實，`narrative` 是模型對那份事實的解讀。
呼叫端可以只要事實 —— 模型不可用、或使用者不想要 AI 介入時，這個功能仍然完整可用。
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.core.audit import append_audit
from app.core.db import get_session
from app.services.investigate import collect_dossier

router = APIRouter(prefix="/investigate", tags=["investigate"])

NARRATIVE_TIMEOUT = 120.0
NARRATIVE_MAX_TOKENS = 900


def _prompt(dossier: dict[str, Any], lang: str) -> str:
    """要模型解讀事實，而不是自由發揮。

    明確要求：只用檔案裡有的東西、不確定就說不確定、指出彼此矛盾之處 —— 因為這個功能
    要解決的正是「線索散在各處、彼此對不上」，矛盾點才是使用者最需要被指出來的。
    """
    import json

    zh = lang.startswith("zh")
    rules = (
        "你是網路與資安維運的助手。以下是 IPAM 系統對某個 IP 位址收集到的**事實**。\n"
        "請用繁體中文寫一段簡短的判讀，並遵守：\n"
        "1. 只根據下列資料推論，資料裡沒有的不要自己補\n"
        "2. 先講最值得注意的一點，再講其餘\n"
        "3. **特別指出彼此矛盾的線索**（例如各來源回報的主機名稱不同、"
        "監控說離線但剛剛還有 ARP、Wazuh agent 已失聯卻仍掛在這個位址）\n"
        "4. 不確定就直說不確定，不要用肯定語氣講沒把握的事\n"
        "5. 不要重複列出原始資料，那些畫面上已經有了\n"
    ) if zh else (
        "You assist with network and security operations. Below are **facts** an IPAM "
        "system collected about one IP address. Write a short reading of it in English:\n"
        "1. Infer only from the data below; do not invent anything\n"
        "2. Lead with the single most notable point\n"
        "3. **Call out contradictions between clues** (sources disagreeing on hostname, "
        "monitoring saying offline while ARP just saw it, a disconnected agent still "
        "claiming the address)\n"
        "4. Say plainly when something is uncertain\n"
        "5. Do not restate the raw data; it is already on screen\n"
    )
    return f"{rules}\n---\n{json.dumps(dossier, ensure_ascii=False, default=str)}\n"


@router.get("")
async def investigate(
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    ip: Annotated[str, Query(max_length=64)],
    narrative: Annotated[bool, Query()] = False,
    lang: Annotated[str, Query(max_length=8)] = "zh-TW",
) -> dict[str, Any]:
    """收集這個位址的線索。`narrative=true` 時另外請模型寫一段判讀。

    可見性由 `collect_dossier` 依子網路授權處理：看不到就回 `found: false`，
    而不是 403 —— 回「無權限」等於確認了這個位址存在。
    """
    try:
        ipaddress.ip_address(ip.strip())
    except ValueError:
        raise HTTPException(422, detail="not an IP address") from None

    dossier = await collect_dossier(session, user=user, ip=ip.strip())
    out: dict[str, Any] = {"dossier": dossier, "narrative": None, "narrative_error": None}
    if not dossier.get("found") or not narrative:
        return out

    # 模型不可用不該讓整個功能失效 —— 事實已經在手上了，敘述是加分項
    try:
        from app.services.ai import raw_chat
        out["narrative"] = (await raw_chat(
            session, _prompt(dossier, lang),
            timeout=NARRATIVE_TIMEOUT, max_output_tokens=NARRATIVE_MAX_TOKENS,
            no_thinking=True,
        )).strip() or None
    except Exception as exc:
        out["narrative_error"] = str(exc)[:300]

    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="ip_address",
        object_id=dossier.get("address", {}).get("ip_address_id"),
        action="investigate",
        diff={"ip": ip, "narrative": bool(out["narrative"])},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return out
