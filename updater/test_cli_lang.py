"""Language of the `opnexus` CLI: German with a German locale, English otherwise (OPNEXUS_LANG wins)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus")
EMPTY = tempfile.mkdtemp()  # an install directory with an empty .env and no docker-compose.yml
Path(EMPTY, ".env").touch()


def run(args, **env):
    base = {k: v for k, v in os.environ.items() if k not in ("OPNEXUS_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
    return subprocess.run(["bash", str(SCRIPT), *args], env={**base, "OPNEXUS_DIR": EMPTY, **env},
                          capture_output=True, text=True, timeout=30)


class CliLanguage(unittest.TestCase):
    def test_default_is_english(self):
        r = run(["status"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Error:", r.stderr)
        self.assertIn("not found", r.stderr)

    def test_german_locale_gives_german(self):
        for env in ({"LANG": "de_DE.UTF-8"}, {"LC_ALL": "de_AT.UTF-8"}, {"OPNEXUS_LANG": "de"}):
            r = run(["status"], **env)
            self.assertIn("Fehler:", r.stderr, env)
            self.assertIn("nicht gefunden", r.stderr, env)

    def test_opnexus_lang_overrides_locale(self):
        self.assertIn("Error:", run(["status"], LANG="de_DE.UTF-8", OPNEXUS_LANG="en").stderr)
        self.assertIn("Fehler:", run(["status"], LANG="en_US.UTF-8", OPNEXUS_LANG="de").stderr)

    def test_other_locales_give_english(self):
        for lang in ("C", "fr_FR.UTF-8", "en_GB.UTF-8"):
            self.assertIn("Error:", run(["status"], LANG=lang).stderr, lang)

    def test_usage_follows_language(self):
        en = run([], OPNEXUS_LANG="en")
        de = run([], OPNEXUS_LANG="de")
        self.assertEqual((en.returncode, de.returncode), (1, 1))
        self.assertIn("Undo the last update", en.stdout)
        self.assertNotIn("rückgängig", en.stdout)
        self.assertIn("Letztes Update rückgängig machen", de.stdout)

    def test_every_message_has_both_languages(self):
        """Each say/die call with a German text must carry an English one (second argument)."""
        import re
        call = re.compile(r'\b(?:say|die)\s+"((?:[^"\\]|\\.)*)"(\s+"(?:[^"\\]|\\.)*")?')
        missing = []
        for n, line in enumerate(SCRIPT.read_text(encoding="utf-8").split("\n"), 1):
            if line.lstrip().startswith("#") or n < 96:
                continue
            for m in call.finditer(line):
                # calls with a nested "$(… "x" …)" are not parsed whole: accept them when a second string follows
                if re.search(r"[äöüßÄÖÜ]", m.group(1)) and not m.group(2) and not re.search(r'"\s+"[A-Z(]', line[m.end():]):
                    missing.append(n)
        self.assertEqual(missing, [], "German message without English text in line(s) %s" % missing)


if __name__ == "__main__":
    unittest.main()
