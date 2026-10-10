"""audit_logs_no_truncate：稽核表也擋 TRUNCATE

0149 擋了 UPDATE／DELETE，但 TRUNCATE 刻意沒擋（當時測試每次都要清空全部資料表）。
一行 `TRUNCATE audit_logs` 就能把整條稽核鏈清掉 —— 這裡補上語句層級的觸發器；測試的清表程序
改成先暫停這個觸發器（測試用的資料庫帳號是表的擁有者）。

正式環境的另一半在安裝／升級腳本：把稽核表交給一個不能登入的資料庫角色擁有
（`scripts/jt-ipam.sh harden-audit`），應用程式帳號只剩 SELECT／INSERT ——
停用觸發器、改寫、刪除、清空都需要表的擁有者，應用程式帳號就做不到了。

Revision ID: 0201_audit_logs_no_truncate
Revises: 0200_user_sessions_mfa
"""

from __future__ import annotations

from alembic import op

revision: str = "0201_audit_logs_no_truncate"
down_revision: str | None = "0200_user_sessions_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_logs_no_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only: TRUNCATE is not allowed'
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("""
        CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_no_truncate()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_no_truncate()")
