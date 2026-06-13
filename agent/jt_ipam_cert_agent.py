#!/usr/bin/env python3
"""jt-ipam 憑證派送代理。

定期(或單次)向 jt-ipam 詢問「我負責的憑證有沒有新版」,有的話下載 bundle、原子寫入各站台、
config-test 通過才 reload,失敗自動回滾,最後把結果回報給 jt-ipam(後台看站台健康度/飄移)。

特性:
- pull 模型(agent 主動連 server),站台在防火牆後也能用。
- 內建 service profiles(nginx/apache/haproxy/pve/pmg/postfix/dovecot/zimbra/generic),設定極簡。
- 原子寫入 + 時間戳備份 + config-test gate + 失敗回滾;new==current 直接跳過(冪等)。
- --dry-run:只顯示「會寫哪些檔(舊→新指紋)、會跑哪個 reload」,完全不動檔/不 reload。

設定檔(預設 /etc/jt-ipam-cert-agent/config.yaml):

    server: https://ipam.example.com
    agent_key: "<enrollment key>"
    verify_tls: true            # server 自簽時設 false,或用 ca_cert 指定 CA
    # ca_cert: /etc/ssl/certs/ipam-ca.pem
    deployments:
      - cert: wildcard-example-com   # 對應 jt-ipam 裡的憑證名稱
        profile: nginx
      - cert: mail-cert
        profile: pmg
      - cert: wildcard-example-com
        profile: generic              # generic 必須自訂路徑與 reload
        crt_path: /etc/myapp/tls.crt
        key_path: /etc/myapp/tls.key
        reload: "systemctl reload myapp"

只需 Python 3.8+ 與 PyYAML(installer 會裝 python3-yaml)。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    sys.stderr.write("需要 PyYAML:apt-get install -y python3-yaml(或 pip install pyyaml)\n")
    sys.exit(2)

__version__ = "0.4.135"

DEFAULT_CONFIG = "/etc/jt-ipam-cert-agent/config.yaml"
STATE_DIR = "/var/lib/jt-ipam-cert-agent"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
TLS_BASE = "/etc/ssl/jt-ipam"  # 一般 profile 預設寫入目錄

# content kinds:cert=leaf、chain=中繼、fullchain=cert+chain、key=私鑰、combined=cert+chain+key
PROFILES: dict[str, dict] = {
    "nginx": {
        "files": [("fullchain", "{base}/{cert}.fullchain.pem", 0o644),
                  ("key", "{base}/{cert}.key", 0o600)],
        "test": "nginx -t", "reload": "systemctl reload nginx",
    },
    "apache": {  # Debian/Ubuntu=apache2、RHEL/SUSE=httpd → reload 兩者都試
        "files": [("cert", "{base}/{cert}.crt", 0o644),
                  ("chain", "{base}/{cert}.chain.pem", 0o644),
                  ("key", "{base}/{cert}.key", 0o600)],
        "test": "apachectl configtest 2>/dev/null || httpd -t",
        "reload": "systemctl reload apache2 2>/dev/null || systemctl reload httpd",
    },
    "haproxy": {
        "files": [("combined", "{base}/{cert}.pem", 0o600)],
        "test": "haproxy -c -f /etc/haproxy/haproxy.cfg", "reload": "systemctl reload haproxy",
    },
    "pve": {  # Proxmox VE:pveproxy
        "files": [("fullchain", "/etc/pve/local/pveproxy-ssl.pem", 0o640),
                  ("key", "/etc/pve/local/pveproxy-ssl.key", 0o600)],
        "test": None, "reload": "systemctl restart pveproxy",
    },
    "pmg": {  # Proxmox Mail Gateway:pmgproxy(/etc/pmg/pmg-api.pem 含 key+cert)
        "files": [("combined", "/etc/pmg/pmg-api.pem", 0o600)],
        "test": None, "reload": "systemctl restart pmgproxy",
    },
    "pbs": {  # Proxmox Backup Server:proxy.pem(fullchain) + proxy.key
        "files": [("fullchain", "/etc/proxmox-backup/proxy.pem", 0o640),
                  ("key", "/etc/proxmox-backup/proxy.key", 0o600)],
        "test": None, "reload": "systemctl reload proxmox-backup-proxy",
    },
    "postfix": {
        "files": [("fullchain", "{base}/{cert}.fullchain.pem", 0o644),
                  ("key", "{base}/{cert}.key", 0o600)],
        "test": None, "reload": "systemctl reload postfix",
    },
    "dovecot": {
        "files": [("fullchain", "{base}/{cert}.fullchain.pem", 0o644),
                  ("key", "{base}/{cert}.key", 0o600)],
        "test": None, "reload": "systemctl reload dovecot",
    },
    # zimbra 走專用流程(zmcertmgr),見 _apply_zimbra;路徑/版本差異大,必要時用 generic 自訂
    "zimbra": {"special": "zimbra"},
    "generic": {  # 萬用:路徑與 reload 必須在 config 覆寫
        "files": [], "test": None, "reload": None,
    },
}


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────── HTTP ───────────────────

def _ssl_ctx(cfg: dict):  # type: ignore[no-untyped-def]
    ctx = ssl.create_default_context()
    if cfg.get("ca_cert"):
        ctx.load_verify_locations(cfg["ca_cert"])
    elif not cfg.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(cfg: dict, method: str, path: str, body: dict | None = None) -> dict:
    url = cfg["server"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Agent-Key": cfg["agent_key"], "X-Agent-Version": __version__}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx(cfg)) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:200].decode(errors='replace')}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{method} {path} 連線失敗:{exc}") from exc


# ─────────────────── state ───────────────────

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ─────────────────── 檔案寫入 ───────────────────

def _content(kind: str, bundle: dict) -> str:
    cert = (bundle.get("cert_pem") or "").strip() + "\n"
    chain = (bundle.get("chain_pem") or "").strip()
    chain = (chain + "\n") if chain else ""
    key = (bundle.get("key_pem") or "").strip() + "\n"
    if kind == "cert":
        return cert
    if kind == "chain":
        return chain
    if kind == "fullchain":
        return cert + chain
    if kind == "key":
        return key
    if kind == "combined":
        return cert + chain + key
    raise ValueError(f"unknown content kind: {kind}")


def _atomic_write(path: str, content: str, mode: int) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".jtcert-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _run(cmd: str) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)  # noqa: S602
    return p.returncode, (p.stdout + p.stderr).strip()


def _resolve_files(deployment: dict, profile: dict, cert_name: str) -> list[tuple[str, str, int]]:
    """回 [(kind, path, mode)];config 可用 <kind>_path 覆寫,generic 必須提供。"""
    out: list[tuple[str, str, int]] = []
    base = deployment.get("base_dir", TLS_BASE)
    for kind, tmpl, mode in profile.get("files", []):
        override = deployment.get(f"{kind}_path")
        path = override or tmpl.format(base=base, cert=cert_name)
        out.append((kind, path, mode))
    # generic:從 config 推路徑
    if not profile.get("files"):
        for kind in ("cert", "chain", "fullchain", "key", "combined"):
            p = deployment.get(f"{kind}_path")
            if p:
                out.append((kind, p, 0o600 if kind in ("key", "combined") else 0o644))
    return out


# ─────────────────── 套用單一 deployment ───────────────────

def _apply_zimbra(deployment: dict, bundle: dict, dry_run: bool) -> tuple[str, str]:
    stage = os.path.join(STATE_DIR, "zimbra", deployment["cert"])
    cmds = [
        f"cp {stage}/key.pem /opt/zimbra/ssl/zimbra/commercial/commercial.key",
        f"/opt/zimbra/bin/zmcertmgr deploycrt comm {stage}/cert.pem {stage}/chain.pem",
        "su - zimbra -c 'zmcontrol restart'",
    ]
    if dry_run:
        return "dry-run", "zimbra 將執行:\n  " + "\n  ".join(cmds)
    os.makedirs(stage, exist_ok=True)
    _atomic_write(f"{stage}/cert.pem", _content("cert", bundle), 0o600)
    _atomic_write(f"{stage}/chain.pem", _content("chain", bundle) or _content("cert", bundle), 0o600)
    _atomic_write(f"{stage}/key.pem", _content("key", bundle), 0o600)
    for c in cmds:
        rc, out = _run(c)
        if rc != 0:
            return "failed", f"zimbra 指令失敗:{c}\n{out[:300]}"
    return "ok", "zimbra deploycrt 完成"


def apply_deployment(deployment: dict, bundle: dict, dry_run: bool) -> dict:
    cert_name = deployment["cert"]
    profile_name = deployment.get("profile", "generic")
    profile = PROFILES.get(profile_name)
    result = {"cert": cert_name, "profile": profile_name,
              "fingerprint": bundle.get("fingerprint"), "not_after": bundle.get("not_after"),
              "applied_at": datetime.now(timezone.utc).isoformat(), "dry_run": dry_run}
    if profile is None:
        result.update(status="failed", message=f"未知 profile:{profile_name}")
        return result

    if profile.get("special") == "zimbra":
        status, message = _apply_zimbra(deployment, bundle, dry_run)
        result.update(status=status, message=message)
        return result

    targets = _resolve_files(deployment, profile, cert_name)
    if not targets:
        result.update(status="failed", message="generic profile 需在 config 提供 *_path")
        return result
    reload_cmd = deployment.get("reload", profile.get("reload"))
    test_cmd = deployment.get("test", profile.get("test"))

    if dry_run:
        lines = [f"會寫:{path}（{kind}）" for kind, path, _ in targets]
        if test_cmd:
            lines.append(f"會先測試:{test_cmd}")
        lines.append(f"會 reload:{reload_cmd or '(未設定 reload)'}")
        result.update(status="dry-run", message="\n".join(lines))
        return result

    # 備份既有檔
    backups: list[tuple[str, str | None]] = []
    try:
        for _kind, path, mode in targets:
            bak = None
            if os.path.exists(path):
                bak = f"{path}.jtbak-{int(time.time())}"
                shutil.copy2(path, bak)
            backups.append((path, bak))
            _atomic_write(path, _content(_kind, bundle), mode)
    except Exception as exc:  # noqa: BLE001
        _restore(backups)
        result.update(status="failed", message=f"寫檔失敗已回滾:{exc}")
        return result

    # config-test
    if test_cmd:
        rc, out = _run(test_cmd)
        if rc != 0:
            _restore(backups)
            result.update(status="failed", message=f"config-test 失敗已回滾:{out[:300]}")
            return result
    # reload
    if reload_cmd:
        rc, out = _run(reload_cmd)
        if rc != 0:
            _restore(backups)
            result.update(status="failed", message=f"reload 失敗已回滾:{out[:300]}")
            return result

    result.update(status="ok", message=f"已套用並 reload（{len(targets)} 檔）")
    return result


def _restore(backups: list[tuple[str, str | None]]) -> None:
    for path, bak in backups:
        try:
            if bak and os.path.exists(bak):
                shutil.copy2(bak, path)
        except OSError:
            pass


# ─────────────────── 主流程 ───────────────────

def run_once(cfg: dict, dry_run: bool) -> int:
    deployments = cfg.get("deployments") or []
    if not deployments:
        _log("config 沒有 deployments,無事可做")
        return 0
    try:
        check = _request(cfg, "GET", "/api/v1/cert-agents/check")
    except RuntimeError as exc:
        _log(f"check 失敗:{exc}")
        return 1
    current = {c["cert"]: c for c in check.get("certificates", [])}
    state = _load_state()
    results = []
    failed = 0

    for d in deployments:
        cert = d.get("cert")
        prof = d.get("profile", "generic")
        skey = f"{cert}::{prof}"
        cur = current.get(cert)
        if cur is None:
            _log(f"[{cert}/{prof}] server 上找不到此憑證或不在 scope,略過")
            results.append({"cert": cert, "profile": prof, "status": "skipped",
                            "message": "not in scope / no current version",
                            "applied_at": datetime.now(timezone.utc).isoformat(), "dry_run": dry_run})
            continue
        want_fp = cur["fingerprint"]
        if not dry_run and state.get(skey) == want_fp:
            _log(f"[{cert}/{prof}] 已是最新（{want_fp[:12]}…）,略過")
            continue
        try:
            bundle = _request(cfg, "GET", "/api/v1/cert-agents/bundle?cert=" + urllib.parse.quote(cert))
        except RuntimeError as exc:
            _log(f"[{cert}/{prof}] 下載 bundle 失敗:{exc}")
            results.append({"cert": cert, "profile": prof, "status": "failed", "message": str(exc),
                            "applied_at": datetime.now(timezone.utc).isoformat(), "dry_run": dry_run})
            failed += 1
            continue
        if bundle.get("fingerprint") != want_fp:
            _log(f"[{cert}/{prof}] 指紋不一致(check≠bundle),略過")
            failed += 1
            continue
        res = apply_deployment(d, bundle, dry_run)
        results.append(res)
        _log(f"[{cert}/{prof}] {res['status']}: {res.get('message','').splitlines()[0] if res.get('message') else ''}")
        if res["status"] == "ok" and not dry_run:
            state[skey] = want_fp
        elif res["status"] == "failed":
            failed += 1

    if not dry_run:
        _save_state(state)
    try:
        _request(cfg, "POST", "/api/v1/cert-agents/report", {"deployments": results})
    except RuntimeError as exc:
        _log(f"report 失敗（不影響部署）:{exc}")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="jt-ipam 憑證派送代理")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true", help="只顯示計畫,不動檔/不 reload")
    ap.add_argument("--once", action="store_true", help="跑一次就結束(預設;systemd timer 用)")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()
    if args.version:
        print(__version__)
        return
    try:
        with open(args.config) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError as exc:
        sys.stderr.write(f"讀不到設定檔 {args.config}:{exc}\n")
        sys.exit(2)
    for req in ("server", "agent_key"):
        if not cfg.get(req):
            sys.stderr.write(f"設定檔缺少必填欄位:{req}\n")
            sys.exit(2)
    sys.exit(run_once(cfg, args.dry_run))


if __name__ == "__main__":
    main()
