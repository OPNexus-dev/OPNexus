"""Language of the console texts of the backend (app/cli_lang.py): the same rule as the `opnexus` command."""
import socket

import pytest

from app import cli_lang
from app.config_code.__main__ import format_errors, format_plan, run
from app.db_url import explain_database_error


@pytest.fixture(autouse=True)
def _clean_language_env(monkeypatch):
    for name in ("OPNEXUS_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)


def test_language_rule(monkeypatch):
    assert cli_lang.language() == "en"                       # no locale at all (a container): English
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert cli_lang.language() == "de"
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert cli_lang.language() == "en"
    monkeypatch.setenv("OPNEXUS_LANG", "de")                 # the explicit setting wins over the locale
    assert cli_lang.language() == "de"
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("OPNEXUS_LANG", "en")
    assert cli_lang.t("deutsch", "english") == "english"


def test_config_code_texts_follow_the_language(monkeypatch):
    errors = [{"code": "unknown_field", "path": "rules[0].nope", "line": 6, "params": {"field": "nope"}}]
    monkeypatch.setenv("OPNEXUS_LANG", "en")
    assert "Line 6: rules[0].nope: Unknown field (field=nope)" in format_errors(errors)
    plan = {"items": [{"kind": "aliases", "key": "A", "action": "blocked", "planned_action": "delete", "deployed_hosts": 1,
                       "changes": [], "reasons": ["prune_referenced"]}],
            "warnings": [{"code": "target_host_disabled", "params": {"host": "fw1"}}],
            "summary": {"create": 0, "update": 0, "delete": 1, "unchanged": 2, "blocked": 1}}
    text = format_plan(plan)
    assert "! aliases A  blocked (remove): is still in use" in text and "? Target host fw1 is disabled" in text
    assert "Plan: 0 new, 0 changed, 1 removed, 2 unchanged, 1 blocked" in text
    monkeypatch.setenv("OPNEXUS_LANG", "de")
    assert "blockiert (entfernen): wird noch verwendet" in format_plan(plan)


@pytest.mark.asyncio
async def test_config_code_usage_and_unknown_kind_in_english(monkeypatch):
    monkeypatch.setenv("OPNEXUS_LANG", "en")
    code, _, err = await run(["frobnicate"])
    assert code == 1 and err.startswith("Usage:")
    code, _, err = await run(["export", "--kinds", "nope"])
    assert code == 1 and "Unknown kind in --kinds" in err


def test_database_hints_follow_the_language(monkeypatch):
    error = socket.gaierror(-5, "No address associated with hostname")
    monkeypatch.setenv("OPNEXUS_LANG", "en")
    assert "cannot be resolved" in explain_database_error(error)
    monkeypatch.setenv("OPNEXUS_LANG", "de")
    assert "nicht auflösbar" in explain_database_error(error)
