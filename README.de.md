# OPNexus

**Alle OPNsense-Firewalls. Eine Konsole.**
Zentrale Verwaltung mehrerer [OPNsense](https://opnsense.org)-Firewalls: Regeln, Aliase, NAT, VPN, Zertifikate, Logs und Updates — per OPNsense-API ausgerollt, mit Vorschau, Bestätigung und automatischem Rollback.

[Website](https://opnexus.dev/) · [Changelog](CHANGELOG.md) · [Handbuch](docs/HANDBUCH.md) · [English](README.md)

![Zentrale Firewall-Regeln](website/assets/screenshots/de/rules-light-desktop.webp)

> **Stand:** `1.10.1-rc2` ist der aktuelle öffentliche Release-Candidate (er löst `1.10.0-rc1`/`-rc2` ab). Entwickelt wurde gegen echte OPNsense-Instanzen, eine frische Installation aus den veröffentlichten Images ist aber noch nicht im Feld erprobt — bitte Ecken und Kanten melden.

## Funktionen

- **Flotte & Hochverfügbarkeit** — Live-Status aller Hosts, CARP-Rollen nebeneinander, Ereignis-Historie, Alarm bei Split-Brain oder fehlendem MASTER, Wartungsmodus mit optionalem Auto-Revert-Timer.
- **Regeln, Aliase & NAT** — zentral gepflegt, gezielt auf Hosts oder Gerätegruppen ausgerollt; Port-Forward, 1:1- und Outbound-NAT mit Drift-Erkennung zwischen Cluster-Knoten.
- **Sichere Änderungen** — jede Änderung läuft *Prüfen → Ausrollen → Bestätigen*; bleibt die Bestätigung im Zeitfenster aus, rollt OPNexus zurück.
- **Hierarchische Policies** — Pre-/Post-Rulebases mit Vererbung über Gruppenbäume.
- **VPN** — IPsec-Site-to-Site, OpenVPN und WireGuard, einmal definiert und ausgerollt.
- **Zertifikate** — Trust-Store für alle Firewalls und Ausstellung per ACME / Let's Encrypt.
- **Logs & Audit** — zentrale Log-Suche mit Korrelation, Dashboards und lückenloser Deploy-Verlauf.
- **Update-Center** — Firmware-Updates für die Flotte, mit Blick auf HA-Reihenfolge und Wartungsmodus.
- **Zugang** — HTTPS mit gepinnten Firewall-Zertifikaten, optionale Zwei-Faktor-Anmeldung, Rollen (Admin / Nur-Lesen), Tastaturbedienung, Dark Mode, Oberfläche auf Deutsch und Englisch.

Alle Screenshots zeigen fiktive Beispieldaten.

## Schnellstart

Voraussetzung: Docker mit Compose-Plugin.

```bash
git clone https://git.opnexus.dev/opnexus/opnexus.git
cd opnexus
cp .env.example .env        # POSTGRES_PASSWORD (und ADMIN_PASSWORD) setzen
docker compose up -d --build
```

Die Oberfläche läuft dann unter `http://localhost:3100`. Ohne `ADMIN_PASSWORD` wird beim ersten Start ein zufälliges Admin-Passwort erzeugt und **einmalig** im Backend-Log ausgegeben:

```bash
docker compose logs backend | grep -A5 "generated password"
```

Für den Zugriff von anderen Rechnern und den Produktivbetrieb mit TLS (mitgelieferter Caddy-Proxy) siehe das [Handbuch](docs/HANDBUCH.md#zugriff-von-außerhalb-dieser-vm--produktivbetrieb-mit-tls).

**Fertige Images** (amd64) liegen in der Registry dieses Gitea. In der `.env`:

```bash
OPNEXUS_IMAGE_REPO=git.opnexus.dev/opnexus
OPNEXUS_VERSION=1.10.1-rc2
```

```bash
docker compose pull && docker compose up -d --no-build
```

Aktualisiert wird danach mit `scripts/opnexus update` / `rollback` (erst ziehen, vor Migrationen ein Datenbank-Backup, automatisches Zurückrollen, wenn die Dienste nicht gesund werden).

## Datenschutz

OPNexus wird selbst betrieben und hat keine Telemetrie. Es verbindet sich nur mit deinen Firewalls und mit Benachrichtigungszielen, die du selbst einrichtest. Der optionale Hinweis auf neue Versionen (`UPDATE_CHECK_URL=https://updates.opnexus.dev/latest.json`) ist **aus**, solange du ihn nicht setzt; eingeschaltet geht höchstens alle 12 Stunden ein einfaches `GET` mit dem User-Agent `OPNexus/<Version>` hinaus — ohne Instanz- oder Nutzerdaten.

## Lizenz

OPNexus ist **quelloffen einsehbar (source-available)** unter der [PolyForm Shield License 1.0.0](LICENSE) — es ist *keine* Open-Source-Software im Sinne der OSI. Du darfst den Code herunterladen, lesen, verändern und nutzen, auch in einem Unternehmen für die eigenen Firewalls. Nicht erlaubt ist, damit ein konkurrierendes Produkt anzubieten (etwa Weiterverkauf oder Betrieb als gehosteter Dienst). Maßgeblich ist der Lizenztext.

## Sicherheit

Sicherheitslücken bitte vertraulich an **security@opnexus.dev** melden, siehe [SECURITY.md](SECURITY.md).

---

OPNexus ist ein unabhängiges Projekt und steht in keiner Verbindung zu Deciso B.V. oder dem OPNsense®-Projekt. OPNsense® ist eine Marke von Deciso B.V.
