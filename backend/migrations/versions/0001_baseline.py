"""Baseline: das Schema von OPNexus 1.8.1.

Frische Installationen erzeugen hier alle Tabellen. Bestehende Installationen
(aus der Zeit vor Alembic) werden von app/migrate.py nach den Legacy-Patches
auf diese Revision gestempelt, statt sie auszuführen.

Revision ID: 0001
Revises: 
Create Date: 2026-10-04 21:23:24.517130
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('admin_users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('username', sa.String(length=150), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), server_default='admin', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_ip', sa.String(length=45), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('totp_last_step', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('totp_recovery_hashes', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_admin_users_username'), 'admin_users', ['username'], unique=True)
    op.create_table('audit_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('correlation_id', sa.String(length=36), nullable=False),
    sa.Column('actor', sa.String(length=150), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=True),
    sa.Column('entity_name', sa.String(length=255), nullable=True),
    sa.Column('host_id', sa.String(length=36), nullable=True),
    sa.Column('host_name', sa.String(length=255), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'], unique=False)
    op.create_index(op.f('ix_audit_events_actor'), 'audit_events', ['actor'], unique=False)
    op.create_index(op.f('ix_audit_events_correlation_id'), 'audit_events', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_audit_events_created_at'), 'audit_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_events_entity_id'), 'audit_events', ['entity_id'], unique=False)
    op.create_index(op.f('ix_audit_events_host_id'), 'audit_events', ['host_id'], unique=False)
    op.create_index(op.f('ix_audit_events_kind'), 'audit_events', ['kind'], unique=False)
    op.create_index(op.f('ix_audit_events_status'), 'audit_events', ['status'], unique=False)
    # Audit-Historie ist append-only: Trigger, den create_all() früher per
    # DDL-Event in models/audit.py angelegt hat (Autogenerate sieht ihn nicht).
    op.execute("""
    CREATE OR REPLACE FUNCTION opnexus_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Audit history is append-only'; END $$;
    """)
    op.execute("""
    CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION opnexus_audit_immutable();
    """)
    op.create_table('carp_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('host_name', sa.String(length=120), nullable=False),
    sa.Column('cluster_name', sa.String(length=120), nullable=True),
    sa.Column('from_status', sa.String(length=20), nullable=True),
    sa.Column('to_status', sa.String(length=20), nullable=True),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_carp_events_cluster_name'), 'carp_events', ['cluster_name'], unique=False)
    op.create_index(op.f('ix_carp_events_detected_at'), 'carp_events', ['detected_at'], unique=False)
    op.create_index(op.f('ix_carp_events_host_id'), 'carp_events', ['host_id'], unique=False)
    op.create_table('carp_maintenance_windows',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('host_name', sa.String(length=120), nullable=False),
    sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('auto_revert_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('message', sa.String(length=1000), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_carp_maintenance_windows_host_id'), 'carp_maintenance_windows', ['host_id'], unique=False)
    op.create_index(op.f('ix_carp_maintenance_windows_status'), 'carp_maintenance_windows', ['status'], unique=False)
    op.create_table('device_groups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('firewall_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('interface', sa.String(length=64), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('ipprotocol', sa.String(length=8), nullable=False),
    sa.Column('protocol', sa.String(length=16), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('source_net', sa.String(length=128), nullable=False),
    sa.Column('source_port', sa.String(length=32), nullable=True),
    sa.Column('destination_net', sa.String(length=128), nullable=False),
    sa.Column('destination_port', sa.String(length=32), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('host_operations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('host_name', sa.String(length=120), nullable=False),
    sa.Column('actor', sa.String(length=150), nullable=False),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('message', sa.String(length=1000), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_host_operations_host_id'), 'host_operations', ['host_id'], unique=False)
    op.create_table('ipsec_tunnels',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('local_address', sa.String(length=255), nullable=False),
    sa.Column('remote_address', sa.String(length=255), nullable=False),
    sa.Column('ike_version', sa.String(length=8), nullable=False),
    sa.Column('proposal', sa.String(length=128), nullable=False),
    sa.Column('local_identity', sa.String(length=255), nullable=False),
    sa.Column('remote_identity', sa.String(length=255), nullable=False),
    sa.Column('pre_shared_key_set', sa.Boolean(), nullable=False),
    sa.Column('local_networks', sa.String(length=255), nullable=False),
    sa.Column('remote_networks', sa.String(length=255), nullable=False),
    sa.Column('esp_proposal', sa.String(length=128), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('log_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('poll_interval_seconds', sa.Integer(), nullable=False),
    sa.Column('retention_days', sa.Integer(), nullable=False),
    sa.Column('max_size_gb', sa.Float(), server_default='10', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('notification_problems',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('problem_type', sa.String(length=32), nullable=False),
    sa.Column('dedup_key', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('context', sa.JSON(), nullable=False),
    sa.Column('first_detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('problem_type', 'dedup_key', name='uq_notification_problem_key')
    )
    op.create_table('notification_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('webhook_enabled', sa.Boolean(), nullable=False),
    sa.Column('webhook_url', sa.String(length=500), nullable=True),
    sa.Column('webhook_signing_secret_set', sa.Boolean(), nullable=False),
    sa.Column('email_enabled', sa.Boolean(), nullable=False),
    sa.Column('smtp_host', sa.String(length=255), nullable=True),
    sa.Column('smtp_port', sa.Integer(), nullable=False),
    sa.Column('smtp_security', sa.String(length=10), nullable=False),
    sa.Column('smtp_username', sa.String(length=255), nullable=True),
    sa.Column('smtp_password_set', sa.Boolean(), nullable=False),
    sa.Column('smtp_from_address', sa.String(length=255), nullable=True),
    sa.Column('smtp_to_addresses', sa.JSON(), nullable=False),
    sa.Column('host_offline_enabled', sa.Boolean(), nullable=False),
    sa.Column('cert_expiring_enabled', sa.Boolean(), nullable=False),
    sa.Column('rule_drift_enabled', sa.Boolean(), nullable=False),
    sa.Column('revert_failed_enabled', sa.Boolean(), nullable=False),
    sa.Column('maintenance_unknown_enabled', sa.Boolean(), nullable=False),
    sa.Column('carp_health_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('nat_drift_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('carp_auto_revert_failed_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('poll_interval_seconds', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('openvpn_instances',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=8), nullable=False),
    sa.Column('proto', sa.String(length=8), nullable=False),
    sa.Column('port', sa.String(length=8), nullable=False),
    sa.Column('local', sa.String(length=255), nullable=False),
    sa.Column('remote', sa.String(length=255), nullable=False),
    sa.Column('server_network', sa.String(length=255), nullable=False),
    sa.Column('cert_refs', sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    sa.Column('ca_refs', sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    sa.Column('tls_key_enabled', sa.Boolean(), nullable=False),
    sa.Column('tls_key_mode', sa.String(length=16), nullable=False),
    sa.Column('tls_key_material_set', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('policy_hierarchy_groups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.String(length=2000), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['policy_hierarchy_groups.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('restore_operations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('host_name', sa.String(length=120), nullable=False),
    sa.Column('actor', sa.String(length=150), nullable=False),
    sa.Column('backup_id', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('manifest', sa.JSON(), nullable=False),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('confirm_name', sa.String(length=120), nullable=True),
    sa.Column('handed_off_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message', sa.String(length=1000), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_restore_operations_host_id'), 'restore_operations', ['host_id'], unique=False)
    op.create_table('wireguard_tunnels',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('server_name', sa.String(length=64), nullable=False),
    sa.Column('server_public_key', sa.String(length=64), nullable=False),
    sa.Column('server_private_key_set', sa.Boolean(), nullable=False),
    sa.Column('listen_port', sa.String(length=8), nullable=False),
    sa.Column('server_tunnel_address', sa.String(length=255), nullable=False),
    sa.Column('dns', sa.String(length=255), nullable=False),
    sa.Column('mtu', sa.String(length=8), nullable=False),
    sa.Column('peer_name', sa.String(length=64), nullable=False),
    sa.Column('peer_public_key', sa.String(length=64), nullable=False),
    sa.Column('peer_psk_enabled', sa.Boolean(), nullable=False),
    sa.Column('peer_psk_set', sa.Boolean(), nullable=False),
    sa.Column('peer_allowed_ips', sa.String(length=255), nullable=False),
    sa.Column('peer_endpoint_address', sa.String(length=255), nullable=False),
    sa.Column('peer_endpoint_port', sa.String(length=8), nullable=False),
    sa.Column('peer_keepalive', sa.String(length=8), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('firewall_aliases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('policy_hierarchy_group_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['policy_hierarchy_group_id'], ['policy_hierarchy_groups.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ipsec_tunnel_groups',
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tunnel_id'], ['ipsec_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tunnel_id', 'group_id')
    )
    op.create_table('ipsec_tunnel_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['tunnel_id'], ['ipsec_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ipsec_tunnel_revisions_tunnel_id'), 'ipsec_tunnel_revisions', ['tunnel_id'], unique=False)
    op.create_table('one_to_one_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('interface', sa.String(length=64), nullable=False),
    sa.Column('source_net', sa.String(length=128), nullable=False),
    sa.Column('destination_net', sa.String(length=128), nullable=False),
    sa.Column('source_not', sa.Boolean(), nullable=False),
    sa.Column('destination_not', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('type', sa.String(length=8), nullable=False),
    sa.Column('external', sa.String(length=128), nullable=False),
    sa.Column('natreflection', sa.String(length=16), nullable=False),
    sa.Column('log', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('policy_hierarchy_group_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['policy_hierarchy_group_id'], ['policy_hierarchy_groups.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('openvpn_instance_groups',
    sa.Column('instance_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instance_id'], ['openvpn_instances.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instance_id', 'group_id')
    )
    op.create_table('openvpn_instance_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('instance_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['instance_id'], ['openvpn_instances.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_openvpn_instance_revisions_instance_id'), 'openvpn_instance_revisions', ['instance_id'], unique=False)
    op.create_table('opnsense_hosts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('base_url', sa.String(length=255), nullable=False),
    sa.Column('api_key', sa.String(length=255), nullable=False),
    sa.Column('verify_tls', sa.Boolean(), nullable=False),
    sa.Column('tls_ca_pem', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('cluster_name', sa.String(length=120), nullable=True),
    sa.Column('policy_hierarchy_group_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['policy_hierarchy_group_id'], ['policy_hierarchy_groups.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_index(op.f('ix_opnsense_hosts_cluster_name'), 'opnsense_hosts', ['cluster_name'], unique=False)
    op.create_table('outbound_nat_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('interface', sa.String(length=64), nullable=False),
    sa.Column('source_net', sa.String(length=128), nullable=False),
    sa.Column('destination_net', sa.String(length=128), nullable=False),
    sa.Column('source_not', sa.Boolean(), nullable=False),
    sa.Column('destination_not', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('ipprotocol', sa.String(length=8), nullable=False),
    sa.Column('protocol', sa.String(length=16), nullable=False),
    sa.Column('source_port', sa.String(length=32), nullable=True),
    sa.Column('destination_port', sa.String(length=32), nullable=True),
    sa.Column('target', sa.String(length=128), nullable=False),
    sa.Column('target_port', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('policy_hierarchy_group_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['policy_hierarchy_group_id'], ['policy_hierarchy_groups.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('policy_hierarchy_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('hierarchy_group_id', sa.UUID(), nullable=False),
    sa.Column('rulebase', sa.String(length=8), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('interface', sa.String(length=64), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('ipprotocol', sa.String(length=8), nullable=False),
    sa.Column('protocol', sa.String(length=16), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('source_net', sa.String(length=128), nullable=False),
    sa.Column('source_port', sa.String(length=32), nullable=True),
    sa.Column('destination_net', sa.String(length=128), nullable=False),
    sa.Column('destination_port', sa.String(length=32), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['hierarchy_group_id'], ['policy_hierarchy_groups.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('port_forward_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('interface', sa.String(length=64), nullable=False),
    sa.Column('ipprotocol', sa.String(length=8), nullable=False),
    sa.Column('protocol', sa.String(length=16), nullable=False),
    sa.Column('source_net', sa.String(length=128), nullable=False),
    sa.Column('source_port', sa.String(length=32), nullable=True),
    sa.Column('destination_net', sa.String(length=128), nullable=False),
    sa.Column('destination_port', sa.String(length=32), nullable=True),
    sa.Column('target', sa.String(length=128), nullable=False),
    sa.Column('local_port', sa.String(length=32), nullable=True),
    sa.Column('natreflection', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('policy_hierarchy_group_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['policy_hierarchy_group_id'], ['policy_hierarchy_groups.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('rule_groups',
    sa.Column('rule_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['firewall_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('rule_id', 'group_id')
    )
    op.create_table('rule_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('rule_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['rule_id'], ['firewall_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_rule_revisions_rule_id'), 'rule_revisions', ['rule_id'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sessions_token_hash'), 'sessions', ['token_hash'], unique=True)
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('user_preferences',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('theme', sa.String(length=10), nullable=False),
    sa.Column('language', sa.String(length=5), server_default='de', nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('wireguard_tunnel_groups',
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tunnel_id'], ['wireguard_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tunnel_id', 'group_id')
    )
    op.create_table('wireguard_tunnel_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['tunnel_id'], ['wireguard_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_wireguard_tunnel_revisions_tunnel_id'), 'wireguard_tunnel_revisions', ['tunnel_id'], unique=False)
    op.create_table('acme_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('ca', sa.String(length=32), nullable=False),
    sa.Column('custom_ca', sa.String(length=255), nullable=False),
    sa.Column('eab_kid', sa.String(length=255), nullable=False),
    sa.Column('eab_hmac_set', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('opnsense_uuid', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('register_status', sa.String(length=16), nullable=True),
    sa.Column('register_message', sa.String(length=2000), nullable=True),
    sa.Column('register_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_acme_accounts_host_id'), 'acme_accounts', ['host_id'], unique=False)
    op.create_table('alias_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('alias_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_alias_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revert_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('previous_payload', sa.JSON(), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['alias_id'], ['firewall_aliases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('alias_id', 'host_id', name='uq_alias_host')
    )
    op.create_table('alias_groups',
    sa.Column('alias_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['alias_id'], ['firewall_aliases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('alias_id', 'group_id')
    )
    op.create_table('alias_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('alias_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['alias_id'], ['firewall_aliases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_alias_revisions_alias_id'), 'alias_revisions', ['alias_id'], unique=False)
    op.create_table('alias_targets',
    sa.Column('alias_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['alias_id'], ['firewall_aliases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('alias_id', 'host_id')
    )
    op.create_table('device_group_members',
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('group_id', 'host_id')
    )
    op.create_table('firewall_log_entries',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=True),
    sa.Column('interface', sa.String(length=64), nullable=True),
    sa.Column('direction', sa.String(length=16), nullable=True),
    sa.Column('protocol', sa.String(length=32), nullable=True),
    sa.Column('source_net', sa.String(length=255), nullable=True),
    sa.Column('source_port', sa.String(length=16), nullable=True),
    sa.Column('destination_net', sa.String(length=255), nullable=True),
    sa.Column('destination_port', sa.String(length=16), nullable=True),
    sa.Column('label', sa.String(length=500), nullable=True),
    sa.Column('raw', sa.JSON(), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('host_id', 'digest', name='uq_firewall_log_entry_host_digest')
    )
    op.create_index(op.f('ix_firewall_log_entries_action'), 'firewall_log_entries', ['action'], unique=False)
    op.create_index(op.f('ix_firewall_log_entries_host_id'), 'firewall_log_entries', ['host_id'], unique=False)
    op.create_index('ix_firewall_log_entries_host_timestamp', 'firewall_log_entries', ['host_id', 'timestamp'], unique=False)
    op.create_index(op.f('ix_firewall_log_entries_interface'), 'firewall_log_entries', ['interface'], unique=False)
    op.create_index(op.f('ix_firewall_log_entries_timestamp'), 'firewall_log_entries', ['timestamp'], unique=False)
    op.create_table('host_log_cursor',
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('last_digest', sa.String(length=64), nullable=True),
    sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('host_id')
    )
    op.create_table('host_resource_samples',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('sampled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('mem_used_pct', sa.Float(), nullable=True),
    sa.Column('load_avg_1m', sa.Float(), nullable=True),
    sa.Column('disk_used_pct', sa.Float(), nullable=True),
    sa.Column('raw', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_host_resource_samples_host_id'), 'host_resource_samples', ['host_id'], unique=False)
    op.create_index('ix_host_resource_samples_host_sampled_at', 'host_resource_samples', ['host_id', 'sampled_at'], unique=False)
    op.create_index(op.f('ix_host_resource_samples_sampled_at'), 'host_resource_samples', ['sampled_at'], unique=False)
    op.create_table('ipsec_tunnel_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_connection_uuid', sa.String(length=64), nullable=True),
    sa.Column('opnsense_local_uuid', sa.String(length=64), nullable=True),
    sa.Column('opnsense_remote_uuid', sa.String(length=64), nullable=True),
    sa.Column('opnsense_child_uuid', sa.String(length=64), nullable=True),
    sa.Column('opnsense_psk_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tunnel_id'], ['ipsec_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tunnel_id', 'host_id', name='uq_ipsec_tunnel_host')
    )
    op.create_table('ipsec_tunnel_targets',
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tunnel_id'], ['ipsec_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tunnel_id', 'host_id')
    )
    op.create_table('managed_certificates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('commonname', sa.String(length=255), nullable=False),
    sa.Column('country', sa.String(length=8), nullable=False),
    sa.Column('state', sa.String(length=255), nullable=False),
    sa.Column('city', sa.String(length=255), nullable=False),
    sa.Column('organization', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('key_type', sa.String(length=16), nullable=False),
    sa.Column('digest', sa.String(length=16), nullable=False),
    sa.Column('cert_type', sa.String(length=32), nullable=False),
    sa.Column('lifetime', sa.Integer(), nullable=False),
    sa.Column('caref', sa.String(length=64), nullable=True),
    sa.Column('csr_payload', sa.Text(), nullable=True),
    sa.Column('crt_payload', sa.Text(), nullable=True),
    sa.Column('private_key_set', sa.Boolean(), nullable=False),
    sa.Column('opnsense_uuid', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_managed_certificates_host_id'), 'managed_certificates', ['host_id'], unique=False)
    op.create_table('one_to_one_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('one_to_one_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_one_to_one_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revert_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('previous_payload', sa.JSON(), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['one_to_one_id'], ['one_to_one_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('one_to_one_id', 'host_id', name='uq_one_to_one_host')
    )
    op.create_table('one_to_one_groups',
    sa.Column('one_to_one_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['one_to_one_id'], ['one_to_one_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('one_to_one_id', 'group_id')
    )
    op.create_table('one_to_one_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('one_to_one_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['one_to_one_id'], ['one_to_one_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_one_to_one_revisions_one_to_one_id'), 'one_to_one_revisions', ['one_to_one_id'], unique=False)
    op.create_table('one_to_one_targets',
    sa.Column('one_to_one_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['one_to_one_id'], ['one_to_one_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('one_to_one_id', 'host_id')
    )
    op.create_table('openvpn_instance_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('instance_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_statickey_uuid', sa.String(length=64), nullable=True),
    sa.Column('opnsense_instance_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instance_id'], ['openvpn_instances.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('instance_id', 'host_id', name='uq_openvpn_instance_host')
    )
    op.create_table('openvpn_instance_targets',
    sa.Column('instance_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instance_id'], ['openvpn_instances.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instance_id', 'host_id')
    )
    op.create_table('outbound_nat_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('outbound_nat_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_outbound_nat_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revert_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('previous_payload', sa.JSON(), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['outbound_nat_id'], ['outbound_nat_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('outbound_nat_id', 'host_id', name='uq_outbound_nat_host')
    )
    op.create_table('outbound_nat_groups',
    sa.Column('outbound_nat_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['outbound_nat_id'], ['outbound_nat_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('outbound_nat_id', 'group_id')
    )
    op.create_table('outbound_nat_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('outbound_nat_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['outbound_nat_id'], ['outbound_nat_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outbound_nat_revisions_outbound_nat_id'), 'outbound_nat_revisions', ['outbound_nat_id'], unique=False)
    op.create_table('outbound_nat_targets',
    sa.Column('outbound_nat_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['outbound_nat_id'], ['outbound_nat_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('outbound_nat_id', 'host_id')
    )
    op.create_table('policy_hierarchy_rule_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('policy_hierarchy_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_policy_hierarchy_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revert_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('previous_payload', sa.JSON(), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['policy_hierarchy_id'], ['policy_hierarchy_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('policy_hierarchy_id', 'host_id', name='uq_policy_hierarchy_host')
    )
    op.create_table('policy_hierarchy_rule_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('policy_hierarchy_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['policy_hierarchy_id'], ['policy_hierarchy_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_policy_hierarchy_rule_revisions_policy_hierarchy_id'), 'policy_hierarchy_rule_revisions', ['policy_hierarchy_id'], unique=False)
    op.create_table('port_forward_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('port_forward_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_port_forward_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revert_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('previous_payload', sa.JSON(), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['port_forward_id'], ['port_forward_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('port_forward_id', 'host_id', name='uq_port_forward_host')
    )
    op.create_table('port_forward_groups',
    sa.Column('port_forward_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['port_forward_id'], ['port_forward_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('port_forward_id', 'group_id')
    )
    op.create_table('port_forward_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('port_forward_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['port_forward_id'], ['port_forward_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_port_forward_revisions_port_forward_id'), 'port_forward_revisions', ['port_forward_id'], unique=False)
    op.create_table('port_forward_targets',
    sa.Column('port_forward_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['port_forward_id'], ['port_forward_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('port_forward_id', 'host_id')
    )
    op.create_table('rule_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('rule_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_rule_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revert_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('previous_payload', sa.JSON(), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['firewall_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('rule_id', 'host_id', name='uq_rule_host')
    )
    op.create_table('rule_targets',
    sa.Column('rule_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['firewall_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('rule_id', 'host_id')
    )
    op.create_table('scheduled_maintenance',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('host_name', sa.String(length=120), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('actor', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('host_operation_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scheduled_maintenance_host_id'), 'scheduled_maintenance', ['host_id'], unique=False)
    op.create_index('ix_scheduled_maintenance_status_run_at', 'scheduled_maintenance', ['status', 'run_at'], unique=False)
    op.create_table('wireguard_tunnel_deployments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('opnsense_server_uuid', sa.String(length=64), nullable=True),
    sa.Column('opnsense_peer_uuid', sa.String(length=64), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tunnel_id'], ['wireguard_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tunnel_id', 'host_id', name='uq_wireguard_tunnel_host')
    )
    op.create_table('wireguard_tunnel_targets',
    sa.Column('tunnel_id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tunnel_id'], ['wireguard_tunnels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tunnel_id', 'host_id')
    )
    op.create_table('acme_certificate_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('host_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('alt_names', sa.String(length=2000), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('validation_ref', sa.String(length=64), nullable=False),
    sa.Column('key_length', sa.String(length=16), nullable=False),
    sa.Column('ocsp', sa.Boolean(), nullable=False),
    sa.Column('auto_renewal', sa.Boolean(), nullable=False),
    sa.Column('renew_interval', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('opnsense_uuid', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('last_payload', sa.JSON(), nullable=True),
    sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sign_status', sa.String(length=16), nullable=True),
    sa.Column('sign_message', sa.String(length=2000), nullable=True),
    sa.Column('sign_triggered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('remote_status_code', sa.Integer(), nullable=True),
    sa.Column('remote_status_last_update', sa.DateTime(timezone=True), nullable=True),
    sa.Column('remote_last_update', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['acme_accounts.id'], ),
    sa.ForeignKeyConstraint(['host_id'], ['opnsense_hosts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_acme_certificate_requests_host_id'), 'acme_certificate_requests', ['host_id'], unique=False)


def downgrade() -> None:
    raise NotImplementedError(
        "Die Baseline (1.8.1-Schema) kann nicht zurückgerollt werden — "
        "dafür das Pre-Upgrade-Backup einspielen."
    )
