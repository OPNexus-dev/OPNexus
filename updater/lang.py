"""Language of the texts the updater/HA agent produce (log lines, errors shown in the web interface).

Same rule as the `opnexus` CLI: OPNEXUS_LANG=de|en, otherwise LC_ALL / LC_MESSAGES / LANG ("de…" = German),
English for everything else, which includes the updater container (it has no locale)."""
import os


def language() -> str:
    value = os.environ.get("OPNEXUS_LANG") or os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "de" if value.lower().startswith("de") else "en"


def t(german: str, english: str) -> str:
    return german if language() == "de" else english
