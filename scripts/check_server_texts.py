#!/usr/bin/env python3
"""Guard for the English edition: German prose must not slip back in.

Run from the repository root:  python3 scripts/check_server_texts.py   (needs Node and `npm ci` in frontend/)
Exits non-zero if

  * the backend sends a German sentence that the web interface cannot translate (lookup in
    frontend/lib/i18n/serverMessages.ts; German is the source text of server prose, see translateServerText),
  * a log message in backend/app is still German (logs are English),
  * a console message in the backend or updater is German-only (it has to go through `t(german, english)`).

Not checked on purpose: docstrings and comments, the bilingual halves of t()/_page() calls, the German source tables
(*_DE dicts, notification labels, notification_i18n.py) whose English side is checked by their own tests.
"""
import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
STRONG = re.compile(r"[äöüÄÖÜß]|\b(nicht|wurde|werden|wird|konnte|bitte|keine|kein|noch|prüfen|bereits|sind|und|oder|dem|den|mit|von|nach|wenn|weil|erst|gesetzt|angelegt|gespeichert|fehlgeschlagen|erreichbar)\b")
WORDS = re.compile(r"[A-Za-zÄÖÜäöüß]{2,}")
SKIP_FILES = {"backend/app/services/notification_i18n.py", "backend/app/gui_gateway.py"}
SKIP_CALLS = {"t", "T", "_t", "_page"}
SKIP_TARGETS = re.compile(r"(_DE|_LABELS|LABELS_DE)$")
# Not prose for a person: protocol text, API field help written in English on purpose.
SKIP_TEXTS = {"CONNECT {}:{} HTTP/1.1\r\nHost: {}:{}\r\n\r\n",
              "alter Primaer ist aus",   # the typed confirmation sentence (the English one is next to it), not shown via the table
              "Verbindung zu"}           # matched against the text of an error to classify it, never shown


def _words(node, out):
    if isinstance(node, dict):
        for value in node.values():
            _words(value, out)
    elif isinstance(node, str):
        out.update(w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß]{5,}", node))


def german_only_words() -> set:
    """Words the German catalog uses and the English one does not: a sentence made of them is German, even without an umlaut."""
    de, en = set(), set()
    _words(json.loads((ROOT / "frontend/lib/i18n/de.json").read_text(encoding="utf-8")), de)
    _words(json.loads((ROOT / "frontend/lib/i18n/en.json").read_text(encoding="utf-8")), en)
    return de - en


GERMAN_ONLY = german_only_words()


def looks_german(text: str) -> bool:
    if re.match(r"\s*(ALTER TABLE|SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|/|\^|[A-Z_]+=)", text):
        return False                       # SQL, URL paths, regular expressions, KEY=value: not prose
    if STRONG.search(text):
        return True
    return sum(1 for w in re.findall(r"[A-Za-zÄÖÜäöüß]{5,}", text) if w.lower() in GERMAN_ONLY) >= 1


def files():
    names = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "backend/app", "updater"], text=True).split()
    return [n for n in names if n.endswith(".py") and "/tests/" not in n and "/test_" not in n and "migrations/" not in n and n not in SKIP_FILES]


def collect():
    log, shown, console = [], [], []
    for name in files():
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and not isinstance(parents.get(node), ast.JoinedStr):
                text = node.value
            else:
                continue
            if len(text) < 8 or not looks_german(text):
                continue
            if re.fullmatch(r"[\w./:-]+", text) or text in SKIP_TEXTS:
                continue
            kind, up = "value", node
            while up in parents:
                up = parents[up]
                if isinstance(up, ast.Expr) and isinstance(parents.get(up), (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "docstring"; break
                if isinstance(up, (ast.Assign, ast.AnnAssign)):
                    target = up.targets[0] if isinstance(up, ast.Assign) else up.target
                    if SKIP_TARGETS.search(getattr(target, "id", "")):
                        kind = "table"; break
                if isinstance(up, ast.Call):
                    fn = ast.unparse(up.func)
                    if fn in SKIP_CALLS or fn.endswith(".t"):
                        kind = "bilingual"; break
                    if re.match(r"^(logger|log|logging)\.\w+$", fn):
                        kind = "log"; break
                    if fn == "print":
                        kind = "console"; break
                if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    break
            if kind in ("docstring", "bilingual", "table"):
                continue
            item = {"file": name, "line": node.lineno, "text": text, "sample": text.replace("{}", "X")}
            (log if kind == "log" else console if kind == "console" else shown).append(item)
    return log, shown, console


def lookup(samples: list) -> subprocess.CompletedProcess:
    """Ask the real translation code of the web interface (frontend/lib/i18n) which samples it cannot translate.
    TypeScript is needed to read it: from frontend/node_modules, else (the release host has none) from a throw-away
    node container that installs it without running any install script - nothing is installed on the host."""
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "samples.json"
        path.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
        script = ROOT / "scripts/check_server_texts.cjs"
        if (ROOT / "frontend/node_modules/typescript").exists():
            return subprocess.run(["node", str(script), str(path)], capture_output=True, text=True)
        if shutil.which("docker"):
            spec = json.loads((ROOT / "frontend/package.json").read_text())["devDependencies"].get("typescript", "5")
            inner = (f"npm install --no-save --no-audit --no-fund --ignore-scripts --prefix /tmp/ts 'typescript@{spec}' >/dev/null 2>&1 "
                     "&& TS_MODULE=/tmp/ts/node_modules/typescript node scripts/check_server_texts.cjs /samples/samples.json")
            os.chmod(tmp, 0o755)
            os.chmod(path, 0o644)
            return subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/repo:ro", "-v", f"{tmp}:/samples:ro", "-w", "/repo",
                                   "node:20-alpine", "sh", "-c", inner], capture_output=True, text=True)
        return subprocess.CompletedProcess([], 2, "", "TypeScript not found: run `npm ci` in frontend/ first (or install Docker).")


def main() -> int:
    log, shown, console = collect()
    problems = [f"{i['file']}:{i['line']}: German log message: {i['text'][:90]!r}" for i in log]
    problems += [f"{i['file']}:{i['line']}: German-only console text, use t(german, english): {i['text'][:90]!r}" for i in console]
    updater = [i for i in shown if i["file"].startswith("updater/")]
    problems += [f"{i['file']}:{i['line']}: German-only text, use t(german, english): {i['text'][:90]!r}" for i in updater]
    server = [i for i in shown if not i["file"].startswith("updater/")]
    result = lookup(server)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return 2
    for item in json.loads(result.stdout):
        problems.append(f"{item['file']}:{item['line']}: not translated in frontend/lib/i18n/serverMessages.ts: {item['text'][:90]!r}")
    if problems:
        print("\n".join(problems))
        print(f"\n{len(problems)} German texts without an English counterpart.")
        return 1
    print(f"server texts consistent ({len(server)} prose texts translatable, no German logs or console texts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
