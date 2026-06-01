"""Location / Rack schemas。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.base import StrictModel


class LocationBase(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    address: Annotated[str | None, Field(max_length=512)] = None
    latitude: Annotated[float | None, Field(ge=-90, le=90)] = None
    longitude: Annotated[float | None, Field(ge=-180, le=180)] = None
    description: Annotated[str | None, Field(max_length=1024)] = None


class LocationCreate(LocationBase):
    pass


class LocationUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    address: Annotated[str | None, Field(max_length=512)] = None
    latitude: Annotated[float | None, Field(ge=-90, le=90)] = None
    longitude: Annotated[float | None, Field(ge=-180, le=180)] = None
    description: Annotated[str | None, Field(max_length=1024)] = None


class LocationRead(LocationBase):
    id: uuid.UUID
    floor_plan_path: str | None = None
    created_at: datetime
    updated_at: datetime


RackNumbering = Literal["top-down", "bottom-up"]
RackFace = Literal["front", "rear"]


class RackBase(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    location_id: uuid.UUID | None = None
    u_height: Annotated[int, Field(ge=1, le=99)] = 42
    # 實體尺寸（mm）；機房平面圖用真實腳印按比例畫機櫃方框
    width_mm: Annotated[int | None, Field(ge=100, le=2000)] = None
    depth_mm: Annotated[int | None, Field(ge=100, le=3000)] = None
    description: Annotated[str | None, Field(max_length=1024)] = None
    numbering: RackNumbering = "top-down"
    face: RackFace = "front"


class RackCreate(RackBase):
    pass


class RackUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    location_id: uuid.UUID | None = None
    u_height: Annotated[int | None, Field(ge=1, le=99)] = None
    width_mm: Annotated[int | None, Field(ge=100, le=2000)] = None
    depth_mm: Annotated[int | None, Field(ge=100, le=3000)] = None
    description: Annotated[str | None, Field(max_length=1024)] = None
    numbering: RackNumbering | None = None
    face: RackFace | None = None
    pos_x: Annotated[float | None, Field(ge=0, le=1)] = None
    pos_y: Annotated[float | None, Field(ge=0, le=1)] = None


class RackRead(RackBase):
    id: uuid.UUID
    pos_x: float | None = None
    pos_y: float | None = None
    pos_rot: int = 0
    pos_w: float | None = None
    pos_h: float | None = None
    created_at: datetime
    updated_at: datetime


class RackPosition(StrictModel):
    """機房平面圖上單一機櫃的座標（0..1 比例）+ 旋轉角度 + 方框大小。"""

    id: uuid.UUID
    pos_x: Annotated[float, Field(ge=0, le=1)]
    pos_y: Annotated[float, Field(ge=0, le=1)]
    pos_rot: Annotated[int, Field(ge=0, le=359)] = 0
    pos_w: Annotated[float | None, Field(ge=0.01, le=1)] = None
    pos_h: Annotated[float | None, Field(ge=0.01, le=1)] = None


class RackPositionsUpdate(StrictModel):
    positions: Annotated[list[RackPosition], Field(max_length=500)]
