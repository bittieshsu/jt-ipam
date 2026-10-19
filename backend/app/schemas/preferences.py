"""User preferences schemas。"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from app.schemas.base import StrictModel

#: 儀表板趨勢圖可以只計入的 IP 異動類別（對應 endpoints/dashboard.TREND_EVENT_GROUPS）
TrendEventGroup = Literal["status", "mac", "hostname", "identity", "edited", "lifecycle"]


class DashboardTrendPrefs(StrictModel):
    """「近 14 日 稽核 / IP 異動 趨勢」的個人設定（使用者 2026-10-18）。空值＝照舊全部計入。"""
    exclude_subnet_ids: Annotated[list[uuid.UUID], Field(max_length=2000)] = []
    exclude_dhcp: bool = False
    #: None＝全部類別；有給就只計入這些類別
    event_groups: list[TrendEventGroup] | None = None


class DashboardPrefs(StrictModel):
    trend: DashboardTrendPrefs | None = None


class UserPreferenceRead(StrictModel):
    locale: Literal["zh-TW", "en-US", "ja-JP"]
    theme: Literal["light", "dark", "auto"]
    timezone: str
    calendar: Literal["gregorian", "minguo"]
    page_size: int
    table_columns: dict[str, Any] | None = None
    pinned_subnet_ids: list[uuid.UUID] | None = None
    pinned: dict[str, list[str]] | None = None
    sftp_sort_dirs_first: bool = True
    dashboard: DashboardPrefs | None = None


class UserPreferenceUpdate(StrictModel):
    locale: Literal["zh-TW", "en-US", "ja-JP"] | None = None
    theme: Literal["light", "dark", "auto"] | None = None
    timezone: Annotated[str | None, Field(max_length=64)] = None
    calendar: Literal["gregorian", "minguo"] | None = None
    page_size: Annotated[int | None, Field(ge=10, le=500)] = None
    table_columns: dict[str, Any] | None = None
    pinned_subnet_ids: list[uuid.UUID] | None = None
    pinned: dict[str, list[str]] | None = None
    sftp_sort_dirs_first: bool | None = None
    dashboard: DashboardPrefs | None = None
