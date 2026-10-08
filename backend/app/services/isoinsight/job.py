"""ISOinsight 的工作：測試連線、預覽、同步、排程（規格 §6、§10）。

同步的順序：取得鎖 → 讀設定 → 登入 → 下載 → 驗證結構 → 正規化 → 配對 → 交易寫入 → 更新摘要 → 釋放
Session 與鎖。

- **鎖**：來源列上的 `running_since`／`running_token`，以條件式 UPDATE 取得（跨行程：網頁程序的手動
  同步與 jt-ipam-sync 的排程共用）。測試與預覽也拿同一把鎖 —— 它們一樣會送帳密登入，跟同步同時登入
  可能讓設備把前一個 Session 踢掉。工作被砍掉留下的鎖，超過 `LOCK_STALE` 視為殘留
- **交易**：登入／下載／結構驗證失敗時什麼都不改；寫入在一個交易裡，提交前重新確認來源仍啟用、
  設定版本沒變，對不上就整批還原
- **狀態**：最終狀態用乾淨的 session 寫（工作那個 session 壞了也寫得進去）；錯誤訊息一律先遮蔽
- **排程**：沿用 jt-ipam-sync（systemd timer 每 5 分鐘）；登入失敗後暫停排程（`auth_hold`）避免反覆送錯的
  帳密觸發帳號鎖定，改設定或手動成功一次就解除；429 依 Retry-After 延後
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.safe_http import check_addrs
from app.core.sqlin import in_values
from app.models.isoinsight import IsoInsightSource, IsoInsightSyncRun
from app.services.isoinsight import config as cfg
from app.services.isoinsight import reconcile
from app.services.isoinsight.client import IsoClient, Settings, anonymous_probe
from app.services.isoinsight.errors import IsoError
from app.services.isoinsight.parser import Parsed, parse_leases

log = logging.getLogger(__name__)

#: 出站位址政策（測試以本機 mock 伺服器驗證時換掉；正式環境就是既有的 check_addrs）
OUTBOUND_CHECK: Any = check_addrs
#: 工作被砍掉時留下的鎖，過了這麼久視為殘留（正常工作有整次時限，遠低於這個值）
LOCK_STALE = timedelta(minutes=15)
#: 同步記錄的保留
RUN_RETENTION = timedelta(days=90)
RUN_KEEP_PER_SOURCE = 500
#: 排程的容許誤差：timer 每 5 分鐘、還有隨機延遲，差幾秒不該就跳過一整輪
SCHEDULE_SLACK = timedelta(seconds=30)
#: 預覽回給畫面的筆數上限（計數照樣是全部）
PREVIEW_ROWS = 500


def _factory(session_factory: Any) -> async_sessionmaker[AsyncSession]:
    if session_factory is not None:
        return session_factory
    from app.core.db import SessionLocal
    return SessionLocal


def _now() -> datetime:
    return datetime.now(UTC)


# ── 鎖 ───────────────────────────────────────────────────────────────────────

async def claim(sf: Any, source_id: uuid.UUID) -> uuid.UUID | None:
    tok = uuid.uuid4()
    now = _now()
    async with sf() as s:
        got = (await s.execute(
            update(IsoInsightSource)
            .where(IsoInsightSource.id == source_id,
                   or_(IsoInsightSource.running_since.is_(None),
                       IsoInsightSource.running_since < now - LOCK_STALE))
            .values(running_since=now, running_token=tok)
            .returning(IsoInsightSource.id))).scalar_one_or_none()
        await s.commit()
    return tok if got is not None else None


async def release(sf: Any, source_id: uuid.UUID, tok: uuid.UUID) -> None:
    try:
        async with sf() as s:
            await s.execute(update(IsoInsightSource)
                            .where(IsoInsightSource.id == source_id, IsoInsightSource.running_token == tok)
                            .values(running_since=None, running_token=None))
            await s.commit()
    except Exception:            # 釋放失敗就等它逾時，不讓工作本身變失敗
        log.exception("isoinsight: releasing the lock of %s failed", source_id)


def already_running(source_id: uuid.UUID) -> IsoError:
    return IsoError("SYNC_ALREADY_RUNNING", "another job is running for this source", stage="lock",
                    source=str(source_id))


# ── 記錄 ─────────────────────────────────────────────────────────────────────

async def _start_run(sf: Any, src: IsoInsightSource, *, kind: str, trigger: str, actor_user_id: Any,
                     task_id: Any) -> uuid.UUID:
    run = IsoInsightSyncRun(source_id=src.id, kind=kind, trigger=trigger, actor_user_id=actor_user_id,
                            background_task_id=task_id, config_version=src.config_version,
                            started_at=_now(), result="running", stage="lock",
                            login_method=src.login_method, auth_mode=src.auth_mode)
    async with sf() as s:
        s.add(run)
        await s.commit()
        return run.id


async def _finish_run(sf: Any, run_id: uuid.UUID, source_id: uuid.UUID, t0: float, **values: Any) -> None:
    """最終狀態：乾淨的 session、UPDATE；順便清掉過舊的記錄。絕不 raise。"""
    values["finished_at"] = _now()
    values["duration_ms"] = int((time.monotonic() - t0) * 1000)
    try:
        async with sf() as s:
            await s.execute(update(IsoInsightSyncRun).where(IsoInsightSyncRun.id == run_id).values(**values))
            await s.execute(delete(IsoInsightSyncRun).where(
                IsoInsightSyncRun.source_id == source_id,
                IsoInsightSyncRun.started_at < _now() - RUN_RETENTION))
            keep = (select(IsoInsightSyncRun.id).where(IsoInsightSyncRun.source_id == source_id)
                    .order_by(IsoInsightSyncRun.started_at.desc()).limit(RUN_KEEP_PER_SOURCE))
            await s.execute(delete(IsoInsightSyncRun).where(
                IsoInsightSyncRun.source_id == source_id, IsoInsightSyncRun.id.not_in(keep)))
            await s.commit()
    except Exception:
        log.exception("isoinsight: writing the final state of run %s failed", run_id)


def _err_values(exc: IsoError, secrets: list[str]) -> dict[str, Any]:
    return {"result": "failed", "stage": exc.stage, "http_status": exc.http_status,
            "error_code": exc.spec_code, "error_detail": cfg.scrub(str(exc), secrets)[:500]}


async def _update_source(sf: Any, source_id: uuid.UUID, **values: Any) -> None:
    try:
        async with sf() as s:
            await s.execute(update(IsoInsightSource).where(IsoInsightSource.id == source_id).values(**values))
            await s.commit()
    except Exception:
        log.exception("isoinsight: updating the status of %s failed", source_id)


# ── 抓取 ─────────────────────────────────────────────────────────────────────

async def _fetch(settings: Settings, src_cfg: dict[str, Any], *, check: Any, now: datetime,
                 trace: list[dict[str, Any]]) -> Parsed:
    """登入＋讀租約＋結構驗證（整次時限內）。逐步記錄寫進 `trace`（已遮蔽）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + src_cfg["job_timeout"]
    cli = IsoClient(settings, check=check, deadline=deadline)

    def stage() -> str:
        return cli.trace[-1]["stage"] if cli.trace else "login"

    try:
        async with asyncio.timeout(src_cfg["job_timeout"]):
            async with cli:
                await cli.login()
                fetched = await cli.fetch_leases()
    except IsoError:
        raise
    except TimeoutError as exc:
        raise IsoError("JOB_TIMEOUT", f"the job exceeded {src_cfg['job_timeout']} seconds",
                       stage=stage(), seconds=src_cfg["job_timeout"]) from exc
    except Exception as exc:
        # 意外的錯誤也要收成一筆失敗記錄（否則記錄永遠停在「執行中」）；只帶類別名稱，不帶可能含網址的原文
        log.exception("isoinsight: unexpected error while fetching")
        raise IsoError("INTERNAL_ERROR", f"unexpected {type(exc).__name__}", stage=stage()) from exc
    finally:
        trace.extend(cli.trace)
    try:
        parsed = parse_leases(fetched.status, fetched.content_type, fetched.body, tz=src_cfg["tz"], now=now,
                              max_rows=src_cfg["max_rows"])
    except IsoError:
        raise
    except Exception as exc:          # 例如 JSON 巢狀過深（RecursionError）：一樣是「不是預期的租約 JSON」
        raise IsoError("INVALID_RESPONSE", f"the response could not be parsed ({type(exc).__name__})",
                       stage="validate", kind="parse") from exc
    trace.append({"stage": "validate", "result": "ok", "rows": parsed.fetched, "valid": parsed.fetched - parsed.invalid,
                  "warnings": parsed.warnings})
    return parsed


def _cfg_of(src: IsoInsightSource) -> dict[str, Any]:
    return {"tz": src.source_timezone, "max_rows": int(src.max_rows), "job_timeout": int(src.job_timeout_seconds),
            "version": src.config_version, "name": src.name}


def _parsed_values(parsed: Parsed) -> dict[str, Any]:
    return {"fetched": parsed.fetched, "valid": parsed.fetched - parsed.invalid, "invalid": parsed.invalid,
            "duplicates": parsed.duplicates, "quality": parsed.quality_counts(),
            "warnings": parsed.warnings or None}


# ── 同步 ─────────────────────────────────────────────────────────────────────

async def run_sync(source_id: uuid.UUID, *, trigger: str = "manual", actor_user_id: Any = None,
                   task_id: Any = None, session_factory: Any = None, check: Any = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """同步一次。回傳摘要；失敗時摘要的 result＝failed（呼叫端決定要不要當成作業失敗）。

    拿不到鎖 → raise SYNC_ALREADY_RUNNING（手動顯示 409；排程記一筆略過）。
    """
    sf = _factory(session_factory)
    check = check or OUTBOUND_CHECK
    tok = await claim(sf, source_id)
    if tok is None:
        raise already_running(source_id)
    t0 = time.monotonic()
    try:
        async with sf() as s:
            src = await s.get(IsoInsightSource, source_id)
            if src is None:
                raise IsoError("CONFIG_INVALID", "the source no longer exists", stage="config")
            conf = _cfg_of(src)
            run_id = await _start_run(sf, src, kind="sync", trigger=trigger, actor_user_id=actor_user_id,
                                      task_id=task_id)
            if not src.enabled:
                await _finish_run(sf, run_id, source_id, t0, result="skipped", stage="config",
                                  error_code="SOURCE_DISABLED")
                return {"result": "skipped", "error_code": "SOURCE_DISABLED", "run_id": str(run_id)}
            try:
                settings = Settings.from_source(src)
            except IsoError as exc:
                await _finish_run(sf, run_id, source_id, t0, **_err_values(exc, []))
                await _update_source(sf, source_id, last_attempt_at=_now(), last_result="failed",
                                     last_error_code=exc.spec_code, last_error=str(exc))
                return {"result": "failed", "error_code": exc.spec_code, "run_id": str(run_id)}
        secrets = settings.secrets()
        started = now or _now()
        await _update_source(sf, source_id, last_attempt_at=started)

        trace: list[dict[str, Any]] = []
        try:
            parsed = await _fetch(settings, conf, check=check, now=started, trace=trace)
        except IsoError as exc:
            vals = _err_values(exc, secrets)
            await _finish_run(sf, run_id, source_id, t0, stages=trace, **vals)
            src_vals: dict[str, Any] = {"last_result": "failed", "last_error_code": exc.spec_code,
                                        "last_error": vals["error_detail"]}
            if exc.spec_code == "AUTH_FAILED":
                src_vals["auth_hold"] = True
            if exc.spec_code == "RATE_LIMITED":
                src_vals["retry_after_until"] = _now() + timedelta(seconds=exc.retry_after or 300)
            await _update_source(sf, source_id, **src_vals)
            return {"result": "failed", "error_code": exc.spec_code, "error": vals["error_detail"],
                    "run_id": str(run_id)}
        finally:
            del settings

        fetched_at = _now()
        await _update_source(sf, source_id, last_fetch_ok_at=fetched_at)
        summary = await _commit(sf, source_id, conf, parsed, now=started, run_id=run_id, t0=t0, trace=trace)
        summary["run_id"] = str(run_id)
        return summary
    finally:
        await release(sf, source_id, tok)


async def _commit(sf: Any, source_id: uuid.UUID, conf: dict[str, Any], parsed: Parsed, *, now: datetime,
                  run_id: uuid.UUID, t0: float, trace: list[dict[str, Any]]) -> dict[str, Any]:
    """交易寫入。提交前重新確認設定版本與啟用狀態；失敗整批還原（既有資料保留）。"""
    base = _parsed_values(parsed)
    async with sf() as s:
        try:
            src = (await s.execute(select(IsoInsightSource).where(IsoInsightSource.id == source_id)
                                   .with_for_update())).scalar_one_or_none()
            if src is None or not src.enabled:
                await s.rollback()
                await _finish_run(sf, run_id, source_id, t0, result="skipped", stage="write",
                                  error_code="SOURCE_DISABLED", stages=trace, **base)
                return {"result": "skipped", "error_code": "SOURCE_DISABLED", **base}
            if src.config_version != conf["version"]:
                await s.rollback()
                exc = IsoError("CONFIG_CHANGED", "the source settings changed while the job was running; "
                               "nothing was committed", stage="write")
                await _finish_run(sf, run_id, source_id, t0, stages=trace, **_err_values(exc, []), **base)
                return {"result": "failed", "error_code": "CONFIG_CHANGED", **base}
            pl = await reconcile.plan(s, src, parsed, now=now)
            extra = await reconcile.apply(s, src, parsed, pl, now=now)
            counts = pl.counts()
            ok = not parsed.has_problems() and counts["unmatched"] == 0 and counts["conflicts"] == 0
            result = "success" if ok else "partial"
            summary = {"result": result, **base, **counts, "scope_excluded": pl.scope_excluded,
                       "carried_over": extra["carried_over"]}
            committed = _now()
            src.last_result = result
            src.last_commit_at = committed
            if ok:
                src.last_full_success_at = committed
            src.last_error_code = None if ok else ("SUBNET_UNMAPPED" if counts["unmatched"] else None)
            src.last_error = None
            src.last_summary = summary
            src.auth_hold = False
            src.retry_after_until = None
            await s.commit()
        except Exception as exc:
            await s.rollback()
            log.exception("isoinsight: write transaction for %s failed", source_id)
            err = IsoError("WRITE_FAILED", f"the write transaction failed and was rolled back "
                           f"({type(exc).__name__}); existing data is unchanged", stage="write")
            await _finish_run(sf, run_id, source_id, t0, stages=trace, **_err_values(err, []), **base)
            await _update_source(sf, source_id, last_result="failed", last_error_code="WRITE_FAILED",
                                 last_error=str(err))
            return {"result": "failed", "error_code": "WRITE_FAILED", **base}
    await _finish_run(sf, run_id, source_id, t0, result=result, stage="done", stages=trace,
                      **{k: v for k, v in summary.items() if k in reconcile.BUCKETS}, **base)
    return summary


# ── 測試連線／預覽 ──────────────────────────────────────────────────────────

async def run_test(source_id: uuid.UUID, *, actor_user_id: Any = None, anonymous: bool = False,
                   session_factory: Any = None, check: Any = None) -> dict[str, Any]:
    """分階段測試：登入 → 讀租約 → 結構驗證。全部通過才是「連線與讀取成功」。不寫任何 IP／租約資料。"""
    return await _probe(source_id, kind="test", actor_user_id=actor_user_id, anonymous=anonymous,
                        session_factory=session_factory, check=check)


async def run_preview(source_id: uuid.UUID, *, actor_user_id: Any = None, session_factory: Any = None,
                      check: Any = None) -> dict[str, Any]:
    """預覽：測試的全部步驟＋子網路配對結果（尚未套用；不寫任何 IP／租約資料）。"""
    return await _probe(source_id, kind="preview", actor_user_id=actor_user_id, anonymous=False,
                        session_factory=session_factory, check=check)


async def _probe(source_id: uuid.UUID, *, kind: str, actor_user_id: Any, anonymous: bool,
                 session_factory: Any, check: Any) -> dict[str, Any]:
    sf = _factory(session_factory)
    check = check or OUTBOUND_CHECK
    tok = await claim(sf, source_id)
    if tok is None:
        raise already_running(source_id)
    t0 = time.monotonic()
    try:
        async with sf() as s:
            src = await s.get(IsoInsightSource, source_id)
            if src is None:
                raise IsoError("CONFIG_INVALID", "the source no longer exists", stage="config")
            conf = _cfg_of(src)
            run_id = await _start_run(sf, src, kind=kind, trigger="manual", actor_user_id=actor_user_id,
                                      task_id=None)
            try:
                settings = Settings.from_source(src)
            except IsoError as exc:
                await _finish_run(sf, run_id, source_id, t0, **_err_values(exc, []))
                return {"ok": False, "error_code": exc.spec_code, "error": str(exc), "stages": [],
                        "run_id": str(run_id)}
        secrets = settings.secrets()
        now = _now()
        trace: list[dict[str, Any]] = []
        out: dict[str, Any] = {"run_id": str(run_id), "applied": False}
        try:
            parsed = await _fetch(settings, conf, check=check, now=now, trace=trace)
        except IsoError as exc:
            vals = _err_values(exc, secrets)
            await _finish_run(sf, run_id, source_id, t0, stages=trace, **vals)
            out.update(ok=False, error_code=exc.spec_code, error=vals["error_detail"], stage=exc.stage,
                       http_status=exc.http_status, params=_safe_params(exc, secrets), stages=trace)
            if anonymous:
                out["anonymous"] = await _anonymous(settings, check)
            return out
        base = _parsed_values(parsed)
        out.update(ok=True, error_code=None, stages=trace, summary=base)
        if anonymous:
            out["anonymous"] = await _anonymous(settings, check)
        values: dict[str, Any] = {"result": "partial" if parsed.has_problems() else "success",
                                  "stage": "done", "stages": trace, **base}
        if kind == "preview":
            try:
                async with sf() as s:
                    src = await s.get(IsoInsightSource, source_id)
                    if src is None:
                        raise IsoError("CONFIG_INVALID", "the source no longer exists", stage="config")
                    pl = await reconcile.plan(s, src, parsed, now=now)
                    await s.rollback()            # 預覽只讀：保險起見不留任何東西
            except Exception as exc:
                log.exception("isoinsight: preview matching failed for %s", source_id)
                err = exc if isinstance(exc, IsoError) else IsoError(
                    "INTERNAL_ERROR", f"unexpected {type(exc).__name__} while matching", stage="match")
                await _finish_run(sf, run_id, source_id, t0, stages=trace, **_err_values(err, secrets), **base)
                out.update(ok=False, error_code=err.spec_code, error=str(err), stage=err.stage)
                return out
            counts = pl.counts()
            out["counts"] = counts
            out["scope_excluded"] = pl.scope_excluded
            out["rows"] = _preview_rows(pl)
            out["rows_total"] = len(pl.outcomes)
            values.update({k: v for k, v in counts.items()})
            if counts["unmatched"] or counts["conflicts"]:
                values["result"] = "partial"
        await _finish_run(sf, run_id, source_id, t0, **values)
        ok_vals: dict[str, Any] = {"last_test_ok_at": _now(), "auth_hold": False}
        if kind == "preview":
            ok_vals["preview_ok_at"] = _now()
        await _update_source_if_version(sf, source_id, conf["version"], **ok_vals)
        return out
    finally:
        await release(sf, source_id, tok)


async def _update_source_if_version(sf: Any, source_id: uuid.UUID, version: int, **values: Any) -> None:
    """成功標記只在設定版本沒變時才寫：測試途中改了帳密，舊的成功不算數。"""
    try:
        async with sf() as s:
            await s.execute(update(IsoInsightSource).where(
                IsoInsightSource.id == source_id, IsoInsightSource.config_version == version).values(**values))
            await s.commit()
    except Exception:
        log.exception("isoinsight: updating the status of %s failed", source_id)


def _safe_params(exc: IsoError, secrets: list[str]) -> dict[str, Any]:
    return {k: (cfg.scrub(v, secrets) if isinstance(v, str) else v) for k, v in (exc.params or {}).items()
            if k not in ("reason",)}


async def _anonymous(settings: Settings, check: Any) -> dict[str, Any]:
    got = await anonymous_probe(settings, check=check)
    return {"status": got["status"], "readable": got["readable"], "error_code": got["error_code"]}


_PREVIEW_ORDER = {"conflicts": 0, "unmatched": 1, "unknown_time": 2, "created": 3, "unchanged": 4,
                  "observed_only": 5, "expired": 6, "updated": 4}


def _preview_rows(pl: reconcile.Plan) -> list[dict[str, Any]]:
    import ipaddress as _ip
    rows = sorted(pl.outcomes, key=lambda o: (_PREVIEW_ORDER.get(o.bucket, 9), _ip.ip_address(o.lease.ip)))
    out = []
    for o in rows[:PREVIEW_ROWS]:
        le = o.lease
        # 預覽是「尚未套用」：會新增／會套用，而不是已新增／已更新
        bucket = {"created": "would_create", "unchanged": "would_apply", "updated": "would_apply"}.get(o.bucket,
                                                                                                    o.bucket)
        out.append({"ip": le.ip, "mac": le.mac, "name": le.name,
                    "start": le.start.isoformat() if le.start else None,
                    "end": le.end.isoformat() if le.end else None,
                    "start_raw": le.start_raw, "end_raw": le.end_raw, "state": le.state,
                    "quality": sorted(le.quality), "raw_count": le.raw_count, "bucket": bucket,
                    "reason": o.reason, "match_status": o.match_status, "subnet_cidr": o.subnet_cidr,
                    "ip_address_id": str(o.ip_address_id) if o.ip_address_id else None})
    return out


# ── 排程 ─────────────────────────────────────────────────────────────────────

async def due_source_ids(session: AsyncSession, now: datetime) -> list[uuid.UUID]:
    """這一輪輪到的來源：啟用、開了排程、沒有因登入失敗暫停、沒有在 429 的延後期間、距上次嘗試已超過間隔。
    還沒預覽成功的也算輪到，由 run_due 記下「要先預覽」（不同步），畫面上才看得出排程為什麼沒動。"""
    rows = (await session.execute(select(
        IsoInsightSource.id, IsoInsightSource.last_attempt_at, IsoInsightSource.sync_interval_seconds).where(
        IsoInsightSource.enabled.is_(True), IsoInsightSource.schedule_enabled.is_(True),
        IsoInsightSource.auth_hold.is_(False),
        or_(IsoInsightSource.retry_after_until.is_(None), IsoInsightSource.retry_after_until <= now),
    ).order_by(IsoInsightSource.name))).all()
    out = []
    for sid, last, interval in rows:
        if last is None or last + timedelta(seconds=max(60, int(interval))) - SCHEDULE_SLACK <= now:
            out.append(sid)
    return out


async def run_due(session_factory: Any = None, now: datetime | None = None,
                  check: Any = None) -> list[dict[str, Any]]:
    """jt-ipam-sync 每輪呼叫一次：跑到期的來源。每個來源一筆結果（含名稱，給作業頁的心跳列）。"""
    sf = _factory(session_factory)
    now = now or _now()
    async with sf() as s:
        ids = await due_source_ids(s, now)
        info = {r.id: r for r in (await s.execute(select(
            IsoInsightSource.id, IsoInsightSource.name, IsoInsightSource.preview_ok_at).where(
            in_values(IsoInsightSource.id, ids)))).all()} if ids else {}
    names = {sid: r.name for sid, r in info.items()}
    results = []
    for sid in ids:
        if sid in info and info[sid].preview_ok_at is None:
            # 預設開啟的排程在第一次成功預覽前不同步：不連線、不留同步記錄（免得洗掉手動同步的記錄），
            # 只把原因寫在來源上；記下嘗試時間，照間隔再檢查
            await _update_source(sf, sid, last_attempt_at=now, last_result="skipped",
                                 last_error_code="PREVIEW_REQUIRED",
                                 last_error="the source has no successful preview with the current settings")
            results.append({"id": sid, "name": names[sid], "result": "skipped", "error_code": "PREVIEW_REQUIRED"})
            continue
        try:
            summary = await run_sync(sid, trigger="scheduled", session_factory=sf, check=check)
        except IsoError as exc:
            if exc.spec_code != "SYNC_ALREADY_RUNNING":
                raise
            summary = {"result": "skipped", "error_code": exc.spec_code}
        results.append({"id": sid, "name": names.get(sid, str(sid)), **summary})
    return results


async def count_running(session: AsyncSession) -> int:
    now = _now()
    return int((await session.execute(select(func.count()).select_from(IsoInsightSource).where(
        IsoInsightSource.running_since.is_not(None),
        IsoInsightSource.running_since >= now - LOCK_STALE))).scalar_one())
