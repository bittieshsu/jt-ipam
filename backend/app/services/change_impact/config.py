"""IP 變更評估的設定（system_settings 的 change_impact 鍵）。預設關閉，在網頁開。"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

KEY = "change_impact"

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    # 生效條件：這個開關「且」LLM 設定已啟用
    "ai_enabled": True,
    # 預設不允許建立者自己覆核自己的計畫（管理員例外）
    "allow_self_review": False,
    # 審核人名單（使用者／群組 id）。空＝對目標有修改權的人都能覆核，送審通知管理員（services/change_impact/reviewers.py）
    "reviewer_user_ids": [],
    "reviewer_group_ids": [],
    "limits": {
        "analysis_seconds": 120,
        "max_findings": 5000,
        "max_evidence": 20000,
        "group_depth": 16,
        "per_customer_active_runs": 2,
        "per_user_ai_jobs": 1,
    },
    "run_valid_hours": 24,
    "default_stale_hours": 24,
    "retention_days": 180,
    "failed_retention_days": 30,
}

# 管理員可以調，但不可以超過硬上限（防資源耗盡，規格 §8.4）
_HARD_LIMITS = {"analysis_seconds": 600, "max_findings": 50000, "max_evidence": 200000, "group_depth": 32,
                "per_customer_active_runs": 10, "per_user_ai_jobs": 5}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


async def get_config(session: AsyncSession) -> dict[str, Any]:
    from app.models.system_setting import SystemSetting
    row = await session.get(SystemSetting, KEY)
    return _merge(DEFAULTS, row.value if row and isinstance(row.value, dict) else {})


def _validate(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["ai_enabled"] = bool(cfg["ai_enabled"])
    cfg["allow_self_review"] = bool(cfg["allow_self_review"])
    for k in ("reviewer_user_ids", "reviewer_group_ids"):
        ids: list[str] = []
        for v in cfg.get(k) or []:
            try:
                u = str(uuid.UUID(str(v)))
            except ValueError:
                continue
            if u not in ids:
                ids.append(u)
        cfg[k] = ids[:200]
    for k, hard in _HARD_LIMITS.items():
        try:
            v = int(cfg["limits"][k])
        except (TypeError, ValueError):
            v = DEFAULTS["limits"][k]
        cfg["limits"][k] = max(1, min(v, hard))
    for k, lo, hi in (("run_valid_hours", 1, 168), ("default_stale_hours", 1, 720),
                      ("retention_days", 7, 3650), ("failed_retention_days", 1, 365)):
        try:
            cfg[k] = max(lo, min(int(cfg[k]), hi))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    return cfg


async def _keep_existing_reviewers(session: AsyncSession, cfg: dict[str, Any]) -> None:
    """名單只留存在的使用者與群組（刪掉的帳號、打錯的 id 不留著）。"""
    from sqlalchemy import select

    from app.core.sqlin import in_values
    from app.models.user import Group, User
    for key, model in (("reviewer_user_ids", User), ("reviewer_group_ids", Group)):
        ids = cfg.get(key) or []
        if not ids:
            continue
        found = {str(i) for i in (await session.execute(select(model.id).where(
            in_values(model.id, [uuid.UUID(i) for i in ids])))).scalars()}
        cfg[key] = [i for i in ids if i in found]


async def set_config(session: AsyncSession, patch: dict[str, Any], *, updated_by: uuid.UUID | None) -> dict[str, Any]:
    """合併後驗證再存；呼叫端負責稽核與 commit。"""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting
    cfg = _validate(_merge(await get_config(session), patch))
    await _keep_existing_reviewers(session, cfg)
    row = await session.get(SystemSetting, KEY)
    if row is None:
        row = SystemSetting(key=KEY, value=cfg, updated_by=updated_by)
        session.add(row)
    else:
        row.value = cfg
        row.updated_by = updated_by
        flag_modified(row, "value")
    return cfg


async def ai_available(session: AsyncSession, cfg: dict[str, Any] | None = None) -> bool:
    from app.services.system_config import get_llm_config
    cfg = cfg or await get_config(session)
    if not cfg.get("ai_enabled"):
        return False
    llm = await get_llm_config(session)
    return bool(getattr(llm, "enabled", False))
