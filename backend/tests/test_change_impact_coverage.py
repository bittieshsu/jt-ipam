"""每個整合都要考慮 IP 變更評估（使用者 2026-10-08：新功能、新整合都要同步支援評估）。

整合清單以 /system/integration-presence 的鍵為準（管理選單「外部系統整合」同一份，
tests/test_integration_presence.py 會擋住沒登記的整合）。這裡再要求：每個整合要嘛是評估的資料來源
（services/change_impact/sources.py 的 _SPECS），要嘛寫進 NOT_SOURCES 並附理由。
"""
from __future__ import annotations

from app.services.change_impact.sources import _SPECS, NOT_SOURCES, PRESENCE_KIND

from tests.test_integration_presence import MENU_KEYS


def test_every_integration_is_an_assessment_source_or_explained() -> None:
    kinds = {spec[2] for spec in _SPECS}
    unknown = sorted(k for k in MENU_KEYS if k not in PRESENCE_KIND and k not in NOT_SOURCES)
    assert not unknown, (
        "這些整合還沒決定 IP 變更評估要不要用它的資料：加進 sources._SPECS（並在 adapter 讀它）、"
        f"或寫進 NOT_SOURCES 附理由 → {unknown}")
    missing = sorted(k for k, kind in PRESENCE_KIND.items() if kind not in kinds)
    assert not missing, f"PRESENCE_KIND 指向不存在的來源種類：{missing}"
    assert all(len(reason) > 10 for reason in NOT_SOURCES.values()), "豁免要寫清楚理由"
