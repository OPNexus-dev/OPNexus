import pytest
from sqlalchemy.engine import make_url

from app.db_url import explain_database_error, normalize_database_url


def test_plain_url_unchanged():
    url = "postgresql+asyncpg://opnexus:abc123XYZ@postgres:5432/opnexus"
    assert normalize_database_url(url) == url


@pytest.mark.parametrize("password", ["P@ss/w#rd%1:x", "a@b", "x/y", "p#q", "q?r", "co:lon", "[br]"])
def test_special_characters_roundtrip(password):
    raw = f"postgresql+asyncpg://opnexus:{password}@postgres:5432/opnexus"
    url = make_url(normalize_database_url(raw))
    assert url.password == password
    assert (url.host, url.port, url.database, url.username) == ("postgres", 5432, "opnexus", "opnexus")


def test_already_encoded_password_keeps_working():
    url = "postgresql+asyncpg://opnexus:p%40ss@postgres:5432/opnexus"
    assert normalize_database_url(url) == url
    assert make_url(url).password == "p@ss"


def test_url_without_password_unchanged():
    assert normalize_database_url("postgresql+asyncpg://opnexus@postgres/opnexus") == "postgresql+asyncpg://opnexus@postgres/opnexus"
    assert normalize_database_url("sqlite://") == "sqlite://"


class InvalidPasswordError(Exception):
    pass


def test_explain_password_mismatch_even_when_wrapped():
    try:
        try:
            raise InvalidPasswordError('password authentication failed for user "opnexus"')
        except InvalidPasswordError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as exc:
        hint = explain_database_error(exc)
    assert hint and "down -v" in hint and "leeren Datenbank-Volume" in hint


def test_explain_unresolvable_host():
    import socket
    assert "Host" in explain_database_error(socket.gaierror(-5, "No address associated with hostname"))


def test_explain_unknown_error_is_none():
    assert explain_database_error(ValueError("boom")) is None
