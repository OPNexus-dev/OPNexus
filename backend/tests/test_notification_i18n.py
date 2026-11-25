"""Language of the notification texts: German stays exactly as it was, English is opt-in/automatic, and no hint
written in the checks is left without an English text."""
import ast
import pathlib

import pytest

from app.database import SessionLocal
from app.models.notification import NotificationProblem
from app.models.preferences import UserPreferences
from app.models.user import AdminUser
from app.services import notification_delivery, notification_i18n

SERVICES = pathlib.Path(notification_delivery.__file__).parent


def _problem(kind="host_offline", **context):
    return NotificationProblem(problem_type=kind, dedup_key="k", context=context)


def test_german_text_is_unchanged_by_default():
    subject, body = notification_delivery.render_message(_problem(host_name="fw-1", hinweis="x"), "opened")
    assert subject == "OPNexus: Host nicht erreichbar"
    assert "hinweis: x" in body and "host_name: fw-1" in body
    assert notification_delivery.render_message(_problem(), "resolved")[0] == "OPNexus: Host nicht erreichbar behoben"


def test_english_subject_keys_and_hint():
    hint = "Dienst läuft nicht mehr; absichtlich gestoppt? Dann aus der Überwachungsliste nehmen."
    subject, body = notification_delivery.render_message(
        _problem("service_down", host_name="fw-1", hinweis=hint, schwelle_prozent=90), "opened", "en")
    assert subject == "OPNexus: Service down"
    assert "hint: The service is no longer running" in body
    assert "threshold_percent: 90" in body and "host_name: fw-1" in body
    assert notification_delivery.render_message(_problem("service_down"), "resolved", "en")[0] == "OPNexus: Service down resolved"


def test_unknown_text_passes_through_and_values_keep_their_type():
    _subject, body = notification_delivery.render_message(_problem("rollout_paused", grund="Gate rot", wert=12), "opened", "en")
    assert "reason: Gate rot" in body and "value: 12" in body


def test_pattern_values_are_translated():
    _s, body = notification_delivery.render_message(
        _problem("gateway_down", grund="Paketverlust 25 %, Latenz 600 ms", ressource="Festplatte /dev/ada0"), "opened", "en")
    assert "reason: Packet loss 25 %, Latency 600 ms" in body and "resource: Disk /dev/ada0" in body


def test_language_follows_the_running_delivery():
    token = notification_i18n.set_current("en")
    try:
        assert notification_delivery.render_message(_problem(), "opened")[0] == "OPNexus: Host not reachable"
    finally:
        notification_i18n.reset_current(token)
    assert notification_delivery.render_message(_problem(), "opened")[0] == "OPNexus: Host nicht erreichbar"


def _german_hints() -> list[str]:
    """Every literal text a check puts under the key "hinweis" (module constants it refers to are resolved)."""
    found = []
    for name in ("notification_checks.py", "monitor_checks.py", "ha_checks.py"):
        tree = ast.parse((SERVICES / name).read_text(encoding="utf-8"))
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    constants[node.targets[0].id] = ast.literal_eval(node.value)
                except Exception:
                    pass
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "hinweis":
                    if isinstance(value, ast.Constant):
                        found.append(value.value)
                    elif isinstance(value, ast.Name) and isinstance(constants.get(value.id), str):
                        found.append(constants[value.id])
                    # f-strings (the API-rights hint) are covered by a pattern, tested below
    return found


def test_every_hint_in_the_checks_has_an_english_text():
    hints = _german_hints()
    assert len(hints) >= 15, "the scan found too few hints, the test itself is broken"
    missing = [h for h in hints if h not in notification_i18n.VALUES_EN]
    assert missing == [], "hint without English text: %s" % missing


def test_api_rights_hint_pattern():
    german = 'Dem API-Benutzer fehlt das Recht, api/routes/gateway/status zu lesen (OPNsense-Berechtigung "page-system-gateways"). Alarme, die darauf beruhen, können nicht auslösen.'
    english = notification_i18n.value_text(german, "en")
    assert english.startswith("The API user lacks the privilege to read api/routes/gateway/status")
    assert '"page-system-gateways"' in english


@pytest.mark.asyncio
async def test_effective_language_setting_wins_then_oldest_admin_then_english(client):
    async with SessionLocal() as db:
        assert await notification_i18n.effective_language(db, "de") == "de"
        assert await notification_i18n.effective_language(db, "en") == "en"
        # automatic: the test client's admin has no saved language -> English
        assert await notification_i18n.effective_language(db, None) == "en"
        admin = await db.scalar(__import__("sqlalchemy").select(AdminUser).where(AdminUser.role == "admin").order_by(AdminUser.created_at))
        db.add(UserPreferences(user_id=admin.id, theme="system", language="de", setup_done=False))
        await db.commit()
        assert await notification_i18n.effective_language(db, None) == "de"


@pytest.mark.asyncio
async def test_settings_api_language_round_trip(client):
    assert (await client.get("/api/notifications/settings")).json()["language"] == "auto"
    resp = await client.put("/api/notifications/settings", json={"language": "en"})
    assert resp.status_code == 200 and resp.json()["language"] == "en"
    assert (await client.get("/api/notifications/settings")).json()["language"] == "en"
    resp = await client.put("/api/notifications/settings", json={"language": "auto"})
    assert resp.json()["language"] == "auto"
    assert (await client.put("/api/notifications/settings", json={"language": "fr"})).status_code == 422


@pytest.mark.asyncio
async def test_a_check_cycle_renders_in_the_configured_language(client, monkeypatch):
    from app.services import notification_service

    async def fake_check(db):
        return {"fw-1": {"host_name": "fw-1", "hinweis": "Dienst läuft nicht mehr; absichtlich gestoppt? Dann aus der Überwachungsliste nehmen."}}

    monkeypatch.setattr(notification_service, "_CHECKS", [("host_offline", "host_offline_enabled", fake_check)])
    seen = []

    async def fake_send(settings_row, problem, transition):
        seen.append(notification_delivery.render_message(problem, transition))

    monkeypatch.setattr(notification_delivery, "send_webhook", fake_send)
    for language, expected in (("en", "OPNexus: Host not reachable"), ("de", "OPNexus: Host nicht erreichbar")):
        assert (await client.put("/api/notifications/settings", json={"webhook_enabled": True, "webhook_url": "https://hook.example/x", "language": language})).status_code == 200
        async with SessionLocal() as db:
            for row in (await db.scalars(__import__("sqlalchemy").select(NotificationProblem))).all():
                await db.delete(row)
            await db.commit()
            await notification_service.run_notification_checks(db)
        assert seen and seen[-1][0] == expected, (language, seen)
    assert "hint: The service is no longer running" in notification_delivery.render_message(
        NotificationProblem(problem_type="host_offline", dedup_key="k", context={"hinweis": "Dienst läuft nicht mehr; absichtlich gestoppt? Dann aus der Überwachungsliste nehmen."}), "opened", "en")[1]
