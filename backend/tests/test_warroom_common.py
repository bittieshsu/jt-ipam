"""戰情室的方格分配與分頁：格數、比例與總數要一致（規範：彙整格不冒充單一物件、有值的狀態不可以消失）。"""
from __future__ import annotations

import pytest

from app.services.warroom.common import paginate, unit_for, waffle


@pytest.mark.parametrize(("total", "unit"), [(0, 1), (1, 1), (254, 1), (256, 1), (257, 2), (510, 2),
                                             (1022, 4), (65534, 256), (16777214, 65536)])
def test_unit_for(total: int, unit: int) -> None:
    assert unit_for(total) == unit
    assert -(-total // unit) <= 256


def test_waffle_proportional_and_ordered() -> None:
    cells = waffle([("allocated", 940), ("reserved", 40), ("free", 20)], 100)
    assert len(cells) == 100
    assert cells == sorted(cells, key=["allocated", "reserved", "free"].index)   # 依狀態順序
    assert cells.count("allocated") == 94
    assert cells.count("reserved") == 4
    assert cells.count("free") == 2


def test_waffle_keeps_a_single_anomaly_visible() -> None:
    cells = waffle([("ok", 9999), ("anomaly", 1)], 48)
    assert len(cells) == 48
    assert cells.count("anomaly") == 1


def test_waffle_edges() -> None:
    assert waffle([("a", 0)], 10) == []
    assert waffle([("a", 5)], 0) == []
    assert waffle([("a", 1), ("b", 1), ("c", 1)], 2) == ["a", "b"]
    assert len(waffle([("a", 1), ("b", 2), ("c", 3)], 7)) == 7


def test_paginate() -> None:
    items = list(range(10))
    assert paginate(items, 4, 1) == ([0, 1, 2, 3], 1, 3)
    assert paginate(items, 4, 3) == ([8, 9], 3, 3)
    assert paginate(items, 4, 99) == ([8, 9], 3, 3)        # 超出範圍夾回最後一頁
    assert paginate([], 4, 1) == ([], 1, 1)
