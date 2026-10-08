"""IP 變更評估的審核關卡（「申請審核設定」裡的「IP 變更評估審核」，使用者 2026-10-08）。

模式比照 IP 申請審核（services/ip_request_policy），多一個升級前的預設：
  - editors    ：對目標有修改權限的人（預設；沒存過設定、也沒有舊的審核人名單時）
  - admin      ：只有系統管理員
  - designated ：管理員＋指定的使用者／群組（舊的「審核人名單」就是這個）
  - parallel   ：多組會簽，不分先後，每一組都要有人核准
  - stages     ：依序多關卡，第 1 關通過才輪到第 2 關，最後一關核准才算核准

存 system_settings.change_impact_review_policy。沒存過時從 change_impact 設定的 reviewer_user_ids／
reviewer_group_ids／allow_self_review 推出來，升級後行為不變。
每一關的審核人還是要看得到目標（名單只決定誰可以審，不會讓人看到原本看不到的東西）；管理員任何一關都能核准。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting
from app.services.ip_request_policy import _clean_steps

POLICY_KEY = "change_impact_review_policy"
MODES = ("editors", "admin", "designated", "parallel", "stages")
MULTI_STEP_MODES = ("parallel", "stages")


def _ids(v: Any) -> list[str]:
    out: list[str] = []
    for x in v or []:
        try:
            out.append(str(uuid.UUID(str(x))))
        except (ValueError, TypeError):
            continue
    return out


def from_legacy(cfg: dict[str, Any]) -> dict[str, Any]:
    """舊設定（change_impact 的審核人名單）對應到的審核關卡。"""
    users, groups = _ids(cfg.get("reviewer_user_ids")), _ids(cfg.get("reviewer_group_ids"))
    return {"approver_mode": "designated" if (users or groups) else "editors",
            "designated_user_ids": users, "designated_group_ids": groups,
            "allow_self_approve": bool(cfg.get("allow_self_review")), "stages": [], "saved": False}


def normalize(v: dict[str, Any]) -> dict[str, Any]:
    mode = v.get("approver_mode") if v.get("approver_mode") in MODES else "editors"
    stages = _clean_steps(v.get("stages"))
    for s in stages:
        s["user_ids"], s["group_ids"] = _ids(s["user_ids"]), _ids(s["group_ids"])
    return {"approver_mode": mode, "designated_user_ids": _ids(v.get("designated_user_ids")),
            "designated_group_ids": _ids(v.get("designated_group_ids")),
            "allow_self_approve": bool(v.get("allow_self_approve")), "stages": stages, "saved": True}


def validate(pol: dict[str, Any]) -> str | None:
    """不合法回錯誤代碼（errors.<code>）。"""
    if pol["approver_mode"] in MULTI_STEP_MODES:
        if not pol["stages"]:
            return "approval_need_stage"
        if any(not s["user_ids"] and not s["group_ids"] for s in pol["stages"]):
            return "approval_stage_need_approver"
    if pol["approver_mode"] == "designated" and not (pol["designated_user_ids"] or pol["designated_group_ids"]):
        return "approval_need_designated"
    return None


async def get_policy(session: AsyncSession, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    row = await session.get(SystemSetting, POLICY_KEY)
    if row is not None and isinstance(row.value, dict):
        return normalize(row.value)
    if cfg is None:
        from app.services.change_impact.config import get_config
        cfg = await get_config(session)
    return from_legacy(cfg)


async def set_policy(session: AsyncSession, pol: dict[str, Any], *, updated_by: uuid.UUID | None) -> dict[str, Any]:
    from sqlalchemy.orm.attributes import flag_modified
    clean = normalize(pol)
    value = {k: v for k, v in clean.items() if k != "saved"}
    row = await session.get(SystemSetting, POLICY_KEY)
    if row is None:
        row = SystemSetting(key=POLICY_KEY, value=value, updated_by=updated_by)
        session.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
        flag_modified(row, "value")
    await session.flush()
    return clean
