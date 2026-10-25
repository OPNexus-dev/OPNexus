"""HA: Zustandstabelle ha_state und alle Einstellungs-Singletons als echte Zeilen (docs/HA-H2-DESIGN.md 3.6).

Bisher legte jeder Dienst seine Zeile beim ersten Lesen an (INSERT). An einer Streaming-Replica (HA-Standby) ist die
Datenbank schreibgeschützt: der INSERT scheitert, und schon die Anmeldung stolperte darüber. Die Zeilen stehen jetzt
nach der Migration da und kommen per Replikation zum Standby. Die Werte sind die Standardwerte der Modelle; ein Test
(tests/test_singleton.py) vergleicht sie mit denen, damit sie nicht auseinanderlaufen.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-25
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

# table -> "column list" VALUES (...): every NOT NULL column, spelled out (a table bridged from a pre-Alembic install
# may lack server defaults). ON CONFLICT keeps rows an admin has already changed.
SINGLETONS = {
    "governance_settings": (
        "id, operators_may_deploy, cleanup_max_rules, cleanup_delete_min_days, cleanup_management_sources, cleanup_management_ports",
        "1, true, 10, 7, '[]'::json, '[]'::json"),
    "audit_export_settings": (
        "id, syslog_enabled, syslog_port, syslog_protocol, webhook_enabled, webhook_signing_secret_set",
        "1, false, 514, 'udp', false, false"),
    "oidc_settings": (
        "id, enabled, display_name, client_secret_set, scopes, groups_claim",
        "1, false, 'SSO', false, 'openid profile email', 'groups'"),
    "log_settings": (
        "id, enabled, poll_interval_seconds, retention_days, max_size_gb, updated_at",
        "1, true, 30, 7, 10, now()"),
    "gui_access_settings": (
        "id, enabled, idle_timeout_minutes, absolute_timeout_minutes, slot_count",
        "1, false, 15, 60, 4"),
    "overlay_settings": (
        "id, enabled, subnet, listen_port",
        "1, false, '10.99.0.0/24', 51820"),
    "update_check_settings": (
        "id, enabled, feed_url",
        "1, false, 'https://updates.opnexus.dev/latest.json'"),
    "backup_settings": (
        "id, scheduled_enabled, interval_hours, keep_count, offsite_enabled, s3_region, s3_prefix, s3_path_style, "
        "s3_secret_set, passphrase_set",
        "1, true, 24, 30, false, 'us-east-1', 'opnexus', true, false, false"),
    "notification_settings": (
        "id, webhook_enabled, webhook_signing_secret_set, email_enabled, smtp_port, smtp_security, smtp_password_set, "
        "smtp_to_addresses, host_offline_enabled, cert_expiring_enabled, rule_drift_enabled, revert_failed_enabled, "
        "maintenance_unknown_enabled, carp_health_enabled, nat_drift_enabled, network_drift_enabled, "
        "carp_auto_revert_failed_enabled, scheduled_maintenance_failed_enabled, backup_failed_enabled, "
        "compliance_alerts_enabled, gateway_down_enabled, resource_threshold_enabled, service_down_enabled, "
        "api_rights_enabled, gateway_alert_muted, rollout_problems_enabled, gateway_loss_threshold_pct, "
        "gateway_delay_threshold_ms, server_disk_enabled, server_disk_threshold_pct, disk_threshold_pct, "
        "memory_threshold_pct, swap_threshold_pct, load_per_cpu_threshold, alert_confirm_samples, "
        "resource_confirm_samples, service_watchlist, poll_interval_seconds, updated_at",
        "1, false, false, false, 587, 'starttls', false, '[]'::json, true, true, true, true, true, true, true, true, "
        "true, true, true, false, true, false, false, true, '[]'::json, true, 20, 500, true, 90, 90, 95, 50, 2.0, 3, 5, "
        "'[\"unbound\", \"dnsmasq\", \"dhcpd\", \"dhcpd6\", \"kea-dhcp4\", \"kea-dhcp6\", \"ntpd\", \"openvpn\", "
        "\"wireguard\", \"strongswan\"]'::json, 300, now()"),
}


def insert_singletons(execute=None) -> None:
    run = execute or op.execute
    for table, (columns, values) in SINGLETONS.items():
        run(f"INSERT INTO {table} ({columns}) VALUES ({values}) ON CONFLICT (id) DO NOTHING")


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS ha_state ("
        " id integer PRIMARY KEY,"
        " epoch integer NOT NULL DEFAULT 0,"
        " primary_node varchar(32),"
        " promoted_at timestamptz,"
        " updated_at timestamptz NOT NULL DEFAULT now())"
    )
    op.execute("INSERT INTO ha_state (id, epoch) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    insert_singletons()


def downgrade() -> None:
    # The settings rows stay: they are what the code would create on first read anyway.
    op.execute("DROP TABLE IF EXISTS ha_state")
