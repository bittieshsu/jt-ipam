"""change_impact_services：IP 變更評估 M2（服務依賴、交換器維護、節點停機）

最小的服務物件（只為呈現業務影響，不是 CMDB）、服務端點、依賴群組（k-of-n）與群組成員；
計畫的情境多兩種：switch_maintenance、node_downtime。

Revision ID: 0189_change_impact_services
Revises: 0188_isoinsight
"""

from __future__ import annotations

from alembic import op

revision: str = "0189_change_impact_services"
down_revision: str | None = "0188_isoinsight"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("impact_services", "impact_service_endpoints", "impact_dependency_groups", "impact_dependency_members")
_OLD_SCENARIOS = "('ip_renumber', 'device_decommission')"
_NEW_SCENARIOS = "('ip_renumber', 'device_decommission', 'switch_maintenance', 'node_downtime')"

# 建表語句在寫這個 migration 時由模型產生後固定下來：之後改模型不會改變這個 migration 建出來的東西
_DDL = (
    """
    CREATE TABLE impact_services (
        customer_id UUID,
        name VARCHAR(160) NOT NULL,
        description TEXT,
        owner_user_id UUID,
        owner_group_id UUID,
        criticality VARCHAR(16) DEFAULT 'normal' NOT NULL,
        status VARCHAR(16) DEFAULT 'active' NOT NULL,
        maintenance_notes TEXT,
        version INTEGER DEFAULT '1' NOT NULL,
        created_by UUID,
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_impact_services PRIMARY KEY (id),
        CONSTRAINT ck_impact_services_criticality CHECK (criticality IN ('critical', 'high', 'normal', 'low')),
        CONSTRAINT ck_impact_services_status CHECK (status IN ('active', 'retired')),
        CONSTRAINT fk_impact_services_customer_id_customers FOREIGN KEY(customer_id) REFERENCES customers (id) ON DELETE SET NULL,
        CONSTRAINT fk_impact_services_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_impact_services_owner_group_id_groups FOREIGN KEY(owner_group_id) REFERENCES groups (id) ON DELETE SET NULL,
        CONSTRAINT fk_impact_services_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX ix_impact_services_created_by ON impact_services (created_by) WHERE created_by IS NOT NULL
    """,
    """
    CREATE INDEX ix_impact_services_customer_id ON impact_services (customer_id) WHERE customer_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_impact_services_owner_group_id ON impact_services (owner_group_id) WHERE owner_group_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_impact_services_owner_user_id ON impact_services (owner_user_id) WHERE owner_user_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX uq_impact_services_name ON impact_services (customer_id, lower(name)) NULLS NOT DISTINCT
    """,
    """
    CREATE TABLE impact_service_endpoints (
        service_id UUID NOT NULL,
        object_type VARCHAR(16),
        object_id UUID,
        hostname VARCHAR(255),
        port INTEGER,
        protocol VARCHAR(8),
        source VARCHAR(16) DEFAULT 'manual' NOT NULL,
        confirmed_by UUID,
        confirmed_at TIMESTAMP WITH TIME ZONE,
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_impact_service_endpoints PRIMARY KEY (id),
        CONSTRAINT ck_impact_service_endpoints_object_type CHECK (object_type IS NULL OR object_type IN ('ip', 'device', 'vm')),
        CONSTRAINT ck_impact_service_endpoints_object_pair CHECK ((object_type IS NULL) = (object_id IS NULL)),
        CONSTRAINT ck_impact_service_endpoints_has_target CHECK (object_id IS NOT NULL OR hostname IS NOT NULL),
        CONSTRAINT fk_impact_service_endpoints_service_id_impact_services FOREIGN KEY(service_id) REFERENCES impact_services (id) ON DELETE CASCADE,
        CONSTRAINT fk_impact_service_endpoints_confirmed_by_users FOREIGN KEY(confirmed_by) REFERENCES users (id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX ix_impact_service_endpoints_confirmed_by ON impact_service_endpoints (confirmed_by) WHERE confirmed_by IS NOT NULL
    """,
    """
    CREATE INDEX ix_impact_service_endpoints_object ON impact_service_endpoints (object_type, object_id) WHERE object_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_impact_service_endpoints_service_id ON impact_service_endpoints (service_id)
    """,
    """
    CREATE TABLE impact_dependency_groups (
        service_id UUID NOT NULL,
        name VARCHAR(120) NOT NULL,
        required_count INTEGER DEFAULT '1' NOT NULL,
        purpose TEXT,
        position INTEGER DEFAULT '0' NOT NULL,
        confirmed_by UUID,
        confirmed_at TIMESTAMP WITH TIME ZONE,
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_impact_dependency_groups PRIMARY KEY (id),
        CONSTRAINT ck_impact_dependency_groups_required_count CHECK (required_count >= 1),
        CONSTRAINT fk_impact_dependency_groups_service_id_impact_services FOREIGN KEY(service_id) REFERENCES impact_services (id) ON DELETE CASCADE,
        CONSTRAINT fk_impact_dependency_groups_confirmed_by_users FOREIGN KEY(confirmed_by) REFERENCES users (id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX ix_impact_dependency_groups_confirmed_by ON impact_dependency_groups (confirmed_by) WHERE confirmed_by IS NOT NULL
    """,
    """
    CREATE INDEX ix_impact_dependency_groups_service_id ON impact_dependency_groups (service_id)
    """,
    """
    CREATE TABLE impact_dependency_members (
        group_id UUID NOT NULL,
        object_type VARCHAR(16) NOT NULL,
        object_id UUID NOT NULL,
        relation_type VARCHAR(24) NOT NULL,
        note TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        CONSTRAINT pk_impact_dependency_members PRIMARY KEY (id),
        CONSTRAINT ck_impact_dependency_members_object_type CHECK (object_type IN ('device', 'vm', 'ip', 'subnet', 'service')),
        CONSTRAINT ck_impact_dependency_members_relation_type CHECK (relation_type IN ('hosted_on', 'requires_network', 'requires_storage', 'requires_power', 'requires_service', 'references', 'observed_on')),
        CONSTRAINT uq_impact_dependency_members UNIQUE (group_id, object_type, object_id),
        CONSTRAINT fk_impact_dependency_members_group_id_impact_dependency_groups FOREIGN KEY(group_id) REFERENCES impact_dependency_groups (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_impact_dependency_members_group_id ON impact_dependency_members (group_id)
    """,
    """
    CREATE INDEX ix_impact_dependency_members_object ON impact_dependency_members (object_type, object_id)
    """,
)


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)
    op.execute("ALTER TABLE change_plans DROP CONSTRAINT ck_change_plans_scenario_type")
    op.execute(f"ALTER TABLE change_plans ADD CONSTRAINT ck_change_plans_scenario_type "
               f"CHECK (scenario_type IN {_NEW_SCENARIOS})")


def downgrade() -> None:
    # 降版前要先處理掉 M2 情境的計畫，否則舊的限制加不回去（刻意不默默刪資料）
    op.execute("ALTER TABLE change_plans DROP CONSTRAINT ck_change_plans_scenario_type")
    op.execute(f"ALTER TABLE change_plans ADD CONSTRAINT ck_change_plans_scenario_type "
               f"CHECK (scenario_type IN {_OLD_SCENARIOS})")
    for name in reversed(_TABLES):
        op.drop_table(name)
