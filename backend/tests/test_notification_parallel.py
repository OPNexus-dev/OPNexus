"""S7 (1.29): channels are served in parallel, each with its own timeout, and one cycle has an upper bound."""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from app.models.notification import NotificationProblem, NotificationSettings
from app.services import notification_channels, notification_delivery, notification_service


def _channel(name):
    return SimpleNamespace(name=name, channel_type="ntfy")


def _problem(key="h1"):
    return NotificationProblem(problem_type="host_offline", dedup_key=key, context={"host_name": "fw1"})


def _settings(**over):
    return NotificationSettings(webhook_enabled=False, email_enabled=False, **over)


@pytest.fixture
def fast_limits(monkeypatch):
    monkeypatch.setattr(notification_service, "DELIVERY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(notification_service, "DELIVERY_CYCLE_BUDGET_SECONDS", 3.0)


@pytest.mark.asyncio
async def test_channels_run_at_the_same_time_not_one_after_another(monkeypatch, fast_limits):
    started, delivered = [], []
    everybody = asyncio.Event()

    async def send(channel, problem, transition):
        started.append(channel.name)
        if len(started) == 3:
            everybody.set()
        await everybody.wait()  # sequential delivery would never get past the first channel
        delivered.append(channel.name)

    monkeypatch.setattr(notification_channels, "send", send)
    problem = _problem()
    await notification_service._deliver(_settings(), problem, "opened", [_channel("a"), _channel("b"), _channel("c")])
    assert sorted(delivered) == ["a", "b", "c"] and problem.last_notified_at is not None


@pytest.mark.asyncio
async def test_hanging_channel_times_out_alone_others_still_delivered(monkeypatch, fast_limits, caplog):
    delivered = []

    async def send(channel, problem, transition):
        if channel.name == "stuck":
            await asyncio.sleep(60)
        delivered.append(channel.name)

    monkeypatch.setattr(notification_channels, "send", send)
    problem = _problem()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await notification_service._deliver(_settings(), problem, "opened", [_channel("stuck"), _channel("a"), _channel("b")])
    assert time.monotonic() - started < 2.0
    assert sorted(delivered) == ["a", "b"]
    assert problem.last_notified_at is not None  # attempted once, not retried
    assert "stuck" in caplog.text and "timed out" in caplog.text


@pytest.mark.asyncio
async def test_failing_channel_does_not_stop_the_others_and_log_has_no_url(monkeypatch, fast_limits, caplog):
    delivered = []

    async def send(channel, problem, transition):
        if channel.name == "bad":
            raise notification_delivery.NotificationDeliveryError("Kanal „bad“ (ntfy): Zustellung fehlgeschlagen (HTTP 500)")
        if channel.name == "boom":
            raise RuntimeError("https://ntfy.example/secret-topic exploded")
        delivered.append(channel.name)

    monkeypatch.setattr(notification_channels, "send", send)
    with caplog.at_level(logging.WARNING):
        await notification_service._deliver(_settings(), _problem(), "opened", [_channel("bad"), _channel("boom"), _channel("ok")])
    assert delivered == ["ok"]
    assert "HTTP 500" in caplog.text
    assert "secret-topic" not in caplog.text  # unexpected errors are logged by type only


@pytest.mark.asyncio
async def test_webhook_and_email_are_not_blocked_by_a_hanging_channel(monkeypatch, fast_limits):
    done = []

    async def hang(channel, problem, transition):
        await asyncio.sleep(60)

    async def webhook(row, problem, transition):
        done.append("webhook")

    async def email(row, subject, body):
        done.append("email")

    monkeypatch.setattr(notification_channels, "send", hang)
    monkeypatch.setattr(notification_delivery, "send_webhook", webhook)
    monkeypatch.setattr(notification_delivery, "send_email", email)
    row = NotificationSettings(webhook_enabled=True, email_enabled=True)
    await notification_service._deliver(row, _problem(), "opened", [_channel("stuck")])
    assert sorted(done) == ["email", "webhook"]


@pytest.mark.asyncio
async def test_cycle_has_an_upper_bound_and_records_attempts(monkeypatch, caplog):
    monkeypatch.setattr(notification_service, "DELIVERY_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(notification_service, "DELIVERY_CYCLE_BUDGET_SECONDS", 0.4)

    async def hang(channel, problem, transition):
        await asyncio.sleep(60)

    monkeypatch.setattr(notification_channels, "send", hang)
    problems = [_problem(f"h{i}") for i in range(3)]
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await notification_service._deliver_all(_settings(), [(p, "opened") for p in problems], [_channel("stuck")])
    assert time.monotonic() - started < 2.0
    assert all(p.last_notified_at is not None for p in problems)
    assert "stopped after" in caplog.text


@pytest.mark.asyncio
async def test_parallelism_is_limited(monkeypatch, fast_limits):
    monkeypatch.setattr(notification_service, "DELIVERY_PARALLEL", 2)
    running = peak = 0

    async def send(channel, problem, transition):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    monkeypatch.setattr(notification_channels, "send", send)
    await notification_service._deliver_all(_settings(), [(_problem("a"), "opened"), (_problem("b"), "opened")],
                                            [_channel(str(i)) for i in range(3)])
    assert peak == 2


@pytest.mark.asyncio
async def test_event_like_resolved_problems_send_nothing(monkeypatch, fast_limits):
    calls = []

    async def send(channel, problem, transition):
        calls.append(channel.name)

    monkeypatch.setattr(notification_channels, "send", send)
    problem = NotificationProblem(problem_type="rollout_start_failed", dedup_key="r1", context={})
    await notification_service._deliver(_settings(), problem, "resolved", [_channel("a")])
    assert calls == [] and problem.resolved_notified_at is not None


@pytest.mark.asyncio
async def test_check_cycle_with_a_hanging_webhook_still_finishes_and_records(client, respx_mock, monkeypatch):
    """End to end through run_notification_checks: a webhook that never answers is cut off by its timeout."""
    import httpx
    from sqlalchemy import select
    from app.database import SessionLocal
    from tests.test_notifications import _create_host
    from app.services import notification_settings_service

    monkeypatch.setattr(notification_service, "DELIVERY_TIMEOUT_SECONDS", 0.3)
    respx_mock.get("https://fw-hang.test/api/diagnostics/interface/getVipStatus").mock(side_effect=httpx.ConnectError("no route"))
    await _create_host(client, "fw-hang", "https://fw-hang.test")

    async def hang(row, problem, transition):
        await asyncio.sleep(60)

    monkeypatch.setattr(notification_delivery, "send_webhook", hang)
    async with SessionLocal() as db:
        row = await notification_settings_service.get_settings(db)
        row.webhook_enabled, row.webhook_url = True, "https://hooks.test/notify"
        await db.commit()
        started = time.monotonic()
        result = await notification_service.run_notification_checks(db)
        assert time.monotonic() - started < 5 and result["opened"] >= 1
        problem = (await db.execute(select(NotificationProblem).where(NotificationProblem.problem_type == "host_offline"))).scalar_one()
        assert problem.last_notified_at is not None


@pytest.mark.asyncio
async def test_messages_never_tried_before_the_budget_ended_are_not_recorded_as_sent(monkeypatch):
    monkeypatch.setattr(notification_service, "DELIVERY_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(notification_service, "DELIVERY_CYCLE_BUDGET_SECONDS", 0.4)
    monkeypatch.setattr(notification_service, "DELIVERY_PARALLEL", 1)

    async def hang(channel, problem, transition):
        await asyncio.sleep(60)

    monkeypatch.setattr(notification_channels, "send", hang)
    problems = [_problem(f"h{i}") for i in range(3)]
    await notification_service._deliver_all(_settings(), [(p, "opened") for p in problems], [_channel("stuck")])
    tried = [p for p in problems if p.last_notified_at is not None]
    assert len(tried) == 1  # only the first got a slot; the other two were never attempted


@pytest.mark.asyncio
async def test_open_problem_that_was_never_delivered_is_sent_again_next_cycle(client, monkeypatch):
    from sqlalchemy import select
    from app.database import SessionLocal

    async def fake_check(db):
        return {"retry-me": {"host_name": "fw1"}}

    sent = []

    async def record(settings_row, pending, channels):
        sent.append([problem.dedup_key for problem, _ in pending])

    monkeypatch.setattr(notification_service, "_CHECKS", [("host_offline", "host_offline_enabled", fake_check)])
    monkeypatch.setattr(notification_service, "_deliver_all", record)
    async with SessionLocal() as db:
        await notification_service.run_notification_checks(db)          # opens it; "delivery" records nothing
    async with SessionLocal() as db:
        await notification_service.run_notification_checks(db)          # still never delivered: again
        row = (await db.scalars(select(NotificationProblem).where(NotificationProblem.dedup_key == "retry-me"))).one()
        row.last_notified_at = notification_service._now()
        await db.commit()
    async with SessionLocal() as db:
        await notification_service.run_notification_checks(db)          # delivered once: quiet again
    assert sent == [["retry-me"], ["retry-me"], []]
