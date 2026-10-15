"""ip_addresses.state 允許 deprecated／quarantine（2026-10-15 盤點 AI 工具時發現）

畫面的狀態選單與 API schema 早就有「準備退場（deprecated）」「隔離中（quarantine）」，資料表的 CHECK 約束卻只有
active／reserved／offline／dhcp／used —— 選了這兩個存檔就是 500（IntegrityError），網頁、API、AI 工具都一樣。
這裡把約束放寬成跟 schema 一致。降版時先把這兩種狀態改回 reserved 才能收回約束。

Revision ID: 0218_ip_state_lifecycle
Revises: 0217_snmp_collector
"""
from __future__ import annotations

from alembic import op

revision: str = "0218_ip_state_lifecycle"
down_revision: str | None = "0217_snmp_collector"
branch_labels = None
depends_on = None

#: 模型的 CheckConstraint(name="ip_state_valid") 經命名慣例就是這個名字
_NAME = "ck_ip_addresses_ip_state_valid"
_NEW = "state IN ('active','reserved','offline','dhcp','used','deprecated','quarantine')"
_OLD = "state IN ('active','reserved','offline','dhcp','used')"

#: 0001 把已經帶前綴的名字又丟進命名慣例，實際名稱是 ck_ip_addresses_ck_ip_addresses_ip_state_valid
#: （正式站台與新裝都是）；保險起見依內容找，不依名稱：ip_addresses 上所有檢查 state 的 CHECK 都拿掉
_DROP_STATE_CHECKS = """
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'ip_addresses'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) ~ '\\mstate\\M'
  LOOP
    EXECUTE format('ALTER TABLE ip_addresses DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(_DROP_STATE_CHECKS)
    op.execute(f"ALTER TABLE ip_addresses ADD CONSTRAINT {_NAME} CHECK ({_NEW})")


def downgrade() -> None:
    op.execute("UPDATE ip_addresses SET state = 'reserved' WHERE state IN ('deprecated', 'quarantine')")
    op.execute(_DROP_STATE_CHECKS)
    op.execute(f"ALTER TABLE ip_addresses ADD CONSTRAINT {_NAME} CHECK ({_OLD})")
