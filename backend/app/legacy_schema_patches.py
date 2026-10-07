"""EINGEFROREN — nur noch die Brücke für Installationen aus der Zeit vor Alembic.

Bis 1.8.x hat OPNexus Schema-Änderungen an bestehenden Tabellen über diese
idempotenten `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`-Statements nach
`create_all()` nachgezogen. Ab 1.9.0 verwaltet Alembic das Schema (siehe
migrations/ und app/migrate.py); diese Datei wird nur noch ein einziges Mal
ausgeführt: beim ersten Start einer Datenbank, die Tabellen, aber noch keine
`alembic_version` hat. Danach wird sie auf Revision 0001 gestempelt.

REGELN:
- NIE wieder einen Eintrag hinzufügen. Neue Schema-Änderungen sind eine
  Alembic-Migration: `alembic revision --autogenerate -m "..."`.
- Keinen Eintrag ändern oder entfernen: die Brücke muss jede Datenbank von
  1.2.0 bis 1.8.x auf das 1.8.1-Schema bringen.
- Die Brücke ruft `create_all()` mit den AKTUELLEN Modellen auf, um fehlende
  Tabellen anzulegen. Das ist nur korrekt, solange diese Tabellen dem
  Baseline-Schema entsprechen. Installationen älter als 1.8.1 müssen deshalb
  erst auf 1.9.x aktualisiert werden, bevor sie ein späteres Release
  überspringen (siehe README, "Upgrade").
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_STATEMENTS: tuple[str, ...] = (
    # 2026-09-08 — cluster identity for CARP-aware fleet grouping.
    "ALTER TABLE opnsense_hosts ADD COLUMN IF NOT EXISTS cluster_name VARCHAR(120)",
    # 2026-09-08 — deploy lockout / auto-revert protection for rule pushes.
    "ALTER TABLE rule_deployments ADD COLUMN IF NOT EXISTS revert_deadline TIMESTAMPTZ",
    "ALTER TABLE rule_deployments ADD COLUMN IF NOT EXISTS previous_payload JSON",
    "ALTER TABLE rule_deployments ADD COLUMN IF NOT EXISTS last_payload JSON",
    # 2026-09-08 — hierarchical policy grouping, one column per rule-shaped
    # table (host + the four alias/NAT tables that can belong to a group).
    "ALTER TABLE opnsense_hosts ADD COLUMN IF NOT EXISTS policy_hierarchy_group_id UUID REFERENCES policy_hierarchy_groups(id) ON DELETE SET NULL",
    "ALTER TABLE firewall_aliases ADD COLUMN IF NOT EXISTS policy_hierarchy_group_id UUID REFERENCES policy_hierarchy_groups(id) ON DELETE SET NULL",
    "ALTER TABLE port_forward_rules ADD COLUMN IF NOT EXISTS policy_hierarchy_group_id UUID REFERENCES policy_hierarchy_groups(id) ON DELETE SET NULL",
    "ALTER TABLE one_to_one_rules ADD COLUMN IF NOT EXISTS policy_hierarchy_group_id UUID REFERENCES policy_hierarchy_groups(id) ON DELETE SET NULL",
    "ALTER TABLE outbound_nat_rules ADD COLUMN IF NOT EXISTS policy_hierarchy_group_id UUID REFERENCES policy_hierarchy_groups(id) ON DELETE SET NULL",
    # 2026-09-09 — per-session login security info shown post-login.
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(45)",
    # 2026-09-10 — role-based access control (admin / read_only). Default
    # keeps every pre-existing account (until then, necessarily a single
    # admin) as "admin" with no manual backfill needed.
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin'",
    # 2026-09-15 — CARP health / NAT drift / CARP auto-revert alerting
    # toggles on the single existing NotificationSettings row.
    "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS carp_health_enabled BOOLEAN NOT NULL DEFAULT true",
    "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS nat_drift_enabled BOOLEAN NOT NULL DEFAULT true",
    "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS carp_auto_revert_failed_enabled BOOLEAN NOT NULL DEFAULT true",
    # 2026-09-17 — per-user UI language (German remains the default).
    "ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS language VARCHAR(5) NOT NULL DEFAULT 'de'",
    # 2026-10-04 — size cap for stored firewall logs (GB, 0 = unlimited).
    # 2026-10-04 — optional TOTP second factor.
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_last_step BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS totp_recovery_hashes JSON",
    # 2026-10-04 — pinned TLS certificate per firewall.
    "ALTER TABLE opnsense_hosts ADD COLUMN IF NOT EXISTS tls_ca_pem TEXT",
    "ALTER TABLE log_settings ADD COLUMN IF NOT EXISTS max_size_gb DOUBLE PRECISION NOT NULL DEFAULT 10",
)


async def apply(conn: AsyncConnection) -> None:
    """Run every statement, in order, inside the caller's connection/
    transaction. Postgres DDL is transactional, so a failure partway
    through rolls back cleanly rather than leaving the schema half-patched.
    """
    for statement in _STATEMENTS:
        await conn.execute(text(statement))
