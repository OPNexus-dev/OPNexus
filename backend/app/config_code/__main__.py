"""Host-Konsole: `python -m app.config_code {export|plan|apply|schema}`.

Normalerweise über `opnexus export` und `opnexus apply` auf dem Docker-Host aufgerufen. Wer die Konsole des Hosts
bedient, ist ohnehin allmächtig, deshalb braucht das CLI kein Token; der Akteur im Audit ist `cli (Host-Konsole)`.
Die Datei kommt über stdin (das Backend öffnet nie eine Datei), das Ergebnis geht nach stdout.

Exit-Codes (für CI): 0 angewendet bzw. keine Unterschiede, 2 Unterschiede (nur `plan`), 3 Datei ungültig,
4 blockiert/veraltet/Selbstprüfung, 1 sonstiger Fehler.
"""
import argparse
import asyncio
import json
import re
import sys
import uuid

from app.config_code import apply as applier
from app.config_code import plan as planner
from app.config_code import schema
from app.config_code.export import export_yaml
from app.config_code.loader import MAX_BYTES, ConfigError, display, parse

ACTOR = "cli (Host-Konsole)"
TOKEN_MARK = "# plan-token: "
_ANSI = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

ERRORS_DE = {
    "yaml_alias_forbidden": "Aliase (*name) sind nicht erlaubt",
    "yaml_anchor_forbidden": "Anker (&name) sind nicht erlaubt",
    "yaml_merge_forbidden": "Merge-Schlüssel (<<) sind nicht erlaubt",
    "yaml_tag_forbidden": "Explizite Tags (!, !!) sind nicht erlaubt",
    "yaml_directive_forbidden": "Direktiven (%YAML, %TAG) sind nicht erlaubt",
    "yaml_multiple_documents": "Mehr als ein YAML-Dokument",
    "yaml_empty": "Die Datei ist leer",
    "yaml_syntax": "YAML-Syntaxfehler",
    "duplicate_key": "Doppelter Schlüssel",
    "non_string_key": "Schlüssel muss Text sein",
    "too_deep": "Zu tief verschachtelt",
    "too_many_nodes": "Zu viele Elemente",
    "scalar_too_long": "Wert zu lang",
    "too_many_objects": "Zu viele Objekte",
    "too_many_entries": "Zu viele Einträge",
    "too_many_variables": "Zu viele Variablen für diesen Host",
    "too_large": "Datei zu groß",
    "bad_encoding": "Kein gültiges UTF-8",
    "not_a_mapping": "Die Datei muss ein Mapping sein",
    "missing_version": "opnexus_config: 1 fehlt",
    "unsupported_version": "Diese Version des Dateiformats ist unbekannt",
    "unknown_field": "Unbekanntes Feld",
    "missing_field": "Pflichtfeld fehlt",
    "bad_type": "Falscher Typ",
    "bad_value": "Ungültiger Wert",
    "bad_format": "Falsches Format",
    "too_short": "Zu kurz",
    "too_long": "Zu lang",
    "control_character": "Steuerzeichen nicht erlaubt",
    "surrounding_whitespace": "Leerzeichen am Rand nicht erlaubt",
    "bad_variable_name": "Ungültiger Variablenname",
    "bad_variable_value": "Ungültiger Variablenwert",
    "duplicate_object_key": "Schlüssel kommt zweimal in der Datei vor",
    "duplicate_entry": "Eintrag kommt doppelt vor",
    "unknown_host": "Unbekannter Host",
    "unknown_group": "Unbekannte Gerätegruppe",
    "unknown_hierarchy_group": "Unbekannte Hierarchiegruppe",
    "hierarchy_cycle": "Zyklus im Hierarchiebaum",
    "host_in_two_hierarchy_groups": "Host in zwei Hierarchiegruppen",
    "placeholder_not_supported": "Platzhalter {{…}} sind in Hierarchie-Regeln nicht möglich",
}
REASONS_DE = {
    "busy_deployment": "offenes Deployment (erst bestätigen oder zurückrollen)",
    "prune_deployed": "war ausgerollt (zuerst in der Oberfläche entfernen)",
    "prune_referenced": "wird noch verwendet",
}
WARNINGS_DE = {
    "redeploy_needed": "{count} geänderte Objekte sind ausgerollt: danach über die Vorschau neu ausrollen",
    "targets_without_link": "{count} neue Objekte haben Ziele, aber keine Verknüpfung zu Objekten auf den Firewalls (Duplikate beim Ausrollen möglich)",
    "unknown_alias_reference": "{value}: kein zentraler Alias in OPNexus (auf der Firewall von Hand angelegt? dann in Ordnung)",
    "variable_removed_in_use": "Variable {name} von {host} wird entfernt, ein Objekt nutzt sie noch",
    "target_host_disabled": "Ziel-Host {host} ist deaktiviert",
}
SYMBOL = {"create": "+", "update": "~", "delete": "-", "blocked": "!"}
LABEL = {"create": "(neu)", "delete": "(entfernen)"}


def safe(text) -> str:
    """Nothing from a foreign file reaches the terminal as a control sequence."""
    return _ANSI.sub("?", str(text))


def _short(value) -> str:
    return safe(display(json.dumps(value, ensure_ascii=False), 60))


def format_errors(errors: list[dict]) -> str:
    lines = []
    for e in errors:
        where = f"Zeile {e['line']}: " if e.get("line") else ""
        path = f"{safe(e['path'])}: " if e.get("path") else ""
        detail = ""
        params = {k: v for k, v in (e.get("params") or {}).items() if k in ("value", "field", "key", "host", "group", "max", "probes")}
        if params:
            detail = " (" + ", ".join(f"{k}={safe(v)}" for k, v in params.items()) + ")"
        lines.append(f"  {where}{path}{ERRORS_DE.get(e['code'], e['code'])}{detail}")
    return "\n".join(lines)


def format_plan(public: dict) -> str:
    lines = []
    for item in public["items"]:
        action = item["action"]
        planned = item["planned_action"]
        name = f"{item['kind']} {safe(item['key'])}"
        suffix = "" if item["deployed_hosts"] == 0 else f"  [auf {item['deployed_hosts']} Firewall(s) ausgerollt]"
        if action == "blocked":
            reasons = "; ".join(REASONS_DE.get(r, r) for r in item["reasons"])
            lines.append(f"! {name}  blockiert {LABEL.get(planned, '(ändern)')}: {reasons}")
        elif planned in LABEL:
            lines.append(f"{SYMBOL[planned]} {name}  {LABEL[planned]}{suffix}")
        else:
            for change in item["changes"] or [{"field": "", "before": None, "after": None}]:
                if change["field"]:
                    lines.append(f"~ {name}  {safe(change['field'])}: {_short(change['before'])} → {_short(change['after'])}{suffix}")
                else:
                    lines.append(f"~ {name}{suffix}")
            if item.get("implicit"):
                lines[-1] += "  (Folge der Reihenfolge)"
    for warning in public["warnings"]:
        template = WARNINGS_DE.get(warning["code"])
        params = {k: safe(v) for k, v in (warning.get("params") or {}).items()}
        text = template.format(**params) if template else warning["code"]
        where = f"{safe(warning['path'])}: " if warning.get("path") else ""
        lines.append(f"? {where}{text}")
    s = public["summary"]
    lines.append(f"Plan: {s['create']} neu, {s['update']} geändert, {s['delete']} entfernt, {s['unchanged']} unverändert, {s['blocked']} blockiert")
    return "\n".join(lines)


def _read_stdin() -> bytes:
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    return data


async def run(argv: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). Separate from main() so tests can call it inside their event loop."""
    parser = argparse.ArgumentParser(prog="python -m app.config_code", add_help=False)
    parser.add_argument("command", choices=["export", "plan", "apply", "schema"])
    parser.add_argument("--kinds", default=None)
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--plan-token", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1, "", "Aufruf: python -m app.config_code {export|plan|apply|schema}\n"
    if args.command == "schema":
        return 0, schema.json_schema_text(), ""

    from app.database import SessionLocal
    from app.services import audit_service

    context = audit_service.context.set({"actor": ACTOR, "id": str(uuid.uuid4()), "action": args.command})
    try:
        async with SessionLocal() as db:
            if args.command == "export":
                wanted = None
                if args.kinds:
                    wanted = {k.strip() for k in args.kinds.split(",") if k.strip()}
                    if not wanted <= set(schema.KINDS):
                        return 1, "", f"Unbekannte Art in --kinds. Möglich: {', '.join(schema.KINDS)}\n"
                return 0, await export_yaml(db, wanted), ""

            raw = stdin if stdin is not None else _read_stdin()
            try:
                parsed = parse(raw)
                if args.command == "plan":
                    plan = await planner.build_plan(db, parsed, args.prune)
                    await db.rollback()
                    public = plan.public()
                    out = json.dumps(public, ensure_ascii=False, indent=2) + "\n" if args.json else (
                        format_plan(public) + f"\n{TOKEN_MARK}{plan.token}\n")
                    s = public["summary"]
                    if not plan.applicable:
                        return 4, out, ""
                    return (2 if s["create"] + s["update"] + s["delete"] else 0), out, ""

                if not args.plan_token:
                    return 1, "", "apply braucht --plan-token (opnexus apply zeigt erst den Plan und fragt dann).\n"
                try:
                    plan = await applier.apply_config(db, parsed, args.prune, args.plan_token)
                except applier.PlanStale:
                    await db.rollback()
                    return 4, "", "Seit dem Plan hat sich etwas geändert. Nichts wurde übernommen. Bitte neu planen.\n"
                except applier.PlanBlocked as exc:
                    await db.rollback()
                    return 4, format_plan(exc.plan.public()) + "\n", "Der Plan enthält blockierte Objekte. Nichts wurde übernommen.\n"
                except applier.SelfCheckFailed as exc:
                    await db.rollback()
                    return 4, "", f"Selbstprüfung fehlgeschlagen, zurückgenommen: {', '.join(exc.differences[:10])}\n"
                except Exception:
                    await db.rollback()
                    raise
                await db.commit()
                s = plan.summary()
                if args.json:
                    return 0, json.dumps({"summary": s, "sha256": parsed.sha256}, ensure_ascii=False) + "\n", ""
                return 0, (f"Übernommen: {s['create']} neu, {s['update']} geändert, {s['delete']} entfernt. "
                           "Es wurde nichts auf Firewalls geschrieben: die Entwürfe jetzt über Vorschau → Ausrollen → Bestätigen ausrollen.\n"), ""
            except ConfigError as exc:
                if args.json:
                    return 3, json.dumps({"code": exc.code, "errors": exc.errors}, ensure_ascii=False, indent=2) + "\n", ""
                return 3, "", f"Die Datei ist ungültig:\n{format_errors(exc.errors)}\n"
    finally:
        audit_service.context.reset(context)


def main(argv: list[str]) -> int:
    try:
        code, out, err = asyncio.run(run(argv))
    except Exception as exc:  # noqa: BLE001  the host CLI shows one line, details are in the container log
        sys.stderr.write(f"Fehler: {safe(exc)[:300]}\n")
        return 1
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
