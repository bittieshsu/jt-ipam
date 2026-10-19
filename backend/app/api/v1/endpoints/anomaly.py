"""異常偵測 endpoint：trigger run + read latest report。"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin, require_global_read
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import ui_detail
from app.schemas.base import StrictModel
from app.services.anomaly import ANOMALY_IGNORABLE, run_detection

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.post("/scan", dependencies=[Depends(require_admin)])
async def scan(
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """執行所有偵測規則。Phase 2 的排程版（Celery beat）會週期觸發此邏輯。"""
    report = await run_detection(session, notify_admins=True)
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="anomaly",
        object_id=None,
        action="scan",
        diff={
            "ip_conflicts": len(report.ip_conflicts),
            "mac_drifts": len(report.mac_drifts),
            "ghost_ips": len(report.ghost_ips),
            "unauthorized_ips": len(report.unauthorized_ips),
            "rogue_dhcp": len(report.rogue_dhcp),
            "external_exposure": len(report.external_exposure),
            "dangling_dns": len(report.dangling_dns),
            "duplicate_ip_records": len(report.duplicate_ip_records),
            "suspicious_changes": len(report.suspicious_changes),
            "fw_rule_rot": len(report.fw_rule_rot),
            "mac_flapping": len(report.mac_flapping),
            "identity_changes": len(report.identity_changes),
        },
        request_id=getattr(request.state, "request_id", None),
    )
    from app.services.anomaly import attach_liveness
    from app.services.system_config import set_anomaly_report
    data = report.to_dict()
    await set_anomaly_report(session, data, trigger="manual")
    await session.commit()
    return await attach_liveness(session, data)


@router.get("/last", dependencies=[Depends(require_admin)])
async def last_report(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """上一次偵測的結果（手動或排程）。進頁面先顯示這份，不用每次重跑。

    上線狀態（`live`）是**現在**算的，不是偵測當時的快照。
    """
    from app.services.anomaly import attach_liveness
    from app.services.system_config import get_anomaly_report
    saved = await get_anomaly_report(session)
    if saved is None:
        return {"report": None, "at": None, "trigger": None}
    return {"report": await attach_liveness(session, saved["report"]),
            "at": saved.get("at"), "trigger": saved.get("trigger")}

@router.post("/triage", dependencies=[Depends(require_admin)])
async def triage(
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """對單一 IP 產 AI 鑑識卡（未授權 IP 的判讀）。

    admin 限定：跟 /scan 同級 —— 它會呼叫 LLM（成本），且判讀對象多半是異常清單
    裡的項目。證據彙整走 get_ip_history（RBAC 同規），LLM 拿到的是定界後的快照。
    """
    import ipaddress as _ipaddr

    from fastapi import HTTPException

    from app.services.ip_triage import triage_ip

    ip = str(payload.get("ip") or "").strip()
    try:
        _ipaddr.ip_address(ip)
    except ValueError:
        raise HTTPException(422, detail=ui_detail("anom_bad_ip", "請提供合法的 IP")) from None
    try:
        result = await triage_ip(session, user, ip)
    except Exception as exc:
        # LLM 沒開／連不上要回可讀訊息，不是 500（跟 AI chat 同一課）
        raise HTTPException(502, detail=ui_detail("anom_triage_failed", f"AI 判讀失敗：{exc}", reason=str(exc)[:300])) from exc
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="triage", diff={"ip": ip})
    return result



@router.get("/fw-rule-changes", dependencies=[Depends(require_admin)])
async def fw_rule_changes(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    """防火牆規則異動歷史（異動偵測快照，admin 限定 —— 規則內容屬純管理資料）。

    通知只給摘要；這裡回完整 diff，讓「細節到快照裡看」是真的做得到的事。
    """
    from sqlalchemy import select as _select

    from app.models.fw_snapshot import FwRuleSnapshot

    limit = max(1, min(int(limit or 50), 200))
    rows = (await session.execute(
        _select(FwRuleSnapshot).order_by(FwRuleSnapshot.taken_at.desc()).limit(limit)
    )).scalars().all()
    return {"items": [{
        "id": str(r.id), "source_type": r.source_type, "instance_name": r.instance_name,
        "taken_at": r.taken_at.isoformat(), "rule_count": r.rule_count,
        "is_baseline": r.diff is None,
        "diff": r.diff,
        "ack": None if r.ack_at is None else {
            "at": r.ack_at.isoformat(), "note": r.ack_note or "",
        },
    } for r in rows]}

@router.post("/fw-rule-changes/{snapshot_id}/analyze", dependencies=[Depends(require_admin)])
async def fw_rule_change_analyze(
    snapshot_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """對一筆規則異動產 AI 解讀卡（admin 限定，按需觸發）。

    偵測與告警永遠是確定性的；這裡是解讀層 —— 帶上目標位址的全系統整合證據
    （IPAM／ARP／Wazuh／DNS／NAT 曝露／虛擬化／管理單位）讓模型判讀。
    """
    from fastapi import HTTPException

    from app.models.fw_snapshot import FwRuleSnapshot
    from app.services.fw_review import analyze_change

    snap = await session.get(FwRuleSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, detail=ui_detail("anom_snapshot_not_found", "找不到這筆快照"))
    if not snap.diff:
        raise HTTPException(422, detail=ui_detail("anom_first_snapshot_no_diff", "初次快照是比對基準，沒有異動可以解讀"))
    try:
        result = await analyze_change(session, user, snap)
    except Exception as exc:
        raise HTTPException(502, detail=ui_detail("anom_fw_analyze_failed", f"AI 解讀失敗：{exc}", reason=str(exc)[:300])) from exc
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="fw_analyze",
        diff={"snapshot": str(snapshot_id)})
    return result

@router.post("/fw-rule-changes/{snapshot_id}/ack", dependencies=[Depends(require_admin)])
async def fw_rule_change_ack(
    snapshot_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """認可一筆規則異動：這是已知變更＋說明（合規證據鏈）。

    沒被認可的異動累積起來就是稽核報表：「本月 N 筆防火牆變更，M 筆無人說明」。
    """
    from datetime import UTC, datetime

    from fastapi import HTTPException

    from app.models.fw_snapshot import FwRuleSnapshot

    snap = await session.get(FwRuleSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, detail=ui_detail("anom_snapshot_not_found", "找不到這筆快照"))
    if snap.diff is None:
        raise HTTPException(422, detail=ui_detail("anom_first_snapshot_no_ack", "初次快照是比對基準，不需要認可"))
    snap.ack_by = user.id
    snap.ack_at = datetime.now(UTC)
    snap.ack_note = str(payload.get("note") or "")[:500]
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="fw_ack",
        diff={"snapshot": str(snapshot_id), "note": snap.ack_note})
    await session.commit()
    return {"ok": True, "at": snap.ack_at.isoformat()}

@router.get("/attack-surface")
async def get_attack_surface(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    _gr: Annotated[None, Depends(require_global_read)],
) -> dict[str, Any]:
    """對外開放服務清單（require_global_read：稽核員這類萬用唯讀帳號正是它的受眾）。

    只列明確可判定的對外開口；目的是別名／any／網段的規則不展開猜測 ——
    稽核拿去簽名的清單不能有猜的成分。
    """
    from app.services.attack_surface_ai import surface_key
    from app.services.fw_lookup import attack_surface
    from app.services.surface_service import enrich
    from app.services.system_config import get_llm_config
    items = await enrich(session, await attack_surface(session))   # 每列的服務判定（證據等級，2026-10-19）
    for it in items:   # AI 分析時前端送回畫面上那些列的 key（後端照 key 重挑，不採信前端送來的內容）
        it["key"] = surface_key(it)
    # 範圍說明由前端 i18n 提供（surface.scope_note）——後端硬寫中文會讓英文介面
    # 冒出中文（使用者截圖）。
    return {"items": items, "ai_available": bool(getattr(await get_llm_config(session), "enabled", False))}


class SurfaceAnalyzeIn(StrictModel):
    #: 畫面上（篩選後）那些列的 key；不給＝整份清單
    keys: Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=5000)] | None = None


@router.post("/attack-surface/analyze/stream")
async def attack_surface_analyze(
    payload: SurfaceAnalyzeIn,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _gr: Annotated[None, Depends(require_global_read)],
) -> Any:
    """對外開放服務清單的 AI 分析（SSE，邊寫邊送）。

    統計由程式算（services/attack_surface_ai），模型只寫判讀；跑幾十秒是常態，
    nginx 對 /api/ 只等 30 秒，所以模型還沒吐字時每隔幾秒送保活。
    """
    import asyncio
    import json as _json
    import time

    from fastapi.responses import StreamingResponse

    from app.core import sse
    from app.services import ai as ai_mod
    from app.services.attack_surface_ai import (
        build_prompt,
        run_analysis,
        surface_facts,
        surface_key,
    )
    from app.services.fw_lookup import attack_surface
    from app.services.surface_service import enrich
    items = await attack_surface(session)
    if payload.keys is not None:
        want = set(payload.keys)
        items = [i for i in items if surface_key(i) in want]
    items = await enrich(session, items)
    facts = surface_facts(items)
    await append_audit(
        session, actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
        object_type="anomaly", object_id=None, action="surface_analyze",
        diff={"rows": len(items), "filtered": payload.keys is not None})
    await session.commit()
    prompt = build_prompt(facts, items) + await ai_mod.answer_language(session, user)

    async def gen() -> Any:
        started = time.monotonic()
        queue: list[str] = []

        async def on_chunk(text: str, kind: str) -> None:
            queue.append(_json.dumps({"type": kind, "text": text,
                                      "elapsed": round(time.monotonic() - started, 1)}, ensure_ascii=False))

        task = asyncio.create_task(run_analysis(session, prompt, on_chunk=on_chunk))
        last_sent = time.monotonic()
        try:
            while not task.done() or queue:
                while queue:
                    yield f"data: {queue.pop(0)}\n\n"
                    last_sent = time.monotonic()
                if task.done():
                    break
                if time.monotonic() - last_sent >= sse.KEEPALIVE_SECONDS:
                    yield sse.KEEPALIVE      # 模型還沒吐字：保活，免得反向代理逾時切斷
                    last_sent = time.monotonic()
                await asyncio.sleep(0.05)
            text, model = await task
            yield "data: " + _json.dumps(
                {"type": "done", "text": (text or "").strip(), "model": model, "facts": facts,
                 "elapsed": round(time.monotonic() - started, 1),
                 "disclaimer": "此為語言模型依清單統計所做的判讀，請對照原始資料後再行動。"},
                ensure_ascii=False) + "\n\n"
        except Exception as exc:   # 模型不可用不可以變成 500；清單本身照樣在畫面上
            admin = bool(getattr(user, "is_admin", False))
            if isinstance(exc, (ai_mod.AIError, TimeoutError)):
                err = ai_mod.ai_error_event(exc, admin=admin)
            else:
                err = ai_mod.ai_error_event(exc, admin=admin, code="ai_failed", reason=type(exc).__name__)
            yield "data: " + _json.dumps({"type": "error", **err}, ensure_ascii=False) + "\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 排程設定 ────────────────────────────────────────────────────────────────


class AnomalyScheduleIn(StrictModel):
    """排程設定。形狀刻意與巡檢排程一致 —— 使用者在兩個地方看到的是同一套語意。"""

    schedule_enabled: bool | None = None
    times: list[Annotated[str, Field(max_length=5)]] | None = None
    frequency: Literal["daily", "weekly", "monthly", "interval"] | None = None
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] | None = None
    month_day: Annotated[int, Field(ge=1, le=31)] | None = None
    # 「每隔 N 分鐘」。上限一天：再長就該用每日排程，語意也比較清楚
    interval_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None


@router.get("/schedule", dependencies=[Depends(require_admin)])
async def get_schedule(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    from app.services.system_config import get_anomaly_config, get_anomaly_last_run

    cfg = await get_anomaly_config(session)
    last = await get_anomaly_last_run(session)
    return {
        "schedule_enabled": cfg.schedule_enabled,
        "times": cfg.times,
        "frequency": cfg.frequency,
        "weekdays": cfg.weekdays,
        "month_day": cfg.month_day,
        "interval_minutes": cfg.interval_minutes,
        "last_run_at": last.isoformat() if last else None,
    }


@router.put("/schedule", dependencies=[Depends(require_admin)])
async def put_schedule(
    payload: AnomalyScheduleIn,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    from app.services.system_config import get_anomaly_last_run, set_anomaly_config

    cfg = await set_anomaly_config(
        session,
        schedule_enabled=payload.schedule_enabled,
        times=payload.times,
        frequency=payload.frequency,
        weekdays=payload.weekdays,
        month_day=payload.month_day,
        interval_minutes=payload.interval_minutes,
    )
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="anomaly", object_id=None, action="update_schedule",
        diff={"schedule_enabled": cfg.schedule_enabled, "times": cfg.times,
              "frequency": cfg.frequency, "weekdays": cfg.weekdays,
              "month_day": cfg.month_day, "interval_minutes": cfg.interval_minutes},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    last = await get_anomaly_last_run(session)
    return {
        "schedule_enabled": cfg.schedule_enabled,
        "times": cfg.times,
        "frequency": cfg.frequency,
        "weekdays": cfg.weekdays,
        "month_day": cfg.month_day,
        "interval_minutes": cfg.interval_minutes,
        "last_run_at": last.isoformat() if last else None,
    }


# ── 逐 IP 的忽略清單 ────────────────────────────────────────────────────────


class AnomalyIgnoreIn(StrictModel):
    """要忽略哪幾類異常。空清單＝全部恢復報告。"""

    categories: list[Annotated[str, Field(max_length=32)]]


@router.put("/ignore/{ip_id}", dependencies=[Depends(require_admin)])
async def set_ignore(
    ip_id: uuid.UUID,
    payload: AnomalyIgnoreIn,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """把某個 IP 標記為「這幾類異常不用再報」。

    這是給「本來就會這樣」的位址用的 —— 最典型的是開了隱私隨機化的裝置
    （Windows 11 / macOS / iOS / Android）每次連線都換 MAC。沒有這個機制，
    使用者只能把整條規則關掉，連真正的 IP 搶用也一起看不到。
    """
    from app.models.address import IPAddress

    ipa = await session.get(IPAddress, ip_id)
    if ipa is None:
        raise HTTPException(status_code=404, detail="IP not found")
    # 只接受清單裡的類別 —— 亂塞字串進 JSONB 之後沒有人查得出那是什麼
    cats = sorted({c for c in payload.categories if c in set(ANOMALY_IGNORABLE)})
    before = list(ipa.anomaly_ignore or [])
    ipa.anomaly_ignore = cats
    await append_audit(
        session,
        actor_user_id=str(user.id),
        actor_ip=request.client.host if request.client else None,
        actor_user_agent=request.headers.get("user-agent"),
        object_type="ip", object_id=str(ipa.id), action="anomaly_ignore",
        diff={"before": before, "after": cats},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return {"ip_id": str(ipa.id), "categories": cats}


@router.get("/ignorable", dependencies=[Depends(require_admin)])
async def list_ignorable() -> dict[str, Any]:
    """可以逐 IP 忽略的類別 —— 前端用它產生選項，不要自己再寫一份清單。"""
    return {"categories": list(ANOMALY_IGNORABLE)}
