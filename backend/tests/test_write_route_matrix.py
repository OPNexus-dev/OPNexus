"""Every write route is either a rollout (blocked for operators when deploying is admin-only), admin-only, or
EXPLICITLY listed here as something an operator may do. A new write route that is in none of them fails this test,
so it cannot slip through the regex-based rights (audit 2026-10-23, AUD-05)."""
import re

from app.main import app
from app.services.api_token_service import is_rollout, operator_denies

OPERATOR_ALLOWED_WRITES = {
    # Drafts: creating and editing objects, which only becomes a change on a firewall by /deploy or /push
    ("POST", "/api/acme/accounts"),
    ("PUT", "/api/acme/accounts/x"),
    ("POST", "/api/acme/certificates"),
    ("PUT", "/api/acme/certificates/x"),
    ("POST", "/api/aliases"),
    ("PUT", "/api/aliases/x"),
    ("POST", "/api/certificates/managed"),
    ("PUT", "/api/certificates/managed/x"),
    ("POST", "/api/groups"),
    ("PUT", "/api/groups/x"),
    ("PUT", "/api/hosts/x/variables"),
    ("POST", "/api/ipsec/tunnels"),
    ("PUT", "/api/ipsec/tunnels/x"),
    ("POST", "/api/one-to-one"),
    ("PUT", "/api/one-to-one/x"),
    ("POST", "/api/openvpn/instances"),
    ("PUT", "/api/openvpn/instances/x"),
    ("POST", "/api/outbound-nat"),
    ("PUT", "/api/outbound-nat/x"),
    ("POST", "/api/policy-hierarchy/groups"),
    ("PUT", "/api/policy-hierarchy/groups/x"),
    ("POST", "/api/policy-hierarchy/rules"),
    ("PUT", "/api/policy-hierarchy/rules/x"),
    ("POST", "/api/port-forward"),
    ("PUT", "/api/port-forward/x"),
    ("POST", "/api/rollouts/plans"),
    ("PUT", "/api/rollouts/plans/x"),
    ("POST", "/api/rules"),
    ("PUT", "/api/rules/x"),
    ("POST", "/api/wireguard/tunnels"),
    ("PUT", "/api/wireguard/tunnels/x"),
    # Adopting hand-made firewall objects: database only, nothing is written to a firewall
    ("POST", "/api/aliases/adopt"),
    ("POST", "/api/aliases/adopt-all"),
    ("POST", "/api/one-to-one/adopt"),
    ("POST", "/api/one-to-one/adopt-all"),
    ("POST", "/api/outbound-nat/adopt"),
    ("POST", "/api/outbound-nat/adopt-all"),
    ("POST", "/api/port-forward/adopt"),
    ("POST", "/api/port-forward/adopt-all"),
    ("POST", "/api/rules/adopt"),
    # Confirming or cancelling an open operation ("I still have access", cancel a preview, acknowledge)
    ("POST", "/api/aliases/x/confirm"),
    ("POST", "/api/hosts/x/maintenance/x/acknowledge"),
    ("POST", "/api/hosts/x/maintenance/x/cancel"),
    ("POST", "/api/hosts/x/restore/x/cancel"),
    ("POST", "/api/hosts/x/restore/x/confirm"),
    ("POST", "/api/hosts/x/restore/x/handoff"),
    ("POST", "/api/hosts/x/rule-cleanup/x/cancel"),
    ("POST", "/api/hosts/x/rule-cleanup/x/confirm"),
    ("POST", "/api/one-to-one/x/confirm"),
    ("POST", "/api/outbound-nat/x/confirm"),
    ("POST", "/api/policy-hierarchy/rules/x/confirm"),
    ("POST", "/api/port-forward/x/confirm"),
    ("POST", "/api/rules/x/confirm"),
    # Own account, session and theme (self-service for every role)
    ("POST", "/api/auth/2fa/disable"),
    ("POST", "/api/auth/2fa/enable"),
    ("POST", "/api/auth/2fa/setup"),
    ("POST", "/api/auth/change-password"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("PUT", "/api/settings"),
    ("POST", "/api/tokens"),
    ("DELETE", "/api/tokens/x"),
    # Backups: reading a configuration into OPNexus' own storage
    ("POST", "/api/backups/run"),
    ("POST", "/api/hosts/x/backups"),
    # Read-only POSTs: previews, checks and diagnostics that change nothing on a firewall
    ("POST", "/api/compliance/check"),
    ("POST", "/api/config/import/plan"),
    ("POST", "/api/groups/x/preview"),
    ("POST", "/api/hosts/x/compliance/check"),
    ("POST", "/api/hosts/x/diagnostics/dns"),
    ("POST", "/api/hosts/x/diagnostics/ping"),
    ("POST", "/api/hosts/x/diagnostics/portprobe"),
    ("POST", "/api/hosts/x/diagnostics/traceroute"),
    ("POST", "/api/hosts/x/maintenance/carp-preview"),
    ("POST", "/api/hosts/x/maintenance/check-updates"),
    ("POST", "/api/hosts/x/maintenance/preview"),
    ("POST", "/api/hosts/x/network/changes/preview"),
    ("POST", "/api/hosts/x/restore/preview"),
    ("POST", "/api/hosts/x/rule-cleanup/preview"),
    ("POST", "/api/mcp"),
    ("POST", "/api/rollouts/plans/x/preview"),
    # Deleting a draft-only object that is never deployed itself (groups, hierarchy groups, rollout plans)
    ("DELETE", "/api/groups/x"),
    ("DELETE", "/api/policy-hierarchy/groups/x"),
    ("DELETE", "/api/rollouts/plans/x"),
    # Reordering inside the policy hierarchy: a draft until deployed
    ("PUT", "/api/policy-hierarchy/groups/x/rules/x/order"),
    ("POST", "/api/policy-hierarchy/rules/x/move"),
    # Pausing a firmware run and switching a plan off: the safe direction, deliberately not a rollout
    ("POST", "/api/rollouts/plans/x/deactivate"),
    ("POST", "/api/rollouts/runs/x/pause")
}


def _write_routes():
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            method = method.upper()
            if method not in ("GET", "HEAD", "OPTIONS"):
                yield method, re.sub(r"\{[^}]+\}", "x", path)


def test_every_write_route_has_an_explicit_operator_decision():
    unclassified = sorted(
        (m, p) for m, p in _write_routes()
        if not is_rollout(m, p) and not operator_denies(m, p) and (m, p) not in OPERATOR_ALLOWED_WRITES
    )
    assert not unclassified, (
        "New write routes without a rights decision. Add them to _ROLLOUT_POST/_ROLLOUT_DELETE (changes the fleet), "
        "_ADMIN_ONLY_WRITES (changes access/destinations) or, if an operator may do it, to OPERATOR_ALLOWED_WRITES: "
        + str(unclassified)
    )


def test_the_allow_list_stays_honest():
    existing = set(_write_routes())
    stale = sorted(OPERATOR_ALLOWED_WRITES - existing)
    assert not stale, f"routes that no longer exist: {stale}"
    now_covered = sorted((m, p) for m, p in OPERATOR_ALLOWED_WRITES if is_rollout(m, p) or operator_denies(m, p))
    assert not now_covered, f"already covered by the rights rules, remove from the allow-list: {now_covered}"
