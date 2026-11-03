"""ha_agent_down (docs/DESIGN-HA-UI.md): the agent of this node or of the partner has been silent for 5 minutes."""
from datetime import datetime, timedelta, timezone

import pytest

from app import leader
from app.config import settings
from app.database import SessionLocal
from app.services import ha_checks, ha_service, notification_service, updater_client


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ha_node", "a")
    monkeypatch.setattr(settings, "ha_peer_ip", "192.168.2.21")
    monkeypatch.setattr(settings, "ha_vip", "192.168.2.22/24")
    monkeypatch.setattr(settings, "updater_token_file", str(tmp_path / "token"))
    monkeypatch.setattr(ha_service, "_shared", None)
    monkeypatch.setattr(leader, "gate", None)
    ha_checks._agent_down_since.clear()
    snap = ha_service.Snapshot(db_role="primary")

    async def collect(db):
        return snap

    monkeypatch.setattr(ha_service, "collect", collect)
    yield
    ha_checks._agent_down_since.clear()


def _paired(tmp_path, yes=True):
    marker = tmp_path / "ha-paired"
    marker.unlink(missing_ok=True)
    if yes:
        marker.write_text("1\n")


def _agent(monkeypatch, *, unavailable=False, peer=True):
    async def status():
        if unavailable:
            raise updater_client.UpdaterUnavailable("unreachable")
        return {"peer_reachable": peer, "paired": True}

    monkeypatch.setattr(updater_client, "ha_local_status", status)


async def _run():
    async with SessionLocal() as db:
        return await ha_checks.check_agent_down(db)


def _ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


@pytest.mark.asyncio
async def test_a_pair_without_an_agent_never_alarms(monkeypatch, tmp_path):
    _paired(tmp_path, yes=False)
    _agent(monkeypatch, unavailable=True)
    ha_checks._agent_down_since["own"] = _ago(60)
    assert await _run() == {}
    assert ha_checks._agent_down_since == {}   # and forgets what it saw before the pairing was dissolved


@pytest.mark.asyncio
async def test_a_healthy_pair_is_quiet_and_clears_earlier_failures(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch)
    ha_checks._agent_down_since.update(own=_ago(20), partner=_ago(20))
    assert await _run() == {}
    assert ha_checks._agent_down_since == {}


@pytest.mark.asyncio
async def test_the_first_failed_look_starts_a_clock_and_raises_nothing_yet(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch, unavailable=True)
    result = await _run()
    assert not dict(result) and "agent" in result.preserve_prefixes   # an alarm that is already open stays open meanwhile
    assert "own" in ha_checks._agent_down_since


@pytest.mark.asyncio
async def test_a_silent_own_agent_raises_after_five_minutes(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch, unavailable=True)
    ha_checks._agent_down_since["own"] = _ago(4)
    assert not dict(await _run())
    ha_checks._agent_down_since["own"] = _ago(6)
    problem = (await _run())["agent"]
    assert problem["agent_antwortet_nicht"] == "dieser Knoten" and problem["knoten"] == "a" and problem["partner"] == "192.168.2.21"
    assert problem["dauer"] == "6 min" and "docker compose ps updater ha-gate" in problem["hinweis"]


@pytest.mark.asyncio
async def test_a_silent_partner_agent_raises_after_five_minutes(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch, peer=False)
    ha_checks._agent_down_since["partner"] = _ago(7)
    assert (await _run())["agent"]["agent_antwortet_nicht"] == "der Partner"


@pytest.mark.asyncio
async def test_while_the_own_agent_is_silent_nothing_is_known_about_the_partners(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch, unavailable=True)
    ha_checks._agent_down_since.update(own=_ago(8), partner=_ago(30))
    problem = (await _run())["agent"]
    assert problem["agent_antwortet_nicht"] == "dieser Knoten" and problem["dauer"] == "8 min"   # the earlier look at the partner is stale


@pytest.mark.asyncio
async def test_the_longer_lasting_fault_is_the_one_reported(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch, peer=False)
    ha_checks._agent_down_since["partner"] = _ago(12)
    result = await _run()
    assert result["agent"]["agent_antwortet_nicht"] == "der Partner" and result["agent"]["dauer"] == "12 min"


@pytest.mark.asyncio
async def test_an_unknown_partner_state_is_no_fault(monkeypatch, tmp_path):
    _paired(tmp_path)

    async def status():
        return {"peer_reachable": None, "paired": False}

    monkeypatch.setattr(updater_client, "ha_local_status", status)
    assert await _run() == {} and ha_checks._agent_down_since == {}


@pytest.mark.asyncio
async def test_a_rejecting_updater_counts_as_silent(monkeypatch, tmp_path):
    _paired(tmp_path)

    async def status():
        raise updater_client.UpdaterRejected("boom")

    monkeypatch.setattr(updater_client, "ha_local_status", status)
    ha_checks._agent_down_since["own"] = _ago(10)
    assert "agent" in await _run()


@pytest.mark.asyncio
async def test_only_a_primary_with_a_complete_snapshot_judges(monkeypatch, tmp_path):
    _paired(tmp_path)
    _agent(monkeypatch, unavailable=True)
    ha_checks._agent_down_since["own"] = _ago(30)

    async def replica(db):
        return ha_service.Snapshot(db_role="replica")

    monkeypatch.setattr(ha_service, "collect", replica)
    assert await _run() == {}
    monkeypatch.setattr(ha_service, "_shared", None)

    async def incomplete(db):
        return ha_service.Snapshot(db_role=None, complete=False)

    monkeypatch.setattr(ha_service, "collect", incomplete)
    assert await _run() == {}
    monkeypatch.setattr(settings, "ha_node", "")
    assert await _run() == {}


def test_the_alarm_is_wired_into_the_notification_cycle_behind_the_ha_switch():
    entry = [item for item in notification_service._CHECKS if item[0] == "ha_agent_down"]
    assert entry and entry[0][1] == "ha_alerts_enabled" and entry[0][2] is ha_checks.check_agent_down


def test_every_check_has_a_title_a_schema_entry_and_a_dashboard_line():
    from app.schemas import notification as schema
    from app.services import notification_delivery
    assert "ha_agent_down" in notification_delivery._LABELS and "ha_agent_down" in schema.ProblemType.__args__
