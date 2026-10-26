"""區段加數字的相容 ID（2026-10-24）

Proxmox VE SDN 的 phpIPAM 外掛設定裡「區段」只收整數（properties: section => integer），
而 jt-ipam 的區段 ID 是 UUID，所以 PVE 根本填不進來。給每個區段一個穩定的流水號，
phpIPAM 相容 API 的 sections/{id} 與建立子網路的 sectionId 都接受它。

既有區段依建立時間依序編號；之後新建的由 sequence 自動給號。

Revision ID: 0230_section_compat_id
Revises: 0229_openstack
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0230_section_compat_id"
down_revision: str | None = "0229_openstack"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS sections_compat_id_seq")
    op.add_column("sections", sa.Column("compat_id", sa.Integer(), nullable=True))
    op.execute("""
        UPDATE sections s SET compat_id = n.rn
          FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn FROM sections) n
         WHERE n.id = s.id
    """)
    op.execute("SELECT setval('sections_compat_id_seq', GREATEST((SELECT COALESCE(MAX(compat_id), 0) FROM sections), 1), "
               "(SELECT COUNT(*) > 0 FROM sections))")
    op.execute("ALTER TABLE sections ALTER COLUMN compat_id SET DEFAULT nextval('sections_compat_id_seq')")
    op.execute("ALTER SEQUENCE sections_compat_id_seq OWNED BY sections.compat_id")
    op.alter_column("sections", "compat_id", nullable=False)
    op.create_unique_constraint("uq_sections_compat_id", "sections", ["compat_id"])


def downgrade() -> None:
    op.drop_constraint("uq_sections_compat_id", "sections", type_="unique")
    op.drop_column("sections", "compat_id")
    op.execute("DROP SEQUENCE IF EXISTS sections_compat_id_seq")
