"""守門：文件與網站（GitHub Pages 的 docs/*.html、三份 README）跟得上程式（使用者 2026-10-08：
「別忘了文件、站台 pages 等等，該更新的都要一併更新到」「以後這些都要檢查，列入發版前作業跟守門」）。

文件落後的方式很安靜：網站照樣打得開、看起來完整，只是少寫了一個整合、一段只有中文。
這裡擋得住的就自動擋：
1. 網站每一頁的中／英／日段落數一致（新增一段中文卻沒補英、日文）
2. 每個整合（整合選單上的每一項）的產品名稱都出現在功能清單、首頁與三份 README
3. 主要功能出現在首頁與三份 README（新增大功能時把名稱加進 MAJOR_FEATURES）
4. 文件不用全形「／」與破折號「—」（使用者 2026-10-01 的規定）
擋不住的（內容是否還正確、截圖是否過時）列在 TEST_CHECKLIST 5k，發版前逐項看。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
HTML = sorted(DOCS.glob("*.html"))
READMES = [ROOT / "README.md", ROOT / "README_zh-TW.md", ROOT / "README_ja.md"]

#: 整合選單的鍵 → 文件上一定要出現的產品名稱（新增整合時，test_integration_presence 的 MENU_KEYS 會多一個鍵，
#: 這裡沒有對照就會失敗 → 逼著把名稱寫進來，接著下面的測試就會檢查文件有沒有寫）
PRODUCT_NAMES: dict[str, list[str]] = {
    "dns": ["PowerDNS", "BIND", "Unbound", "Windows DNS", "Univention", "Technitium"],
    "adguard": ["AdGuard"], "librenms": ["LibreNMS"], "opnsense": ["OPNsense"], "pfsense": ["pfSense"],
    "fortigate": ["FortiGate"], "paloalto": ["Palo Alto"], "checkpoint": ["Check Point"], "mikrotik": ["MikroTik"],
    "windows_dhcp": ["Windows DHCP"], "kea_dhcp": ["Kea"], "isc_dhcp": ["ISC DHCP"], "isoinsight": ["ISOinsight"],
    "technitium": ["Technitium"], "rustdesk": ["RustDesk"], "proxmox": ["Proxmox VE"], "esxi": ["ESXi"],
    "wazuh": ["Wazuh"], "zabbix": ["Zabbix"], "ocs": ["OCS Inventory"], "graylog": ["Graylog"],
}

#: 主要功能：(中文, 英文, 日文) 名稱，首頁與三份 README 都要寫到
MAJOR_FEATURES: list[tuple[str, str, str]] = [
    ("IP 變更評估", "IP change assessment", "IP 変更の影響評価"),
]


def test_every_menu_integration_has_documented_product_names() -> None:
    from tests.test_integration_presence import MENU_KEYS
    missing = sorted(set(MENU_KEYS) - set(PRODUCT_NAMES))
    assert not missing, f"新整合要在 PRODUCT_NAMES 寫下文件上該出現的產品名稱：{missing}"


@pytest.mark.parametrize("path", [DOCS / "features.html", DOCS / "index.html", *READMES], ids=lambda p: p.name)
def test_integrations_appear_on_the_site_and_readmes(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    missing = sorted({n for names in PRODUCT_NAMES.values() for n in names if n not in text})
    assert not missing, f"{path.name} 沒有寫到這些整合：{missing}"


@pytest.mark.parametrize("path", [DOCS / "index.html", *READMES], ids=lambda p: p.name)
def test_major_features_appear(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    for zh, en, ja in MAJOR_FEATURES:
        want = {"README.md": [en], "README_zh-TW.md": [zh], "README_ja.md": [ja]}.get(path.name, [zh, en, ja])
        for w in want:
            assert w in text, f"{path.name} 沒有寫到主要功能「{w}」"


@pytest.mark.parametrize("path", HTML, ids=lambda p: p.name)
def test_site_pages_are_trilingual(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    zh, en, ja = (len(re.findall(rf'class="{c}"', text)) for c in ("zh", "en", "ja"))
    assert zh == en == ja, f"{path.name} 的三語段落數對不上：zh={zh} en={en} ja={ja}（有段落只寫了其中一種語言）"


@pytest.mark.parametrize("path", [*HTML, *READMES, *sorted(DOCS.glob("DATA_MODEL*.md")), *sorted(DOCS.glob("INSTALL*.md"))],
                         ids=lambda p: p.name)
def test_docs_use_halfwidth_slash_and_no_em_dash(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    bad = [(i + 1, line.strip()[:80]) for i, line in enumerate(text.splitlines()) if "／" in line or "—" in line]
    assert not bad, f"{path.name} 用了全形「／」或破折號「—」（文件一律半形 /、不用破折號）：{bad[:5]}"
