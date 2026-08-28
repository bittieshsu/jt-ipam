"""套用匯出包的 inner payload 到目標機。

- 保留來源 UUID → 外鍵與機密 AAD 自動成立。
- merge：依主鍵 upsert（ON CONFLICT DO UPDATE），冪等。
- replace：先反相依序清空 in-scope 表（保護目前登入的 admin 那列，避免自斷 session），再 upsert。
- 每列包 SAVEPOINT（begin_nested）做錯誤隔離，單列失敗只計數不中斷整批。
- dry_run：全程照跑但最後 rollback，不落地（回預覽計數）。
- 向下相容：只寫「目標表存在的欄位」；匯出包多出的未知欄位忽略、缺的欄位吃預設。
"""

from __future__ import annotations

import base64
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Date, DateTime, LargeBinary, Time, delete, select
from sqlalchemy import and_ as sa_and
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Uuid as GenericUUID

from app.services.system_transfer import registry, secrets


@dataclass
class TableResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errored: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted, "updated": self.updated,
            "skipped": self.skipped, "errored": self.errored,
            "errors": self.errors[:20],
        }


def _coerce(table, row: dict[str, Any]) -> dict[str, Any]:
    """依目標表欄位型別轉換值；只保留目標表存在的欄位（向下相容關鍵）。"""
    cols = table.columns
    out: dict[str, Any] = {}
    for name, val in row.items():
        if name not in cols:
            continue  # 未知欄位（新版匯出→舊實例）忽略
        col = cols[name]
        if val is None:
            out[name] = None
            continue
        ctype = col.type
        if isinstance(ctype, LargeBinary):
            out[name] = base64.b64decode(val) if isinstance(val, str) else val
        elif isinstance(ctype, (DateTime, Date, Time)):
            out[name] = _parse_temporal(val)
        elif isinstance(ctype, (PGUUID, GenericUUID)):
            out[name] = uuid.UUID(val) if isinstance(val, str) else val
        else:
            out[name] = val
    return out


def _parse_temporal(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    try:
        return _dt.datetime.fromisoformat(val)
    except ValueError:
        try:
            return _dt.date.fromisoformat(val)
        except ValueError:
            return val


def deferred_fk_columns(name: str) -> set[str]:
    """這張表裡「指向還沒建立的那一列」的外鍵欄位。

    匯入照外鍵相依序逐表寫入，但有些欄位是往後指的：`devices.primary_ip_id` 指向排在
    後面的 `ip_addresses`，`sections.parent_id` / `subnets.master_subnet_id` /
    `device_ports.peer_port_id` 則是指向同一張表的其他列（同表內誰先誰後由匯出順序決定）。
    寫到那一列時目標還不存在 → 外鍵違反 → **整列進不去**。

    客戶回報的「裝置少了一半」就是這個：設了主要 IP 的裝置全部失敗、沒設的照常匯入，
    看起來像隨機掉資料。所以這些欄位先留空，等所有表都寫完再回頭補上（見 `_apply_deferred`）。

    只延後**可為空**的欄位 —— 不可為空的往後指外鍵無法先留空，那種結構要靠調整相依序解決；
    目前 metadata 裡沒有這種欄位。
    """
    table = registry.table_by_name(name)
    order = registry.all_tablenames()
    idx = {n: i for i, n in enumerate(order)}
    here = idx.get(name, -1)
    out: set[str] = set()
    for fk in table.foreign_keys:
        ref = fk.column.table.name
        if idx.get(ref, -1) >= here and fk.parent.nullable:
            out.add(fk.parent.name)
    return out


async def _apply_deferred(
    session: AsyncSession, pending: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> tuple[int, list[str]]:
    """所有表寫完後，把先前留空的往後指外鍵補回去。"""
    fixed, errors = 0, []
    for name, pk, values in pending:
        table = registry.table_by_name(name)
        try:
            async with session.begin_nested():
                cond = sa_and(*[table.c[k] == v for k, v in pk.items()])
                await session.execute(table.update().where(cond).values(**values))
            fixed += 1
        except Exception as exc:
            if len(errors) < 20:
                errors.append(f"{name} pk={pk}: {type(exc).__name__}: {exc}")
    return fixed, errors


def _pk_cols(table) -> list[str]:
    return [c.name for c in table.primary_key.columns]


async def _existing_pks(session: AsyncSession, table) -> set:
    pk = list(table.primary_key.columns)
    if not pk:
        return set()
    result = await session.execute(select(*pk))
    if len(pk) == 1:
        return {r[0] for r in result.all()}
    return {tuple(r) for r in result.all()}


def _pk_value(table, coerced: dict[str, Any]):
    pk = _pk_cols(table)
    vals = tuple(coerced.get(c) for c in pk)
    return vals[0] if len(vals) == 1 else vals


async def _import_table(
    session: AsyncSession, name: str, rows: list[dict[str, Any]], *, mode: str,
    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] | None = None,
) -> TableResult:
    table = registry.table_by_name(name)
    res = TableResult()
    existing = set() if mode == "replace" else await _existing_pks(session, table)
    pk_cols = _pk_cols(table)
    deferred = deferred_fk_columns(name) if pending is not None else set()
    for raw in rows:
        raw = dict(raw)
        sec = raw.pop("__secrets__", None)
        try:
            coerced = _coerce(table, raw)
            if name == "system_settings":
                coerced["value"] = secrets.transform_settings_in(coerced.get("key"), coerced.get("value"))
            secrets.apply_column_secrets(name, coerced, sec)
            secrets.apply_envelope_secrets(name, coerced, sec)
            # 往後指的外鍵先留空，等全部寫完再補（否則整列會因為外鍵違反而消失）。
            # 這裡是把欄位整個拿掉而不是填 None：merge 模式下 upsert 就不會把目標端
            # 既有的值蓋成空的。
            if deferred:
                hold = {c: coerced.pop(c) for c in deferred if c in coerced}
                hold = {c: v for c, v in hold.items() if v is not None}
                if hold and pending is not None:
                    pending.append((name, {c: coerced[c] for c in pk_cols}, hold))
            pkv = _pk_value(table, coerced)
            is_update = pkv in existing
            async with session.begin_nested():
                stmt = pg_insert(table).values(**coerced)
                update_cols = {c: stmt.excluded[c] for c in coerced if c not in pk_cols}
                if update_cols:
                    stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
                else:
                    stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
                await session.execute(stmt)
            if is_update:
                res.updated += 1
            else:
                res.inserted += 1
        except Exception as exc:
            res.errored += 1
            if len(res.errors) < 20:
                res.errors.append(f"{name} pk={raw.get('id', raw.get('key', '?'))}: {type(exc).__name__}: {exc}")
    return res


async def _wipe(session: AsyncSession, names: list[str], *, protect_user_id: uuid.UUID | None) -> None:
    """反相依序清空 in-scope 表；users 表保留目前登入 admin 那列。每表獨立 SAVEPOINT。"""
    for name in reversed(names):
        table = registry.table_by_name(name)
        try:
            async with session.begin_nested():
                stmt = delete(table)
                if name == "users" and protect_user_id is not None:
                    stmt = stmt.where(table.c.id != protect_user_id)
                await session.execute(stmt)
        except Exception:
            pass


async def apply_import(
    session: AsyncSession,
    inner: dict[str, Any],
    *,
    mode: str = "merge",
    dry_run: bool = False,
    scope: list[str] | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """套用匯入。回 report dict（各表計數 + 中央機密計數 + mode/dry_run）。"""
    if mode not in ("merge", "replace"):
        raise ValueError(f"unknown import mode: {mode!r}")
    tables_in = inner.get("tables") or {}
    central_in = inner.get("central_secrets") or []
    # 決定要處理哪些表：交集（匯出包有的 ∩ 相依序）；scope 未給則用匯出包內全部表
    ordered = registry.all_tablenames()
    present = [n for n in ordered if n in tables_in]

    report: dict[str, Any] = {"mode": mode, "dry_run": dry_run, "tables": {}}

    if mode == "replace":
        await _wipe(session, present, protect_user_id=actor_user_id)

    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for name in present:
        res = await _import_table(session, name, tables_in[name], mode=mode, pending=pending)
        report["tables"][name] = res.as_dict()

    if pending:
        fixed, errors = await _apply_deferred(session, pending)
        report["deferred_refs"] = {"fixed": fixed, "total": len(pending), "errors": errors}

    # 中央機密（encrypted_secrets）：以目標金鑰重加密後 upsert
    if central_in:
        report["central_secrets"] = await _import_central(session, central_in, mode=mode)

    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return report


async def _import_central(session: AsyncSession, entries: list[dict[str, Any]], *, mode: str) -> dict[str, Any]:
    table = registry.table_by_name(registry.ENCRYPTED_SECRETS_TABLE)
    res = TableResult()
    pk_cols = ["object_type", "object_id", "field", "key_id"]  # 業務唯一鍵（UniqueConstraint）
    for entry in entries:
        built = secrets.import_central_row(entry)
        if built is None:
            res.skipped += 1
            continue
        try:
            built["object_id"] = uuid.UUID(str(built["object_id"]))
            async with session.begin_nested():
                stmt = pg_insert(table).values(**built)
                stmt = stmt.on_conflict_do_update(
                    index_elements=pk_cols,
                    set_={"ciphertext": stmt.excluded.ciphertext, "nonce": stmt.excluded.nonce},
                )
                await session.execute(stmt)
            res.inserted += 1
        except Exception as exc:
            res.errored += 1
            if len(res.errors) < 20:
                res.errors.append(f"encrypted_secrets {entry.get('object_type')}: {type(exc).__name__}: {exc}")
    return res.as_dict()
