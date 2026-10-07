#!/usr/bin/env python3
"""Consistency check for the DE/EN catalogs (frontend/lib/i18n/{de,en}.json).

Run from the repository root:  python3 scripts/check_i18n.py
Exits non-zero if
  * the two catalogs differ in keys or in {placeholders},
  * a backend ErrorCode has no `errors.<CODE>` entry,
  * the frontend references a static key (`t("a.b")`, `errorMessage(e, "a.b")`)
    that does not exist.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def flatten(node, prefix=""):
    out = {}
    for key, value in node.items():
        if isinstance(value, dict):
            out.update(flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


de = flatten(json.loads((FRONTEND / "lib/i18n/de.json").read_text(encoding="utf-8")))
en = flatten(json.loads((FRONTEND / "lib/i18n/en.json").read_text(encoding="utf-8")))
problems = []

for key in sorted(set(de) ^ set(en)):
    problems.append(f"key only in {'de' if key in de else 'en'}: {key}")
for key in sorted(set(de) & set(en)):
    if sorted(re.findall(r"\{(\w+)\}", de[key])) != sorted(re.findall(r"\{(\w+)\}", en[key])):
        problems.append(f"placeholder mismatch: {key}")
    if not de[key].strip() or not en[key].strip():
        problems.append(f"empty text: {key}")

codes = re.findall(r'^\s+\w+ = "(\w+)"', (ROOT / "backend/app/errors.py").read_text(encoding="utf-8"), re.M)
for code in codes:
    for lang, catalog in (("de", de), ("en", en)):
        if f"errors.{code}" not in catalog:
            problems.append(f"ErrorCode {code} missing in {lang}.json")

sources = [p for d in ("app", "components", "lib") for p in (FRONTEND / d).rglob("*.ts*") if "i18n" not in p.parts]
used = set()
for path in sources:
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"""\b(?:t|translate)\(\s*(?:language\s*,\s*)?(["'])([^"'`$]+?)\1""", text):
        used.add(match.group(2))
    for match in re.finditer(r"""(?:errorMessage|errRef\.current)\([^,()]+,\s*(["'])([^"']+)\1""", text):
        used.add(match.group(2))
for key in sorted(used):
    if key not in de or key not in en:
        problems.append(f"referenced but missing: {key}")

print(f"{len(de)} keys, {len(codes)} error codes, {len(used)} statically referenced keys")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("i18n catalogs consistent")
