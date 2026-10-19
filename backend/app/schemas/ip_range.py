"""子網路內的位址範圍（集區）—— GitHub issue #40。"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.base import StrictModel

IPRangePurpose = Literal["dhcp", "reserved", "other"]


class IPRangeCreate(StrictModel):
    start_ip: Annotated[str, Field(min_length=2, max_length=64)]
    end_ip: Annotated[str, Field(min_length=2, max_length=64)]
    purpose: IPRangePurpose = "dhcp"
    name: Annotated[str | None, Field(max_length=64)] = None
    description: Annotated[str | None, Field(max_length=1024)] = None


class IPRangeUpdate(StrictModel):
    start_ip: Annotated[str | None, Field(min_length=2, max_length=64)] = None
    end_ip: Annotated[str | None, Field(min_length=2, max_length=64)] = None
    purpose: IPRangePurpose | None = None
    name: Annotated[str | None, Field(max_length=64)] = None
    description: Annotated[str | None, Field(max_length=1024)] = None


class IPRangeRead(StrictModel):
    id: uuid.UUID
    subnet_id: uuid.UUID
    start_ip: str
    end_ip: str
    purpose: IPRangePurpose
    name: str | None = None
    description: str | None = None
    #: 範圍內有幾個位址、已用（DHCP 範圍只算使用中的記錄）、可用、全部記錄數、
    #: 過期記錄數（DHCP 範圍裡離線又沒租約的，不算已用）、下一個還沒有記錄的位址
    size: int = 0
    used: int = 0
    free: int = 0
    records: int = 0
    stale: int = 0
    first_free: str | None = None
    #: 自動範圍：DHCP 伺服器排除、不發放的位址數（不算在 size 裡）與排除區間切出來的各段起訖
    #: （起訖是設定的整個範圍；位址圖要照各段畫，排除的位址不是 DHCP 範圍）
    excluded: int = 0
    segments: list[list[str]] | None = None
    #: 由偵測到的 DHCP 發放範圍自動建立（跟著上游走，不能手動改／刪）與它的來源（firewall-a · KEA）
    auto: bool = False
    source_label: str | None = None
