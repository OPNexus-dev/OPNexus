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

from app.cli_lang import language, t
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
ERRORS_EN = {
    "yaml_alias_forbidden": "Aliases (*name) are not allowed",
    "yaml_anchor_forbidden": "Anchors (&name) are not allowed",
    "yaml_merge_forbidden": "Merge keys (<<) are not allowed",
    "yaml_tag_forbidden": "Explicit tags (!, !!) are not allowed",
    "yaml_directive_forbidden": "Directives (%YAML, %TAG) are not allowed",
    "yaml_multiple_documents": "More than one YAML document",
    "yaml_empty": "The file is empty",
    "yaml_syntax": "YAML syntax error",
    "duplicate_key": "Duplicate key",
    "non_string_key": "Key must be text",
    "too_deep": "Nested too deeply",
    "too_many_nodes": "Too many elements",
    "scalar_too_long": "Value too long",
    "too_many_objects": "Too many objects",
    "too_many_entries": "Too many entries",
    "too_many_variables": "Too many variables for this host",
    "too_large": "File too large",
    "bad_encoding": "Not valid UTF-8",
    "not_a_mapping": "The file must be a mapping",
    "missing_version": "opnexus_config: 1 is missing",
    "unsupported_version": "This version of the file format is unknown",
    "unknown_field": "Unknown field",
    "missing_field": "Required field missing",
    "bad_type": "Wrong type",
    "bad_value": "Invalid value",
    "bad_format": "Wrong format",
    "too_short": "Too short",
    "too_long": "Too long",
    "control_character": "Control characters not allowed",
    "surrounding_whitespace": "Leading or trailing spaces not allowed",
    "bad_variable_name": "Invalid variable name",
    "bad_variable_value": "Invalid variable value",
    "duplicate_object_key": "Key appears twice in the file",
    "duplicate_entry": "Entry appears twice",
    "unknown_host": "Unknown host",
    "unknown_group": "Unknown device group",
    "unknown_hierarchy_group": "Unknown hierarchy group",
    "hierarchy_cycle": "Cycle in the hierarchy tree",
    "host_in_two_hierarchy_groups": "Host in two hierarchy groups",
    "placeholder_not_supported": "Placeholders {{…}} are not possible in hierarchy rules",
}
REASONS_EN = {
    "busy_deployment": "open deployment (confirm or roll back first)",
    "prune_deployed": "was deployed (remove it in the interface first)",
    "prune_referenced": "is still in use",
}
WARNINGS_EN = {
    "redeploy_needed": "{count} changed objects are deployed: deploy them again afterwards via the preview",
    "targets_without_link": "{count} new objects have targets but no link to objects on the firewalls (duplicates possible when deploying)",
    "unknown_alias_reference": "{value}: not a central alias in OPNexus (created by hand on the firewall? then this is fine)",
    "variable_removed_in_use": "Variable {name} of {host} will be removed, an object still uses it",
    "target_host_disabled": "Target host {host} is disabled",
}
SYMBOL = {"create": "+", "update": "~", "delete": "-", "blocked": "!"}
LABEL_DE = {"create": "(neu)", "delete": "(entfernen)"}
LABEL_EN = {"create": "(new)", "delete": "(remove)"}


def _pick(german: dict, english: dict) -> dict:
    return german if language() == "de" else english


def safe(text) -> str:
    """Nothing from a foreign file reaches the terminal as a control sequence."""
    return _ANSI.sub("?", str(text))


def _short(value) -> str:
    return safe(display(json.dumps(value, ensure_ascii=False), 60))


def format_errors(errors: list[dict]) -> str:
    lines = []
    for e in errors:
        where = t(f"Zeile {e['line']}: ", f"Line {e['line']}: ") if e.get("line") else ""
        path = f"{safe(e['path'])}: " if e.get("path") else ""
        detail = ""
        params = {k: v for k, v in (e.get("params") or {}).items() if k in ("value", "field", "key", "host", "group", "max", "probes")}
        if params:
            detail = " (" + ", ".join(f"{k}={safe(v)}" for k, v in params.items()) + ")"
        lines.append(f"  {where}{path}{_pick(ERRORS_DE, ERRORS_EN).get(e['code'], e['code'])}{detail}")
    return "\n".join(lines)


def format_plan(public: dict) -> str:
    lines = []
    LABEL = _pick(LABEL_DE, LABEL_EN)
    for item in public["items"]:
        action = item["action"]
        planned = item["planned_action"]
        name = f"{item['kind']} {safe(item['key'])}"
        suffix = "" if item["deployed_hosts"] == 0 else t(f"  [auf {item['deployed_hosts']} Firewall(s) ausgerollt]", f"  [deployed on {item['deployed_hosts']} firewall(s)]")
        if action == "blocked":
            reasons = "; ".join(_pick(REASONS_DE, REASONS_EN).get(r, r) for r in item["reasons"])
            lines.append(f"! {name}  " + t("blockiert", "blocked") + f" {LABEL.get(planned, t('(ändern)', '(change)'))}: {reasons}")
        elif planned in LABEL:
            lines.append(f"{SYMBOL[planned]} {name}  {LABEL[planned]}{suffix}")
        else:
            for change in item["changes"] or [{"field": "", "before": None, "after": None}]:
                if change["field"]:
                    lines.append(f"~ {name}  {safe(change['field'])}: {_short(change['before'])} → {_short(change['after'])}{suffix}")
                else:
                    lines.append(f"~ {name}{suffix}")
            if item.get("implicit"):
                lines[-1] += t("  (Folge der Reihenfolge)", "  (follows from the order)")
    for warning in public["warnings"]:
        template = _pick(WARNINGS_DE, WARNINGS_EN).get(warning["code"])
        params = {k: safe(v) for k, v in (warning.get("params") or {}).items()}
        text = template.format(**params) if template else warning["code"]
        where = f"{safe(warning['path'])}: " if warning.get("path") else ""
        lines.append(f"? {where}{text}")
    s = public["summary"]
    lines.append(t(f"Plan: {s['create']} neu, {s['update']} geändert, {s['delete']} entfernt, {s['unchanged']} unverändert, {s['blocked']} blockiert",
                   f"Plan: {s['create']} new, {s['update']} changed, {s['delete']} removed, {s['unchanged']} unchanged, {s['blocked']} blocked"))
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
        return 1, "", t("Aufruf: python -m app.config_code {export|plan|apply|schema}\n", "Usage: python -m app.config_code {export|plan|apply|schema}\n")
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
                        return 1, "", t(f"Unbekannte Art in --kinds. Möglich: {', '.join(schema.KINDS)}\n", f"Unknown kind in --kinds. Possible: {', '.join(schema.KINDS)}\n")
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
                    return 1, "", t("apply braucht --plan-token (opnexus apply zeigt erst den Plan und fragt dann).\n", "apply needs --plan-token (opnexus apply shows the plan first and then asks).\n")
                try:
                    plan = await applier.apply_config(db, parsed, args.prune, args.plan_token)
                except applier.PlanStale:
                    await db.rollback()
                    return 4, "", t("Seit dem Plan hat sich etwas geändert. Nichts wurde übernommen. Bitte neu planen.\n", "Something changed since the plan. Nothing was applied. Please plan again.\n")
                except applier.PlanBlocked as exc:
                    await db.rollback()
                    return 4, format_plan(exc.plan.public()) + "\n", t("Der Plan enthält blockierte Objekte. Nichts wurde übernommen.\n", "The plan contains blocked objects. Nothing was applied.\n")
                except applier.SelfCheckFailed as exc:
                    await db.rollback()
                    return 4, "", t(f"Selbstprüfung fehlgeschlagen, zurückgenommen: {', '.join(exc.differences[:10])}\n", f"Self-check failed, rolled back: {', '.join(exc.differences[:10])}\n")
                except Exception:
                    await db.rollback()
                    raise
                await db.commit()
                s = plan.summary()
                if args.json:
                    return 0, json.dumps({"summary": s, "sha256": parsed.sha256}, ensure_ascii=False) + "\n", ""
                return 0, t(f"Übernommen: {s['create']} neu, {s['update']} geändert, {s['delete']} entfernt. "
                            "Es wurde nichts auf Firewalls geschrieben: die Entwürfe jetzt über Vorschau → Ausrollen → Bestätigen ausrollen.\n",
                            f"Applied: {s['create']} new, {s['update']} changed, {s['delete']} removed. "
                            "Nothing was written to firewalls: now deploy the drafts via Preview → Deploy → Confirm.\n"), ""
            except ConfigError as exc:
                if args.json:
                    return 3, json.dumps({"code": exc.code, "errors": exc.errors}, ensure_ascii=False, indent=2) + "\n", ""
                return 3, "", t("Die Datei ist ungültig:\n", "The file is invalid:\n") + f"{format_errors(exc.errors)}\n"
    finally:
        audit_service.context.reset(context)


def main(argv: list[str]) -> int:
    try:
        code, out, err = asyncio.run(run(argv))
    except Exception as exc:  # noqa: BLE001  the host CLI shows one line, details are in the container log
        sys.stderr.write(t(f"Fehler: {safe(exc)[:300]}\n", f"Error: {safe(exc)[:300]}\n"))
        return 1
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
