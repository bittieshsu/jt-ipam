"""OS 家族：認得更多 Linux 發行版，優先來源判不出家族時改用其他來源（使用者 2026-10-16）。

一台 Bazzite（Fedora Kinoite 衍生）桌機：Wazuh 回報 os.name「Bazzite」，家族對照表不認得 → 畫面顯示「未知（來源 Wazuh）」；
同一台 LibreNMS 說 linux、OCS 說「Bazzite release 44 (Kinoite)」、掃描代理猜 Linux 5.x。「就算無法顯示，至少它是 Linux 也是已知」。
"""
from __future__ import annotations

import pytest

from app.core.os_fingerprint import normalize_os
from tests.test_os_sources_rustdesk_wazuh import _ip

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("raw", [
    "Bazzite", "Bazzite release 44 (Kinoite)", "Fedora Silverblue 41", "Arch Linux", "Manjaro", "Linux Mint 22",
    "Pop!_OS 22.04", "NixOS 24.05", "Alpine 3.20", "Kali GNU/Linux Rolling", "SteamOS", "EndeavourOS", "CachyOS",
    "Gentoo", "openSUSE Tumbleweed", "Zorin OS 17", "elementary OS 8", "AlmaLinux 9", "RHEL 9.4",
])
def test_common_linux_distributions_are_linux(raw: str) -> None:
    assert normalize_os(raw) == "linux"


def test_words_that_only_contain_a_distro_name_are_not_linux() -> None:
    """「arch」「mint」單獨比對會誤判（architecture、mint condition）：要整個名稱才算。"""
    assert normalize_os("unknown architecture") == "unknown"


async def test_family_falls_back_to_a_source_that_knows_it(db_session) -> None:
    from app.services import os_precedence
    ip = await _ip(db_session, "198.51.100.71")
    ip.os_guess = "Linux 5.3 - 5.4"                    # 掃描代理（家族 linux）
    ip.os_ocs = "SomeVendorOS 1.0"                     # 家族對不到的原文
    await db_session.flush()
    await os_precedence.set_order(db_session, order=["ocs", "scanner", "librenms", "wazuh", "rustdesk"])
    eff = await os_precedence.effective_os(db_session, ip)
    assert eff["os_source"] == "ocs" and eff["os_guess"] == "SomeVendorOS 1.0", "顯示仍用優先來源的原文"
    assert eff["os_family"] == "linux", "優先來源判不出家族時，用其他來源判得出的家族"


async def test_unknown_only_when_no_source_knows(db_session) -> None:
    from app.services import os_precedence
    ip = await _ip(db_session, "198.51.100.72")
    ip.os_guess = "SomeVendorOS 1.0"
    await db_session.flush()
    eff = await os_precedence.effective_os(db_session, ip)
    assert eff["os_family"] == "unknown" and eff["os_guess"] == "SomeVendorOS 1.0"
