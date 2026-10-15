"""「連線管理」可連線目標的篩選（REST 連線管理頁與 AI 工具 list_connection_targets 共用）。

2026-10-15 MCP 工具盤點：AI 工具自己寫了一份查詢，只看 SSH／RDP／VNC —— 只開了 noVNC、BMC、
RustDesk 的位址整列消失，模型回答「沒有可以連的主機」。篩選搬到這裡，兩邊呼叫同一份。

權限與 can_use_ssh/can_use_rdp… 一致（deny-by-default）：admin 全部；否則限可見子網路，再依
「對該子網路有 write」或「具 can_ssh 能力且至少 read」逐筆放行。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.address import IPAddress
from app.services.permission import get_object_permission, has_permission, visible_ids


async def usable_connection_targets(
    session: AsyncSession, *, user: Any, include_sftp: bool = False,
) -> tuple[list[IPAddress], set[uuid.UUID], set[uuid.UUID] | None]:
    """回傳 (可連線的 IP, 可走相容 RustDesk 網頁連線的 IP id, 可見子網路 id 或 None＝不限)。

    連線管理頁沒有 SFTP 欄（只開 SFTP 的位址不列）；AI 工具要回答「哪些主機可以傳檔」，帶 include_sftp。
    相容 RustDesk 的網頁連線：IP 開了 rustdesk_enabled、對應到的 RustDesk 裝置所在的伺服器開放網頁連線，
    才算一種可用的連線；只開了 RustDesk 卻不能網頁連線的 IP 不列出。
    """
    cond = (
        IPAddress.ssh_enabled.is_(True)
        | IPAddress.rdp_enabled.is_(True)
        | IPAddress.vnc_enabled.is_(True)
        | IPAddress.novnc_enabled.is_(True)
        | IPAddress.bmc_enabled.is_(True)
        | IPAddress.rustdesk_enabled.is_(True)
    )
    if include_sftp:
        cond = cond | IPAddress.sftp_enabled.is_(True)
    stmt = select(IPAddress).where(cond)
    vis: set[uuid.UUID] | None = None  # None = 不限（admin 或萬用可見）
    if not user.is_admin:
        vis = await visible_ids(session, user=user, object_type="subnet")
        if vis is not None:
            if not vis:
                return [], set(), vis
            stmt = stmt.where(in_values(IPAddress.subnet_id, vis))
    rows = (await session.execute(stmt)).scalars().all()

    perm_cache: dict[uuid.UUID, str] = {}
    kept: list[IPAddress] = []
    for ip in rows:
        if user.is_admin:
            usable = True
        else:
            lvl = perm_cache.get(ip.subnet_id)
            if lvl is None:
                lvl = await get_object_permission(
                    session, user=user, object_type="subnet", object_id=ip.subnet_id
                )
                perm_cache[ip.subnet_id] = lvl
            if lvl == "none":
                continue
            usable = has_permission(lvl, "write") or bool(user.can_ssh)
        if not usable:
            continue
        kept.append(ip)

    # 相容 RustDesk 的網頁連線：一次查完（IP 可能上萬筆，不逐筆查）
    rd_web = await rustdesk_web_ready(session, [ip.id for ip in kept if ip.rustdesk_enabled])
    kept = [ip for ip in kept
            if ip.ssh_enabled or ip.rdp_enabled or ip.vnc_enabled or ip.novnc_enabled
            or ip.bmc_enabled or (include_sftp and ip.sftp_enabled) or ip.id in rd_web]
    return kept, rd_web, vis


async def rustdesk_web_ready(session: AsyncSession, ip_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """這些 IP 裡，哪些可以走相容 RustDesk 的網頁連線（伺服器設定面；權限已在呼叫端過濾）。

    與 IP 詳細資料同一個選法（rustdesk.matched_peer：取最近上線的那一台），所以清單上的按鈕與
    詳細資料頁的按鈕一致。
    """
    if not ip_ids:
        return set()
    from app.models.rustdesk import RustDeskPeer, RustDeskServer
    from app.services.rustdesk import web_problem
    rows = (await session.execute(
        select(RustDeskPeer.address_id, RustDeskServer)
        .join(RustDeskServer, RustDeskServer.id == RustDeskPeer.server_id)
        .where(in_values(RustDeskPeer.address_id, ip_ids), RustDeskPeer.match_status == "matched")
        .order_by(RustDeskPeer.address_id, RustDeskPeer.online.desc(),
                  RustDeskPeer.last_online_at.desc().nulls_last())
    )).all()
    out: set[uuid.UUID] = set()
    seen: set[uuid.UUID] = set()
    for addr_id, srv in rows:
        if addr_id in seen:
            continue                    # 每個 IP 只看排第一的那台（同 matched_peer）
        seen.add(addr_id)
        if web_problem(srv) is None:
            out.add(addr_id)
    return out
