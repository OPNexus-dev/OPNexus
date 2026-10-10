# OPNexus – Handbuch

OPNexus verwaltet mehrere [OPNsense](https://opnsense.org)-Firewalls an einer
Stelle: Regeln, Aliase, NAT, VPN, Zertifikate, Netzwerk, Logs und Updates. Alle
Änderungen laufen über die OPNsense-API und folgen demselben Ablauf: **prüfen,
ausrollen, bestätigen**. Bleibt die Bestätigung aus, stellt OPNexus den
vorherigen Stand wieder her.

Dieses Handbuch richtet sich an alle, die OPNexus installieren und betreiben.
Eine Kurzfassung steht in der [README](../README.md), die Änderungen je Version im
[Changelog](../CHANGELOG.md).

## Inhalt

1. [Erste Schritte](#erste-schritte)
2. [Zugriff aus dem Netz und TLS](#zugriff-aus-dem-netz-und-tls)
3. [Konfiguration (`.env`)](#konfiguration-env)
4. [Benutzer, Rollen und Anmeldung](#benutzer-rollen-und-anmeldung)
5. [Firewalls anbinden](#firewalls-anbinden)
6. [Regeln sicher ausrollen](#regeln-sicher-ausrollen)
7. [Gerätegruppen und hierarchische Policies](#gerätegruppen-und-hierarchische-policies)
8. [Aliase und NAT](#aliase-und-nat)
9. [Netzwerk und Host-System](#netzwerk-und-host-system)
10. [VPN: IPsec, OpenVPN, WireGuard](#vpn-ipsec-openvpn-wireguard)
11. [Zertifikate und ACME](#zertifikate-und-acme)
12. [HA-Cluster und CARP](#ha-cluster-und-carp)
13. [Wartung und Updates der Firewalls](#wartung-und-updates-der-firewalls)
14. [Überwachung: Dashboard, Logs, Audit, Benachrichtigungen](#überwachung-dashboard-logs-audit-benachrichtigungen)
15. [OPNexus betreiben](#opnexus-betreiben)
16. [Sicherheit](#sicherheit)
17. [Fehlerbehebung](#fehlerbehebung)
18. [Grenzen](#grenzen)
19. [Bedienung, Sprache, Barrierefreiheit](#bedienung-sprache-barrierefreiheit)
20. [Lizenz und Datenschutz](#lizenz-und-datenschutz)

---

## Erste Schritte

### Voraussetzungen

- Ein Server mit **Docker und dem Compose-Plugin**. OPNexus läuft als
  Compose-Stack (Datenbank, Backend, Frontend, TLS-Proxy).
- Eine oder mehrere **OPNsense-Firewalls**, deren Weboberfläche vom OPNexus-Server
  aus per HTTPS erreichbar ist. OPNexus nutzt die REST-API des OPNsense-Kerns; auf
  den Firewalls muss dafür nichts installiert werden. Nur die ACME-Ausstellung
  braucht dort das Plugin `os-acme-client`.
- Je Firewall ein **API-Schlüssel** (siehe [Firewalls anbinden](#firewalls-anbinden)).

### Installation aus dem Release-Bundle

Das Bundle (`opnexus-<version>.tar.gz` auf der Release-Seite) enthält eine
fertige Compose-Datei mit den Release-Images, eine Vorlage für die `.env`, das
Werkzeug `opnexus` und eine Kurzanleitung.

```bash
tar xzf opnexus-<version>.tar.gz && cd opnexus-<version>
cp .env.example .env          # POSTGRES_PASSWORD setzen (und was sonst nötig ist)
docker compose pull && docker compose up -d --no-build
./opnexus password            # zeigt das Admin-Passwort, wenn ADMIN_PASSWORD leer war
```

### Installation aus dem Quellcode

```bash
git clone https://git.opnexus.dev/opnexus/opnexus.git && cd opnexus
cp .env.example .env          # POSTGRES_PASSWORD setzen
docker compose up -d --build
scripts/opnexus password
```

Alternativ lassen sich auch aus einem Checkout die fertigen Images verwenden: in
der `.env` `OPNEXUS_IMAGE_REPO=git.opnexus.dev/opnexus` und `OPNEXUS_VERSION=<version>`
setzen, dann `docker compose pull && docker compose up -d --no-build`.

### Erster Start und Passwort

Ohne `ADMIN_PASSWORD` in der `.env` erzeugt OPNexus beim ersten Start ein
zufälliges Passwort für den Benutzer `ADMIN_USERNAME` (Standard `admin`) und
schreibt es **einmalig** ins Backend-Log. Einfacher ist:

```bash
./opnexus password        # im Repo-Checkout: scripts/opnexus password
```

Das Kommando setzt ein neues Zufallspasswort, zeigt es einmal an und beendet alle
Sitzungen des Kontos. Es funktioniert auch später, wenn das Passwort vergessen
wurde. Eine Rückfrage kommt nur, wenn sich schon jemand angemeldet hat
(`--yes` überspringt sie, ein Benutzername als Argument wählt ein anderes Konto).
Ein altes Passwort lässt sich nicht anzeigen, gespeichert wird nur ein
Argon2id-Hash.

Danach die Oberfläche öffnen: `https://<server-ip>` (lokal auch
`http://localhost:3100`). Der Browser zeigt anfangs eine Zertifikatswarnung, siehe
[Zugriff aus dem Netz und TLS](#zugriff-aus-dem-netz-und-tls).

### Der Einrichtungsassistent

Nach der ersten Anmeldung öffnet sich der **Einrichtungsassistent** von selbst,
solange noch keine Firewall angebunden ist. Er führt in vier Schritten durch das
Nötigste:

1. **Willkommen:** was der Assistent tut, Sprache (Deutsch/Englisch). Er liest nur
   von der Firewall; geändert wird dort erst, wenn du später selbst etwas ausrollst.
2. **Konto:** eigenes Passwort setzen und die Zwei-Faktor-Anmeldung einrichten.
   Beides ist optional, aber empfohlen.
3. **Firewall:** Anleitung für den API-Schlüssel in OPNsense, dann Name, Adresse,
   Schlüssel und Secret eintragen, das Zertifikat abrufen und den Fingerabdruck
   bestätigen. Nach dem Anlegen zeigt der Schritt sofort, ob die Firewall
   antwortet, und bei „offline“, was zu prüfen ist. Weitere Firewalls, auch die
   Knoten eines HA-Paars, trägst du im selben Schritt nacheinander ein.
4. **Fertig:** Zusammenfassung und Hinweise, was als Nächstes sinnvoll ist.

„Assistent überspringen“ merkt sich das für deinen Benutzer. Wieder öffnen
kannst du ihn jederzeit über **System › Einrichtung**, die Einstellungen oder die
Schnellsuche. Nur-Lesen-Benutzer bekommen ihn nicht angeboten.

---

## Zugriff aus dem Netz und TLS

Der mitgelieferte **Caddy-Proxy** ist der einzige Dienst, der im Netz lauscht
(Ports 80 und 443; Port 80 leitet auf HTTPS um). Backend und Frontend sind nur an
`127.0.0.1` gebunden. Browser und API sprechen denselben Host an, deshalb
funktionieren IP-Adresse und Hostname gleichermaßen.

**Zertifikate ohne DNS.** Der Proxy stellt Zertifikate aus einer eigenen internen
CA aus, auch für reine IP-Adressen. Die Root-CA lädst du einmal unter
`https://<adresse>/opnexus-ca.crt` herunter und importierst sie in Browser oder
System (Firefox: Einstellungen › Zertifikate › Zertifizierungsstellen;
Windows/macOS: als vertrauenswürdige Stammzertifizierungsstelle installieren).
Bis dahin zeigt der Browser eine Warnung, die Verbindung ist trotzdem verschlüsselt.

**Root-CA vor dem Import prüfen.** Wer zwischen Browser und Server sitzt, könnte
eine eigene CA unterschieben. Vergleiche deshalb vor dem Import den Fingerabdruck
auf dem Server mit dem der heruntergeladenen Datei:

```bash
# auf dem Server
docker compose exec -T proxy cat /data/caddy/pki/authorities/local/root.crt \
  | openssl x509 -noout -fingerprint -sha256
# auf dem Client, für die heruntergeladene Datei
openssl x509 -in opnexus-root-ca.crt -noout -fingerprint -sha256
```

Nur bei identischem SHA-256-Fingerabdruck importieren, oder die CA über die
Unternehmens-/MDM-Verteilung ausrollen.

**Adressen festlegen.** In der `.env` trägst du alle Adressen ein, unter denen
OPNexus erreichbar sein soll:

```bash
OPNEXUS_SITES=10.0.0.5, opnexus.lan     # alle Adressen (IP und/oder Namen)
OPNEXUS_DEFAULT_SNI=10.0.0.5            # die primäre; Browser senden bei reiner IP keinen Namen
FRONTEND_ORIGIN=https://10.0.0.5,https://opnexus.lan
COOKIE_SECURE=true
TRUST_PROXY_HEADERS=true
```

Danach `docker compose up -d`. Ein Name ohne DNS braucht auf dem Client einen
Eintrag in der Hosts-Datei. `TRUST_PROXY_HEADERS=true` darf nur gesetzt sein, wenn
ein Proxy davor steht, der `X-Forwarded-For` selbst setzt; der bundled Proxy tut das.
Sonst könnte jeder Client die Anmeldesperre mit einer gefälschten Kopfzeile umgehen.

**Sichern:** Der Schlüssel der internen CA liegt im Volume `caddy_data`. Geht er
verloren, entsteht eine neue CA, und alle Clients müssen sie neu importieren.

**Eigener Reverse-Proxy.** Statt des mitgelieferten Proxys kannst du einen eigenen
verwenden (z. B. Caddy mit Let's-Encrypt-Zertifikat). Backend (`127.0.0.1:8100`)
und Frontend (`127.0.0.1:3100`) bleiben dann hinter ihm:

```
opnexus.example.com {
    reverse_proxy /api/* localhost:8100
    reverse_proxy localhost:3100
}
```

In der `.env`: `FRONTEND_ORIGIN=https://opnexus.example.com`, `COOKIE_SECURE=true`,
`TRUST_PROXY_HEADERS=true`.

**Ohne TLS (nur für vertrauenswürdige Testnetze).** `BIND_ADDRESS=0.0.0.0` in der
`.env` veröffentlicht Frontend (3100) und Backend (8100) im Netz,
`FRONTEND_ORIGIN=http://<ip>:3100` erlaubt den Zugriff. Das Sitzungs-Cookie geht
dann unverschlüsselt über das Netz; für ein nicht vertrauenswürdiges Netz oder das
Internet ist das nicht geeignet.

---

## Konfiguration (`.env`)

Alle Einstellungen stehen in der `.env` neben der `docker-compose.yml`. Nach einer
Änderung `docker compose up -d` ausführen.

| Variable | Bedeutung |
| --- | --- |
| `POSTGRES_PASSWORD` | **Pflicht.** Passwort der Datenbank. Der Start verweigert sich ohne Wert. |
| `POSTGRES_DB`, `POSTGRES_USER` | Datenbank- und Benutzername (Standard `opnexus`). Wirken nur beim allerersten Start eines leeren Volumes, siehe [Datenbank-Namen](#datenbank-namen). |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Erstes Konto. Ohne Passwort wird eines erzeugt. Gilt nur beim Anlegen des ersten Kontos. |
| `OPNEXUS_SITES`, `OPNEXUS_DEFAULT_SNI` | Adressen für den TLS-Proxy, siehe oben. |
| `PROXY_BIND_ADDRESS`, `PROXY_HTTPS_PORT`, `PROXY_HTTP_PORT` | Schnittstelle und Ports des Proxys (Standard `0.0.0.0`, 443, 80). |
| `FRONTEND_ORIGIN` | Exakte Ursprünge (`https://…`, kommagetrennt), von denen API-Anfragen kommen dürfen. Ohne Wert akzeptiert das Backend nur `localhost`. |
| `COOKIE_SECURE` | `true`, sobald OPNexus nur über HTTPS genutzt wird. |
| `TRUST_PROXY_HEADERS` | `true` nur hinter einem Proxy, der `X-Forwarded-For` setzt. |
| `BIND_ADDRESS` | Schnittstelle für Backend und Frontend (Standard `127.0.0.1`). |
| `BACKEND_PORT`, `FRONTEND_PORT` | Host-Ports (Standard 8100 und 3100), z. B. für eine zweite Instanz. |
| `OPNEXUS_IMAGE_REPO`, `OPNEXUS_VERSION` | Registry und Version der Release-Images. `OPNEXUS_VERSION` ist die „Spur“, siehe [Updates](#opnexus-aktualisieren). |
| `UPDATE_CHECK_URL`, `UPDATE_CHECK_INTERVAL_HOURS` | Hinweis auf neue Versionen, standardmäßig **aus**. |
| `COMPOSE_PROFILES=updater` | Aktiviert den optionalen Updater für Updates per Klick. |
| `ALLOW_INSECURE_FIREWALL_URLS` | Erlaubt `http://`-Adressen für Firewalls. Nur für isolierte Labore. |
| `SMTP_CA_FILE`, `ALLOW_INSECURE_SMTP_AUTH` | Eigene CA für den Mailserver bzw. SMTP-Anmeldung ohne Verschlüsselung (bleibt standardmäßig gesperrt). |

---

## Benutzer, Rollen und Anmeldung

**Rollen.** Es gibt zwei: `admin` (voller Zugriff, inklusive Benutzerverwaltung) und
`read_only`. Ein Nur-Lesen-Benutzer sieht alles, kann aber nichts ändern: alle
Schreibaktionen sind in der Oberfläche gesperrt und werden vom Backend mit 403
abgelehnt. Nur das eigene Passwort sowie Farbschema und Sprache bleiben änderbar.

**Benutzerverwaltung** (System › Benutzer, nur Admins): Benutzer anlegen, Rolle
ändern, Passwort zurücksetzen, Zwei-Faktor eines anderen Benutzers zurücksetzen,
löschen. Du kannst dich nicht selbst löschen und dich nicht zum letzten Admin
herabstufen.

**Anmeldung.**
- Mehrere Fehlversuche (Standard: 5 in 5 Minuten, je Benutzer und IP) sperren die
  Anmeldung für fünf Minuten.
- Eine Sitzung gilt 30 Tage. Ein Passwortwechsel beendet alle Sitzungen des Kontos.
- Nach der Anmeldung zeigt ein Hinweis Zeitpunkt und IP der **vorherigen**
  Anmeldung, fehlgeschlagene Versuche seitdem und das Ende der aktuellen Sitzung.
  Stimmt etwas nicht, ändere das Passwort.

**Zwei-Faktor-Anmeldung** (optional, pro Benutzer): Einstellungen › Zwei-Faktor-
Anmeldung. QR-Code mit einer Authenticator-App scannen, Code bestätigen und die
10 einmaligen Wiederherstellungscodes sicher aufbewahren. Das Passwort wird beim
Einrichten erneut verlangt. Ein Code kann nur einmal verwendet werden, falsche
Codes zählen für die Anmeldesperre.

**Zugang verloren?**
- Passwort: `./opnexus password [benutzer]`, siehe [Erster Start](#erster-start-und-passwort).
- Zwei-Faktor, wenn auch die Wiederherstellungscodes weg sind: ein anderer Admin
  setzt es in der Benutzerliste zurück, für den letzten Admin gilt auf dem Server
  `docker compose exec backend python -m app.reset_2fa <benutzername>`.

---

## Firewalls anbinden

### API-Schlüssel in OPNsense anlegen

1. In OPNsense **System › Access › Users** öffnen.
2. Einen eigenen Benutzer (z. B. `opnexus`) anlegen oder einen vorhandenen bearbeiten.
3. Beim Benutzer unter **API keys** auf das Plus klicken. Der Browser lädt eine Datei
   `apikey.txt` herunter. Dort steht der Schlüssel hinter `key=` und das Secret
   hinter `secret=`. Das Secret gibt es nur in dieser Datei.
4. Dem Benutzer ausreichende Rechte geben, am einfachsten über die Gruppe `admin`.
   Mit eingeschränkten Rechten schlagen einzelne Funktionen fehl.

### Host eintragen

Unter **Übersicht › Hosts** (oder im Einrichtungsassistenten) legst du eine
Firewall an: Name, Adresse (`https://…`, bei abweichendem Port mit Port), API-Key,
API-Secret, optional Cluster-Name und Policy-Gruppe.

- **HA-Paare:** Trage beide Knoten mit ihren **eigenen Management-Adressen** ein,
  nicht mit der gemeinsamen CARP-Adresse. Regeln müssen auf jeden Knoten einzeln
  gelangen, und über die CARP-Adresse wäre nur der gerade aktive Master erreichbar.
  Gib beiden Knoten denselben **Cluster-Namen**, dann gruppiert OPNexus sie.
- **API-Secrets** liegen nie in der Datenbank, sondern als Datei (Rechte `0600`)
  in einem eigenen Volume.
- `http://`-Adressen werden abgelehnt, weil die Zugangsdaten per HTTP-Basic-Auth
  gesendet werden (Ausnahme: `ALLOW_INSECURE_FIREWALL_URLS=true` für Labore).

### Zertifikat festsetzen (Pinning)

Firewalls haben meist ein selbstsigniertes Zertifikat, dessen Name nicht zur
Adresse passt. Statt die Prüfung abzuschalten, setzt OPNexus das Zertifikat fest:

1. Beim Anlegen oder unter Hosts › Bearbeiten auf **Zertifikat abrufen** klicken.
2. Den angezeigten SHA-256-Fingerabdruck mit dem in OPNsense vergleichen
   (System › Vertrauen › Zertifikate).
3. Passt er, bestätigen.

Danach vertraut OPNexus **ausschließlich** diesem Zertifikat, bevor Zugangsdaten
gesendet werden, unabhängig davon, ob die Firewall per IP oder Name erreicht wird.
Erneuert die Firewall ihr Zertifikat, meldet der Host „Zertifikat passt nicht zum
festgesetzten“. Dann erneut abrufen und bestätigen. Der Fingerabdruck-Vergleich
ist der Schritt, der das Festsetzen absichert.

### Verwalten

- **Host-Liste:** Status (online/offline/deaktiviert), CARP-Rolle, Cluster,
  Policy-Gruppe. **In OPNsense öffnen** springt in die Weboberfläche der Firewall.
- **Host-Detailseite** (Klick auf den Namen): Status, zentral verwaltete und
  **live von der Firewall gelesene** Regeln (auch von Hand angelegte),
  Konfigurationssicherungen, Dienste, Systemauslastung und Wartung.
- **Deaktivieren** nimmt einen Host aus Rollouts und Abfragen, ohne ihn zu löschen.
  Ein Host, auf den noch Regeln, Aliase oder NAT ausgerollt sind, lässt sich nicht
  löschen: Sonst ginge OPNexus der Überblick verloren, was dort liegt. Entferne
  zuerst die Deployments (Regel löschen oder früheres Ziel unter „Prüfen & Push“
  bereinigen).

---

## Regeln sicher ausrollen

### Der Ablauf

Zentral gepflegte Regeln (**Regelwerk › Regeln**) folgen immer demselben Weg:

1. **Entwurf speichern.** Das ändert keine Firewall.
2. **Prüfen:** pro Ziel-Host zeigt die Vorschau neue, geänderte und unveränderte
   Regeln mit Feldvergleich. Grundlage ist der zuletzt von OPNexus gespeicherte
   Stand, **kein Live-Abgleich**. Den Vergleich mit der Firewall zeigt die
   Live-Ansicht.
3. **Ausrollen (Push):** OPNexus schreibt die Regel und wendet sie an. Sie gilt
   sofort, aber nur **auf Probe**.
4. **Bestätigen:** Innerhalb der Frist (Standard 180 Sekunden) klickst du
   **Bestätigen**. Dann ist die Änderung dauerhaft.
5. **Automatischer Rollback:** Wird nicht bestätigt, macht OPNexus die Änderung
   rückgängig: eine neu angelegte Regel wird wieder entfernt, eine geänderte auf den
   vorherigen Stand gesetzt. Wer sich mit einer Regel aussperrt, muss also nur
   warten.

Weitere Regeln dazu:

- Ein erneuter Push einer bestätigten Regel mit **unverändertem** Inhalt macht
  keine Firewall-Aufrufe. Solange ein Deployment offen ist, sind erneuter Push und
  Bearbeiten gesperrt.
- Schlägt der Rollback selbst fehl (Host nicht erreichbar), steht die Regel mit dem
  Status „Rollback fehlgeschlagen“ da und braucht Aufmerksamkeit. In der Vorschau
  lässt er sich gezielt wiederholen. Das meldet auch eine Benachrichtigung.
- **Löschen** ist eine bewusste, sofort wirksame Aktion ohne Auto-Rollback.
- Ändert jemand zwischen Vorschau und Push den Entwurf, die Ziele oder ein
  Deployment, lehnt OPNexus den Push ab und verlangt eine neue Vorschau. Gleichzeitiges
  Bearbeiten derselben Regel wird erkannt (409).
- Jede Regel hat einen **Verlauf**: jede Änderung ist ein unveränderlicher Stand,
  der sich ins Formular übernehmen und als neuen Entwurf speichern lässt.

### Zustände einer Regel

Das Backend berechnet den Zustand aus Zielen und Deployments: *Ziele fehlen*,
*Ziel-Hosts deaktiviert*, *Änderungen ausstehend*, *Bestätigung offen*, *Fehler
prüfen*, *frühere Ziele prüfen* und *ausgerollt*. „Ausgerollt“ bezieht sich auf
den von OPNexus gespeicherten Stand; Handänderungen auf der Firewall zeigt die
Live-Ansicht.

### Ziele bereinigen

Unter „Prüfen & Push“ lassen sich **frühere Ziele** einzeln entfernen: OPNexus
löscht die Regel nur auf diesem Host, die zentrale Regel und ihre anderen
Deployments bleiben. Das wirkt sofort und ohne Auto-Rollback. Ein Ziel aus der
Zielliste zu nehmen löscht auf der Firewall nichts.

### Live-Ansicht und Drift

- **Regeln › Live über alle Hosts:** pro Cluster eine Tabelle der Regeln, die nicht
  auf allen Knoten identisch vorhanden sind, mit Häkchen je Host. Der Abgleich nutzt
  einen Fingerabdruck aus Beschreibung, Interface, Richtung, Protokoll, Quelle,
  Ziel und Aktion; ein Unterschied in einem anderen Feld fällt dabei nicht auf.
- Die Cluster-Ansicht und das Dashboard weisen auf Drift hin; optional meldet sie
  eine Benachrichtigung.

### Von Hand angelegte Regeln übernehmen

**Regeln › Von Firewall übernehmen** erfasst Regeln, die von Hand in OPNsense
entstanden sind, als verwaltete Regel mit ihrer vorhandenen UUID. Die Übernahme
liest nur und schreibt nichts auf die Firewall; erst die erste Änderung läuft durch
den normalen Ablauf mit Bestätigung und Auto-Rollback. Felder, die OPNexus nicht
kennt (Gateway, Zustandsoptionen, Zeitpläne), bleiben unverändert.

Nicht übernommen werden Regeln, die das Modell verfälschen würde: invertierte
Schnittstelle, Quelle oder Ziel, mehrere oder keine Schnittstelle, „Empfangen auf“,
keine Quick-Regel, Richtung weder ein noch aus, IPv4+IPv6 in einer Regel, fehlende
Beschreibung und Systemregeln. Die Oberfläche nennt den Grund je Regel.

> **Reihenfolge:** OPNexus setzt jede Regel mit `quick`, die Firewall wertet also
> strikt „erster Treffer gewinnt“ aus. Die Reihenfolge ist dadurch
> sicherheitsrelevant, siehe [hierarchische Policies](#gerätegruppen-und-hierarchische-policies).

---

## Gerätegruppen und hierarchische Policies

### Gerätegruppen

**Regelwerk › Gruppen** bündeln Hosts für gezieltes Ausrollen. Ein Host kann
mehreren Gruppen angehören; Gruppen sind unabhängig von HA-Clustern. Eine Regel
kann mehrere Gruppen **und** einzelne Hosts als Ziel haben. Beim Push werden die
aktuellen Mitglieder zusammengeführt und dedupliziert, deaktivierte Hosts
übersprungen. Neue Mitglieder erhalten eine Regel erst beim nächsten Push.

- Vor dem Speichern einer bestehenden Gruppe zeigt die Oberfläche die Auswirkungen:
  neue und entfernte Mitglieder und Ziele je Regel.
- Gruppen, die noch verwendet werden, lassen sich nicht löschen.
- „Regel für diese Gruppe anlegen“ öffnet das Regelformular mit vorausgewählter
  Gruppe.

### Hierarchische Policies

**Regelwerk › Policy-Hierarchie** ist ein Gruppenbaum mit **Pre-** und
**Post-Regelwerken**. Eine Regel, die an einer übergeordneten Gruppe hängt, wird
automatisch auf alle Hosts der Gruppe und ihrer Untergruppen ausgerollt, in
korrekter Reihenfolge:

- **Pre-Regeln:** die oberste Gruppe zuerst. Eine organisationsweite Pre-Regel
  gewinnt vor allem Spezifischeren.
- **Post-Regeln:** die speziellste Gruppe zuerst, nahe am host-lokalen Default-Deny.

Ein Host gehört zu **höchstens einer** Hierarchiegruppe (im Host-Formular
einstellbar). Aliase und alle NAT-Arten können ebenfalls auf einen Teilbaum zielen,
haben aber bewusst kein Pre/Post. Die Reihenfolge ändert man per Drag & Drop oder
mit den Pfeilen. Bereits ausgerollte Regeln bringt **Reihenfolge neu herstellen**
anschließend auf die Firewall; nach normalen Regel-, Alias- und NAT-Deploys
geschieht das automatisch. Zyklen im Baum und das Löschen referenzierter Gruppen
lehnt OPNexus ab.

Schlägt das Einsortieren nach einem erfolgreichen Push fehl, steht das Deployment
auf **Fehler** statt auf „offen“: eine live, aber falsch einsortierte Regel ist
sicherheitsrelevant falsch, nicht nur unbestätigt. Legacy- und Automatikregeln
verschiebt OPNsense nicht und OPNexus fasst sie nicht an.

---

## Aliase und NAT

**Aliase** (Host, Netz, Port, Gruppe, MAC, ASN) werden zentral gepflegt und
folgen demselben Ablauf mit Vorschau, Bestätigung und Rollback wie Regeln.

**NAT** (Regelwerk › NAT) umfasst **Port Forward**, **1:1-NAT** und **Outbound-NAT**
mit getrennten Formularen. Alle drei haben Entwurf, Vorschau, Push, Bestätigung,
Verlauf, Duplizieren, Ziele (Gruppen, Hosts, Hierarchie), Rollback-Wiederholung und
eine Live-Ansicht der auf der Firewall aktiven Einträge, getrennt nach
automatischen und konfigurierten.

- **Port Forward** legt keine Filterregel an: eine passende Regel muss separat
  vorhanden sein.
- **Outbound-Modus:** OPNexus liest den globalen Modus und prüft ihn vor jedem Push.
  Nur `hybrid` oder `advanced` (Manual) erlauben manuelle Regeln; bei `automatic`,
  `disabled` oder unbekanntem Modus bricht der Push mit
  `OUTBOUND_MODE_BLOCKS_PUSH` ab. OPNexus ändert den globalen Modus nie. Stelle ihn
  in OPNsense unter Firewall › NAT › Outbound auf „Hybrid“.
- Die NAT-Drift-Erkennung vergleicht die Knoten eines Clusters und meldet
  Abweichungen.

---

## Netzwerk und Host-System

### Netzwerk (je Firewall)

Der Bereich **Netzwerk** verwaltet **Schnittstellen**, **VLANs**, **virtuelle IPs**,
**Gateways** (einschließlich Gateway-Gruppen), **Routen**, **DNS** (Unbound-Einträge
und Weiterleitungen), **DHCP** (dnsmasq-Hosts und -Bereiche, Kea-Subnetze und
-Reservierungen) und **Geräte** (LAGG, Bridge, Loopback, GRE, GIF, VXLAN). Diese
Objekte sind firewall-spezifisch, deshalb gibt es keine zentrale Kopie und keinen
Multi-Host-Push: die Firewall ist die Quelle der Wahrheit.

**Ablauf einer Änderung:**

1. **Vorschau:** OPNexus liest den Live-Stand und zeigt Unterschiede, Risiken,
   Warnungen und Sperren.
2. **Ausführen:** OPNexus liest den Stand erneut und verweigert, wenn er sich
   zwischenzeitlich geändert hat. Risikoreiche Änderungen verlangen zusätzlich die
   Eingabe des Firewall-Namens. Dann: Konfigurationssicherung speichern (schlägt das
   fehl, wird nichts geändert), schreiben, anwenden, zurücklesen und mit dem
   Gewünschten vergleichen.
3. Jeder Schritt landet im Audit-Log.

**Schutz vor dem Kappen der eigenen Verbindung.** OPNexus ermittelt, über welche
Schnittstelle die Firewall unter der hinterlegten Adresse erreichbar ist. Diese
Änderungen verlangen eine ausdrückliche Bestätigung: Änderung oder Löschen dieser
Schnittstelle, ihres VLANs oder Gateways, Änderung des Standard-Gateways, eine
Standardroute oder eine Route, die die Firewall-Adresse abdeckt, und jede Änderung
bestehender Objekte, wenn sich der Verbindungsweg nicht bestimmen lässt.

> **Kein automatischer Rollback.** Anders als bei Regeln kann OPNexus eine Änderung
> nicht zurücknehmen, die seine eigene Verbindung trennt. Der Rückweg ist die
> Sicherung vor der Änderung. Wird die Firewall danach unter der alten Adresse
> nicht erreicht („angewendet, nicht prüfbar“), trage unter Hosts › Bearbeiten die
> neue Adresse ein.

Weitere Hinweise:

- **Schnittstellen-Zuweisung** ist zweistufig: Änderungen werden erst vorgemerkt,
  dann angewendet. OPNexus blockiert neue Änderungen, solange dort schon etwas
  vorgemerkt ist, damit nichts Fremdes mitgeht. Beim Löschen entfernt OPNsense alle
  Regeln und NAT-Einträge der Schnittstelle; die Vorschau listet sie.
- Die IP-Konfiguration von Schnittstellen ist per API **erst ab OPNsense 26.7.6**
  möglich. Auf älteren Ständen sperrt OPNexus diese Änderung mit Begründung.
- Geheimnisse (CARP-Passwort, DDNS-Schlüssel) werden nie angezeigt, gespeichert oder
  auditiert.
- **Auf den Partner übertragen:** Nach dem Anlegen eines Objekts auf einem
  Cluster-Knoten bietet OPNexus an, es mit eigener Vorschau auf den Partner zu
  übertragen. Empfohlen: zuerst der Backup-Knoten, dann der Master.
- **Abgleich** (Netzwerk › Abgleich) vergleicht die Knoten eines Clusters. Adressen,
  CARP-Advskew und -Passwort sowie das aktive Standard-Gateway sind pro Knoten
  verschieden und werden nicht verglichen. Eine Abweichung löst optional den Alarm
  „Netzwerk-Abweichung im Cluster“ aus, der sich nach der Korrektur selbst auflöst.

### Host-System

- **Tunables:** jede Änderung braucht die Bestätigung. Eingebaute Vorgaben lassen
  sich nur mit einem eigenen Eintrag überschreiben, nicht bearbeiten.
- **Cron:** geplante Aufgaben der Firewall.
- **Syslog:** entfernte Log-Ziele.
- **Snapshots:** ZFS-Snapshots anlegen und löschen. Das aktive Boot-Environment lässt
  sich nicht löschen; Aktivieren mit Neustart gibt es bewusst nicht.
- **Diagnose:** Ping, Traceroute, TCP-Port-Prüfung und DNS-Abfrage, ausgeführt **von
  der Firewall aus**. Nur für Admins, mit strenger Eingabeprüfung; jeder Lauf
  steht im Audit-Log, Ergebnisse werden nicht gespeichert, die Port-Prüfung gibt nie
  den Banner der Gegenstelle weiter. Ein Test auf einen geschlossenen oder blockierten
  Port dauert bei OPNsense rund 10 Sekunden. Packet-Capture gibt es bewusst nicht,
  denn Mitschnitte enthalten fremde Nutzdaten und Zugangsdaten.

---

## VPN: IPsec, OpenVPN, WireGuard

VPN-Objekte (VPN › IPsec-VPN, OpenVPN, WireGuard) werden zentral definiert und auf
Hosts oder Gerätegruppen ausgerollt, etwa derselbe Tunnel auf beiden Knoten eines
HA-Paars. Der Ablauf ist Entwurf → Vorschau → Push; Verlauf und Löschen gibt es
wie bei Regeln.

> **Kein automatischer Rollback bei VPN.** Ein Neuladen des VPN-Dienstes kann
> genau die Verbindung kappen, über die OPNexus die Firewall erreicht, und ein
> Auto-Rollback könnte sie dann nicht mehr erreichen. Statt eines trügerischen
> Sicherheitsnetzes zeigt OPNexus vor **jedem** Push eine deutliche Warnung und
> verlangt zwei Schritte („Warnung verstanden“, dann „Ja, jetzt ausrollen“).
> Schlägt ein Schritt fehl, bleibt der bereits geschriebene Teil-Stand stehen, das
> Deployment steht auf „Fehler“ mit Klartextmeldung. Scheitert nur das
> Neuladen, lautet die Meldung: „Konfiguration wurde geschrieben, Dienst-Reload aber
> nicht bestätigt — Erreichbarkeit manuell prüfen.“

Schlüsselmaterial (Pre-Shared-Keys, Static-Keys, WireGuard-Schlüssel) liegt nie in
der Datenbank, sondern als Datei in eigenen Volumes. Beim Bearbeiten lässt du das
Feld leer, um den vorhandenen Schlüssel zu behalten.

| | IPsec | OpenVPN | WireGuard |
| --- | --- | --- | --- |
| Umfang | Site-to-Site-Tunnel | Server- und Client-Instanzen | ein Server und ein Peer je Tunnel |
| Authentifizierung | Pre-Shared-Key | Zertifikat und CA je Ziel-Host, optional TLS-Auth/-Crypt-Schlüssel | Schlüsselpaar, optional Peer-PSK |
| Nicht enthalten | Zertifikat/EAP, mehrere Child-SAs, Pools | `crypt-v2`, Authentifizierungsserver | Multi-Peer-Hub, Schlüsselgenerator, Live-Status |

Hinweise:

- **OpenVPN:** Zertifikat und CA wählst du pro Ziel-Host aus dessen eigenem
  Trust-Store (die UUIDs sind hostlokal). Fehlt einem Ziel die Auswahl, bricht der
  Push ab, ohne etwas zu schreiben.
- **WireGuard:** Server-Privatschlüssel, Server-Public-Key und Peer-Public-Key
  lieferst du selbst. Aktiviere in OPNsense einmalig **VPN › WireGuard ›
  Allgemein**, bevor du dich auf gepushte Tunnel verlässt.

Teste die VPN-Rollouts zuerst auf einer Nicht-Produktiv-Firewall.

---

## Zertifikate und ACME

**Zertifikate** zeigt live die Zertifikate aller Firewalls (Ablauf, Status) und
verwaltet den Trust-Store sowie die ACME-Ausstellung. Alles ist **pro Host**
modelliert, weil Zertifikats- und CA-UUIDs hostlokal sind; es gibt keinen
Multi-Host-Push.

**Trust-Store** (Zertifikate und CAs): anlegen (importieren, intern ausstellen,
erneut ausstellen), bearbeiten, pushen, löschen. Das ist ein einzelner,
sofort wirksamer Schritt ohne Dienst-Neustart und ohne Lockout-Risiko. Das
Zertifikat der Firewall-Weboberfläche lässt sich per API nicht umstellen, also
auch nicht versehentlich. Ein importierter privater Schlüssel liegt nur als Datei
in einem eigenen Volume. Ein noch verwendetes Zertifikat lässt OPNsense nicht
löschen; OPNexus reicht die Meldung durch.

**ACME / Let's Encrypt** (Plugin `os-acme-client` auf der Firewall): Konten und
Zertifikatsanfragen anlegen und pushen. Anlegen, Ändern und Pushen sind reine
Konfiguration. Zwei Aktionen kontaktieren **echt** die Zertifizierungsstelle über
das Internet, zählen gegen deren Rate-Limits und stehen deshalb hinter einer
zweistufigen Warnung: **Registrieren** (Konto) und **Signieren** (Zertifikat).

- **Signieren läuft asynchron.** Die Meldung „angestoßen“ ist **kein** Erfolg. Das
  Ergebnis holst du mit **Status** ab.
- Nutze für erste Versuche die Staging-CA (`letsencrypt_test`).
- **Erneuerungsintervall** zählt Tage seit der letzten Ausstellung, nicht Tage vor
  Ablauf.
- DNS-Provider und Validierungsmethoden richtest du in OPNsense selbst ein;
  OPNexus referenziert sie über die UUID. Die automatische Erneuerung übernimmt
  OPNsense, OPNexus bietet den manuellen Sofort-Trigger.
- Der EAB-HMAC-Schlüssel eines ACME-Kontos liegt nur als Datei in einem eigenen Volume.

---

## HA-Cluster und CARP

Hosts mit demselben **Cluster-Namen** bilden einen Cluster. **Übersicht › Cluster**
zeigt die Knoten nebeneinander mit MASTER/BACKUP-Rolle, Regelanzahl, Regel- und
NAT-Drift und einem einklappbaren **CARP-Verlauf**.

- **CARP-Verlauf:** Rollenwechsel werden im Takt der Benachrichtigungsprüfung
  erkannt (Einstellungen › Benachrichtigungen). Ein Wechsel, der zwischen zwei
  Durchläufen wieder zurückgeht, taucht nicht auf; der erste Durchlauf nach dem
  Anlegen protokolliert nur den Anfangszustand.
- **Alarme:** *Split-Brain* (mehrere MASTER) und *kein Master* melden sich über die
  eingerichteten Kanäle und lösen sich selbst auf.
- **Wartungsmodus:** Ein Knoten lässt sich kontrolliert in den CARP-Wartungsmodus
  versetzen, damit der Partner übernimmt. Optional mit **Auto-Revert-Timer**
  (15, 30, 60 Minuten oder dauerhaft). Der Timer stellt den Normalbetrieb
  selbstständig wieder her; scheitert das, meldet es der Alarm
  „CARP-Auto-Revert fehlgeschlagen“.
- **Sperre gegen doppelte Demotion:** Ist ein Knoten bereits im Wartungsmodus,
  blockiert OPNexus das Demoten des Partners und nennt den Host, der zuerst
  „Normalbetrieb herstellen“ braucht.
- Wird ein Rollentausch trotz Wartungsmodus nicht ausgelöst (ein bekannter
  OPNsense-Fehler), hilft **Vorgang abbrechen**: Es schließt den Vorgang mit
  Begründung, ohne einen Erfolg zu behaupten.

---

## Wartung und Updates der Firewalls

### Host-Wartung

Auf der Host-Detailseite: Update-Status, Update-Suche, **Neustart**, **stabile
Updates** und **Major-Upgrades**. Jede Aktion läuft über dieselben Schutzschritte:

- Eine **Vorschau**, fünf Minuten gültig und nur einmal verwendbar.
- Eingabe des **Hostnamens** und Bestätigung der möglichen **Downtime**.
- Ein **lokales Konfigurations-Backup**, höchstens 24 Stunden alt.
- Gesperrt, solange Deployments, Rollbacks, Wiederherstellungen oder andere
  Wartungen offen sind.

„An Firewall übergeben“ bestätigt nur die Annahme. Nach einem Timeout bleibt der
Ausgang unklar und wird **nie** automatisch wiederholt. Prüfe Erreichbarkeit,
Version und Dienste und dokumentiere den Abschluss. Hängt ein Vorgang, schließt
**Vorgang abbrechen** ihn mit Begründung. Offene Wartung sperrt neue Pushes auf dem
betroffenen Host oder Cluster.

### Update Center

**System › Update Center** zeigt die ganze Flotte: installierte und verfügbare
Versionen, stabile Updates, Major-Upgrades, Neustartbedarf, Alter des lokalen
Backups, Paketänderungen, Prüflog und offene Aufträge. Beim Öffnen startet für
Admins einmal eine Update-Suche auf allen geeigneten Hosts.

- **HA-Updates** laufen geführt: Updates nur auf dem BACKUP-Knoten (auf dem MASTER
  verweigert OPNexus sie). Der MASTER wird nach eigener Vorschau und
  Doppelbestätigung in den Wartungsmodus versetzt, damit der aktualisierte Partner
  übernimmt, und kehrt danach in den Normalbetrieb zurück. Jeder Schritt wird erst
  nach beobachteten gesunden Rollen abgeschlossen und auditiert. Es gibt keine
  unbeaufsichtigten Update-Ketten und keine automatischen Rollenwechsel.
- **Major-Upgrades** (z. B. 25.7 → 26.1) sind nur freigeschaltet, wenn OPNsense
  selbst ein Upgrade meldet, also nie, solange noch ein stabiles Paketupdate
  aussteht. Sie haben eine zusätzliche rote Warnung (länger dauernd, kein
  automatischer Rollback in OPNexus).

### Geplante Wartung

Neustart, Update oder Major-Upgrade lassen sich auf einen **einmaligen**
zukünftigen Zeitpunkt legen. Das ist ausdrücklich „läuft unbeaufsichtigt“ und
verlangt eine Bestätigung. Zum Zündzeitpunkt prüft OPNexus die Sperren und das
frische Backup erneut. Eine blockierte Aktion wird als „fehlgeschlagen“ markiert
und **nicht** wiederholt; sie meldet sich nicht per Benachrichtigung, sondern steht
im Status der geplanten Wartung. Wiederkehrende Zeitpläne gibt es nicht.

### Dienste und Systemauslastung

- **Dienste** (Host-Detailseite): alle von OPNsense gemeldeten Dienste mit Status und
  Start/Stopp/Neustart. Es gibt keine Auswahlliste, alle Dienste sind steuerbar,
  deshalb steht ein allgemeiner Warnhinweis davor.
- **Systemauslastung:** Zeitverläufe der letzten 24 Stunden für CPU-Last,
  Speicher und Plattenbelegung je Host. Gesampelt wird alle 60 Sekunden, die
  Aufbewahrung beträgt 30 Tage.

### Konfigurationssicherungen der Firewalls

Auf der Host-Detailseite sicherst du die Konfiguration einer Firewall
(**Jetzt sichern**) und siehst die lokal gespeicherten Sicherungen. Das ist nicht
die Sicherung von OPNexus selbst, siehe [Sichern und Wiederherstellen](#sichern-und-wiederherstellen).

**Wiederherstellen** prüft eine gespeicherte Sicherung als Vorschau (Format,
Metadaten, geheime Felder werden unkenntlich gemacht) und übergibt dann an das
OPNsense-Webinterface. **OPNexus schreibt dabei nichts auf die Firewall.** Du
importierst die Sicherung selbst in OPNsense. Solange der Vorgang offen ist,
sperrt OPNexus Regel-, Alias-, NAT- und Wartungs-Pushes auf diesem Host; bestätige
oder breche den Vorgang ab. Bei Abweichungen (etwa ein anderer Hostname in der
Sicherung) zeigt die Vorschau eine Warnung.

---

## Überwachung: Dashboard, Logs, Audit, Benachrichtigungen

### Dashboard

Die Startseite (**Übersicht › Dashboard**) zeigt: offene Aktionen (ausstehende
Bestätigungen, Fehler, hängende Wartungen, laufende CARP-Wartungsfenster),
Fleet- und CARP-Gesundheit, Sicherheits- und Ablaufwarnungen (Zertifikate, Regel-
und NAT-Drift, verfügbare Updates) und einen Aktivitäts-Feed der letzten
Audit-Ereignisse.

### Logs

OPNexus holt die Firewall-Logs aller Hosts regelmäßig selbst ab und speichert sie
in der eigenen Datenbank. **Monitoring › Logs** durchsucht sie serverseitig:
einzelne Hosts, ganze Cluster oder alle, Freitext, Filter (Aktion, Interface,
Protokoll), Zeitraum (letzte Stunde/24 Stunden/7 Tage/alles/eigener Bereich) und
ein Statistik-Panel (Verteilung nach Aktion und Interface, meistgeblockte Quellen,
häufigste Zielports).

Einstellungen › **Log-Erfassung**:

| Einstellung | Standard | Bedeutung |
| --- | --- | --- |
| Erfassung aktiv | an | schaltet das Abholen ein oder aus |
| Abrufintervall | 30 s | Mindestwert 10 s |
| Aufbewahrung | 7 Tage | ältere Einträge werden entfernt |
| Max. Speicher | 10 GB | `0` = unbegrenzt; bei Erreichen werden die ältesten Einträge gelöscht |

Der Speicher ist eine **Schätzung** aus der Tabellengröße: Postgres gibt gelöschten
Platz nicht an das Dateisystem zurück, nutzt ihn aber für neue Logs. Bei
stark ausgelasteten Firewalls können schon wenige Tage mehrere GB belegen, setze
Aufbewahrung und Limit entsprechend. Die Zeitstempel sind die Uhrzeit der Firewall
**unkorrigiert**: Steht die Firewall nicht auf UTC, sind Zeitfilter um diesen
Offset verschoben.

### Audit

**Monitoring › Audit** protokolliert lückenlos Anfragen, Ergebnisse je Host,
Bestätigungen, Löschungen, automatische und manuelle Rollbacks, Wartungs- und
Netzwerkaktionen, Diagnoseläufe und Update-Anfragen, jeweils mit Benutzer,
Zeitpunkt, Korrelations-ID und Inhalt. Filter, Seitenwechsel und JSON-Export sind
vorhanden. Einträge bleiben auch nach dem Löschen einer Regel oder eines Hosts
erhalten; ein Datenbank-Trigger verhindert Änderungen und Löschen. Passwörter,
API-Secrets und Konfigurationsdateien werden nicht protokolliert.

### Benachrichtigungen

Einstellungen › **Benachrichtigungen**. Kanäle:

- **Webhook** mit optionalem Signatur-Secret.
- **E-Mail (SMTP)** mit Host, Port, Verschlüsselung (STARTTLS/SSL), Zugangsdaten,
  Absender und Empfängern. Zertifikat und Hostname werden geprüft; ein Mailserver
  mit privater CA braucht `SMTP_CA_FILE`. Anmeldung ohne Verschlüsselung bleibt
  gesperrt. Mit **testen** prüfst du jeden Kanal.

Ereignisse (jeweils einzeln schaltbar): Host nicht erreichbar, Zertifikat läuft ab,
Regel-Drift im Cluster, NAT-Drift im Cluster, Netzwerk-Abweichung im Cluster,
fehlgeschlagener Rollback, Wartungsergebnis unklar, CARP-Zustand kritisch
(Split-Brain/kein Master), CARP-Auto-Revert fehlgeschlagen. Geprüft wird im
eingestellten Intervall (Standard 300 s). Ein Problem meldet sich einmal und löst
sich selbst auf, sobald es behoben ist; die Dashboard-Karte „Warnungen“ zeigt die
offenen. Der Text der Benachrichtigungen ist nicht übersetzt.

### Über OPNexus

Ein Klick auf das Logo öffnet **About**: alle Hosts mit Status, CARP-Rolle, Cluster,
installierter OPNsense-Version und Regelanzahl. Die Werte laden je Host nach; eine
nicht erreichbare Firewall zeigt „nicht abrufbar“. Die eigene Version steht im Footer
jeder Seite und in den Einstellungen.

---

## OPNexus betreiben

### Daten und Volumes

| Volume | Inhalt |
| --- | --- |
| `postgres_data` | die Datenbank (Hosts, Regeln, Verlauf, Audit, Benutzer) |
| `postgres_backups` | automatische und manuelle Datenbank-Sicherungen |
| `secrets` | API-Secrets der Firewalls |
| `backups` | Konfigurationssicherungen der Firewalls (XML) |
| `ipsec_psk_secrets`, `openvpn_key_secrets`, `wireguard_key_secrets` | VPN-Schlüssel |
| `cert_key_secrets`, `acme_secrets` | private Zertifikatsschlüssel, ACME-Kontodaten |
| `totp_secrets`, `notification_secrets` | Zwei-Faktor-Geheimnisse, Webhook-/SMTP-Zugangsdaten |
| `caddy_data`, `caddy_config` | Schlüssel der internen CA und Proxy-Konfiguration |
| `updater_state` | Token des Updaters (nur mit aktivem Updater) |

### Sichern und Wiederherstellen

**Automatisch:** Der Dienst `postgres-backup` schreibt beim Start und danach alle
24 Stunden einen Dump nach `postgres_backups` und behält 14 Tage. Zusätzlich
entsteht vor jeder Schema-Migration ein Dump (die letzten 5 bleiben).
**Manuell:** `./opnexus backup` schreibt sofort `manual-<zeit>.dump`.

**Was nicht im Dump ist:**
- Die Tabellen der **Log-Erfassung** und der **Systemauslastung** (nur das Schema).
  Nach einer Wiederherstellung sind sie leer und füllen sich aus den Firewalls.
- **Geheimnisse** (API-Secrets, VPN- und Zertifikatsschlüssel, Zwei-Faktor): sie
  liegen in den eigenen Volumes. Für eine vollständige Sicherung auch diese Volumes
  und `caddy_data` sichern.

**Wiederherstellen** in eine **leere** Datenbank (nicht mit `--clean` in eine
bestehende):

```bash
docker compose exec postgres-backup ls /backups                    # Dumps anzeigen
docker compose cp postgres-backup:/backups/<datei>.dump /tmp/restore.dump
docker compose cp /tmp/restore.dump postgres:/tmp/restore.dump
docker compose stop backend frontend
docker compose exec -e PGPASSWORD=<PASSWORT> postgres \
  psql -U opnexus -d postgres -c "DROP DATABASE opnexus; CREATE DATABASE opnexus OWNER opnexus;"
docker compose exec -e PGPASSWORD=<PASSWORT> postgres \
  pg_restore -h localhost -U opnexus -d opnexus /tmp/restore.dump
docker compose up -d backend frontend
```

Ein Dump lässt sich nur in einen Postgres derselben Hauptversion zurückspielen
(derzeit 16). Wer nach einem Update zurückrollt, nutzt am besten `./opnexus rollback`.

### OPNexus aktualisieren

Ein Update ersetzt nur den Code; die Daten bleiben im Volume, die Konfiguration in
der `.env`.

**Mit `opnexus`** (eine einzelne Bash-Datei, braucht nur Docker; im Verzeichnis mit
`docker-compose.yml` und `.env`, oder `OPNEXUS_DIR=…` setzen):

| Kommando | Wirkung |
| --- | --- |
| `opnexus status` | Version, Datenbank-Revision, Zustand der Container |
| `opnexus update [VERSION]` | zieht die neuen Images **bevor** etwas angefasst wird, wechselt, wartet bis alles „gesund“ ist, sonst automatischer Rollback |
| `opnexus rollback` | stellt Version und, falls das Update die Datenbank migriert hat, die Datenbank aus dem Backup vor dem Update wieder her |
| `opnexus backup` | Sofort-Sicherung der Datenbank |
| `opnexus password [BENUTZER]` | neues Zufallspasswort setzen und anzeigen |

`status`, `update` und `rollback` brauchen Release-Images (`OPNEXUS_IMAGE_REPO`
gesetzt); `backup` und `password` funktionieren in jeder Installation.

`OPNEXUS_VERSION` in der `.env` ist die **Spur**: `1.16.0` pinnt exakt, `1.16` folgt
Patch-Releases, `1` folgt allen 1.x. Ohne Argument holt `opnexus update` die neueste
Version der Spur, mit Argument wird die Spur umgestellt. Nach einem Rollback steht
die Version fest auf dem alten Stand.

**Rollback mit Datenbank-Wiederherstellung** bedeutet: alles steht auf dem Stand vor
dem Update, was danach geschrieben wurde, ist weg, und Log-Historie und
Messwerte sind leer. Vorher legt das Werkzeug eine Sicherheitskopie des jetzigen
Zustands an (`pre-rollback-….dump`), prüft die Lesbarkeit der Sicherung und stellt
bei einem Fehler die Kopie wieder her. Hat das Update die Datenbank nicht
verändert, wird nichts zurückgespielt. `OPNEXUS_UPDATE_TIMEOUT` (Sekunden, Standard
240) verlängert die Wartezeit für lange Migrationen, `OPNEXUS_NO_AUTO_ROLLBACK=1`
schaltet das automatische Zurückrollen ab.

**Aus dem Quellcode gebaute Installationen** aktualisieren mit
`git pull && docker compose up -d --build`.

**Schema-Migrationen** laufen beim Start des Backends: Liegt die Datenbank hinter der
Code-Version, entsteht **zuerst** ein Dump; schlägt er fehl, wird nicht migriert und
der Start bricht mit klarer Meldung ab. Dann läuft die Migration in einer
Transaktion, ein Fehler rollt alles zurück. Hat eine **neuere** Version die
Datenbank bereits migriert, startet ein älterer Code nicht (neuere Version starten
oder Sicherung einspielen). Ein Downgrade des Schemas gibt es nicht, der Rückweg ist
die Sicherung. Installationen bis 1.8.x gehen zuerst auf 1.9.x.

### Hinweis auf neue Versionen

Optional zeigt OPNexus Administratoren oben einen Hinweis, wenn eine neuere Version
veröffentlicht ist:

```bash
UPDATE_CHECK_URL=https://updates.opnexus.dev/latest.json
```

Standardmäßig ist das **aus**, dann macht OPNexus keine ausgehende Anfrage. Eingeschaltet
geht höchstens alle 12 Stunden (`UPDATE_CHECK_INTERVAL_HOURS`) ein einzelnes `GET` mit
dem User-Agent `OPNexus/<Version>` hinaus, ohne Instanz- oder Nutzerdaten. Ein
unerreichbarer Feed bleibt unauffällig; Vorabversionen werden nie vorgeschlagen. Der
Hinweis installiert nichts.

### Updates per Klick (optional)

Statt `opnexus update` kann ein Admin unter **Einstellungen › OPNexus aktualisieren**
Version und Release-Notes ansehen, aktualisieren, Fortschritt verfolgen und bei Bedarf
auf die Vorversion zurücksetzen. Ein fehlgeschlagenes Update rollt automatisch
zurück.

Das ist **bewusst aus** und muss aktiviert werden, weil es Docker-Zugriff braucht:

```bash
# .env
COMPOSE_PROFILES=updater
UPDATE_CHECK_URL=https://updates.opnexus.dev/latest.json
```
```bash
docker compose pull && docker compose up -d --no-build
```

**Sicherheitsmodell:**
- Der **Updater** ist der einzige Container mit dem Docker-Socket, was
  root-gleichen Rechten auf dem Host entspricht. Deshalb ist er optional und klein.
  Das Backend bekommt den Socket nie.
- Er hat keinen veröffentlichten Port und ist nur aus dem Compose-Netz mit einem
  selbst erzeugten Token erreichbar.
- Er kann nur „aktualisieren auf X.Y.Z“, „zurückrollen“ und „Status“, keinen
  beliebigen Befehl. Die Zielversion muss eine stabile, neuere Version mit derselben
  Hauptversion sein und die sein, die der Release-Feed als neueste stabile meldet.
- Nur Admin-Anfragen werden weitergereicht, jede steht im Audit-Log (Typ
  „OPNexus-System“).
- **Hauptversions-Sprünge** (1.x → 2.0) lassen sich bewusst nicht per Klick auslösen:
  Release-Notes lesen, dann `opnexus update 2.0.0`.

Voraussetzungen: Release-Images und ein aktiver Release-Feed. Ein Update wechselt die
Images, **nicht** die Compose-Datei; braucht eine Version neue Compose-Einträge,
steht das in den Release-Notes.

### Datenbank-Namen

Neue Installationen legen Datenbank und Benutzer als `opnexus` an (die Namen setzen
`POSTGRES_DB` und `POSTGRES_USER`, nur beim allerersten Start eines leeren Volumes).
Eine **ältere** Installation mit anderen Namen trägt diese unverändert in die `.env`
ein, dann läuft alles wie zuvor (nachsehen mit
`docker compose exec postgres psql -U <benutzer> -l`). Umbenennen: Dienste außer
`postgres` stoppen, per `psql -U <benutzer> -d postgres` `ALTER DATABASE <alt> RENAME TO
opnexus;` und `ALTER ROLE <alt> RENAME TO opnexus;` ausführen, `POSTGRES_DB` und
`POSTGRES_USER` auf `opnexus` setzen oder entfernen und neu starten. Vorher mit
`opnexus backup` sichern.

### Zweite Instanz auf demselben Rechner

Lege das Compose-File in ein eigenes Verzeichnis (oder nutze `docker compose -p
<name>`), mit eigener `.env`: anderes `POSTGRES_PASSWORD` und `ADMIN_PASSWORD`,
abweichende `PROXY_HTTPS_PORT` (z. B. 8443), `PROXY_HTTP_PORT`, `BACKEND_PORT`,
`FRONTEND_PORT` und passendes `FRONTEND_ORIGIN`. Browser trennen Cookies nicht nach
Port: Öffne zwei Instanzen unter derselben IP in getrennten Profilen oder Fenstern.

### Speicherplatz

Ein voller Datenträger lässt Postgres abstürzen. Behalte den Platz im Blick, setze
für die Log-Erfassung Aufbewahrung und Limit passend (siehe [Logs](#logs)) und
räume gelegentlich ungenutzte Docker-Images und Build-Caches auf
(`docker image prune`, `docker builder prune`).

### Versionierung

OPNexus nutzt [Semantic Versioning](https://semver.org/lang/de/) und gibt jeder
Minor-Version einen Raubvogel-Codenamen. Ein Patch-Release behält den Namen. Die
laufende Version steht im Footer, in den Einstellungen und auf der About-Seite; alle
Änderungen stehen im [Changelog](../CHANGELOG.md).

---

## Sicherheit

- **Verschlüsselung:** HTTPS über den Proxy; Backend und Frontend lauschen nur
  lokal. Cookie `Secure`/`HttpOnly`/`SameSite=Lax`. Das Backend lehnt schreibende
  Anfragen ab, deren `Origin`/`Referer` nicht derselbe Host oder in `FRONTEND_ORIGIN`
  gelistet ist.
- **Header:** HSTS, `Content-Security-Policy` (nur eigener Ursprung),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`;
  `Server` und `X-Powered-By` entfernt; API-Antworten `no-store`.
- **Firewall-Verbindungen:** nur HTTPS, Zertifikat festsetzbar (Pinning), Secrets
  nur als Dateien.
- **Passwörter:** Argon2id; Anmeldesperre; Passwortwechsel beendet alle Sitzungen.
- **Geheimnisse außerhalb der Datenbank:** API-Secrets, VPN-Schlüssel,
  Zertifikatsschlüssel, ACME-Kontodaten, Zwei-Faktor, Webhook-/SMTP-Zugangsdaten.
  Im Verlauf und im Audit stehen nie Klartext-Geheimnisse.
- **Audit:** lückenlos und per Trigger gegen Änderungen geschützt.
- **Rollen:** der Nur-Lesen-Schutz gilt serverseitig, nicht nur in der Oberfläche.
- **Keine Telemetrie.** OPNexus verbindet sich nur mit deinen Firewalls und mit den
  Benachrichtigungszielen, die du einrichtest; der Versionshinweis ist optional.
- **Fehlerdetails:** Rohe Antworttexte der Firewalls bei Verbindungsfehlern werden nicht
  an Benutzer weitergegeben, auch nicht an Nur-Lesen-Konten.

Sicherheitslücken meldest du vertraulich an **security@opnexus.dev**
(siehe [SECURITY.md](../SECURITY.md)).

---

## Fehlerbehebung

| Symptom | Ursache und Lösung |
| --- | --- |
| Browser warnt vor dem Zertifikat | Die interne CA ist noch nicht importiert, siehe [Zugriff aus dem Netz und TLS](#zugriff-aus-dem-netz-und-tls). |
| Anmeldung schlägt trotz richtigem Passwort fehl, im Netzwerk-Tab steht ein CORS-Fehler | `FRONTEND_ORIGIN` passt nicht zur aufgerufenen Adresse. Exakten Ursprung eintragen (`https://…`, ohne Schrägstrich am Ende). |
| Passwort vergessen | `./opnexus password` |
| Zwei-Faktor-Gerät und Wiederherstellungscodes verloren | Anderer Admin setzt es zurück, sonst `docker compose exec backend python -m app.reset_2fa <benutzer>`. |
| Host „offline“, Detail nennt Verbindung/Zertifikat | Adresse, API-Key und Rechte prüfen; die Firewall muss die Weboberfläche vom OPNexus-Server aus erlauben. Meldet der Host „Zertifikat passt nicht zum festgesetzten“, hat die Firewall ihr Zertifikat erneuert: unter Hosts › Bearbeiten erneut abrufen und bestätigen. |
| Firewall online, aber Firmware-Info oder Log-Erfassung schlagen fehl, im Log „beschädigte HTTP-Antwort“ / `malformed chunk footer` | Siehe [kTLS bei OPNsense 26.x](#ktls-bei-opnsense-26x). |
| Outbound-NAT-Push wird mit `OUTBOUND_MODE_BLOCKS_PUSH` abgelehnt | Gewollt: die Firewall steht auf „Automatic“. In OPNsense unter Firewall › NAT › Outbound auf „Hybrid“ stellen. |
| Änderung an der Schnittstellen-IP wird gesperrt | Die IP-Konfiguration per API gibt es erst ab OPNsense 26.7.6. |
| Nach einer Netzwerkänderung ist die Firewall nicht mehr erreichbar („angewendet, nicht prüfbar“) | Neue Adresse unter Hosts › Bearbeiten eintragen. Notfalls die Sicherung vor der Änderung verwenden. |
| Aktion gesperrt, Meldung nennt offene Wartung, Wiederherstellung oder offenes Deployment | Zuerst den offenen Vorgang bestätigen, abschließen oder abbrechen. |
| Beide Knoten stehen im CARP-Wartungsmodus | An dem Knoten, der zuerst herabgestuft wurde, „Normalbetrieb herstellen“; hängt der Vorgang, „Vorgang abbrechen“. |
| Backend startet nicht, Meldung zur Datenbank-Revision | Die Datenbank wurde von einer neueren Version migriert. Neuere Version starten oder eine Sicherung einspielen. |
| Backend startet nicht, Migrations-Backup fehlgeschlagen | Platz auf `postgres_backups` und Version des Postgres-Servers prüfen (Dump und Server brauchen dieselbe Hauptversion). |
| Alles wird langsam oder Postgres startet neu | Datenträger voll: Platz schaffen, Log-Aufbewahrung und -Limit senken. |
| Zeitfilter in den Logs sind verschoben | Die Firewall steht nicht auf UTC, die Zeitstempel sind unkorrigiert. |

### kTLS bei OPNsense 26.x

**Symptom:** Firmware-Info und Log-Erfassung schlagen fehl, obwohl die Firewall online
ist und kleine Abfragen funktionieren; im Backend-Log steht `malformed chunk footer`
oder „beschädigte HTTP-Antwort“, in der Oberfläche ein 502.

**Ursache:** OPNsense 26.7 verschlüsselt TLS im Kernel (kTLS,
`kern.ipc.tls.enable=1`). In manchen Umgebungen (z. B. Proxmox/virtio) werden dadurch
Antworten ab etwa 64 KiB beim Abruf von einem *anderen* Rechner verfälscht. Das ist
kein Fehler in OPNexus.

**Lösung:** Auf der Firewall unter *System › Einstellungen › Tunables* den Eintrag
`kern.ipc.tls.enable` mit Wert `0` anlegen und anwenden. Er wirkt sofort und bleibt
nach einem Neustart erhalten. Alternativ legt ihn OPNexus unter
*Host-System › Tunables* an.

---

## Grenzen

- **Keine Mehrmandantenfähigkeit**, keine Organisationen.
- **Kein IP-Allowlisting, keine Passkeys/WebAuthn;** die Sitzungsdauer beträgt 30 Tage.
- **Regeln:** kein Export und Import von Regeldefinitionen, keine Konfigurations-
  Templates oder Policy-Pakete mit atomarem Commit. Der Regel-Drift-Abgleich nutzt
  einen Fingerabdruck und ist kein vollständiger Feld-für-Feld-Vergleich.
- **Rollback:** nur für Regeln, Aliase und NAT (Deploys). Löschen, VPN- und
  Netzwerkänderungen haben keinen automatischen Rollback.
- **Wiederherstellung** einer Firewall-Sicherung ist eine geführte Übergabe an
  OPNsense, keine Ausführung durch OPNexus.
- **Logs:** kein Live-Streaming (Polling im Sekundenbereich), die Freitextsuche ist
  eine einfache Teilstring-Suche ohne Index.
- **Geplante Wartung:** nur einmalige Zeitpunkte, ohne Benachrichtigung bei
  Fehlschlag.
- **Dienste:** keine Auswahlliste, alle Dienste sind steuerbar.
- **VPN:** Umfang je Verfahren siehe [VPN](#vpn-ipsec-openvpn-wireguard).
- **Zertifikate:** kein Push auf mehrere Hosts, keine Verwaltung von DNS-Providern
  und Nachbearbeitungs-Aktionen, kein eigener Erneuerungs-Scheduler.
- **Netzwerk:** keine Vorlagen über mehrere Firewalls, kein automatisches
  Rolling-Apply auf CARP-Paaren (nur das Übertragen auf den Partner per Klick),
  keine Kea-DDNS-Felder.
- **Übernommene Regeln** gelten je Firewall einzeln: wer eine übernommene Regel
  zusätzlich auf einen weiteren Host zielt, legt dort eine zweite, doppelte Regel an
  (die Vorschau zeigt „create“).
- **Benachrichtigungstexte, Server-Logs und OPNsense-Antworten** sind nicht
  übersetzt.

---

## Bedienung, Sprache, Barrierefreiheit

- **Navigation:** acht Gruppen in der Kopfzeile mit Untermenü; unterhalb von etwa
  1150 px nur Symbole mit Tooltip. Auf dem Smartphone passen sich Formulare,
  Tabellen und Dialoge an.
- **Schnellsuche:** `Strg`/`⌘` + `K` oder der Such-Knopf. Findet Seiten, Hosts und
  Inhalte (Regeln, Aliase, NAT, VPN, …) und springt direkt dorthin.
- **Hinweise** stehen als Info-Symbole und Tooltips statt als Textblöcke, sie
  erscheinen bei Hover, Tastaturfokus und Tippen und schließen mit `Esc`.
- **Darstellung:** Hell, Dunkel oder System, pro Konto gespeichert.
- **Sprache:** Deutsch oder Englisch, pro Konto (Einstellungen › Sprache). Die Wahl
  gilt sofort und wird auf allen Geräten übernommen.
- **Barrierefreiheit:** Ziel ist WCAG 2.2 AA: vollständige Tastaturbedienung mit
  Sprunglink, sichtbarem Fokus, Dialogen mit Fokusfalle, wechselnden Seitentiteln und
  Kontrasten in beiden Darstellungen. Ein Test mit einem echten Screenreader steht
  noch aus.

---

## Lizenz und Datenschutz

OPNexus steht unter der **PolyForm Shield License 1.0.0** (siehe [LICENSE](../LICENSE)).
Das ist eine *source-available*-Lizenz und **keine** Open-Source-Lizenz im Sinne der
OSI:

- **Erlaubt:** kostenlos herunterladen, Quellcode ansehen, für eigene Zwecke
  anpassen und einsetzen, auch geschäftlich, etwa in einer Firma für die eigenen
  Firewalls.
- **Nicht erlaubt:** OPNexus oder Ableitungen als Produkt oder Dienst anzubieten
  oder weiterzuverkaufen, das mit OPNexus konkurriert, auch nicht kostenlos.

Diese Zusammenfassung ersetzt nicht den Lizenztext.

**Datenschutz:** OPNexus wird von dir selbst betrieben und hat keine Telemetrie. Alle
Daten liegen auf deinem Server. Ausgehende Verbindungen gehen nur zu deinen
Firewalls, zu eingerichteten Benachrichtigungszielen und, falls aktiviert, zum
Versionshinweis.

```
Required Notice: Copyright 2026 Julian Boldt (https://opnexus.dev)
```
