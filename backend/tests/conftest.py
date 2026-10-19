import os

# Point at a dedicated *_test database (created by postgres-init/001-create-
# test-db.sh) instead of whatever DATABASE_URL the backend service was
# started with, so tests never touch real data. Must happen before any
# `app.*` import, since app.config.settings is a module-level singleton.
_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://opnexus:opnexus@postgres:5432/opnexus",
)
if not _raw_url.rsplit("/", 1)[-1].endswith("_test"):
    _raw_url += "_test"
os.environ["DATABASE_URL"] = _raw_url
os.environ["OPNSENSE_SECRET_DIR"] = "/tmp/opnexus-test-secrets"
os.environ["OPNSENSE_BACKUP_DIR"] = "/tmp/opnexus-test-backups"
os.environ["NOTIFICATION_SECRET_DIR"] = "/tmp/opnexus-test-notification-secrets"
os.environ["IPSEC_PSK_SECRET_DIR"] = "/tmp/opnexus-test-ipsec-secrets"
os.environ["OPENVPN_KEY_SECRET_DIR"] = "/tmp/opnexus-test-openvpn-secrets"
os.environ["WIREGUARD_KEY_SECRET_DIR"] = "/tmp/opnexus-test-wireguard-secrets"
os.environ["CERT_KEY_SECRET_DIR"] = "/tmp/opnexus-test-cert-key-secrets"
os.environ["ACME_SECRET_DIR"] = "/tmp/opnexus-test-acme-secrets"
os.environ["TOTP_SECRET_DIR"] = "/tmp/opnexus-test-totp-secrets"
os.environ["OVERLAY_DIR"] = "/tmp/opnexus-test-overlay"
os.environ.setdefault("ADMIN_USERNAME", "admin")
# Force-set rather than setdefault: docker-compose's backend service sets
# ADMIN_PASSWORD="" (present-but-empty) by default, which setdefault would
# leave alone since the key already exists — but an empty password fails
# LoginRequest's min_length validation, breaking every test.
os.environ["ADMIN_PASSWORD"] = "test-admin-password-123"
# Tests talk plain http to an in-process app: never inherit the production
# transport settings from .env (a Secure cookie would never be sent back).
os.environ["COOKIE_SECURE"] = "false"
os.environ["FRONTEND_ORIGIN"] = ""
os.environ["TRUST_PROXY_HEADERS"] = "false"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth_service  # noqa: E402

# admin_users must survive _clean_tables (see below) — the bootstrap admin
# is created once for the whole test session, not re-created per test.
_PRESERVE_TABLES = {"admin_users"}


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        await auth_service.bootstrap_admin(db)
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    yield
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name not in _PRESERVE_TABLES:
                if table.name == "audit_events":
                    from sqlalchemy import text
                    await conn.execute(text("TRUNCATE TABLE audit_events"))
                else:
                    await conn.execute(table.delete())


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    # The throttle is a module-level singleton (by design — it must survive
    # across requests/sessions in production), so it leaks between tests
    # unless explicitly cleared. Without this, a lockout test running before
    # a login-success test would spuriously 429 it.
    for throttle in (auth_service.throttle, auth_service.account_throttle):
        throttle._records.clear()
    yield
    for throttle in (auth_service.throttle, auth_service.account_throttle):
        throttle._records.clear()


@pytest_asyncio.fixture
async def client():
    """An authenticated client — logged in as the bootstrap admin before
    the test runs, since AuthMiddleware guards every route now."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert resp.status_code == 200, f"test login failed: {resp.text}"
        yield ac


@pytest_asyncio.fixture
async def anon_client():
    """An unauthenticated client, for testing the auth boundary itself."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def outbound_mode_hybrid(respx_mock):
    import httpx
    respx_mock.get(url__regex=r".*/firewall/source_nat/get$").mock(return_value=httpx.Response(200, json={"filter": {"general": {"snat_mode": {"hybrid": {"selected": 1}}}}}))
