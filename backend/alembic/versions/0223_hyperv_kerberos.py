"""Hyper-V：WinRM 的 Kerberos 驗證（客戶的網域只開 Kerberos）

- hyperv_instances.auth_method：ntlm（預設）或 kerberos
- kerberos_realm：帳號寫 DOMAIN\\user 時要填（NetBIOS 名稱不一定等於 Kerberos 領域）；寫 user@domain 可以留空
- kerberos_kdc：留空＝照 AD 慣例查 DNS SRV；填了就用填的（換行或逗號分隔）

Revision ID: 0223_hyperv_kerberos
Revises: 0222_hyperv
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0223_hyperv_kerberos"
down_revision: str | None = "0222_hyperv"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("hyperv_instances", sa.Column("auth_method", sa.String(16), nullable=False, server_default="ntlm"))
    op.add_column("hyperv_instances", sa.Column("kerberos_realm", sa.String(255)))
    op.add_column("hyperv_instances", sa.Column("kerberos_kdc", sa.Text()))
    op.create_check_constraint("ck_hyperv_instances_auth", "hyperv_instances", "auth_method IN ('ntlm','kerberos')")


def downgrade() -> None:
    op.drop_constraint("ck_hyperv_instances_auth", "hyperv_instances", type_="check")
    op.drop_column("hyperv_instances", "kerberos_kdc")
    op.drop_column("hyperv_instances", "kerberos_realm")
    op.drop_column("hyperv_instances", "auth_method")
