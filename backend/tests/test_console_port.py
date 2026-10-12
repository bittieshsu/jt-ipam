"""主控台的連接埠（使用者 2026-10-11：VNC 可以選連接埠，RDP 沒有）。

VNC 與 RDP 都從連線設定訊息讀 `port`，同一個解析函式：沒給＝預設埠，不是 1 到 65535 的整數就拒絕
（以前 VNC 直接 int()，送字串會讓整條連線丟例外，而不是回可讀的錯誤）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.services.console_route import config_port

EP = Path(__file__).resolve().parent.parent / "app" / "api" / "v1" / "endpoints"


@pytest.mark.parametrize(("cfg", "expected"), [
    ({}, 3389), ({"port": None}, 3389), ({"port": ""}, 3389),
    ({"port": 3390}, 3390), ({"port": "3390"}, 3390), ({"port": 1}, 1), ({"port": 65535}, 65535),
    ({"port": 0}, None), ({"port": 65536}, None), ({"port": -1}, None), ({"port": "abc"}, None),
    ({"port": 3389.5}, None), ({"port": True}, None),
])
def test_config_port(cfg: dict, expected: int | None) -> None:
    assert config_port(cfg, 3389) == expected


@pytest.mark.parametrize(("name", "default"), [("rdp_console", "_RDP_PORT"), ("vnc_console", "_DEFAULT_PORT")])
def test_console_dials_the_chosen_port(name: str, default: str) -> None:
    """選的連接埠要真的用在連線路徑上（跳板與代理中繼的允許埠檢查在 open_route 裡），稽核也要記。"""
    src = (EP / f"{name}.py").read_text(encoding="utf-8")
    assert f"config_port(cfg, {default})" in src, f"{name} 沒有用共用的連接埠解析"
    assert "console_route.open_route(route, host, port," in src, f"{name} 沒有把選的連接埠交給 open_route"
    assert '"console_bad_port"' in src, f"{name} 連接埠不合法時沒有回可讀的錯誤"
    assert '"port": port' in src, f"{name} 的稽核沒有記連接埠"
