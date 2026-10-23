"""Rollout plans: CRUD, wave resolution, preview token, activation, roles."""
import uuid

import pytest

from tests.test_maintenance_windows import _WEEKLY
from tests.test_rules import _create_host


async def _fleet(client):
    """canary: c1 · pilot: p1 + ha-b (BACKUP) · prod: ha-a; ha-a/ha-b form cluster "ha"."""
    hosts = {}
    for name in ("c1", "p1", "ha-a", "ha-b"):
        hosts[name] = await _create_host(client, name, f"https://{name}.test")
    for name in ("ha-a", "ha-b"):
        await client.patch(f"/api/hosts/{hosts[name]['id']}", json={"cluster_name": "ha"})
    groups = {}
    for gname, members in (("ring-canary", ["c1"]), ("ring-pilot", ["p1", "ha-b"]), ("ring-prod", ["ha-a", "c1"])):
        resp = await client.post("/api/groups", json={"name": gname, "host_ids": [hosts[m]["id"] for m in members]})
        assert resp.status_code == 201, resp.text
        groups[gname] = resp.json()["id"]
    return hosts, groups


def _plan_body(groups, **kw):
    body = {"name": "Ringe", "action": "update", "waves": [
        {"label": "canary", "group_ids": [groups["ring-canary"]], "soak_minutes": 10},
        {"label": "pilot", "group_ids": [groups["ring-pilot"]], "soak_minutes": 10},
        {"label": "prod", "group_ids": [groups["ring-prod"]], "soak_minutes": 0, "carp_handover": True, "require_approval": True},
    ]}
    body.update(kw)
    return body


@pytest.mark.asyncio
async def test_plan_crud_and_preview_resolves_rings(client):
    _hosts, groups = await _fleet(client)
    resp = await client.post("/api/rollouts/plans", json=_plan_body(groups))
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert [w["label"] for w in plan["waves"]] == ["canary", "pilot", "prod"] and plan["host_count"] == 4
    prev = await client.post(f"/api/rollouts/plans/{plan['id']}/preview")
    assert prev.status_code == 200
    body = prev.json()
    assert [[h["name"] for h in w["hosts"]] for w in body["waves"]] == [["c1"], ["ha-b", "p1"], ["ha-a"]]  # c1 only once
    codes = {w["code"] for w in body["warnings"]}
    assert {"duplicate_hosts", "mixed_versions", "master_needs_handover", "no_window"} <= codes
    assert body["errors"] == [] and body["can_start"] and len(body["preview_token"]) == 64
    assert (await client.post("/api/rollouts/plans", json=_plan_body(groups))).json()["detail"]["code"] == "ROLLOUT_NAME_TAKEN"
    assert (await client.delete(f"/api/groups/{groups['ring-canary']}")).json()["detail"]["code"] == "ROLLOUT_GROUP_IN_USE"
    assert (await client.delete(f"/api/rollouts/plans/{plan['id']}")).status_code == 204
    assert (await client.get(f"/api/rollouts/plans/{plan['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_major_update_must_keep_a_cluster_in_one_wave(client):
    _hosts, groups = await _fleet(client)
    plan = (await client.post("/api/rollouts/plans", json=_plan_body(groups, action="major_update"))).json()
    assert {e["code"] for e in plan["errors"]} == {"cluster_split"}
    prev = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()
    assert prev["can_start"] is False


@pytest.mark.asyncio
async def test_activation_needs_auto_start_a_window_and_the_current_token(client):
    _hosts, groups = await _fleet(client)
    window = (await client.post("/api/maintenance-windows", json=_WEEKLY)).json()
    plan = (await client.post("/api/rollouts/plans", json=_plan_body(groups))).json()
    token = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    resp = await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})
    assert resp.status_code == 422 and resp.json()["detail"]["params"]["field"] == "auto_start"

    plan = (await client.put(f"/api/rollouts/plans/{plan['id']}", json=_plan_body(groups, auto_start=True, window_id=window["id"]))).json()
    stale = await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "ROLLOUT_PREVIEW_STALE"
    token = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    active = await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})
    assert active.status_code == 200 and active.json()["active"] and active.json()["fingerprint_ok"]
    assert active.json()["activated_by"] == "admin"

    # A group change behind the plan's back: still "active", but the fingerprint no longer matches.
    await client.put(f"/api/groups/{groups['ring-canary']}", json={"name": "ring-canary", "host_ids": []})
    assert (await client.get(f"/api/rollouts/plans/{plan['id']}")).json()["fingerprint_ok"] is False
    # A window that a plan uses cannot be deleted (the plan would silently lose its time limit).
    assert (await client.delete(f"/api/maintenance-windows/{window['id']}")).json()["detail"]["code"] == "WINDOW_IN_USE"

    # Any edit disarms it.
    token = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})
    edited = await client.put(f"/api/rollouts/plans/{plan['id']}", json=_plan_body(groups, auto_start=True, window_id=window["id"], name="Ringe 2"))
    assert edited.json()["active"] is False
    off = await client.post(f"/api/rollouts/plans/{plan['id']}/deactivate")
    assert off.status_code == 200 and off.json()["active"] is False


@pytest.mark.asyncio
async def test_auto_start_only_for_minor_updates_with_a_window(client):
    _hosts, groups = await _fleet(client)
    plan = (await client.post("/api/rollouts/plans", json=_plan_body(groups, auto_start=True))).json()
    assert "auto_start_needs_window" in {e["code"] for e in plan["errors"]}
    token = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    resp = await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "ROLLOUT_PLAN_INVALID"


@pytest.mark.parametrize("change,field", [
    ({"gate_checks": {"nonsense": True}}, "gate_checks"),
    ({"window_id": "00000000-0000-0000-0000-000000000000"}, "window_id"),
])
@pytest.mark.asyncio
async def test_plan_validation(client, change, field):
    _hosts, groups = await _fleet(client)
    resp = await client.post("/api/rollouts/plans", json=_plan_body(groups, **change))
    assert resp.status_code == 422 and resp.json()["detail"]["params"]["field"] == field
    bad_group = _plan_body(groups)
    bad_group["waves"][0]["group_ids"] = ["00000000-0000-0000-0000-000000000000"]
    assert (await client.post("/api/rollouts/plans", json=bad_group)).json()["detail"]["params"]["field"] == "group_ids"
    too_many = _plan_body(groups, waves=[{"label": str(i), "group_ids": [groups["ring-canary"]]} for i in range(11)])
    assert (await client.post("/api/rollouts/plans", json=too_many)).status_code == 422


@pytest.mark.asyncio
async def test_role_matrix_for_plans(client):
    from tests.test_operator_role import _operator
    _hosts, groups = await _fleet(client)
    window = (await client.post("/api/maintenance-windows", json=_WEEKLY)).json()
    op = await _operator(client, "rollop")
    try:
        made = await op.post("/api/rollouts/plans", json=_plan_body(groups, auto_start=True, window_id=window["id"]))
        assert made.status_code == 201  # preparing is fine
        pid = made.json()["id"]
        token = (await op.post(f"/api/rollouts/plans/{pid}/preview")).json()["preview_token"]
        await client.put("/api/settings/governance", json={"operators_may_deploy": False})
        denied = await op.post(f"/api/rollouts/plans/{pid}/activate", json={"preview_token": token})
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "DEPLOY_NEEDS_ADMIN"
        assert (await op.post(f"/api/rollouts/plans/{pid}/deactivate")).status_code == 200  # braking is always allowed
        assert (await client.post(f"/api/rollouts/plans/{pid}/activate", json={"preview_token": token})).status_code == 200
    finally:
        await op.aclose()
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})
    from tests.test_api_tokens import _login
    assert (await client.post("/api/users", json={"username": "rollro", "password": "readonly-pass-1", "role": "read_only"})).status_code == 201
    ro = await _login("rollro", "readonly-pass-1")
    try:
        assert (await ro.get("/api/rollouts/plans")).status_code == 200
        for path in ("/api/rollouts/plans", f"/api/rollouts/plans/{pid}/deactivate"):
            assert (await ro.post(path, json={})).json()["detail"]["code"] == "READ_ONLY_ACCESS"
    finally:
        await ro.aclose()


@pytest.mark.asyncio
async def test_gate_probes_are_validated_stored_and_frozen_into_the_fingerprint(client):
    """1.32 (B5): probes per plan; a plan without probes keeps the fingerprint of earlier versions."""
    from app.database import SessionLocal
    from app.models.rollout import RolloutPlan
    from app.services import rollout_service

    _hosts, groups = await _fleet(client)
    plain = (await client.post("/api/rollouts/plans", json=_plan_body(groups))).json()
    assert plain["gate_probes"] == []
    async with SessionLocal() as db:
        assert "gate_probes" not in rollout_service.definition(await db.get(RolloutPlan, uuid.UUID(plain["id"])))
    probes = [{"type": "ping", "target": "1.1.1.1"}, {"type": "tcp", "target": "intranet.example", "port": 443},
              {"type": "ping", "target": "1.1.1.1"}]
    resp = await client.put(f"/api/rollouts/plans/{plain['id']}", json=_plan_body(groups, gate_probes=probes))
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_probes"] == [{"type": "ping", "target": "1.1.1.1", "port": None},
                                          {"type": "tcp", "target": "intranet.example", "port": 443}]
    for bad in ([{"type": "tcp", "target": "x"}], [{"type": "ping", "target": "x", "port": 1}],
                [{"type": "ping", "target": "-rf"}], [{"type": "ping", "target": "a b"}], [{"type": "udp", "target": "x"}],
                [{"type": "ping", "target": f"h{i}"} for i in range(11)]):
        assert (await client.put(f"/api/rollouts/plans/{plain['id']}", json=_plan_body(groups, gate_probes=bad))).status_code == 422, bad
