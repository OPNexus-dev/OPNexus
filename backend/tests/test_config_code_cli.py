"""Host console of configuration as code (1.32 P8): exit codes, text diff, actor, no control sequences."""
import copy
import json

import pytest
import yaml
from sqlalchemy import select

from app.config_code.__main__ import TOKEN_MARK, format_errors, run
from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.rule import FirewallRule
from tests.config_code_seed import seed_everything


async def cli(*argv, stdin: str | bytes | None = None):
    data = stdin.encode() if isinstance(stdin, str) else stdin
    return await run(list(argv), data)


def token_of(out: str) -> str:
    return next(line[len(TOKEN_MARK):] for line in out.splitlines() if line.startswith(TOKEN_MARK))


async def export_text() -> str:
    code, out, _ = await cli("export")
    assert code == 0
    return out


def edit(text: str, fn) -> str:
    data = yaml.safe_load(text)
    fn(data)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


async def test_export_plan_without_changes_exits_0_and_a_change_exits_2_with_a_readable_diff():
    await seed_everything()
    text = await export_text()
    code, out, _ = await cli("plan", stdin=text)
    assert code == 0 and "Plan: 0 neu, 0 geändert, 0 entfernt" in out and token_of(out)
    def change(d):
        d["rules"][0]["destination_port"] = 853
        d["rules"].append({**copy.deepcopy(d["rules"][0]), "key": "new-rule"})  # no shared objects: YAML would write anchors

    changed = edit(text, change)
    code, out, _ = await cli("plan", stdin=changed)
    assert code == 2
    assert '+ rules new-rule  (neu)' in out
    assert '~ rules allow-dns-lan  destination_port: "53" → "853"' in out
    assert out.rstrip().splitlines()[-2].startswith("Plan: 1 neu, 1 geändert, 0 entfernt")


async def test_json_output_is_the_api_answer():
    await seed_everything()
    code, out, _ = await cli("plan", "--json", stdin=await export_text())
    body = json.loads(out)
    assert code == 0 and body["applicable"] and len(body["token"]) == 64 and body["summary"]["create"] == 0


async def test_apply_needs_the_token_of_the_plan_and_writes_audit_as_the_console():
    await seed_everything()
    changed = edit(await export_text(), lambda d: d["rules"][0].update(description="per CLI"))
    code, _, err = await cli("apply", stdin=changed)
    assert code == 1 and "--plan-token" in err
    code, out, _ = await cli("plan", stdin=changed)
    token = token_of(out)
    code, out, err = await cli("apply", "--plan-token", "0" * 64, stdin=changed)
    assert code == 4 and "neu planen" in err
    code, out, err = await cli("apply", "--plan-token", token, stdin=changed)
    assert code == 0 and "Übernommen: 0 neu, 1 geändert, 0 entfernt" in out and "nichts auf Firewalls geschrieben" in out
    async with SessionLocal() as db:
        rule = await db.scalar(select(FirewallRule).where(FirewallRule.config_key == "allow-dns-lan"))
        events = (await db.execute(select(AuditEvent))).scalars().all()
    assert rule.description == "per CLI"
    assert events and {e.actor for e in events} == {"cli (Host-Konsole)"}


async def test_invalid_files_exit_3_and_name_line_and_path_without_control_characters():
    await seed_everything()
    bad = "opnexus_config: 1\nrules:\n- key: r-1\n  description: \"x\\e[31mred\"\n  interface: lan\n  nope: 1\n"
    code, out, err = await cli("plan", stdin=bad)
    assert code == 3 and not out
    assert "Zeile 6: rules[0].nope: Unbekanntes Feld (field=nope)" in err and "\x1b" not in err
    code, out, _ = await cli("plan", "--json", stdin="a: &x 1\n")
    assert code == 3 and json.loads(out)["errors"][0]["code"] == "yaml_anchor_forbidden"
    assert "\x1b" not in format_errors([{"code": "x", "path": "p\x1b[2J", "line": 1, "params": {"value": "v\x1b[0m"}}])


async def test_blocked_plans_exit_4_and_apply_refuses_them():
    await seed_everything()
    only_aliases = "opnexus_config: 1\naliases: []\n"
    code, out, _ = await cli("plan", "--prune", stdin=only_aliases)
    assert code == 4 and "! aliases WEB_SERVERS  blockiert (entfernen): wird noch verwendet" in out
    code, out, err = await cli("apply", "--prune", "--plan-token", token_of(out), stdin=only_aliases)
    assert code == 4 and "blockierte Objekte" in err


async def test_kinds_schema_and_usage():
    await seed_everything()
    code, out, _ = await cli("export", "--kinds", "aliases")
    assert code == 0 and list(yaml.safe_load(out)) == ["opnexus_config", "aliases"]
    assert (await cli("export", "--kinds", "nope"))[0] == 1
    code, out, _ = await cli("schema")
    assert code == 0 and json.loads(out)["title"].startswith("OPNexus-Konfiguration")
    assert (await cli("frobnicate"))[0] == 1
