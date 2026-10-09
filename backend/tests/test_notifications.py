import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.host_operation import HostOperation
from app.models.notification import NotificationProblem, NotificationSettings
from app.models.rule_deployment import RuleDeployment
from app.services import notification_checks, notification_dedup, notification_delivery, notification_secret_store
from app.services.notification_service import run_notification_checks


def _now():
    return datetime.now(timezone.utc)


async def _create_host(client, name: str, base_url: str, cluster_name: str | None = None) -> dict:
    payload = {"name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret", "verify_tls": False}
    if cluster_name:
        payload["cluster_name"] = cluster_name
    resp = await client.post("/api/hosts", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _live_rule_row(uuid_: str, description: str) -> dict:
    return {
        "uuid": uuid_, "description": description, "action": "pass", "%action": "Pass",
        "direction": "in", "%direction": "in", "interface": "lan", "%interface": "LAN",
        "protocol": "tcp", "%protocol": "TCP", "source_net": "any", "source_port": "",
        "destination_net": "lan", "destination_port": "22", "enabled": "1", "is_automatic": "",
    }


# ── Dedup ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_opens_once_then_skips_then_resolves():
    async with SessionLocal() as db:
        opened, resolved = await notification_dedup.diff_and_update(db, "host_offline", {"h1": {"host_name": "fw1"}})
        await db.commit()
        assert len(opened) == 1
        assert len(resolved) == 0

        opened, resolved = await notification_dedup.diff_and_update(db, "host_offline", {"h1": {"host_name": "fw1"}})
        await db.commit()
        assert len(opened) == 0
        assert len(resolved) == 0

        opened, resolved = await notification_dedup.diff_and_update(db, "host_offline", {})
        await db.commit()
        assert len(opened) == 0
        assert len(resolved) == 1
        assert resolved[0].status == "resolved"


@pytest.mark.asyncio
async def test_resolved_problem_that_comes_back_reopens_instead_of_violating_unique_key():
    async with SessionLocal() as db:
        await notification_dedup.diff_and_update(db, "revert_failed", {"k": {"n": 1}})
        await db.commit()
        await notification_dedup.diff_and_update(db, "revert_failed", {})
        await db.commit()
        opened, resolved = await notification_dedup.diff_and_update(db, "revert_failed", {"k": {"n": 2}})
        await db.commit()  # used to raise IntegrityError (uq_notification_problem_key)
        assert len(opened) == 1 and opened[0].status == "open" and opened[0].resolved_at is None
        assert opened[0].context == {"n": 2} and opened[0].last_notified_at is None
        rows = (await db.scalars(select(NotificationProblem).where(NotificationProblem.problem_type == "revert_failed"))).all()
        assert len(rows) == 1


# ── Fingerprint regression ──────────────────────────────────────────────

def test_fingerprint_matches_frontend_field_order():
    from app.schemas.host import LiveRule
    from app.services.rule_drift_service import fingerprint

    rule = LiveRule(
        uuid="r1", description="Allow SSH", action="pass", action_label="Pass",
        direction="in", direction_label="in", interface="lan", interface_label="LAN",
        protocol="tcp", protocol_label="TCP", source_net="any", source_port=None,
        destination_net="lan", destination_port="22", enabled=True, is_automatic=False,
    )
    assert fingerprint(rule) == "Allow SSH|lan|in|tcp|any||lan|22|pass"


# ── check_host_offline ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_host_offline(client, respx_mock):
    respx_mock.get("https://fw-up.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )
    respx_mock.get("https://fw-down.test/api/diagnostics/interface/getVipStatus").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    await _create_host(client, "fw-up", "https://fw-up.test")
    down = await _create_host(client, "fw-down", "https://fw-down.test")

    async with SessionLocal() as db:
        problems = await notification_checks.check_host_offline(db)

    assert list(problems.keys()) == [down["id"]]
    assert problems[down["id"]]["host_name"] == "fw-down"


# ── check_certificates ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_certificates_only_reports_expiring_or_expired(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    now = int(_now().timestamp())
    respx_mock.get("https://fw1.test/api/trust/cert/search").mock(
        return_value=httpx.Response(200, json={"rows": [
            {"uuid": "expired", "descr": "old", "commonname": "old.local", "%caref": "",
             "valid_from": str(now - 400 * 86400), "valid_to": str(now - 5 * 86400), "in_use": "1"},
            {"uuid": "valid", "descr": "fine", "commonname": "fine.local", "%caref": "",
             "valid_from": str(now - 10 * 86400), "valid_to": str(now + 400 * 86400), "in_use": "1"},
        ]})
    )

    async with SessionLocal() as db:
        problems = await notification_checks.check_certificates(db)

    assert len(problems) == 1
    key = f"{host['id']}:expired"
    assert key in problems
    assert problems[key]["status"] == "expired"


# ── check_rule_drift ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_rule_drift_reports_missing_rule(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _create_host(client, "fw-a", "https://fw-a.test", cluster_name="ha")
    await _create_host(client, "fw-b", "https://fw-b.test", cluster_name="ha")

    respx_mock.get("https://fw-a.test/api/firewall/filter/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [
            _live_rule_row("r1", "Allow SSH"), _live_rule_row("r2", "Allow HTTPS"),
        ]})
    )
    respx_mock.get("https://fw-b.test/api/firewall/filter/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_live_rule_row("r1", "Allow SSH")]})
    )

    async with SessionLocal() as db:
        problems = await notification_checks.check_rule_drift(db)

    assert len(problems) == 1
    row = next(iter(problems.values()))
    assert row["description"] == "Allow HTTPS"
    assert row["present_on"] == ["fw-a"]
    assert row["missing_from"] == ["fw-b"]


@pytest.mark.asyncio
async def test_check_rule_drift_no_drift_when_identical(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _create_host(client, "fw-a", "https://fw-a.test", cluster_name="ha")
    await _create_host(client, "fw-b", "https://fw-b.test", cluster_name="ha")

    same = {"rows": [_live_rule_row("r1", "Allow SSH")]}
    respx_mock.get("https://fw-a.test/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json=same))
    respx_mock.get("https://fw-b.test/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json=same))

    async with SessionLocal() as db:
        problems = await notification_checks.check_rule_drift(db)

    assert problems == {}


@pytest.mark.asyncio
async def test_drift_stays_open_while_a_cluster_member_is_unreachable(client, respx_mock):
    """Live 26.7.5: rebooting one firewall made every open drift problem
    resolve (only one host reachable => nothing to compare) and re-open
    afterwards, sending a resolved+opened pair per problem."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _create_host(client, "fw-a", "https://fw-a.test", cluster_name="ha")
    await _create_host(client, "fw-b", "https://fw-b.test", cluster_name="ha")
    respx_mock.get("https://fw-a.test/api/firewall/filter/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_live_rule_row("r1", "Allow SSH"), _live_rule_row("r2", "Only A")]})
    )
    b = respx_mock.get("https://fw-b.test/api/firewall/filter/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_live_rule_row("r1", "Allow SSH")]})
    )

    async with SessionLocal() as db:
        current = await notification_checks.check_rule_drift(db)
        opened, resolved = await notification_dedup.diff_and_update(db, "rule_drift", current)
        await db.commit()
        assert len(opened) == 1 and not resolved

        b.mock(side_effect=httpx.ConnectError("down"))  # fw-b reboots
        current = await notification_checks.check_rule_drift(db)
        assert dict(current) == {}
        opened, resolved = await notification_dedup.diff_and_update(db, "rule_drift", current)
        await db.commit()
        assert not opened and not resolved  # still open, nothing sent

        b.mock(return_value=httpx.Response(200, json={"rows": [_live_rule_row("r1", "Allow SSH")]}))
        current = await notification_checks.check_rule_drift(db)
        opened, resolved = await notification_dedup.diff_and_update(db, "rule_drift", current)
        await db.commit()
        assert not opened and not resolved  # same problem, no duplicate "opened"


# ── check_revert_failed / check_maintenance_unknown ─────────────────────

@pytest.mark.asyncio
async def test_check_revert_failed(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json={
        "description": "Allow SSH", "interface": "lan", "direction": "in", "protocol": "tcp",
        "action": "pass", "source_net": "any", "destination_net": "lan", "destination_port": "22",
        "target_host_ids": [host["id"]],
    })
    rule_id = resp.json()["id"]

    async with SessionLocal() as db:
        deployment = RuleDeployment(
            rule_id=uuid.UUID(rule_id), host_id=uuid.UUID(host["id"]),
            status="revert_failed", message="host unreachable",
        )
        db.add(deployment)
        await db.commit()

        problems = await notification_checks.check_revert_failed(db)

    key = f"rule:{deployment.id}"
    assert key in problems
    assert problems[key]["host_name"] == "fw1"


@pytest.mark.asyncio
async def test_check_maintenance_unknown():
    async with SessionLocal() as db:
        op = HostOperation(
            host_id=uuid.uuid4(), host_name="fw1", actor="admin", action="reboot",
            status="unknown", expires_at=_now(), fingerprint="fp", snapshot={},
        )
        db.add(op)
        await db.commit()

        problems = await notification_checks.check_maintenance_unknown(db)

    assert str(op.id) in problems
    assert problems[str(op.id)]["host_name"] == "fw1"


# ── Delivery ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_webhook_signs_when_secret_configured(respx_mock):
    notification_secret_store.save_secret("webhook_signing_secret", "supersecret")
    route = respx_mock.post("https://hooks.test/notify").mock(return_value=httpx.Response(200))

    settings_row = NotificationSettings(webhook_url="https://hooks.test/notify", webhook_signing_secret_set=True)
    problem = NotificationProblem(problem_type="host_offline", dedup_key="h1", context={"host_name": "fw1"})

    await notification_delivery.send_webhook(settings_row, problem, "opened")

    assert route.called
    sent = route.calls.last.request
    assert "X-OPNexus-Signature" in sent.headers
    assert sent.headers["X-OPNexus-Signature"].startswith("sha256=")
    notification_secret_store.delete_secret("webhook_signing_secret")


@pytest.mark.asyncio
async def test_send_webhook_failure_raises(respx_mock):
    respx_mock.post("https://hooks.test/notify").mock(return_value=httpx.Response(500))
    settings_row = NotificationSettings(webhook_url="https://hooks.test/notify", webhook_signing_secret_set=False)
    problem = NotificationProblem(problem_type="host_offline", dedup_key="h1", context={"host_name": "fw1"})

    with pytest.raises(notification_delivery.NotificationDeliveryError):
        await notification_delivery.send_webhook(settings_row, problem, "opened")


@pytest.mark.asyncio
async def test_send_email_uses_starttls_and_login():
    notification_secret_store.save_secret("smtp_password", "hunter2")
    settings_row = NotificationSettings(
        smtp_host="smtp.test", smtp_port=587, smtp_security="starttls",
        smtp_username="opnexus", smtp_password_set=True,
        smtp_from_address="opnexus@example.test", smtp_to_addresses=["ops@example.test"],
    )

    with patch("app.services.notification_delivery.smtplib.SMTP") as smtp_cls:
        instance = MagicMock()
        smtp_cls.return_value = instance
        await notification_delivery.send_email(settings_row, "Test", "Body")

    instance.starttls.assert_called_once()
    instance.login.assert_called_once_with("opnexus", "hunter2")
    instance.send_message.assert_called_once()
    instance.quit.assert_called_once()
    notification_secret_store.delete_secret("smtp_password")


# ── run_notification_checks end-to-end ───────────────────────────────────

@pytest.mark.asyncio
async def test_run_notification_checks_delivers_and_resolves(client, respx_mock):
    route = respx_mock.get("https://fw-down.test/api/diagnostics/interface/getVipStatus")
    route.mock(side_effect=httpx.ConnectError("no route to host"))
    host = await _create_host(client, "fw-down", "https://fw-down.test")

    webhook_route = respx_mock.post("https://hooks.test/notify").mock(return_value=httpx.Response(200))

    async with SessionLocal() as db:
        from app.services import notification_settings_service
        settings_row = await notification_settings_service.get_settings(db)
        settings_row.webhook_enabled = True
        settings_row.webhook_url = "https://hooks.test/notify"
        for flag in ("cert_expiring_enabled", "rule_drift_enabled", "revert_failed_enabled", "maintenance_unknown_enabled"):
            setattr(settings_row, flag, False)
        await db.commit()

        result = await run_notification_checks(db)
        assert result["opened"] == 1
        assert webhook_route.call_count == 1

        problem = (await db.execute(select(NotificationProblem).where(NotificationProblem.problem_type == "host_offline"))).scalar_one()
        assert problem.status == "open"
        assert problem.last_notified_at is not None

    # Host comes back online — the problem should resolve and fire a
    # resolution notification, without re-opening.
    respx_mock.get("https://fw-down.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )
    async with SessionLocal() as db:
        result = await run_notification_checks(db)
        assert result["opened"] == 0
        assert result["resolved"] == 1
        assert webhook_route.call_count == 2

        problem = (await db.execute(select(NotificationProblem).where(NotificationProblem.problem_type == "host_offline"))).scalar_one()
        assert problem.status == "resolved"
        assert problem.resolved_notified_at is not None


# ── API ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_roundtrip_never_exposes_secret(client):
    resp = await client.put("/api/notifications/settings", json={
        "webhook_enabled": True,
        "webhook_url": "https://hooks.test/notify",
        "webhook_signing_secret": "supersecret",
        "poll_interval_seconds": 120,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["webhook_signing_secret_set"] is True
    assert "webhook_signing_secret" not in data
    assert data["poll_interval_seconds"] == 120

    resp = await client.get("/api/notifications/settings")
    assert resp.json()["webhook_signing_secret_set"] is True


@pytest.mark.asyncio
async def test_settings_roundtrip_carp_health_toggle(client):
    resp = await client.get("/api/notifications/settings")
    assert resp.json()["carp_health_enabled"] is True

    resp = await client.put("/api/notifications/settings", json={"carp_health_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["carp_health_enabled"] is False


@pytest.mark.asyncio
async def test_settings_rejects_poll_interval_below_floor(client):
    resp = await client.put("/api/notifications/settings", json={"poll_interval_seconds": 5})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_test_notification_success_and_failure(client, respx_mock):
    await client.put("/api/notifications/settings", json={"webhook_url": "https://hooks.test/notify"})

    respx_mock.post("https://hooks.test/notify").mock(return_value=httpx.Response(200))
    resp = await client.post("/api/notifications/test", json={"channel": "webhook"})
    assert resp.status_code == 200
    assert resp.json() == {"success": True}

    respx_mock.post("https://hooks.test/notify").mock(return_value=httpx.Response(500))
    resp = await client.post("/api/notifications/test", json={"channel": "webhook"})
    assert resp.status_code == 502
