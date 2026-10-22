"""Mutation check helper (run manually, not collected by pytest).

    python tests/mutation_check.py tests/mutations_gui.json

Each mutation: {"file": ..., "old": ..., "new": ..., "tests": "pytest selection"}.
The file is patched, the selected tests must FAIL, then the file is restored.
"""
import json
import shlex
import subprocess
import sys
from pathlib import Path


def main(spec_path: str) -> int:
    mutations = json.loads(Path(spec_path).read_text())
    survivors = []
    for i, m in enumerate(mutations, 1):
        path = Path(m["file"])
        original = path.read_text()
        if original.count(m["old"]) != 1:
            print(f"[{i}] SKIP (anchor not unique/found): {m['file']}: {m['old'][:60]!r}")
            survivors.append(m)
            continue
        try:
            path.write_text(original.replace(m["old"], m["new"]))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *shlex.split(m["tests"])],
                                    capture_output=True, text=True)
        finally:
            path.write_text(original)
        killed = result.returncode != 0
        print(f"[{i}] {'KILLED ' if killed else 'SURVIVED'} {m.get('name', m['old'][:50])}")
        if not killed:
            survivors.append(m)
    print(f"{len(mutations) - len(survivors)}/{len(mutations)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
