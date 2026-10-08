"""誰能覆核、送審要通知誰（使用者 2026-10-07／10-08）。

依「申請審核設定」裡 IP 變更評估的審核關卡（services/change_impact/review_policy）：
- editors（預設）：對目標有修改權的人能覆核；送審通知管理員
- admin：只有管理員
- designated：名單裡、而且看得到目標的人（名單本身就是授權，不另外要修改權）；送審通知名單裡的人
- parallel：多組會簽，每一組都要有人核准；送審通知每一組
- stages：依序多關卡，只有目前這一關的人能審；送審通知第一關，通過一關再通知下一關
管理員任何一關都能核准；建立者不能覆核自己的計畫（設定可放寬）。名單只決定誰可以審，不會讓人看到原本看不到的目標。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.change_impact import ChangePlan
from app.models.user import User, UserGroupMember
from app.services.change_impact import review_policy

log = logging.getLogger("change_impact.reviewers")
_OTYPE = {"ip_address": "ip", "device": "device"}
_MAX_RECIPIENTS = 200


def has_list(cfg: dict[str, Any]) -> bool:
    """舊設定有沒有審核人名單（只用在沒存過審核關卡時的推導）。"""
    return bool(cfg.get("reviewer_user_ids") or cfg.get("reviewer_group_ids"))


async def policy(session: AsyncSession, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    return await review_policy.get_policy(session, cfg)


async def _group_ids(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    rows = (await session.execute(select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id))).scalars()
    return {str(g) for g in rows}


async def _in_set(session: AsyncSession, user: Any, user_ids: list[str], group_ids: list[str]) -> bool:
    if str(user.id) in set(user_ids or []):
        return True
    groups = set(group_ids or [])
    return bool(groups and (await _group_ids(session, user.id)) & groups)


async def is_designated(session: AsyncSession, user: Any, cfg: dict[str, Any]) -> bool:
    pol = await policy(session, cfg)
    return await _in_set(session, user, pol["designated_user_ids"], pol["designated_group_ids"])


async def approved_steps(session: AsyncSession, plan: ChangePlan) -> set[int]:
    """這次送審之後已經通過的關卡（同一個版本；重新送審前的不算）。"""
    rows = (await session.execute(text("""
        SELECT DISTINCT step_index FROM impact_reviews
         WHERE plan_id = :p AND revision = :r AND step_index IS NOT NULL
           AND decision IN ('approve', 'accept_risk')
           AND (CAST(:since AS timestamptz) IS NULL OR created_at >= CAST(:since AS timestamptz))
    """), {"p": plan.id, "r": plan.revision, "since": plan.submitted_at})).scalars().all()
    return {int(x) for x in rows}


async def pending_steps(session: AsyncSession, plan: ChangePlan, pol: dict[str, Any]) -> list[int]:
    if pol["approver_mode"] not in review_policy.MULTI_STEP_MODES:
        return []
    done = await approved_steps(session, plan)
    return [i for i in range(len(pol["stages"])) if i not in done]


async def actionable_step(session: AsyncSession, user: Any, plan: ChangePlan, pol: dict[str, Any]) -> int | None:
    """多關卡時這個人現在可以核准哪一關（依序：只有目前這一關；會簽：任一還沒通過、而且他在裡面的那組）。"""
    pending = await pending_steps(session, plan, pol)
    if not pending:
        return None
    cands = pending[:1] if pol["approver_mode"] == "stages" else pending
    for i in cands:
        st = pol["stages"][i]
        if getattr(user, "is_admin", False) or await _in_set(session, user, st["user_ids"], st["group_ids"]):
            return i
    return None


async def can_review(session: AsyncSession, user: Any, plan: ChangePlan, cfg: dict[str, Any]) -> tuple[bool, str | None]:
    """(可以覆核嗎, 不行的原因代碼)。原因代碼直接是前端 errors.<code>。"""
    from app.services.change_impact.scenario import ScenarioError, require_target_access
    if plan.lifecycle != "in_review":
        return False, "impact_invalid_transition"
    if getattr(user, "is_admin", False):
        return True, None
    pol = await policy(session, cfg)
    if plan.created_by == user.id and not pol["allow_self_approve"]:
        return False, "impact_self_review"
    mode = pol["approver_mode"]
    need = "read"
    if mode == "admin":
        return False, "impact_not_reviewer"
    if mode == "editors":
        need = "write"
    elif mode == "designated":
        if not await _in_set(session, user, pol["designated_user_ids"], pol["designated_group_ids"]):
            return False, "impact_not_reviewer"
    elif await actionable_step(session, user, plan, pol) is None:
        member = any([await _in_set(session, user, st["user_ids"], st["group_ids"]) for st in pol["stages"]])
        return False, "impact_review_not_your_stage" if member else "impact_not_reviewer"
    try:
        await require_target_access(session, user, plan.target_type, plan.target_id, need=need)
    except ScenarioError as exc:
        return False, exc.code
    return True, None


async def _members(session: AsyncSession, user_ids: list[str], group_ids: list[str]) -> list[User]:
    uids = {uuid.UUID(u) for u in user_ids or []}
    gids = [uuid.UUID(g) for g in group_ids or []]
    if gids:
        uids |= set((await session.execute(select(UserGroupMember.user_id).where(
            in_values(UserGroupMember.group_id, gids)))).scalars())
    if not uids:
        return []
    return list((await session.execute(select(User).where(
        in_values(User.id, list(uids)), User.is_active.is_(True)).order_by(User.username))).scalars())


async def _who_sees(session: AsyncSession, plan: ChangePlan, users: list[User]) -> list[User]:
    from app.services.permission import get_object_permission, has_permission
    out = []
    for u in users:
        if len(out) >= _MAX_RECIPIENTS:
            break
        if u.is_admin or has_permission(await get_object_permission(
                session, user=u, object_type=_OTYPE[plan.target_type], object_id=plan.target_id), "read"):  # type: ignore[arg-type]
            out.append(u)
    return out


async def pool(session: AsyncSession, plan: ChangePlan, cfg: dict[str, Any]) -> list[User]:
    """現在要通知、會出現在「送審給」的審核人。不含建立者（除非允許自己覆核；管理員例外）。"""
    pol = await policy(session, cfg)
    mode = pol["approver_mode"]
    if mode in ("editors", "admin"):
        out = list((await session.execute(select(User).where(
            User.is_admin.is_(True), User.is_active.is_(True)).order_by(User.username).limit(_MAX_RECIPIENTS))).scalars())
    elif mode == "designated":
        out = await _who_sees(session, plan, await _members(
            session, pol["designated_user_ids"], pol["designated_group_ids"]))
    else:
        pending = await pending_steps(session, plan, pol) if plan.lifecycle == "in_review" else \
            list(range(len(pol["stages"])))
        steps = pending[:1] if mode == "stages" else pending
        seen: dict[uuid.UUID, User] = {}
        for i in steps:
            for u in await _members(session, pol["stages"][i]["user_ids"], pol["stages"][i]["group_ids"]):
                seen.setdefault(u.id, u)
        out = await _who_sees(session, plan, sorted(seen.values(), key=lambda u: u.username))
    if not pol["allow_self_approve"]:
        out = [u for u in out if u.id != plan.created_by or u.is_admin]
    return out


async def steps_progress(session: AsyncSession, plan: ChangePlan, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """多關卡的進度（給計畫頁顯示）：每關名稱、審核人、通過了沒、是不是目前這一關。"""
    pol = await policy(session, cfg)
    if pol["approver_mode"] not in review_policy.MULTI_STEP_MODES:
        return []
    done = await approved_steps(session, plan) if plan.lifecycle in ("in_review", "approved") else set()
    pending = [i for i in range(len(pol["stages"])) if i not in done]
    current = set(pending[:1] if pol["approver_mode"] == "stages" else pending) if plan.lifecycle == "in_review" \
        else set()
    out = []
    for i, st in enumerate(pol["stages"]):
        names = [_name(u) for u in (await _members(session, st["user_ids"], st["group_ids"]))[:10]]
        out.append({"index": i, "name": st["name"], "approved": i in done, "is_current": i in current,
                    "approvers": names})
    return out


def _name(u: User) -> str:
    return u.display_name or u.username


async def _send(session: AsyncSession, *, event: str, users: list[User], plan: ChangePlan, title: str, body: str,
                title_key: str, body_key: str, params: dict[str, Any], severity: str = "info") -> None:
    """站內通知＋外部通知管道＋Email（依通知矩陣）。失敗不影響送審或覆核本身。"""
    from app.services.notification import push_notification
    from app.services.notify_channels import broadcast_channels
    from app.services.system_config import get_notification_channels, get_notification_matrix
    mx = (await get_notification_matrix(session)).get(event, {"in_app": True, "email": False})
    if not users or not (mx.get("in_app") or mx.get("email")):
        return
    link = f"/change-impact/{plan.id}"
    if mx.get("in_app"):
        for u in users:
            await push_notification(session, user_id=u.id, severity=severity, title=title, body=body,
                                    title_key=title_key, body_key=body_key, params=params, link=link,
                                    object_type="change_plan", object_id=plan.id)
    try:
        await broadcast_channels(session, subject=title, text=body)
    except Exception as exc:     # 外部管道壞了不影響流程
        log.warning("change impact notify channels failed: %s", exc)
    if not mx.get("email"):
        return
    try:
        from app.core.config import get_settings
        from app.services.email import send_email_via_config
        ch = await get_notification_channels(session)
        if not ch.get("email_enabled"):
            return
        url = str(get_settings().app_public_url).rstrip("/") + link
        for u in users:
            if u.email:
                await send_email_via_config(ch, to=u.email, subject=f"[jt-ipam] {title}",
                                            body_text=f"{body}\n\n{url}\n", body_html=None)
    except Exception as exc:
        log.warning("change impact notify email failed: %s", exc)


async def notify_submitted(session: AsyncSession, plan: ChangePlan, submitter: Any, cfg: dict[str, Any]) -> int:
    users = [u for u in await pool(session, plan, cfg) if u.id != submitter.id]
    params = {"title": plan.title, "user": _name(submitter), "target": plan.target_label}
    await _send(session, event="change_impact.submitted", users=users, plan=plan,
                title=f"IP 變更評估待覆核：{plan.title}",
                body=f"{_name(submitter)} 送審了「{plan.title}」（{plan.target_label}），請覆核。",
                title_key="notif.change_impact_submitted", body_key="notif.change_impact_submitted_body",
                params=params)
    return len(users)


_DECISION_ZH = {"approve": "核准", "accept_risk": "接受風險後核准", "request_changes": "退回修改", "reject": "駁回"}


async def notify_reviewed(session: AsyncSession, plan: ChangePlan, reviewer: Any, decision: str) -> None:
    if plan.created_by is None or plan.created_by == reviewer.id:
        return
    creator = await session.get(User, plan.created_by)
    if creator is None or not creator.is_active:
        return
    params = {"title": plan.title, "user": _name(reviewer)}
    await _send(session, event="change_impact.reviewed", users=[creator], plan=plan,
                title=f"IP 變更評估已覆核：{plan.title}",
                body=f"{_name(reviewer)} {_DECISION_ZH.get(decision, decision)}了「{plan.title}」。",
                title_key="notif.change_impact_reviewed",
                body_key=f"notif.change_impact_reviewed_{decision}", params=params,
                severity="warning" if decision in ("reject", "request_changes") else "info")
