"""系統自我診斷 —— 後端看得到的那一半（`scripts/jt-ipam.sh doctor` 是另一半）。

由來（2026-09-05 客戶回報）：儀表板顯示 55 台裝置，點進裝置清單卻是
「Internal Server Error」＋空白清單。原因是**資料庫結構落後於程式**（升級時 alembic
沒跑完）：儀表板那個數字是 `count(*)`，不需要讀任何欄位；清單是 `select(Device)`，
會列出每一個欄位 —— 少一欄就整支 500。

真正的問題不是那個 500，而是**系統其實查得出原因卻沒有講**。啟動時就知道結構落後了，
卻讓使用者一頁一頁踩 500 再自己去猜。所以這裡做兩件事：

1. `schema_state()` 在啟動時跑一次，落後就用 error 記下來，並讓管理員在畫面上看得到。
2. `run_checks()` 給管理頁的「系統診斷」用：把後端查得到的狀況一次列出來，
   每一項都附「該怎麼修」，而不是只說「有問題」。

**刻意不呼叫 `scripts/jt-ipam.sh`**：後端是以非特權帳號跑的，去 shell out 一個需要 root
的腳本只會得到一份不可靠的結果，還多開一個執行外部指令的面。系統層的檢查（systemd、
nginx、備份檔、掃描代理）仍然要用 CLI 版的 doctor —— 這一頁會明講哪些是它看不到的。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("self_check")

Status = Literal["ok", "warn", "bad"]


@dataclass
class Check:
    key: str
    title: str
    status: Status
    detail: str = ""
    #: 該怎麼修（指令或動作）。**每個非 ok 的項目都要有** —— 只說「壞了」等於沒說。
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    generated_at: str = ""

    @property
    def bad(self) -> int:
        return sum(1 for c in self.checks if c.status == "bad")

    @property
    def warn(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "bad": self.bad, "warn": self.warn,
            "ok": sum(1 for c in self.checks if c.status == "ok"),
            "checks": [vars(c) for c in self.checks],
        }

    def as_text(self) -> str:
        """可下載的純文字報告（貼進工單用）。"""
        icon = {"ok": "[ OK ]", "warn": "[WARN]", "bad": "[FAIL]"}
        lines = [f"jt-ipam self-check — {self.generated_at}", ""]
        for c in self.checks:
            lines.append(f"{icon[c.status]} {c.title}")
            if c.detail:
                lines.append(f"        {c.detail}")
            if c.fix and c.status != "ok":
                lines.append(f"        → {c.fix}")
        lines += ["", f"{self.bad} failed, {self.warn} warnings, "
                      f"{sum(1 for c in self.checks if c.status == 'ok')} ok", ""]
        lines.append("Note: system-level checks (systemd units, nginx, backups, scan agent)")
        lines.append("are not visible from the backend. Run on the server:")
        lines.append("  sudo bash /opt/jt-ipam/scripts/jt-ipam.sh doctor")
        return "\n".join(lines)


# ─────────────────── 資料庫結構 ───────────────────
def _alembic_heads() -> set[str]:
    """程式這一份原始碼的 migration head（不碰資料庫）。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent.parent      # backend/
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


async def schema_state(session: AsyncSession) -> dict[str, Any]:
    """資料庫結構有沒有跟上程式。

    回 `{"current": ..., "head": ..., "behind": bool, "error": str|None}`。
    讀不出來時 `behind` 是 **False**（不確定就不要嚇人），但 `error` 會帶原因。
    """
    out: dict[str, Any] = {"current": None, "head": None, "behind": False, "error": None}
    try:
        out["head"] = ", ".join(sorted(_alembic_heads())) or None
    except Exception as exc:
        out["error"] = f"cannot read migration scripts: {exc}"
        return out
    try:
        rows = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        out["current"] = ", ".join(sorted(str(r) for r in rows)) or None
    except Exception as exc:
        out["error"] = f"cannot read alembic_version: {exc}"
        return out
    out["behind"] = bool(out["head"]) and out["current"] != out["head"]
    return out


async def warn_if_schema_behind(session: AsyncSession) -> bool:
    """啟動時呼叫：結構落後就用 error 記一筆很明顯的日誌。回傳「是否落後」。"""
    state = await schema_state(session)
    if state["behind"]:
        log.error(
            "database_schema_behind",
            current=state["current"], head=state["head"],
            fix="sudo bash /opt/jt-ipam/scripts/jt-ipam.sh upgrade",
            note="pages that read full rows will fail with 500 until this is fixed",
        )
    elif state["error"]:
        log.warning("schema_check_failed", error=state["error"])
    return bool(state["behind"])


def _frontend_check() -> Check:
    """前端建置是否存在、版本是否與後端相同。

    拆成同步函式而不是寫在 `run_checks()` 裡：那是個 async 函式，在裡面直接用
    pathlib 會被 lint 擋（在事件迴圈裡做阻塞 I/O）。這幾個檔案很小，同步讀沒問題。
    """
    import json

    from app.version import __version__ as backend_ver
    try:
        dist = Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
        if not (dist / "index.html").exists():
            return Check("frontend", "前端建置", "bad", "找不到 dist/index.html",
                         "sudo bash /opt/jt-ipam/scripts/jt-ipam.sh upgrade")
        vfile = dist / "version.json"
        if not vfile.exists():
            return Check("frontend", "前端建置", "warn", "沒有 dist/version.json", "重新建置前端")
        fev = json.loads(vfile.read_text(encoding="utf-8")).get("version")
        if fev != backend_ver:
            return Check("frontend", "前端與後端版本不一致", "warn",
                         f"前端 {fev}、後端 {backend_ver}",
                         "cd /opt/jt-ipam/frontend && npm run build（或重跑 upgrade）")
        return Check("frontend", "前端建置", "ok", f"與後端相同（{fev}）")
    except Exception as exc:
        return Check("frontend", "前端建置", "warn", str(exc)[:200])


# ─────────────────── 各項檢查 ───────────────────
async def run_checks(session: AsyncSession) -> Report:
    """後端查得到的所有檢查。任何一項自己壞掉都不可以讓整份報告失敗。"""
    rep = Report(generated_at=datetime.now(UTC).isoformat(timespec="seconds"))

    # 1) 資料庫結構 —— 放第一個：它是「畫面到處 500」最常見的原因
    state = await schema_state(session)
    if state["error"]:
        rep.checks.append(Check(
            "schema", "資料庫結構版本", "warn", state["error"],
            "確認後端能連到資料庫，且 alembic 目錄完整"))
    elif state["behind"]:
        rep.checks.append(Check(
            "schema", "資料庫結構落後於程式", "bad",
            f"資料庫在 {state['current']}，程式需要 {state['head']}",
            "sudo bash /opt/jt-ipam/scripts/jt-ipam.sh upgrade"
            "（或 alembic upgrade head 後重啟後端）"))
    else:
        rep.checks.append(Check(
            "schema", "資料庫結構", "ok", f"已在最新版本（{state['current']}）"))

    # 2) 資料庫連線與擴充
    try:
        ver = (await session.execute(text("SHOW server_version"))).scalar_one()
        exts = set((await session.execute(
            text("SELECT extname FROM pg_extension"))).scalars().all())
        missing = {"vector", "pg_trgm"} - exts
        if missing:
            rep.checks.append(Check(
                "db_ext", "PostgreSQL 擴充", "bad",
                f"缺少：{', '.join(sorted(missing))}（PostgreSQL {ver}）",
                "psql -d <db> -c 'CREATE EXTENSION IF NOT EXISTS vector; "
                "CREATE EXTENSION IF NOT EXISTS pg_trgm;'"))
        else:
            rep.checks.append(Check("db_ext", "PostgreSQL", "ok",
                                    f"{ver}，vector / pg_trgm 都在"))
    except Exception as exc:
        rep.checks.append(Check("db_ext", "PostgreSQL", "bad", str(exc)[:200],
                                "systemctl status postgresql"))

    # 3) 前端建置版本要與後端一致 —— 不一致＝使用者在跑舊的 JS bundle，
    #    這是「存檔沒生效／功能怪怪的」最常見的假故障來源
    rep.checks.append(_frontend_check())

    # 4) 排程同步 —— 只看「有沒有在跑」，實際內容看整合各自的最後錯誤
    try:
        from sqlalchemy import func, select

        from app.models.background_task import BackgroundTask
        last = (await session.execute(
            select(func.max(BackgroundTask.queued_at)))).scalar_one_or_none()
        if last is None:
            rep.checks.append(Check("sync", "背景作業", "warn", "從來沒有背景作業記錄",
                                    "systemctl status jt-ipam-sync.timer"))
        else:
            age_h = (datetime.now(UTC) - last).total_seconds() / 3600
            if age_h > 24:
                rep.checks.append(Check(
                    "sync", "背景作業停擺", "warn",
                    f"最後一筆是 {age_h:.0f} 小時前（{last:%Y-%m-%d %H:%M}）",
                    "systemctl status jt-ipam-sync.timer；journalctl -u jt-ipam-sync -n 60"))
            else:
                rep.checks.append(Check("sync", "背景作業", "ok",
                                        f"最後一筆 {last:%Y-%m-%d %H:%M}"))
    except Exception as exc:
        rep.checks.append(Check("sync", "背景作業", "warn", str(exc)[:200]))

    # 5) 整合的最後錯誤 —— 一次看完，不用逐頁點
    try:
        rep.checks.append(await _integration_errors(session))
    except Exception as exc:
        rep.checks.append(Check("integrations", "整合狀態", "warn", str(exc)[:200]))

    # 6) 磁碟空間（資料庫與備份都吃這裡）
    try:
        usage = shutil.disk_usage("/")
        free_pct = usage.free / usage.total * 100
        detail = f"根目錄剩餘 {usage.free / 2**30:.1f} GiB（{free_pct:.0f}%）"
        if free_pct < 5:
            rep.checks.append(Check("disk", "磁碟空間不足", "bad", detail, "清理或擴充磁碟"))
        elif free_pct < 15:
            rep.checks.append(Check("disk", "磁碟空間偏低", "warn", detail, "留意成長趨勢"))
        else:
            rep.checks.append(Check("disk", "磁碟空間", "ok", detail))
    except Exception as exc:
        rep.checks.append(Check("disk", "磁碟空間", "warn", str(exc)[:200]))

    # 7) ICMP 能力（LXC 常見）：掃描代理要用得到
    try:
        from app.services.netdiag import icmp_socket_available
        if icmp_socket_available():
            rep.checks.append(Check("icmp", "ICMP 探測", "ok", "非特權 ICMP socket 可用"))
        else:
            rep.checks.append(Check(
                "icmp", "ICMP 探測", "warn",
                "非特權 ICMP socket 不可用（容器內常見；外部 ping 執行檔仍可能可用）",
                "LXC 請以 systemd drop-in 加 AmbientCapabilities=CAP_NET_RAW"))
    except Exception as exc:
        # 不可以靜默跳過：檢查「不見了」跟「通過了」在畫面上長得一樣
        rep.checks.append(Check("icmp", "ICMP 探測", "warn", f"檢查本身失敗：{exc}"[:200]))

    # 8) 資料健檢：哪些列會讓清單頁讀不出來（客戶回報的那一類）
    try:
        rows = await data_health(session)
        bad_tables = [r for r in rows if r.get("bad_count")]
        errored = [r for r in rows if r.get("error")]
        if bad_tables:
            detail = "；".join(
                f"{r['table']} {r['bad_count']} 筆（例：{r['bad'][0]['label']} — {r['bad'][0]['why']}）"
                for r in bad_tables[:3])
            rep.checks.append(Check(
                "data", "有資料無法在清單頁顯示", "bad", detail[:600],
                "這是程式的欄位限制比資料庫嚴造成的。請把這段訊息回報給我們；"
                "先自行處理的話，把上面那幾筆的該欄位改成合規值即可"))
        elif errored:
            rep.checks.append(Check("data", "資料健檢", "warn",
                                    "；".join(f"{r['table']}：{r['error']}" for r in errored)[:300]))
        else:
            truncated = [r["table"] for r in rows if r.get("truncated")]
            note = f"（只檢查了前 {DATA_SCAN_LIMIT} 筆：{'、'.join(truncated)}）" if truncated else ""
            rep.checks.append(Check("data", "資料健檢", "ok",
                                    f"清單頁的資料都讀得出來{note}"))
    except Exception as exc:
        rep.checks.append(Check("data", "資料健檢", "warn", f"檢查本身失敗：{exc}"[:200]))

    # 9) 環境提示：正式環境卻開著 debug
    try:
        from app.core.config import get_settings
        st = get_settings()
        if st.app_debug:
            rep.checks.append(Check("debug", "偵錯模式開啟中", "warn",
                                    f"APP_ENV={st.app_env}", "正式環境請關閉 APP_DEBUG"))
        else:
            rep.checks.append(Check("debug", "執行模式", "ok", f"APP_ENV={st.app_env}"))
    except Exception as exc:
        rep.checks.append(Check("debug", "執行模式", "warn", f"檢查本身失敗：{exc}"[:200]))

    return rep


async def _integration_errors(session: AsyncSession) -> Check:
    """把各整合的 `last_error` 掃一遍。有錯的列出名字，沒錯就一句話帶過。"""
    from sqlalchemy import select

    from app.models.adguard import AdGuardInstance
    from app.models.firewall import OPNsenseFirewall
    from app.models.fortigate import FortiGateFirewall
    from app.models.librenms import LibreNMSInstance
    from app.models.mikrotik import MikroTikRouter
    from app.models.paloalto import PaloAltoFirewall
    from app.models.pfsense import PfSenseFirewall
    from app.models.virt import ProxmoxInstance
    from app.models.wazuh import WazuhInstance
    from app.models.zabbix import ZabbixInstance

    failing: list[str] = []
    total = 0
    for model, label in (
        (OPNsenseFirewall, "OPNsense"), (PfSenseFirewall, "pfSense"),
        (FortiGateFirewall, "FortiGate"), (PaloAltoFirewall, "Palo Alto"),
        (MikroTikRouter, "MikroTik"), (LibreNMSInstance, "LibreNMS"),
        (ZabbixInstance, "Zabbix"), (WazuhInstance, "Wazuh"),
        (AdGuardInstance, "AdGuard"), (ProxmoxInstance, "Proxmox"),
    ):
        for obj in (await session.execute(select(model))).scalars().all():
            total += 1
            if getattr(obj, "last_error", None):
                failing.append(f"{label}／{getattr(obj, 'name', '?')}")
    if not total:
        return Check("integrations", "整合狀態", "ok", "尚未設定任何整合")
    if failing:
        return Check("integrations", "整合有錯誤", "warn",
                     f"{len(failing)} / {total} 個實例有最後錯誤：{'、'.join(failing[:8])}",
                     "到各整合設定頁看「最後錯誤」與「測試連線」")
    return Check("integrations", "整合狀態", "ok", f"{total} 個實例都沒有最後錯誤")


# ─────────────────── 資料健檢 ───────────────────
#: 「清單頁讀得出來嗎」要檢查的表。每一項是（畫面上的名稱, ORM, 讀取用 schema, 顯示欄位）。
#:
#: 為什麼需要這個：讀取用的 schema 繼承了**寫入用**的約束（型別白名單、長度上限、
#: 數值範圍），但整合同步進來的資料不走表單 —— LibreNMS／Proxmox 給的 vendor／model
#: 可以很長，type 也可能不在我們的清單裡。一列不合規就會讓**整頁 500**，而儀表板的
#: count(*) 不讀欄位所以照樣正常（2026-09-05 客戶回報的就是這個組合）。
DATA_TABLES: tuple[tuple[str, str, str, str], ...] = (
    ("裝置", "app.models.device:Device", "app.schemas.device:DeviceRead", "name"),
    ("IP 位址", "app.models.address:IPAddress", "app.schemas.address:IPAddressRead", "ip"),
    ("子網路", "app.models.subnet:Subnet", "app.schemas.subnet:SubnetRead", "cidr"),
    ("區段", "app.models.section:Section", "app.schemas.section:SectionRead", "name"),
    ("機櫃", "app.models.location:Rack", "app.schemas.location:RackRead", "name"),
    ("機房 / 地點", "app.models.location:Location", "app.schemas.location:LocationRead", "name"),
    ("單位", "app.models.customer:Customer", "app.schemas.customer:CustomerRead", "name"),
)

#: 一次最多檢查幾列。這一頁是人按下去才跑的，不能在大型環境上把資料庫拖住；
#: 掃不完時會**明講掃到哪裡**，而不是回一個看起來全綠的結果。
DATA_SCAN_LIMIT = 5000


def _load(path: str) -> Any:
    module, name = path.split(":")
    import importlib
    return getattr(importlib.import_module(module), name)


def _why(exc: Any, row: Any) -> str:
    """把 Pydantic 的錯誤縮成一句人看得懂的話。

    一定要帶**實際值**（或長度）：只說「string too long」的話，看的人還是得自己去
    翻資料庫才知道哪裡要改 —— 那就等於沒有把診斷做完。
    """
    out: list[str] = []
    for err in getattr(exc, "errors", lambda: [])()[:3]:
        field = ".".join(str(x) for x in err.get("loc", ())) or "?"
        msg = err.get("msg", "")
        value = getattr(row, field.split(".")[0], None)
        if isinstance(value, str) and len(value) > 60:
            shown = f"（長度 {len(value)}，開頭：{value[:40]}…）"
        elif value is None:
            shown = "（目前是空值）"
        else:
            shown = f"（目前值：{value}）"
        out.append(f"{field}：{msg}{shown}")
    return "；".join(out) or str(exc)[:200]


async def data_health(session: AsyncSession) -> list[dict[str, Any]]:
    """逐表檢查「這些列在清單頁讀得出來嗎」，回傳讀不出來的那些。

    直接拿**正式在用的讀取 schema** 去驗，所以這裡通過就等於清單頁不會因為資料而爆；
    自己另寫一套規則去猜，遲早會跟真正的 schema 走鐘。
    """
    from sqlalchemy import func, select

    out: list[dict[str, Any]] = []
    for label, model_path, schema_path, name_field in DATA_TABLES:
        try:
            model, schema = _load(model_path), _load(schema_path)
        except Exception as exc:                  # 表或 schema 不在（舊版）→ 跳過但要講
            out.append({"table": label, "error": f"無法載入：{exc}"[:200]})
            continue
        try:
            total = int(await session.scalar(select(func.count()).select_from(model)) or 0)
            rows = (await session.execute(
                select(model).limit(DATA_SCAN_LIMIT))).scalars().all()
        except Exception as exc:
            out.append({"table": label, "error": str(exc)[:200]})
            continue

        bad: list[dict[str, Any]] = []
        for row in rows:
            try:
                schema.model_validate(row)
            except Exception as exc:              # 就是這一列會讓清單頁 500
                bad.append({
                    "id": str(getattr(row, "id", "")),
                    "label": str(getattr(row, name_field, "") or "")[:80],
                    "why": _why(exc, row),
                })
        if bad or total > len(rows):
            out.append({
                "table": label, "checked": len(rows), "total": total,
                "bad": bad[:20], "bad_count": len(bad),
                "truncated": total > len(rows),
            })
    return out


def env_file_hint() -> str:
    return os.environ.get("JTIPAM_ENV_FILE", "/etc/jt-ipam/backend.env")
