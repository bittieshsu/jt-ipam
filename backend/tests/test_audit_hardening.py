"""稽核表的三道保護（2026-10-09 合規核對）。

1. TRUNCATE 也擋（0201）：以前一行 `TRUNCATE audit_logs` 就能清掉整條鏈
2. 表交給不能登入的角色（scripts/sql/audit-harden.sql，安裝與升級會跑）：表的擁有者可以停用
   觸發器，以前擁有者就是應用程式帳號
3. 轉送到外部時帶雜湊、錨定點也送出去：本機的錨定檔與系統日誌和資料庫在同一台主機上
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import text

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.anyio
async def test_truncate_is_blocked(db_session) -> None:
    with pytest.raises(Exception) as ei:
        await db_session.execute(text("TRUNCATE audit_logs"))
    assert "append-only" in str(ei.value)
    await db_session.rollback()


def test_install_and_upgrade_hand_the_table_to_a_nologin_role() -> None:
    sh = (ROOT / "scripts/jt-ipam.sh").read_text(encoding="utf-8")
    install = sh[sh.index("cmd_install() {"):sh.index("cmd_doctor() {")]
    upgrade = sh[sh.index("cmd_upgrade() {"):sh.index("cmd_uninstall() {")]
    assert "harden_audit_table" in install, "安裝沒有把稽核表交出去"
    u_un, u_mig, u_h = (upgrade.index("unharden_audit_table"), upgrade.index("alembic upgrade head"),
                        upgrade.rindex("harden_audit_table"))
    assert u_un < u_mig < u_h, "升級要在遷移前暫時歸還、遷移後收回"
    assert "harden-audit) shift; cmd_harden_audit" in sh, "還原備份之後要能單獨執行"
    sql = (ROOT / "scripts/sql/audit-harden.sql").read_text(encoding="utf-8")
    for must in ("CREATE ROLE jt_ipam_audit_owner NOLOGIN", "ALTER TABLE public.audit_logs OWNER TO jt_ipam_audit_owner",
                 "REVOKE ALL ON public.audit_logs", "GRANT SELECT, INSERT ON public.audit_logs",
                 "ALTER FUNCTION public.audit_logs_append_only() OWNER TO jt_ipam_audit_owner",
                 "ALTER FUNCTION public.audit_logs_no_truncate() OWNER TO jt_ipam_audit_owner"):
        assert must in sql, must
    assert "GRANT USAGE, SELECT ON SEQUENCE" in sql and "UPDATE" not in sql.split("GRANT USAGE")[1]


@pytest.mark.anyio
async def test_forwarded_events_carry_the_chain_and_anchors_are_forwarded(db_session, monkeypatch, tmp_path) -> None:
    from app.core.audit import append_audit
    from app.services import audit_anchor, audit_forward
    from app.services.system_config import AuditForwardConfig

    sent: list[dict] = []

    async def _cfg(_s):   # noqa: ANN001
        return AuditForwardConfig(enabled=True, host="198.51.100.5", port=514, protocol="udp", fmt="gelf")

    async def _do(_cfg, ev):   # noqa: ANN001
        sent.append(ev)

    monkeypatch.setattr(audit_forward, "get_audit_forward", _cfg)
    monkeypatch.setattr(audit_forward, "_do", _do)
    await append_audit(db_session, actor_user_id=None, actor_ip=None, actor_user_agent=None,
                       object_type="system", object_id=None, action="unit_test", diff=None, request_id=None)
    await db_session.commit()
    import asyncio
    await asyncio.sleep(0)
    ev = next(e for e in sent if e.get("action") == "unit_test")
    assert len(ev["this_hash"]) == 64 and len(ev["prev_hash"]) == 64

    res = await audit_anchor.verify_and_anchor(db_session, path=tmp_path / "anchors.jsonl")
    await asyncio.sleep(0)
    assert res["ok"], res
    anchor = next(e for e in sent if e.get("action") == "audit_anchor")
    assert anchor["this_hash"] == ev["this_hash"]
    gelf = audit_forward._gelf("h", ev)
    assert ev["this_hash"].encode() in gelf


@pytest.mark.anyio
async def test_doctor_reports_who_owns_the_audit_table(db_session) -> None:
    from app.services.self_check import _audit_owner_check
    chk = await _audit_owner_check(db_session)
    # 測試資料庫沒有跑 harden（測試帳號就是擁有者）→ 要講出來並給修法
    assert chk.status == "warn" and "harden-audit" in chk.fix
