"""Settings › Betrieb: values that used to need the .env are set in the web interface (app/runtime_settings.py)."""
import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app import runtime_settings as rs
from app.config import settings
from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.runtime_setting import RuntimeSetting
from app.services import notification_checks, notification_secret_store
from tests.test_operator_role import _operator


def _ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mail CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _by_key(body) -> dict:
    return {item["key"]: item for item in body["settings"]}


@pytest.fixture
def secret_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "notification_secret_dir", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_defaults_are_listed_with_bounds_units_and_source(client):
    items = _by_key((await client.get("/api/settings/system")).json())
    assert set(items) == set(rs.BY_KEY)
    session = items["session_lifetime_days"]
    assert session == {**session, "value": 30, "default": 30, "unit": "days", "min": 1, "max": 365, "source": "default"}
    assert items["heartbeat_url"]["value"] is None and items["heartbeat_url"]["is_set"] is False


@pytest.mark.asyncio
async def test_saving_changes_what_the_code_reads_and_is_audited(client):
    resp = await client.put("/api/settings/system", json={"values": {
        "session_lifetime_days": 7, "deploy_confirm_window_seconds": 300, "opnsense_timeout": 25.5}})
    assert resp.status_code == 200, resp.text
    items = _by_key(resp.json())
    assert items["session_lifetime_days"]["value"] == 7 and items["session_lifetime_days"]["source"] == "ui"
    assert rs.value("session_lifetime_days") == 7 * 86400  # the code keeps working in seconds
    assert rs.value("deploy_confirm_window_seconds") == 300
    assert rs.value("opnsense_timeout") == 25.5
    async with SessionLocal() as db:
        event = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "system_settings"))).one()
    assert event.details["changes"]["session_lifetime_days"] == {"old": 30, "new": 7}
    # null resets to the default
    items = _by_key((await client.put("/api/settings/system", json={"values": {"session_lifetime_days": None}})).json())
    assert items["session_lifetime_days"]["source"] == "default" and rs.value("session_lifetime_days") == 30 * 86400


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {"deploy_confirm_window_seconds": 10},          # below the floor: the safety window must stay usable
    {"login_max_attempts": 1000},
    {"session_lifetime_days": 1.5},
    {"session_lifetime_days": "30"},
    {"no_such_setting": 1},
    {"heartbeat_url": "http://hc.example.test/x"},  # https only
    {"heartbeat_url": "https://127.0.0.1/x"},
    {"smtp_ca_pem": "not a certificate"},
])
async def test_invalid_values_are_refused_and_nothing_is_saved(client, values):
    resp = await client.put("/api/settings/system", json={"values": {"login_window_seconds": 600, **values}})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "SETTING_INVALID"
    assert resp.json()["detail"]["params"]["setting"] in values
    async with SessionLocal() as db:
        assert (await db.scalars(select(RuntimeSetting))).all() == []  # the valid one in the same request neither
    assert rs.value("login_window_seconds") == settings.login_window_seconds


@pytest.mark.asyncio
async def test_only_admins_change_settings(client):
    op = await _operator(client, "settings-oper")
    try:
        assert (await op.get("/api/settings/system")).status_code == 200
        resp = await op.put("/api/settings/system", json={"values": {"login_max_attempts": 9}})
        assert resp.status_code == 403
    finally:
        await op.aclose()


@pytest.mark.asyncio
async def test_a_server_value_wins_and_cannot_be_overwritten(client, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc.example.test/from-env")
    monkeypatch.setattr(settings, "heartbeat_url", "https://hc.example.test/from-env")
    item = _by_key((await client.get("/api/settings/system")).json())["heartbeat_url"]
    assert item["source"] == "server" and item["is_set"] is True and item["env"] == "HEARTBEAT_URL"
    resp = await client.put("/api/settings/system", json={"values": {"heartbeat_url": "https://hc.example.test/ui"}})
    assert resp.status_code == 422 and "HEARTBEAT_URL" in resp.json()["detail"]["params"]["reason"]
    assert rs.value("heartbeat_url") == "https://hc.example.test/from-env"


@pytest.mark.asyncio
async def test_an_empty_env_value_does_not_count_as_set(client, monkeypatch, secret_dir):
    monkeypatch.setenv("HEARTBEAT_URL", "")
    assert (await client.put("/api/settings/system", json={"values": {"heartbeat_url": "https://hc.example.test/ui"}})).status_code == 200
    assert rs.value("heartbeat_url") == "https://hc.example.test/ui"


@pytest.mark.asyncio
async def test_the_update_interval_from_an_old_compose_file_does_not_lock_the_field(client, monkeypatch):
    """Compose files before 1.34 always pass UPDATE_CHECK_INTERVAL_HOURS=12; that must stay editable."""
    monkeypatch.setenv("UPDATE_CHECK_INTERVAL_HOURS", "12")
    item = _by_key((await client.get("/api/settings/system")).json())["update_check_interval_hours"]
    assert item["source"] == "default"
    assert (await client.put("/api/settings/system", json={"values": {"update_check_interval_hours": 3}})).status_code == 200
    assert rs.value("update_check_interval_hours") == 3


@pytest.mark.asyncio
async def test_the_heartbeat_url_is_a_secret(client, secret_dir):
    url = "https://hc.example.test/ping/secret-id"
    items = _by_key((await client.put("/api/settings/system", json={"values": {"heartbeat_url": url}})).json())
    assert items["heartbeat_url"] == {**items["heartbeat_url"], "value": None, "is_set": True, "source": "ui"}
    assert rs.value("heartbeat_url") == url
    assert notification_secret_store.read_secret("runtime-heartbeat_url") == url
    async with SessionLocal() as db:
        row = await db.get(RuntimeSetting, "heartbeat_url")
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "system_settings"))).all()
    assert row.value is None  # the database only knows that it is set
    assert "secret-id" not in str([e.details for e in events])
    await client.put("/api/settings/system", json={"values": {"heartbeat_url": ""}})
    assert rs.value("heartbeat_url") == settings.heartbeat_url
    assert notification_secret_store.read_secret("runtime-heartbeat_url") is None


@pytest.mark.asyncio
async def test_a_pasted_mail_ca_is_trusted_by_the_smtp_context(client):
    pem = _ca_pem()
    assert (await client.put("/api/settings/system", json={"values": {"smtp_ca_pem": pem}})).status_code == 200
    ctx = rs.smtp_ssl_context()
    assert any(c.get("subject") == ((("commonName", "Mail CA"),),) for c in ctx.get_ca_certs())


@pytest.mark.asyncio
async def test_another_process_sees_the_value_after_its_refresh(client):
    async with SessionLocal() as db:
        db.add(RuntimeSetting(key="login_lockout_seconds", value=900, updated_by="other-node"))
        await db.commit()
    assert rs.value("login_lockout_seconds") == settings.login_lockout_seconds  # not yet: cache
    await rs.refresh()
    assert rs.value("login_lockout_seconds") == 900


@pytest.mark.asyncio
async def test_login_throttle_follows_the_saved_limit(client, anon_client):
    await client.put("/api/settings/system", json={"values": {"login_max_attempts": 3}})
    statuses = []
    for _ in range(4):
        statuses.append((await anon_client.post("/api/auth/login", json={"username": settings.admin_username,
                                                                          "password": "wrong-password"})).status_code)
    assert statuses[-1] == 429 and statuses.count(401) == 3


# ── server disk alert ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_server_disk_alert_follows_the_threshold(client, monkeypatch):
    monkeypatch.setattr(notification_checks, "server_disk_usage", lambda: (91.5, 915, 1000))
    async with SessionLocal() as db:
        problems = await notification_checks.check_server_disk(db)
    assert problems["server"]["belegt_prozent"] == 91.5 and problems["server"]["schwelle_prozent"] == 90
    resp = await client.put("/api/notifications/settings", json={"server_disk_threshold_pct": 95})
    assert resp.status_code == 200 and resp.json()["server_disk_threshold_pct"] == 95
    async with SessionLocal() as db:
        assert await notification_checks.check_server_disk(db) == {}
    assert (await client.put("/api/notifications/settings", json={"server_disk_threshold_pct": 20})).status_code == 422
    monkeypatch.setattr(notification_checks, "server_disk_usage", lambda: None)
    async with SessionLocal() as db:
        unknown = await notification_checks.check_server_disk(db)
    assert unknown == {} and unknown.preserve_prefixes == {"server"}


@pytest.mark.asyncio
async def test_server_disk_metric(client, monkeypatch):
    monkeypatch.setattr(notification_checks, "server_disk_usage", lambda: (81.25, 8125, 10000))
    assert "opnexus_server_disk_used_percent 81.2" in (await client.get("/api/metrics")).text
