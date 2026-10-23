"""guacd 要裝哪個 build 以 scripts/guacd/SHA256SUMS 實際列出的為準（10-23，為了發佈 Ubuntu 26.10）。

source.env 的 GUACD_BUILD_REV 是「下一次要編」的版本：先把它升上去、推上 GitHub，才能觸發 workflow 編出
新的 release；release 與新的 SHA256SUMS 進來之前，安裝／升級不可以去抓還不存在的檔案（那段空窗期客戶
安裝會失敗）。所以檔名裡的 rev 取 SHA256SUMS 裡這個 OS 最新的那一筆，沒有才退回 source.env。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "jt-ipam.sh"


def _fn(name: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", src, re.S | re.M)
    assert m, name
    return m.group(0)


def _os_tag() -> str:
    out = subprocess.run(["bash", "-c", ". /etc/os-release && echo ${ID}${VERSION_ID}"], capture_output=True, text=True)
    return out.stdout.strip()


def _name(tmp: Path, sums: str, rev: int = 4) -> str:
    g = tmp / "scripts" / "guacd"
    g.mkdir(parents=True, exist_ok=True)
    (g / "source.env").write_text(f"GUACD_VERSION=1.6.1-pre.abc\nGUACD_BUILD_REV={rev}\n")
    (g / "SHA256SUMS").write_text(sums)
    body = _fn("guacd_published_rev") + _fn("guacd_package_name") + 'guacd_package_name\n'
    out = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env={"REPO_ROOT": str(tmp), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_uses_the_build_listed_in_sha256sums(tmp_path: Path) -> None:
    tag = _os_tag()
    arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True).stdout.strip()
    sums = f"aaa  jt-ipam-guacd-1.6.1-pre.abc-3-{tag}-{arch}.tar.gz\nbbb  jt-ipam-guacd-1.6.1-pre.abc-3-other1.0-{arch}.tar.gz\n"
    assert _name(tmp_path, sums) == f"jt-ipam-guacd-1.6.1-pre.abc-3-{tag}-{arch}", "release 還沒出來：繼續用 3 號"
    sums += f"ccc  jt-ipam-guacd-1.6.1-pre.abc-4-{tag}-{arch}.tar.gz\n"
    assert _name(tmp_path, sums) == f"jt-ipam-guacd-1.6.1-pre.abc-4-{tag}-{arch}", "列出 4 號之後改用 4 號"


def test_falls_back_to_source_env_when_not_listed(tmp_path: Path) -> None:
    tag = _os_tag()
    arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True).stdout.strip()
    assert _name(tmp_path, "", rev=4) == f"jt-ipam-guacd-1.6.1-pre.abc-4-{tag}-{arch}"


def test_upgrade_compares_against_the_published_build() -> None:
    src = _fn("ensure_guacd_current")
    assert "guacd_published_rev" in src
    assert "GUACD_BUILD_REV" not in src
