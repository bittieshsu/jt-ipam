"""掃描代理不可以讓印表機印出東西（客戶 2026-10-12：Brother、Ricoh IM C3510 被掃描後印出亂碼）。

根因：nmap 的 banner 腳本對「所有 TCP 埠」連線（不理會 nmap 自己為印表機設的 Exclude 9100-9107），
用的是 comm.get_banner → tryssl：明文連上後等不到對方先說話，就**改用 TLS 再連一次**。印表機的 RAW 列印埠
（9100）連上後不會說話，於是收到一個 TLS ClientHello，當成列印工作印出來 —— 印出來的開頭「▬♥☺」就是
0x16 0x03 0x01（TLS 記錄標頭）。

伺服器只拿 banner 比對 SSH／FTP／SMTP／POP3／IMAP／Telnet（都是伺服器先說話的協定，明文就讀得到，不會退回
TLS），所以 banner 只跑在這些服務的埠上；其他埠送 TLS 只有壞處（印表機、序列設備伺服器、工控設備）。
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_AGENT = pathlib.Path(__file__).resolve().parents[2] / "agent" / "jt_ipam_agent.py"
PRINT_PORTS = (515, *range(9100, 9108))
BANNER_SERVICES = (21, 22, 23, 25, 110, 143, 465, 587, 993, 995)


@pytest.fixture(scope="module")
def agent():
    if not _AGENT.exists():
        pytest.skip(f"agent not found at {_AGENT}")
    spec = importlib.util.spec_from_file_location("jt_ipam_agent_printer_safe", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _capture(agent, monkeypatch):  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []

    def fake_run(argv, **kw):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    return calls


def _ports_in(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.removeprefix("T:").split(","):
        lo, _, hi = part.partition("-")
        out |= set(range(int(lo), int(hi or lo) + 1))
    return out


def _banner_ports(argv: list[str]) -> set[int] | None:
    """argv 裡 banner 會跑的埠；沒有限制（會跑所有埠）回 None。"""
    if "--script-args" not in argv:
        return None
    args = argv[argv.index("--script-args") + 1]
    for kv in [args] if args.count("=") == 1 else args.split(","):
        k, _, v = kv.partition("=")
        if k.strip() == "banner.ports":
            return _ports_in(v.strip().strip('"').strip("'"))
    return None


def _check(argv: list[str]) -> None:
    assert "--allports" not in argv, "--allports 會讓版本偵測也對 9100 送探針"
    scripts = argv[argv.index("--script") + 1] if "--script" in argv else ""
    if "banner" not in scripts.split(","):
        return
    ports = _banner_ports(argv)
    assert ports is not None, "banner 沒有限定埠：在不說話的埠上會改用 TLS 重連，印表機會把 ClientHello 印出來"
    assert not ports & set(PRINT_PORTS), f"banner 會碰到列印埠 {sorted(ports & set(PRINT_PORTS))}"
    assert set(BANNER_SERVICES) <= ports, "伺服器用得到的 banner（SSH/FTP/SMTP/POP3/IMAP/Telnet）不可以少"


@pytest.mark.parametrize(("want_os", "want_ports"), [(True, False), (True, True), (False, True)])
def test_periodic_os_and_port_probes_never_send_to_print_ports(agent, monkeypatch, want_os, want_ports) -> None:
    calls = _capture(agent, monkeypatch)
    agent._nmap_os_ports("192.0.2.5", want_os, want_ports)
    assert calls
    for argv in calls:
        _check(argv)


def test_ip_identify_never_sends_to_print_ports(agent, monkeypatch) -> None:
    calls = _capture(agent, monkeypatch)
    monkeypatch.setattr(agent, "_rdns", lambda ip: (None, None))
    monkeypatch.setattr(agent, "_netbios", lambda ip: None)
    monkeypatch.setattr(agent, "_mdns", lambda ip: None)
    agent._job_run_identify("192.0.2.5")
    nmap_calls = [a for a in calls if a and a[0] == "nmap"]
    assert nmap_calls
    for argv in nmap_calls:
        _check(argv)


def test_the_banner_port_spec_is_valid_for_nmap(agent) -> None:
    """nmap 的 port_range 只吃「數字、區段、逗號」，起點不可大於終點；--script-args 裡有逗號要整段加引號。"""
    spec = agent.BANNER_PORTS
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        assert lo.isdigit() and (not hi or (hi.isdigit() and int(lo) <= int(hi)))
    assert agent.BANNER_SCRIPT_ARGS == f'banner.ports="{spec}"'
