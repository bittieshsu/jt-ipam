"""探測結果解析的正規表示式不可以是二次方（CodeQL #39／#40，2026-10-01）。

nmap 的腳本輸出是代理回報的、內容由被掃的主機決定；解析在 async 處理裡同步執行，慢一次就卡住整個
工作程序。舊的兩條在惡意輸入上要十秒以上（40 KB 的未結束 telnet 子協商、「OS: a ( ( ( …」）。
官方代理會截短腳本輸出，但伺服器不能假設送來的一定是官方代理。
"""
from __future__ import annotations

import time

from app.services.ip_identify import _unescape, recog_observations


def _smb(text: str) -> list[tuple[str, str | None, str]]:
    obs = recog_observations({"host_scripts": {"smb-os-discovery": text}})
    return [(k, v) for k, _w, v in obs if k.startswith("smb.")]


def test_telnet_negotiation_is_still_removed() -> None:
    raw = r"\xff\xfb\x01\xff\xfd\x03\xff\xfa\x18\x01\xff\xf0login:"
    assert _unescape(raw, telnet=True) == "login:"


def test_telnet_cleanup_is_linear() -> None:
    raw = r"\xff\xfa" * 20000 + "login:"
    t0 = time.monotonic()
    _unescape(raw, telnet=True)
    assert time.monotonic() - t0 < 1.0


def test_smb_os_line_parses_the_same() -> None:
    assert _smb("\n  OS: Windows 10 Pro 19045 (Windows 10 Pro 6.3)\n  OS CPE: cpe:/o:microsoft:windows_10::-\n") == [
        ("smb.native_os", "Windows 10 Pro 19045"), ("smb.native_lm", "Windows 10 Pro 6.3")]
    assert _smb("  OS: Unix  ") == [("smb.native_os", "Unix")]
    assert _smb("OS: A (B) (C)") == [("smb.native_os", "A"), ("smb.native_lm", "B) (C")]
    assert _smb("OS: X ()") == [("smb.native_os", "X ()")]
    assert _smb("no os line here") == []


def test_smb_os_line_is_linear() -> None:
    t0 = time.monotonic()
    _smb("OS: a" + " (" * 20000)
    _smb("\n" * 20000 + "OS:")
    assert time.monotonic() - t0 < 1.0
