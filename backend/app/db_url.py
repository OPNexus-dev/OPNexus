"""Datenbank-URL aus der .env tolerant lesen und Startfehler der Datenbank erklären.

docker-compose.yml setzt DATABASE_URL aus POSTGRES_USER/-PASSWORD/-DB zusammen, ohne
das Passwort zu kodieren. Ein Passwort mit '@', '/', '#', '?' oder ':' machte daraus
eine unlesbare URL (das Backend suchte dann einen Host namens "w#rd…" und startete
endlos neu). `normalize_database_url` kodiert so ein Passwort nachträglich.
"""
from __future__ import annotations

from urllib.parse import quote

# Zeichen, die in einer URL nicht roh im Passwort stehen dürfen. Enthält das Passwort keines davon,
# bleibt die URL unverändert (dann hat sie vorher funktioniert, auch mit %XX-Folgen).
_RESERVED = set("@/#?:[]")


def normalize_database_url(url: str) -> str:
    """Kodiert das Passwort, wenn es rohe Sonderzeichen enthält; sonst unverändert."""
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    # Der Host kann weder '@' noch '/' enthalten: das letzte '@' trennt Zugangsdaten und Adresse.
    userinfo, _, location = rest.rpartition("@")
    user, colon, password = userinfo.partition(":")
    if not colon or not (_RESERVED & set(password)):
        return url
    return f"{scheme}://{user}:{quote(password, safe='')}@{location}"


def explain_database_error(exc: BaseException) -> str | None:
    """Erklärung (Deutsch) für die häufigen Fehler beim ersten Verbindungsaufbau, sonst None."""
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    names = {type(e).__name__ for e in chain}
    text = " ".join(str(e) for e in chain).lower()

    if "InvalidPasswordError" in names or "password authentication failed" in text:
        return (
            "Die Datenbank lehnt das Passwort aus der .env ab. POSTGRES_PASSWORD (und POSTGRES_USER/POSTGRES_DB) gelten "
            "nur beim allerersten Start mit einem leeren Datenbank-Volume. Liegt noch ein Volume eines früheren "
            "Versuchs vor (z. B. opnexus_postgres_data), hat es das alte Passwort behalten.\n"
            "  • Neue Installation ohne Daten: 'docker compose down -v' (löscht die Datenbank!), dann 'docker compose up -d'.\n"
            "  • Vorhandene Daten behalten: in der .env das ursprüngliche Passwort eintragen."
        )
    if "InvalidCatalogNameError" in names or ("database" in text and "does not exist" in text):
        return (
            "Die Datenbank aus POSTGRES_DB gibt es im vorhandenen Volume nicht. POSTGRES_DB gilt nur beim ersten "
            "Start mit leerem Volume. In der .env den ursprünglichen Namen eintragen oder bei einer neuen "
            "Installation ohne Daten 'docker compose down -v' (löscht die Datenbank!) und neu starten."
        )
    if "gaierror" in {n.lower() for n in names} or "name or service not known" in text or "no address associated" in text:
        return (
            "Der Datenbank-Host ist nicht auflösbar. Meist enthält POSTGRES_PASSWORD ein Zeichen, das die "
            "Verbindungsadresse zerlegt, oder DATABASE_URL ist falsch gesetzt. Am einfachsten ein Passwort aus "
            "Buchstaben und Ziffern wählen, z. B. 'openssl rand -hex 24'."
        )
    return None
