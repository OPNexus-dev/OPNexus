"""Language of the texts that end up on a console (the `opnexus` host command runs `python -m app.…` modules in the
backend container; start-up messages of the container itself).

Same rule as the `opnexus` command and the updater: OPNEXUS_LANG=de|en, otherwise LC_ALL / LC_MESSAGES / LANG
("de…" = German), English for everything else — which includes a container without a locale. The `opnexus` command passes
its own language on (`-e OPNEXUS_LANG=…`), so a German host console gets German texts."""
import os


def language() -> str:
    value = os.environ.get("OPNEXUS_LANG") or os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "de" if value.lower().startswith("de") else "en"


def t(german: str, english: str) -> str:
    return german if language() == "de" else english
