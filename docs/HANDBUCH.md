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
15. [Automatisierung: API-Tokens, Prometheus, KI-Assistenten](#automatisierung-api-tokens-prometheus-ki-assistenten)
16. [OPNexus betreiben](#opnexus-betreiben)
17. [Sicherheit](#sicherheit)
18. [Fehlerbehebung](#fehlerbehebung)
19. [Grenzen](#grenzen)
20. [Bedienung, Sprache, Barrierefreiheit](#bedienung-sprache-barrierefreiheit)
21. [Lizenz und Datenschutz](#lizenz-und-datenschutz)

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
install -m 600 .env.example .env   # POSTGRES_PASSWORD setzen (und was sonst nötig ist); nur für dich lesbar
docker compose pull && docker compose up -d --no-build
./opnexus password            # zeigt das Admin-Passwort, wenn ADMIN_PASSWORD leer war
```

### Installation aus dem Quellcode

```bash
git clone https://git.opnexus.dev/opnexus/opnexus.git && cd opnexus
install -m 600 .env.example .env   # POSTGRES_PASSWORD setzen; nur für dich lesbar
docker compose up -d --build
scripts/opnexus password
```

Alternativ lassen sich auch aus einem Checkout die fertigen Images verwenden: in
der `.env` `OPNEXUS_IMAGE_REPO=git.opnexus.dev/opnexus` und `OPNEXUS_VERSION=<version>`
setzen, dann `docker compose pull && docker compose up -d --no-build`.

### Installation unter Windows

Unter Windows läuft derselbe Stack in **Docker Desktop mit WSL 2**: WSL und Docker Desktop
installieren, in der WSL-Umgebung (Ubuntu) das Release-Bundle wie oben entpacken, `.env`
anlegen und `docker compose up -d --no-build` ausführen; die Oberfläche öffnet sich unter
`https://localhost`. Die ausführliche Anleitung mit Zertifikatsimport, Zugriff aus dem Netz
(Windows-Firewall), Updates und Fehlerbehebung steht auf der Website:
<https://opnexus.dev/docs/windows/>. Entwickelt und getestet wird OPNexus unter Linux; für den
Dauerbetrieb ist ein Linux-Server oder eine Linux-VM besser, weil Docker Desktop erst nach der
Anmeldung startet und im Ruhezustand anhält.

### Images prüfen (Signatur)

Die Release-Images sind mit [cosign](https://github.com/sigstore/cosign) signiert (ab 1.23.9).
Damit lässt sich prüfen, dass ein Image wirklich von OPNexus stammt und seit dem Bau nicht
verändert wurde. Der öffentliche Schlüssel liegt als `cosign.pub` im Repository und im
Release-Bundle:

```bash
cosign verify --key cosign.pub --insecure-ignore-tlog=true \
  git.opnexus.dev/opnexus/opnexus-backend:<version>
```

Das Gleiche gilt für `opnexus-frontend`, `opnexus-proxy` und `opnexus-updater`. Eine
erfolgreiche Prüfung gibt die Signatur als JSON aus; bei einem fremden oder fehlenden
Schlüssel endet der Befehl mit einem Fehler. `--insecure-ignore-tlog=true` ist hier gewollt:
OPNexus lädt Signaturen nicht in das öffentliche Transparenzlog, damit die Prüfung auch in
abgeschotteten Netzen funktioniert. Die Signatur gilt dem Inhalt (Digest) des Images, nicht
dem Tag.

`opnexus update` prüft die Signaturen selbst, bevor es etwas wechselt, sobald `cosign`
installiert ist und `cosign.pub` neben der `docker-compose.yml` liegt (`cosign.pub` liegt im Bundle
bereits dabei, `cosign` selbst musst du installieren). Der Updater-Container für Updates per Klick
bringt `cosign` mit und prüft deshalb ebenfalls, solange `cosign.pub` im Projektverzeichnis liegt. Schlägt die Prüfung fehl, bricht das
Update ab und es wurde nichts verändert. Fehlt `cosign`, sagt `opnexus update` das und
macht ohne Prüfung weiter.

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
| `UPDATE_CHECK_URL`, `UPDATE_CHECK_INTERVAL_HOURS` | Hinweis auf neue Versionen, standardmäßig **aus**. Einstellbar auch im Webinterface; ein Wert in der `.env` hat Vorrang. |
| `COMPOSE_PROFILES=updater` | Aktiviert den optionalen Updater für Updates per Klick. |
| `ALLOW_INSECURE_FIREWALL_URLS` | Erlaubt `http://`-Adressen für Firewalls. Nur für isolierte Labore. |
| `SMTP_CA_FILE`, `ALLOW_INSECURE_SMTP_AUTH` | Eigene CA für den Mailserver bzw. SMTP-Anmeldung ohne Verschlüsselung (bleibt standardmäßig gesperrt). |

---

## Benutzer, Rollen und Anmeldung

**Rollen.** Es gibt drei:

| Rolle | Darf |
|---|---|
| `admin` | alles, inklusive Benutzer, Einstellungen, Hosts anlegen/löschen, OPNexus-Update, Roh-Download von Konfigurationssicherungen |
| `operator` | ausrollen und betreiben: Regeln, Aliase, NAT, VPN, Zertifikate, Wartung, Sicherungen, Sicherheitsprüfung. **Nicht:** Benutzer, Einstellungen (Benachrichtigungen, Backups, Audit-Export, Logs), Hosts anlegen/ändern/löschen, OPNexus-Update, Roh-Backups herunterladen |
| `read_only` | sieht alles, ändert nichts |

Ein Nur-Lesen-Benutzer kann nichts ändern: alle Schreibaktionen sind in der
Oberfläche gesperrt und werden vom Backend mit 403 abgelehnt. Ein Operator bekommt bei
den Admin-Aktionen ebenfalls 403 („erfordert die Rolle Admin“); der Versuch steht im
Audit. Nur das eigene Passwort sowie Farbschema und Sprache bleiben für jede Rolle änderbar.

**Single Sign-on (OIDC).** Unter Einstellungen › Single Sign-on (OIDC) verbindest du einen
Identitätsanbieter (Authentik, Keycloak, Entra ID, Google, …). Auf der Anmeldeseite erscheint dann ein
zusätzlicher Knopf („Mit … anmelden“). Du brauchst beim Anbieter einen Client mit dem Ablauf
*Authorization Code* (OPNexus nutzt zusätzlich PKCE) und trägst dort die angezeigte
Weiterleitungs-URI ein (`https://<dein-opnexus>/api/auth/oidc/callback`). In OPNexus gibst du
Aussteller-URL, Client-ID, Client-Geheimnis und die **Gruppennamen** an, die die Rollen Admin, Operator
und Nur-Lesen bekommen. „Anbieter testen“ prüft Discovery und Signaturschlüssel.

- **Rolle aus Gruppen, bei jeder Anmeldung:** gilt die höchste passende Rolle; wer in keiner der Gruppen ist,
  kommt nicht hinein (es wird auch kein Konto angelegt). Wird jemand aus der Gruppe entfernt, endet der Zugang
  bei der nächsten Anmeldung (laufende SSO-Sitzungen enden spätestens nach 12 Stunden; beim Anbieter gesperrte Personen
  daher zusätzlich in der Benutzerliste löschen).
- **Konten:** SSO-Benutzer werden bei der ersten Anmeldung angelegt (Kennzeichen „SSO“). Rolle und Passwort
  ändern sich nur beim Anbieter; die Zwei-Faktor-Anmeldung übernimmt der Anbieter. Ein SSO-Konto wird nie mit
  einem lokalen Konto gleichen Namens verknüpft: bei einem Namenskonflikt bekommt es ein Suffix (`~sso`).
- **Lokale Konten bleiben:** Die Anmeldung mit Benutzername und Passwort funktioniert weiter, etwa für den
  Notfall, wenn der Anbieter ausfällt.
- **Geprüft** werden Signatur (nur asymmetrische Verfahren, nie `none` oder HMAC), Aussteller, Zielgruppe, Ablauf
  und Nonce des ID-Tokens; jeder Anmeldeversuch (State) gilt genau einmal und 10 Minuten und ist über ein Cookie (bei HTTPS `__Host-opnexus-oidc-state`) an den Browser gebunden, der ihn gestartet hat; ein Aufruf ohne dieses Cookie scheitert immer. Gruppen kommen aus dem
  ID-Token, ersatzweise aus dem Userinfo-Endpunkt. Aussteller und Endpunkte müssen `https://` sein. Das ist
  gegen einen nachgebauten Anbieter getestet (inklusive gefälschter Token) und gegen echte Server von Keycloak 26 und
  Authelia 4.39 (Anmeldung, Rollen aus Gruppen, Rollenentzug, abgefangener Callback); andere Anbieter: melde, was bei deinem nicht klappt.
  Die Standard-Scopes sind `openid profile email`; die Gruppen müssen als Claim im ID-Token oder Userinfo ankommen
  (Keycloak: Mapper „Group Membership“ am Client; Entra ID: optionaler Gruppen-Claim). Anbieter, die Gruppen nur auf
  Anfrage senden (z. B. Okta, Authelia), brauchen zusätzlich den Scope `groups`.

**Aufgabentrennung.** Einstellungen › Aufgabentrennung: Schaltest du „Operatoren dürfen Änderungen selbst
ausrollen“ aus, bereiten Operatoren Änderungen nur vor (anlegen, bearbeiten, Vorschau), und **nur ein Admin
rollt aus**: ausrollen, ein Deployment entfernen oder zurückrollen, Wartung ausführen oder planen,
Netzwerkänderungen anwenden. Die Ablehnung („nur ein Administrator“) steht im Audit. Das ist ein Zwei-Personen-Schritt
(Vorbereiten und Ausrollen durch verschiedene Personen), keine Freigabe-Warteschlange.

**Benutzerverwaltung** (System › Benutzer, nur Admins): Benutzer anlegen, Rolle
ändern, Passwort zurücksetzen, Zwei-Faktor eines anderen Benutzers zurücksetzen,
löschen. Du kannst dich nicht selbst löschen und dich nicht zum letzten Admin
herabstufen. **Nur Admins sehen die Benutzerliste** (`GET /api/users`); jede andere Rolle,
auch ein Token, sieht nur das eigene Konto.

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
- **Eine Adresse, ein Host:** Dieselbe Adresse zweimal einzutragen lehnt OPNexus ab
  (Groß-/Kleinschreibung und ein abschließender `/` zählen nicht); es würde jeden Rollout,
  Alarm und jede Sicherung verdoppeln.
- **Verbindungsprüfung:** Die Hosts-Seite und der Einrichtungsassistent fragen die
  Firewall beim Anlegen einmal mit den eingegebenen Zugangsdaten ab. Klappt das nicht,
  wird **nichts gespeichert**, und die Meldung nennt den Grund (Schlüssel abgelehnt,
  zu wenig Rechte, Zertifikat passt nicht, keine Verbindung). **Trotzdem anlegen** speichert
  den Host ohne Prüfung, zum Beispiel für eine Firewall hinter NAT, deren Tunnel noch
  nicht steht. Über die API ist die Prüfung mit `"verify_connection": true` zuschaltbar
  (Standard aus, damit Skripte auch nicht erreichbare Firewalls eintragen können).

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
- **Löschen** verlangt den **Hostnamen**: in der Oberfläche im Bestätigungsdialog, über
  die API als `DELETE /api/hosts/{id}?confirm_name=<Name>` (sonst `409`). Beim Löschen
  gehen die gespeicherten Zugangsdaten sofort verloren.
- **Deaktivieren** nimmt einen Host aus Rollouts und Abfragen, ohne ihn zu löschen.
  Ein Host, auf den noch Regeln, Aliase oder NAT ausgerollt sind, lässt sich nicht
  löschen: Sonst ginge OPNexus der Überblick verloren, was dort liegt. Entferne
  zuerst die Deployments (Regel löschen oder früheres Ziel unter „Prüfen & Push“
  bereinigen).

### Firewalls hinter NAT (Management-Tunnel)

OPNexus muss die API einer Firewall erreichen können. Hat die Firewall keine öffentliche Adresse (hinter NAT,
CGNAT, Mobilfunk-Router), baust du mit dem **Management-Tunnel** (System › Management-Tunnel) einen
WireGuard-Hub auf dem OPNexus-Server auf: Die Firewall wählt sich **nach außen** dort ein, OPNexus erreicht
ihre API durch den Tunnel. An der Firewall muss nichts aus dem Internet erreichbar sein. Der Tunnel ist
nur für die Verwaltung gedacht; Nutzverkehr läuft nicht darüber.

**Voraussetzungen:** der Hub ist ein optionaler Container. In `.env` `COMPOSE_PROFILES=overlay` setzen (bei
mehreren Profilen kommagetrennt), `docker compose up -d`, und der UDP-Port `OVERLAY_PORT` (Standard 51820)
des Servers muss aus dem Internet erreichbar sein (Port-Weiterleitung am Router des OPNexus-Servers). Der Hub
läuft mit dem Image des Backends und braucht nur die Berechtigung `NET_ADMIN` für die WireGuard-Schnittstelle;
das Linux-Kernelmodul `wireguard` muss auf dem Server vorhanden sein (jeder aktuelle Kernel).

**Einrichten:** Im Management-Tunnel einschalten, die öffentliche Adresse (DNS-Name oder IP) des Servers
eintragen, speichern. Dann pro Firewall **„Firewall hinzufügen“**: OPNexus vergibt eine Tunnel-Adresse (z. B.
`10.99.0.2`) und einen Pre-Shared Key und zeigt eine Anleitung mit kopierbaren Werten:

1. In OPNsense (VPN › WireGuard) eine **lokale Instanz** anlegen, Schlüsselpaar erzeugen lassen, die
   Tunnel-Adresse eintragen.
2. Den **Hub als Gegenstelle** eintragen (öffentlicher Schlüssel, Pre-Shared Key, Endpunkt, erlaubte IP
   `10.99.0.1/32`, Keepalive 25).
3. Eine **Firewall-Regel** auf der WireGuard-Schnittstellengruppe: TCP von der Hub-Adresse zu „Diese
   Firewall“, Port der Weboberfläche (443).
4. Den **öffentlichen Schlüssel** der neuen Instanz bei „Schlüssel / Host“ in OPNexus eintragen.
5. Die Firewall wie gewohnt als **Host** anlegen, aber mit der Tunnel-Adresse (`https://10.99.0.2`). Der
   Status im Management-Tunnel wechselt auf „verbunden“.

Ab dann funktioniert alles wie bei jeder anderen Firewall (Regeln, Wartung, Logs, Sicherungen,
Sicherheitslage). Das Festsetzen des Zertifikats funktioniert auch durch den Tunnel.

**Sicherheit:** Der Hub nimmt von den Firewalls **keine neuen Verbindungen** an (iptables `INPUT … NEW`
verworfen), eine kompromittierte Außenstelle erreicht also weder OPNexus noch andere Außenstellen. Der
Proxy, über den OPNexus die Firewalls erreicht, ist nur im Compose-Netz erreichbar und verbindet
ausschließlich in das Tunnel-Netz. Schlüssel liegen als Dateien im Volume `overlay_state` (dort auch der
private Schlüssel des Hubs; die Vollsicherung enthält ihn). Je Firewall gibt es einen eigenen
Pre-Shared Key; „Löschen“ entfernt sie sofort aus dem Hub. Hosts im Tunnel-Netz werden nur über IP-Adressen
angesprochen (keine DNS-Namen). Nicht enthalten: Die Einrichtung auf der Firewall übernimmt OPNexus nicht
(bei einer Firewall hinter NAT kann es sie ja nicht erreichen); bei einer erreichbaren Firewall kannst du
alternativ unter VPN › WireGuard einen Tunnel mit denselben Werten verteilen.

### Firewall-Oberfläche über OPNexus

Mit **Oberfläche öffnen** auf der Host-Seite öffnest du die Weboberfläche einer Firewall in einem neuen
Browser-Tab, und zwar **über OPNexus**. Das lohnt sich vor allem bei Firewalls hinter NAT, die nur über den
[Management-Tunnel](#firewalls-hinter-nat-management-tunnel) erreichbar sind: Dein Browser braucht keinen eigenen
Zugang zum Tunnel-Netz. Du meldest dich an der Firewall wie gewohnt mit deren Benutzer und Passwort an; OPNexus
kennt und speichert diese Zugangsdaten nicht und meldet nichts automatisch an.

**Standardmäßig ist die Funktion aus** und doppelt gesichert: Sie braucht eine Einrichtung auf dem Server und
einen Schalter in den Einstellungen.

**Einrichten** (in der `.env` im OPNexus-Verzeichnis):

1. **HTTPS und `COOKIE_SECURE=true`** (siehe [Zugriff aus dem Netz und TLS](#zugriff-aus-dem-netz-und-tls)).
   Ohne sichere Cookies lässt sich die Funktion nicht einschalten.
2. **Compose-Profil `gui`** aktivieren: `COMPOSE_PROFILES=gui` (mit weiteren Profilen kommagetrennt, z. B.
   `overlay,gui`). Das startet den Container `gui-gateway`.
3. **Slot-Adressen** festlegen: `OPNEXUS_GUI_SITES=https://10.0.0.5:9441, https://10.0.0.5:9442, https://10.0.0.5:9443, https://10.0.0.5:9444`.
   Es ist dieselbe Adresse wie in `OPNEXUS_SITES`, je Slot ein Port. Diese Ursprünge dürfen **nicht** in
   `FRONTEND_ORIGIN` stehen (OPNexus startet sonst nicht).
4. **Gemeinsames Geheimnis** `GUI_GATEWAY_TOKEN` (mindestens 32 Zeichen, z. B. `openssl rand -hex 32`).
5. **Ports:** `GUI_PORTS=9441-9444` (die Ports aus `OPNEXUS_GUI_SITES`) sind die Slot-Ports am Proxy. Setze den
   Eintrag ausdrücklich: ohne ihn vergibt Docker zufällige freie Ports, damit ein Update nicht an schon belegten
   Ports (z. B. Portainer auf 9443) scheitert. `GUI_BIND_ADDRESS` bestimmt, wo
   sie lauschen; Standard ist `127.0.0.1` (nur lokal). Für den Zugriff aus dem Netz trägst du die Adresse des
   OPNexus-Rechners ein und gibst die Ports in der Firewall des Rechners nur für Administratoren frei.
6. `docker compose up -d`, dann unter **Einstellungen › Firewall-Oberfläche über OPNexus** den Schalter einschalten.
   Fehlt etwas, nennt die Einstellungsseite genau, was.

In den Einstellungen legst du außerdem fest: **Inaktivität** (Standard 15 Minuten, 1 bis 240), **Gesamtdauer**
(Standard 60 Minuten, 5 bis 720) und die Zahl der **gleichzeitigen Plätze** (Standard 4, 1 bis 4). Ausschalten
beendet alle offenen Sitzungen (das Gateway bemerkt es spätestens nach 20 Sekunden).

**So ist es abgesichert:**

- **Eigene Origin:** Jede Firewall-Oberfläche läuft auf einem eigenen Port (einem „Platz“) und damit in einem
  eigenen Browser-Ursprung, getrennt von OPNexus. Skripte der Firewall-Oberfläche können OPNexus nicht
  ansprechen; OPNexus lehnt außerdem Anfragen mit Anmelde-Cookie von fremden Ursprüngen ab.
- **Cookies bleiben auf dem Server:** Das Gateway reicht die OPNexus-Sitzung nie an die Firewall weiter und
  die Cookies der Firewall nie an deinen Browser. Die Firewall-Sitzung liegt nur im Arbeitsspeicher des Gateways.
- **Nur Admins**, nur mit angemeldeter Browsersitzung (nie mit einem API-Token). Das Gateway prüft alle 20
  Sekunden nach: Abmelden, Entzug der Admin-Rolle oder Ausschalten beendet die Sitzung.
- **Einmal-Ticket:** Der Klick erzeugt ein Ticket, das nur **30 Sekunden** und **genau einmal** gilt und an deine
  Sitzung und den Platz gebunden ist. Danach verschwindet es aus der Adresszeile.
- **Nur sichere Firewalls:** https-Adresse und geprüftes oder festgesetztes Zertifikat (siehe Pinning); sonst
  bleibt der Knopf deaktiviert und nennt den Grund.
- **Zeitlimits** (Inaktivität und Gesamtdauer) und **Audit:** Unter **Monitoring › Audit** (Art „GUI-Zugriff“)
  stehen Ticket, Start und Ende jeder Sitzung mit Benutzer, Firewall, Platz und Client-IP. Was du in der
  Oberfläche tust, protokolliert die Firewall selbst, dort aber mit der Adresse von OPNexus; das OPNexus-Audit
  zeigt, wer dahinter stand.
- **Anmeldeschutz:** Das Gateway lässt höchstens drei fehlgeschlagene Anmeldungen pro Firewall in fünf Minuten zu.

**Wichtig: OPNexus-Adresse beim Anmeldeschutz der Firewall eintragen.** Über das Gateway kommen alle
Anmeldungen von der Adresse von OPNexus (bei Firewalls im Tunnel von der des Hubs). Die Anmeldesperre von
OPNsense (sshguard) sperrt nach mehreren Fehlversuchen diese Adresse, und damit auch die **API-Zugriffe von
OPNexus**. Trage sie deshalb auf jeder Firewall unter **System › Einstellungen › Administration** (Login
Protection) in die Whitelist ein, bevor du die Funktion benutzt.

**Einschränkungen:**

- **Kein WebSocket** (Funktionen, die einen brauchen, gehen nicht) und **keine automatische Anmeldung**.
- **Höchstens 4 Plätze gleichzeitig.** Ein Platz bleibt pro OPNexus-Sitzung bei derselben Firewall; sind alle
  belegt, schließe eine Oberfläche oder warte, bis sie abläuft.
- **Abkühlzeit:** Ein frei gewordener Platz geht an eine **andere** Firewall erst **5 Minuten** nach dem Ende der
  letzten Sitzung dort (derselbe Benutzer; eine noch offene Sitzung zählt als belegt). Dieselbe Firewall bekommt
  ihn sofort wieder. Grund: Ein Tab der alten Firewall-Oberfläche kann noch offen sein und teilt sich mit dem
  Platz die Cookies. Sind alle Plätze belegt oder noch in der Abkühlzeit, nennt OPNexus den Grund; schließe die
  alten Tabs nach der Arbeit.
- **Änderung der Firewall-Adresse:** Ändern sich Adresse, festgesetztes Zertifikat oder Tunnelweg einer Firewall,
  während ihre Oberfläche offen ist, endet die Sitzung spätestens bei der nächsten Prüfung (20 Sekunden).
- **Höchstens 64 Sitzungen** laufen gleichzeitig. Eine laufende Sitzung wird nie von einer neuen verdrängt; ist die
  Grenze erreicht, wird die neue abgelehnt, bis eine endet.
- Nach einem **Neustart des wg-hub** braucht der Management-Tunnel 2 bis 4 Minuten, bis die Firewalls
  sich wieder verbunden haben; bis dahin lässt sich die Oberfläche nicht öffnen.
- Mit `COOKIE_SECURE=false` ist die Funktion nicht verfügbar.

**Hinweis für Betreiber mit eigenen Skripten:** Seit dieser Funktion heißt das Sitzungs-Cookie bei HTTPS
`__Host-opnexus-session`, und Anfragen an `/api` mit Cookie
und `Sec-Fetch-Site: same-site` oder `cross-site` werden abgelehnt. Seit 1.29 wird das alte Cookie `opnexus-session`
bei HTTPS **nicht mehr akzeptiert**: wer sich seit der Umstellung nicht neu angemeldet hat, muss das einmal tun
(Skripte mit einem rohen Sitzungs-Cookie müssen den neuen Namen verwenden; ohne HTTPS bleibt `opnexus-session` der
normale Name). Skripte mit API-Token (`Authorization: Bearer`)
sind nicht betroffen.

**Alternative ohne diese Funktion:** Du richtest dir einen eigenen WireGuard-Zugang in das Netz der Firewalls ein
(z. B. am Standort oder auf der Firewall selbst) und rufst die Oberfläche direkt auf. Das ist unabhängig von
OPNexus, hat aber keine Rollenprüfung und kein Audit durch OPNexus.

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
- **Nach einem Fehler beim Anwenden** (Konfiguration geschrieben, Anwenden aber nicht bestätigt) darfst du
  erneut ausrollen. Der Rückweg bleibt dabei der **ursprüngliche** Stand vor dem ersten Versuch: eine neue Regel wird
  beim Fristablauf entfernt, eine geänderte auf den bestätigten Stand zurückgesetzt.
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

### Aliase und NAT von der Firewall übernehmen

Wie die Regeln lassen sich auch **Aliase** und **NAT-Regeln** (Port Forward, 1:1, Outbound) übernehmen, die von Hand
in OPNsense entstanden sind: auf der jeweiligen Seite **Von Firewall übernehmen**, Firewall wählen, dann entweder ein
Objekt mit **Übernehmen** oder alle auf einmal mit **Alle übernehmbaren übernehmen**. So kommt eine bestehende Firewall
ohne Abtippen unter zentrale Verwaltung.

Die Übernahme **schreibt nichts auf die Firewall**. OPNexus liest das Objekt frisch von der Firewall, legt es zentral an
(Ziel: genau diese Firewall) und merkt sich die UUID aus OPNsense. Deshalb zeigt die Vorschau direkt danach „keine Änderung“,
und der nächste Rollout nach einer Änderung aktualisiert dasselbe Objekt, statt ein zweites anzulegen. Die Übernahme steht im
Audit-Log; Operatoren dürfen übernehmen (sie legt nur zentrale Einträge an), Nur-Lese-Benutzer nicht.

Nicht übernommen wird, was das Modell verfälschen würde. Die Oberfläche nennt den Grund je Objekt:

| Objekt | Nicht übernehmbar, weil … |
|---|---|
| Aliase | OPNsense-eigen (`bogons`, `sshlockout`, `__lan_network` …); Typ unbekannt für OPNexus (URL, URL-Tabelle, GeoIP, dynamische IPv6-Hosts, OpenVPN-Gruppe, „Extern“); der Name existiert in OPNexus schon; Name mit Sonderzeichen; BGP-ASN mit IPv4/IPv6-Einschränkung; Verweis auf einen Alias, der nicht übernommen ist |
| Port Forward | mehrere oder keine Schnittstelle; IP-Version weder IPv4 noch IPv6; Protokoll „beliebig“; „Kein Redirect“; kein Ziel; verknüpfte Filterregel; invertierte Quelle oder Ziel; NAT-Reflection-Wert unbekannt; Beschreibung fehlt |
| 1:1-NAT | mehrere oder keine Schnittstelle; unbekannter Typ oder NAT-Reflection-Wert; Pflichtangaben oder Beschreibung fehlen |
| Outbound-NAT | mehrere oder keine Schnittstelle; „Kein NAT“-Regel (`nonat`); IP-Version weder IPv4 noch IPv6; Beschreibung fehlt |

Automatisch erzeugte NAT-Regeln (Anti-Lockout, „Auto created rule“) erscheinen nie in der Liste. **Verschachtelte Aliase:**
Ein Alias, der einen anderen, noch nicht übernommenen Alias enthält, wartet auf diesen; „Alle übernehmbaren übernehmen“
übernimmt beide in der richtigen Reihenfolge, einzeln zuerst den enthaltenen Alias. Verweist er auf einen Alias, der nie
übernommen werden kann (z. B. vom Typ URL), bleibt er mit diesem Grund zurück. **Outbound-NAT** lässt sich nur übernehmen,
wenn der Outbound-Modus der Firewall Hybrid oder Manuell ist; im Modus Automatisch zeigt OPNsense manuelle Regeln gar nicht an,
OPNexus verweist dann darauf und ändert den Modus nicht.

Einstellungen, die OPNexus nicht verwaltet (Protokollierung und Pool-Optionen beim Port Forward, Tags, Kategorien, statischer
NAT-Port), werden nie überschrieben und im Dialog als „Bleibt auf der Firewall unverändert“ angezeigt.

#### Gleiches Objekt auf mehreren Firewalls: verknüpfen statt doppelt übernehmen

Gibt es dieselbe Regel, denselben Alias oder dieselbe NAT-Regel auf mehreren Firewalls (typisch: beide Knoten eines
HA-Clusters, mehrere Standorte mit gleicher Vorgabe), übernimmst du sie **einmal** und **verknüpfst** sie auf den anderen
Firewalls mit dem zentralen Objekt. Das gilt für Regeln, Aliase, Port Forward, 1:1-NAT und Outbound-NAT.

1. Auf der ersten Firewall normal **übernehmen**.
2. Im Dialog die zweite Firewall wählen. Bei jedem Eintrag, der einem zentralen Objekt **exakt** entspricht, steht
   „Gibt es schon zentral“ und die Aktion **Verknüpfen** (daneben **Als eigenes Objekt übernehmen**). Verknüpfen trägt die
   Firewall als weiteres Ziel in das Objekt ein und merkt sich die UUID dort. Auf der Firewall wird nichts geändert.
3. Die Vorschau zeigt danach für diese Firewall „keine Änderung“. Eine spätere Änderung am Objekt rollst du wie gewohnt für
   alle Ziele gemeinsam aus; es entsteht kein Duplikat.

**Was „exakt“ heißt:** Alle von OPNexus verwalteten Felder sind gleich, nachdem die `{{VARIABLEN}}` des zentralen Objekts mit
den Werten **dieser** Firewall ersetzt wurden (ein Alias `{{LAN_NET}}` passt also zu `192.168.2.0/24`, wenn das die Variable
dieser Firewall ist). Bei Aliasen müssen Name, Typ, Inhalt einschließlich der Verweise auf andere Aliase, Beschreibung und
Aktiv-Status stimmen. Fehlt der Firewall ein Variablenwert, passt das Objekt nicht, der Dialog nennt die Variable.

**Abweichungen:** Gibt es ein Objekt mit gleichem Namen (Alias) bzw. gleicher Beschreibung (Regeln, NAT), das sich im Inhalt
unterscheidet, zeigt der Dialog „Ähnlich“ mit den abweichenden Feldern (zentral gegenüber Firewall). Verknüpft wird dann **nie**,
auch nicht automatisch. Gleiche entweder die Firewall oder das zentrale Objekt an, oder übernimm das Objekt getrennt (bei Aliasen
geht das nur nach einer Umbenennung, der Name ist zentral eindeutig).

**Alle übernehmbaren:** Der Dialog zeigt vorher, was passiert („3 verknüpfen, 2 neu übernehmen, 1 nicht übernehmbar“), und du
bestätigst. Verknüpft wird nur, wenn genau **ein** zentrales Objekt exakt passt; gibt es mehrere identische (Altlast aus früheren
Einzelübernahmen), entscheidest du bei dem Eintrag selbst, und „Alle“ übernimmt ihn als neues Objekt. Ein zentrales Objekt wird in
einem Lauf höchstens einmal verknüpft.

**Sicherheit:** Die Firewall wird beim Verknüpfen frisch gelesen und noch einmal exakt verglichen; hat sich zwischen Anzeige und Klick
etwas geändert, wird nichts verknüpft und die Meldung nennt die Felder. Ein zweiter Klick ändert nichts. Die Aktion steht im
Audit-Log und in den Revisionen des Objekts („Mit <Firewall> verknüpft“). Rollen wie beim Übernehmen: Operatoren dürfen, Nur-Lese-Benutzer
nicht. Hat das zentrale Objekt eine offene Bestätigung oder einen offenen Rollback, wird erst nach deren Klärung verknüpft.
„Alle übernehmbaren“ lässt solche Einträge dann aus (Hinweis im Ergebnis). **Wichtig:** Ein verknüpftes Objekt wird wie jedes verwaltete Objekt behandelt: Wird das zentrale Objekt gelöscht oder die Firewall aus dessen Zielen entfernt, löscht der nächste Rollout das Objekt auch auf dieser Firewall.

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
- Gruppen, die noch verwendet werden (von Regeln, Aliasen, NAT, **VPN-Objekten** oder
  Rollout-Wellen), lassen sich nicht löschen.
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

### Standort-Variablen

Mit **Variablen** pflegst du ein Objekt einmal und rollst es auf Firewalls mit unterschiedlichen Netzen aus.
Schreibe `{{NAME}}` (Großbuchstaben, Ziffern und `_`, beginnend mit einem Buchstaben) in ein Feld einer
**Regel**, eines **Alias**, einer **Portweiterleitung**, einer **1:1-NAT-** oder **Outbound-NAT-Regel**, etwa als
Quelle `{{LAN_NET}}` oder als Beschreibung `Zugriff {{SITE}}`. Den Wert legst du je Firewall auf der Host-Seite
unter **Standort-Variablen** fest (`192.168.7.0/24`, `Filiale Nord`, …). Die Seite listet automatisch alle
Platzhalter, die die für diesen Host bestimmten Objekte verwenden, und zeigt, welche noch ohne Wert sind.

- **Je Host aufgelöst:** Vorschau, Ausrollen, die Änderungszähler und das Audit sehen die echten Werte des
  jeweiligen Hosts; ändert sich ein Wert, erscheint nur bei diesem Host eine Änderung. Das zentral gespeicherte
  Objekt behält seine Platzhalter.
- **Fehlt ein Wert, wird nichts gesendet:** Die Vorschau markiert den Host mit „Variable fehlt“, das Ausrollen
  wird abgelehnt, bevor irgendein Host beschickt wurde, und als letzte Absicherung sendet OPNexus keine Anfrage
  an eine Firewall, die noch einen Platzhalter enthält.
- **Werte** dürfen Buchstaben, Ziffern und `. : / , @ + - _` sowie Leerzeichen enthalten (höchstens 255 Zeichen);
  sie werden wörtlich eingesetzt, nie als Muster oder weiterer Platzhalter ausgewertet.
- **Nicht** unterstützt: Platzhalter in VPN-Tunneln und in der Policy-Hierarchie (dort stehen die Werte direkt im
  Objekt); Variablen für Gerätegruppen (Vorgabewerte) gibt es nicht, jeder Host hat seine eigenen. Operatoren dürfen
  Variablen pflegen; ob sie auch ausrollen dürfen, regelt die [Aufgabentrennung](#benutzer-rollen-und-anmeldung).

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
  Port dauert bei OPNsense rund 10 Sekunden. Zwischenstationen, die nicht auf Traceroute
  antworten, erscheinen als Zeile „* keine Antwort“ statt als Lücke. Packet-Capture gibt es bewusst nicht,
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

Bricht ein Ausrollen mittendrin ab (Netz weg, Neustart von OPNexus), bleibt ein Eintrag mit dem Status „Fehler“ und
dem Hinweis „Ausrollen unterbrochen“ stehen, samt aller Kennungen, die die Firewall bis dahin vergeben hat. Ein
erneutes Ausrollen setzt dort fort, statt Objekte doppelt anzulegen. Pro VPN läuft immer nur ein Ausrollen oder
Entfernen; ein zweites wird mit „läuft bereits“ abgelehnt.

---

## Zertifikate und ACME

**Zertifikate** zeigt live die Zertifikate aller Firewalls (Ablauf, Status) und
verwaltet den Trust-Store sowie die ACME-Ausstellung. Alles ist **pro Host**
modelliert, weil Zertifikats- und CA-UUIDs hostlokal sind; es gibt keinen
Multi-Host-Push. **Der Ziel-Host ist nach dem ersten Push fest** (Zertifikat, ACME-Konto und
ACME-Anfrage): Für einen anderen Host löschst du den Eintrag und legst ihn neu an. Ein ACME-Konto, das
Zertifikatsanfragen nutzen, lässt sich auch vorher nicht auf einen anderen Host verschieben.

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

**Rückweg-Punkt:** Vor jedem stabilen Update und Major-Upgrade (manuell, geplant, im
Rollout und bei der CARP-Übergabe) legt OPNexus auf der Firewall einen **Rückweg-Punkt**
an: ein ZFS-Boot-Environment des laufenden Systems, benannt
`opnexus-pre-<Version>-<Zeit>`. Gelingt das nicht, startet das Update **nicht**. Hat die
Firewall kein ZFS (UFS-Installation), läuft das Update, und die Meldung sagt, dass es
keinen Rückweg gibt. Je Firewall bleiben die zwei neuesten Punkte von OPNexus erhalten;
Boot-Environments von Hand werden nie angefasst. Manuell angelegte Snapshots dürfen den
Präfix `opnexus-pre-` nicht tragen.

„An Firewall übergeben“ bestätigt nur die Annahme. Nach einem Timeout bleibt der
Ausgang unklar und wird **nie** automatisch wiederholt. Prüfe Erreichbarkeit,
Version und Dienste und dokumentiere den Abschluss. Hängt ein Vorgang, schließt
**Vorgang abbrechen** ihn mit Begründung. Offene Wartung sperrt neue Pushes auf dem
betroffenen Host oder Cluster.

### Zurück zur vorherigen Firmware

**Zur vorherigen Firmware** (Host-Wartung) macht ein Update rückgängig: OPNexus
aktiviert den neuesten Rückweg-Punkt und startet die Firewall neu. Die Aktion läuft über
dieselben Schutzschritte wie ein Neustart (Vorschau, Hostname, Downtime-Bestätigung, im
Cluster nur auf dem BACKUP, gesperrt bei offenen Vorgängen). Die Vorschau zeigt die
Zielversion, den Punkt und **was verloren geht**.

**Wichtig:** Ein Boot-Environment enthält auch die Konfiguration (`/conf`). Der Rückweg
setzt also Firmware **und alle Konfigurationsänderungen seit dem Punkt** zurück, auch
solche von Hand. Deshalb gilt:

- Vor dem Rückweg sichert OPNexus die aktuelle Konfiguration lokal (scheitert das, startet
  er nicht).
- Alle Ausrollvorgänge von OPNexus auf diese Firewall seit dem Punkt (Regeln, Aliase,
  NAT, VPN) werden als „unklar“ markiert. Das nächste Ausrollen schreibt sie wirklich neu,
  statt „unverändert“ anzunehmen. Danach die zentralen Objekte über Vorschau → Ausrollen
  → Bestätigen neu ausrollen.
- OPNexus aktiviert den Punkt, prüft, dass er für den nächsten Start vorgemerkt ist, und
  erst dann folgt der Neustart. Scheitert eines davon, schaltet OPNexus das vorherige
  Boot-Environment wieder ein, damit kein alter Stand auf den nächsten ungeplanten Neustart
  wartet.
- Nach dem Start Version prüfen und die Wartung abschließen. Ein Rollout macht **nie**
  von selbst einen Rückweg; bei einem fehlgeschlagenen Host hält er an, und du entscheidest.
- Das verlassene Boot-Environment (der Stand nach dem Update) bleibt erhalten, damit du auch
  wieder vorwärts kannst. Brauchst du es nicht mehr, lösche es unter Host-System › Snapshots.
  Die Firewall läuft danach aus dem Boot-Environment mit dem Namen des Rückweg-Punkts.
- Im Cluster gilt wie beim Neustart: nur auf dem BACKUP. Für den MASTER zuerst die
  CARP-Übergabe ausführen.

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
und **nicht** wiederholt. Sie steht im Status der geplanten Wartung und meldet sich
zusätzlich per Benachrichtigung („Geplante Wartung nicht ausgeführt“, einzeln
schaltbar), damit man es am nächsten Morgen nicht erst beim Nachsehen merkt.
Wiederkehrende Termine und ganze Flotten laufen über [Rollouts](#rollouts-in-wellen).

**Ergebnis automatisch prüfen und abschließen** (im Dialog vorausgewählt): Nach dem Auftrag
wartet OPNexus, bis der Firmware-Auftrag beendet ist, und prüft dann selbst: Firmware
mindestens eine Minute am Stück bereit, ein erwarteter Neustart hat wirklich stattgefunden
(Host offline gesehen oder neue Boot-Zeit), Version lesbar. Erst dann wird die Wartungsaktion
als abgeschlossen markiert und sperrt Host und Cluster nicht mehr. Gelingt das nicht innerhalb
von 20 Minuten, bleibt sie „unklar“ (gesperrt, mit Benachrichtigung). Ohne die Option bleibt
sie wie bisher „übergeben“, bis jemand sie von Hand abschließt.

In einem Cluster läuft unbeaufsichtigte Wartung standardmäßig **nur auf dem
CARP-BACKUP**. Ist der Host beim Einplanen gerade MASTER, lehnt OPNexus das sofort mit
Begründung ab, statt erst zur Uhrzeit zu scheitern.

**Mit automatischer CARP-Umschaltung** (Option im Planungsdialog, nur für Cluster-Knoten,
standardmäßig aus) darf auch der MASTER eingeplant werden. Zum Zeitpunkt läuft dann:

1. Voraussetzungen prüfen (Partner erreichbar und BACKUP, keine offene Wartung, bei
   Updates ein frisches Backup). Ist etwas nicht erfüllt, wird **nichts** verändert.
2. CARP-Wartungsmodus auf dem Host einschalten. OPNexus wartet bis zu 3 Minuten, bis der
   Partner tatsächlich MASTER ist. Wird er es nicht, schaltet OPNexus zurück und
   aktualisiert nichts.
3. Erst dann startet die Aktion (Neustart/Update/Major-Upgrade).
4. OPNexus wartet, bis der Host wieder erreichbar und der Firmware-Auftrag beendet ist
   (bei einem Neustart muss der Host erst als ausgefallen beobachtet worden sein).
5. Wartungsmodus verlassen und prüfen, dass beide Knoten wieder erreichbar sind und genau
   einen MASTER haben. Je nach CARP-Einstellung holt sich der Host dabei die MASTER-Rolle
   zurück (ein kurzer zweiter Wechsel).

Fällt nach Schritt 3 etwas aus (Zeitüberschreitung, unklare Antwort, Host kommt nicht
wieder), bleibt der Host **absichtlich im Wartungsmodus** und der Partner weiter MASTER;
der Plan steht auf „fehlgeschlagen“, die Wartungsaktion auf „unklar“, und es gibt die
Benachrichtigung. Der CARP-Umschalter wird nie blind wiederholt. Beendest du die
Wartungsaktion von Hand, stoppt der Ablauf und fasst nichts mehr an. Bei Erfolg schließt
OPNexus die Wartungsaktion selbst ab. Sie läuft auch einen Neustart des OPNexus-Backends
mitten im Ablauf weiter (der Zustand steht in der Datenbank). Mit „Ergebnis automatisch
prüfen“ kommt die Prüfung **vor** dem Verlassen des Wartungsmodus: Der Host bekommt den
Verkehr erst zurück, wenn er nachweislich fertig ist.

### Rollouts in Wellen

**System › Rollouts** verteilt ein Firmware-Update (stabiles Update oder Major-Upgrade) in
Wellen über die Flotte. Die Wellen bestehen aus [Gerätegruppen](#gerätegruppen), die man
als Ringe anlegt, zum Beispiel `ring-canary` (eine unwichtige Firewall), `ring-pilot`
(einige) und `ring-prod` (der Rest). Steht ein Host in mehreren Wellen, gilt die früheste.

**Ablauf je Welle:**

1. **Vorprüfung:** Update-Suche auf den Hosts der Welle, dann Bestand aufnehmen (Version,
   Gateways, laufende Dienste, Sicherheits-Score). Hosts ohne passendes Update werden
   übersprungen. Die Version, die in der ersten Welle installiert wird, ist die
   **Zielversion**; bietet ein Host einer späteren Welle eine andere an, hält der Rollout an
   (OPNsense installiert immer die neueste, sonst bekäme „prod“ eine Version, die im
   Canary-Ring nie lief).
2. **Hosts nacheinander:** Jeder Host läuft als [geplante Wartung](#geplante-wartung) mit
   automatischer Prüfung (Backup vor dem Update, Sperren, Audit). Immer nur ein Host,
   immer nur ein Rollout gleichzeitig. Im Cluster zuerst der BACKUP, dann der MASTER;
   der MASTER nur, wenn die Welle die CARP-Übergabe erlaubt, sonst hält der Rollout an.
   Vor dem zweiten Knoten eines Clusters werden der Cluster (alle Knoten erreichbar,
   genau ein MASTER je VHID, kein Wartungsmodus) und der schon aktualisierte Knoten
   erneut geprüft.
3. **Beobachtungszeit** (je Welle einstellbar), dann die **Gesundheitsprüfung** der
   aktualisierten Hosts: erreichbar und Firmware bereit, Zielversion installiert, CARP
   gesund, Gateways und Dienste, die vorher liefen, laufen wieder (vorher schon gestörte
   nur als Hinweis), keine offenen Sperren, optional Sicherheits-Score nicht gesunken
   (Hinweis oder Halt) und optional die **Erreichbarkeitsproben** des Plans. Ist etwas rot, prüft OPNexus zwei Minuten später noch einmal
   (langsamer Start nach dem Neustart) und hält dann an.
4. **Freigabe** (optional je Welle): Die Welle beginnt erst, wenn jemand auf „Welle
   freigeben“ klickt.

**Erreichbarkeitsproben:** Im Plan lassen sich bis zu zehn Proben eintragen (Ping oder TCP
auf einen Port, Ziel als IP oder Name), zum Beispiel der Uplink, ein DNS-Server oder ein
interner Server. Die **Firewall selbst** führt sie aus: einmal vor der Welle (Ausgangslage)
und nach der Beobachtungszeit. Eine Probe, die vorher klappte und jetzt nicht mehr, hält den
Rollout an; eine, die schon vorher scheiterte, ist nur ein Hinweis. Ehrlich benannt: Das
prüft die Erreichbarkeit **von der Firewall aus**, nicht den Verkehr, der durch sie
hindurchläuft. Jede Probe steht im Audit (Akteur `system:rollout`). Vor jedem Update
legt OPNexus auch hier den [Rückweg-Punkt](#host-wartung) an.

**Angehalten** wird bei jeder Unklarheit, nie wird etwas von selbst wiederholt: Gate rot,
Host fehlgeschlagen oder ungewöhnlich lange unterwegs, Host in der Vorprüfung nicht bereit,
abweichende Zielversion, MASTER ohne Übergabe, eine fremde offene Wartung im Cluster länger
als 15 Minuten, die verantwortliche Person darf nicht mehr ausrollen, das Fenster wurde
gelöscht, der Lauf ist älter als 14 Tage. Es kommt die Benachrichtigung „Rollout
angehalten“. **Fortsetzen** prüft zuerst alle bisher aktualisierten Hosts erneut; wer
fortsetzt, ist ab dann verantwortlich. Ein fehlgeschlagener Host muss vorher ausdrücklich
**übersprungen** werden (seine Wartungsaktion bleibt „unklar“, bis du sie auf der
Host-Seite abschließt). **Abbrechen** verwirft alles, was noch nicht begonnen hat; eine
Aktion, die schon auf einer Firewall läuft, wird geführt zu Ende gebracht. **Anhalten**
darf jeder mit Schreibrecht, alles andere braucht das Recht zum Ausrollen.

**Wartungsfenster** (Reiter „Wartungsfenster“): wöchentlich an bestimmten Tagen oder
monatlich (erster…vierter oder letzter Wochentag, oder ein fester Tag 1–28), Beginn in einer
Zeitzone, Dauer und Abschneidezeit: Nach den ersten N Minuten beginnt kein neuer Host mehr,
laufende Updates laufen zu Ende, und der Rollout wartet auf den nächsten Termin (dann wird
zuerst das Bisherige erneut geprüft). Sommerzeit: Eine Uhrzeit, die es an dem Tag nicht gibt,
rückt vor (02:30 → 03:30); eine doppelte gilt beim ersten Mal. Einzelne Termine lassen sich
auslassen oder verschieben. Ein Fenster, das ein Plan nutzt, lässt sich nicht löschen.

**Automatischer Start:** Ein Plan mit Fenster und stabilem Update kann zu jedem Termin von
selbst starten. Dazu muss ihn jemand mit Ausroll-Recht nach der Vorschau **aktivieren**.
OPNexus merkt sich dabei einen Fingerabdruck von Plan, aufgelösten Hosts und Fenster
(**einschließlich der ausgelassenen oder verschobenen Termine**); ändert sich davon etwas
(auch die Mitglieder einer Gruppe), startet nichts, und es kommt „Rollout nicht
automatisch gestartet“. Jede Bearbeitung des Plans deaktiviert ihn. So kann niemand
die Ziele eines aktivierten Plans unbemerkt ändern. Wer einen Termin auslässt oder
verschiebt, muss einen automatischen Plan danach neu aktivieren. Die aktivierende Person
wird zusätzlich über ihre Konto-ID erkannt: wird ihr Konto gelöscht und ein neues mit
gleichem Namen angelegt, startet der Plan nicht mehr. Überlappen sich zwei Termine eines
Fensters (z. B. ein verschobener mit dem regulären), zählt der zuletzt begonnene.

Das **Update Center** zeigt den laufenden Rollout als Banner; Hosts, die er noch bearbeitet,
sind dort für manuelle Firmware-Aktionen gesperrt. Ein Neustart oder Update von OPNexus
während eines Rollouts ist sicher (der Zustand steht in der Datenbank), hält ihn aber für
diese Zeit an.

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

**Geplant und ausgelagert** (Einstellungen › Backups): OPNexus sichert jede Firewall
automatisch (Standard: täglich) und behält die neuesten (Standard 30 je Firewall). Optional wird jede
Sicherung **auf deinem Server mit einer Passphrase verschlüsselt** (AES-256-GCM, Schlüssel per scrypt
aus der Passphrase) und auf **S3-kompatiblen Speicher** hochgeladen (AWS S3, MinIO, Backblaze B2,
Wasabi, Cloudflare R2, Hetzner Object Storage …; Endpunkt muss `https://` sein). „Ziel testen“
schreibt, liest und löscht ein Prüfobjekt. Jede Sicherung geht nur einmal hoch, gelöscht wird
offsite nie (dafür die Lifecycle-Regeln des Buckets nutzen). **Ohne die Passphrase ist eine
Offsite-Kopie unlesbar** – bewahre sie außerhalb von OPNexus auf. Entschlüsseln:

```bash
docker compose exec -T -e BACKUP_PASSPHRASE='…' backend python -m app.backup_decrypt < datei.opxbak > config.xml
```

Die Benachrichtigung „Kein aktuelles Backup“ meldet sich, wenn ein Host drei Intervalle (mindestens
6 Stunden) ohne Sicherung war, „Offsite-Backup fehlgeschlagen“ bei Problemen mit dem Ziel. Die
Offsite-Anbindung ist gegen die offiziellen AWS-Signaturbeispiele und gegen einen laufenden S3-Server mit echter
Signaturprüfung (Garage, MinIO, RustFS und Versity S3 Gateway: Upload, Download, Löschen, 20-MB-Objekt, Sonderzeichen im
Schlüssel, falsches Geheimnis wird abgelehnt) geprüft; bei AWS selbst, Backblaze, Wasabi & Co. zeigt dir „Ziel testen“ sofort, ob dein Anbieter funktioniert.

**Änderungen ansehen:** Der Knopf „Änderungen“ neben einer Sicherung zeigt, was sich gegenüber der
vorherigen geändert hat. Geheimnisse (Passwörter, Schlüssel, Tokens, Zertifikate) erscheinen nur als
Hash: ein geändertes Geheimnis zeigt sich als geänderter Hash, der Wert verlässt den Server nie.

**Wiederherstellen** prüft eine gespeicherte Sicherung als Vorschau (Format,
Metadaten, geheime Felder werden unkenntlich gemacht) und übergibt dann an das
OPNsense-Webinterface. **OPNexus schreibt dabei nichts auf die Firewall.** Du
importierst die Sicherung selbst in OPNsense. Solange der Vorgang offen ist,
sperrt OPNexus Regel-, Alias-, NAT- und Wartungs-Pushes auf diesem Host; bestätige
oder breche den Vorgang ab. Bei Abweichungen (etwa ein anderer Hostname in der
Sicherung) zeigt die Vorschau eine Warnung.

---

## Überwachung: Dashboard, Logs, Audit, Benachrichtigungen

### Regelnutzung

Auf der Seite **Sicherheitslage** zeigt die Tabelle „Regelnutzung“, wann jede Regel
zuletzt Verkehr getroffen hat. OPNexus liest dafür die Paketzähler der Firewall. Weil diese
bei jeder Änderung und jedem Neustart von vorn beginnen, merkt sich OPNexus den Zeitpunkt, an dem
ein Zähler zuletzt gewachsen ist (Auflösung: eine Beobachtung, alle 15 Minuten). Unmittelbar vor jedem Anwenden, das OPNexus
auf einer Firewall auslöst (Regeln, NAT, Aliase, VPN, Netzwerk), liest OPNexus die Zähler zusätzlich, denn jedes Anwenden setzt sie
zurück; so gehen keine Treffer verloren. Die Zähler kommen aus `ruleStats` der
Firewall (je Regel schon summiert; bei älteren OPNsense-Versionen aus dem geladenen Regelsatz). Eine Regel gilt
als **ungenutzt**, wenn OPNexus sie mindestens 30 Tage beobachtet hat, ohne einen Treffer zu sehen;
bis dahin steht sie auf „Daten werden gesammelt“ (die Beobachtungszeit beginnt neu, wenn die Regel eingeschaltet oder geändert wird, und nach einem pf-Neuladen mit langer Lücke bei den Zählern), damit eine neue Installation keinen Fehlalarm
auslöst. Erfasst werden die Regeln aus dem Regel-Editor bzw. der API und noch nicht migrierte Regeln im Altformat (die
automatischen Regeln von OPNsense nicht). Ungenutzte Regeln fließen als Befund „Regeln in Benutzung“ in die Sicherheitslage ein.

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

### Audit-Export

Unter **Einstellungen › Audit-Export** leitest du das Audit (wer hat wann was mit welchem
Ergebnis geändert) an einen **Syslog-Server** (RFC 5424 über UDP, TCP oder TLS; Facility local0,
Schweregrad „warning“ bei abgelehnten oder fehlgeschlagenen Aktionen) und/oder einen **Webhook**
(JSON-Stapel, optional mit HMAC-SHA256-Signatur im Header `X-OPNexus-Signature`) weiter, damit es
auch außerhalb von OPNexus vorliegt. Gesendet werden nur neue Ereignisse ab dem Einschalten; der
Inhalt von Firewall-Objekten (Vorher-/Nachher-Daten) ist nie dabei. Die Zustellung ist „mindestens
einmal“: scheitert das Ziel, versucht OPNexus dieselben Ereignisse erneut, jedes trägt seine ID zum
Entdoppeln. Der Status der letzten Zustellung steht in den Einstellungen.

### Benachrichtigungen

Einstellungen › **Benachrichtigungen**. Kanäle:

- **Webhook** mit optionalem Signatur-Secret.
- **E-Mail (SMTP)** mit Host, Port, Verschlüsselung (STARTTLS/SSL), Zugangsdaten,
  Absender und Empfängern. Zertifikat und Hostname werden geprüft; ein Mailserver
  mit privater CA braucht `SMTP_CA_FILE`. Anmeldung ohne Verschlüsselung bleibt
  gesperrt. Mit **testen** prüfst du jeden Kanal.
- **Weitere Kanäle** (Abschnitt „Weitere Kanäle“, beliebig viele je Typ, jeweils mit **Testen**):

  | Typ | Angaben | Zugangsdaten |
  |---|---|---|
  | **ntfy** | Serveradresse (Standard `https://ntfy.sh`), Thema, Priorität 1–5 | Zugriffstoken, optional |
  | **Gotify** | Serveradresse, Priorität 0–10 | App-Token |
  | **Telegram** | Chat-ID (Zahl oder `@kanal`), optional eine eigene API-Adresse | Bot-Token (von @BotFather) |
  | **Microsoft Teams** | – | Webhook-Adresse eines Workflows („Beim Empfang einer Webhook-Anfrage in einem Kanal posten“) oder eines Incoming-Webhooks |
  | **Matrix** | Homeserver-Adresse, Raum-ID (`!abc:example.org`, nicht der Alias) | Zugriffstoken eines Benutzers, der im Raum ist |

  Die Zugangsdaten liegen wie das SMTP-Passwort in der Geheimnis-Ablage des Servers, nie in der
  Datenbank, werden nie wieder angezeigt (nur „gesetzt“) und stehen nicht in Fehlermeldungen oder Logs.
  Weil bei Telegram und Teams die Adresse selbst das Geheimnis ist, wird sie wie ein Token behandelt.
  Ziele müssen `http(s)` sein, ohne Zugangsdaten in der Adresse; lokale Adressen (`localhost`, Loopback,
  Link-Local, Metadaten-Dienst) werden abgelehnt, Ziele im eigenen Netz sind erlaubt. Die Prüfung gilt auch
  beim Verbindungsaufbau: OPNexus verbindet genau zu der Adresse, die es geprüft hat (Schutz vor DNS-Rebinding,
  Meldung „Zieladresse nicht erlaubt“); dasselbe gilt für die Adressen des SSO-Anbieters. Weiterleitungen werden
  nicht verfolgt. Webhook, E-Mail und alle Kanäle werden **gleichzeitig** bedient, jeweils mit eigener
  Zeitgrenze (20 Sekunden) und insgesamt höchstens 90 Sekunden je Prüfdurchlauf: ein ausgefallener oder
  hängender Kanal hält weder die anderen noch die Prüfung auf (im Log steht „timed out“). Zugestellt wird
  einmal je Ereignis, ohne Wiederholung.

Ereignisse (jeweils einzeln schaltbar): Host nicht erreichbar, Zertifikat läuft ab,
Regel-Drift im Cluster, NAT-Drift im Cluster, Netzwerk-Abweichung im Cluster,
fehlgeschlagener Rollback, Wartungsergebnis unklar, CARP-Zustand kritisch
(Split-Brain/kein Master), CARP-Auto-Revert fehlgeschlagen, geplante Wartung nicht
ausgeführt, Gateway gestört, Ressourcen-Schwelle überschritten, Dienst ausgefallen,
API-Rechte fehlen (die letzten vier sind unten beschrieben). Geprüft wird im eingestellten Intervall (Standard 300 s). Ein Problem
meldet sich einmal und löst sich selbst auf, sobald es behoben ist; die
Dashboard-Karte „Warnungen“ zeigt die offenen. Ausnahme „geplante Wartung nicht
ausgeführt“: Sie bleibt drei Tage nach dem geplanten Zeitpunkt offen und schließt dann
ohne „behoben“-Meldung, weil sich ein verpasster Termin nicht von selbst behebt. Der
Text der Benachrichtigungen ist nicht übersetzt.

#### Gateway-, Ressourcen- und Dienst-Alarme

Diese drei Ereignisse lesen die **Minutenmessung** (dieselbe wie die Auslastungs-Diagramme), fragen die
Firewalls also nicht zusätzlich ab. Damit Einzelspitzen und flackernde Verbindungen nichts melden,
öffnet ein Alarm erst, wenn die letzten *N* Messungen alle schlecht sind, und schließt erst, wenn die letzten
*N* alle wieder gut sind; dazwischen bleibt der Zustand, wie er ist. *N* ist einstellbar (Standard 3 für
Gateways und Dienste, 5 für Ressourcen; die Messung läuft jede Minute). Meldet eine Firewall gerade nicht,
beurteilt OPNexus sie nicht (das meldet „Host nicht erreichbar“) und lässt offene Alarme stehen.

- **Gateway gestört** – **standardmäßig an**, weil ein WAN-Ausfall das ist, was jeder wissen will. Ein Alarm je Gateway, wenn OPNsense es als
  *down* führt, oder wenn der Paketverlust (Standard ab 20 %) oder die Latenz (Standard ab 500 ms) darüber liegt (0 schaltet die jeweilige Schwelle ab).
  OPNsense selbst entscheidet über „down“ nach den Grenzen im Gateway (Einstellung „Gateway-Überwachung“). Von Hand abgeschaltete
  Gateways („Gateway deaktivieren/erzwungen aus“) und Gateways ohne Überwachung (kein Monitor-IP, Werte „~“) melden nichts.
  Der Status steht auch auf der Host-Seite (Abschnitt „Gateways“, live gelesen), als Kachel auf dem Dashboard und in Prometheus.
  **Einzelne Gateways abschalten:** Ein ruhendes Zweit-WAN oder ein totes IPv6-Gateway melden dauerhaft „offline“. Auf der Host-Seite schaltet die
  Glocke hinter dem Gateway den Alarm nur für dieses Gateway aus (Tooltip „Alarm für dieses Gateway“); das Gateway trägt dann das Badge „Alarm aus“,
  ein offener Alarm löst sich auf, und ein Klick auf die Glocke schaltet ihn wieder ein. Das dürfen nur Administratoren (wie die übrigen
  Benachrichtigungs-Einstellungen), die Änderung steht im Audit-Log. Höchstens 200 Gateways lassen sich abschalten.
- **API-Rechte fehlen** – **standardmäßig an**. Der API-Benutzer von OPNexus braucht in OPNsense Leserechte für die Messwerte, auf denen die Alarme
  beruhen: *System: Gateways* (Gateway-Status), *Status: Services* (Dienste) und *Lobby: Dashboard* (CPU, Swap; dieselbe Berechtigung wie die
  Basiswerte). Verweigert die Firewall einen dieser Zugriffe ausdrücklich (HTTP 403), würde der zugehörige Alarm nie auslösen. Dann zeigt die
  Host-Seite und das Dashboard das Warn-Badge „API-Rechte fehlen: …“ (Tooltip nennt die fehlende Berechtigung), es gibt einen Alarm je Firewall und
  Zugriff, und er löst sich auf, sobald der Zugriff wieder klappt. Zeitüberschreitungen und Serverfehler gelten nur als „unbekannt“ und lösen weder
  etwas aus noch schließen sie den Alarm. Abhilfe: in OPNsense unter System › Zugang beim Benutzer oder der Gruppe des API-Schlüssels die genannte
  Berechtigung ergänzen. Prometheus: `opnexus_host_api_rights_missing{host,read}` (1, nur solange es fehlt).
- **Ressourcen-Schwelle** – standardmäßig **aus**: Auf einer bestehenden Flotte würde das Einschalten sonst gleich alles melden, was schon länger voll
  ist. Schwellen: Festplatte ab 90 % (je ZFS-Pool bzw. UFS-Gerät ein Alarm), Arbeitsspeicher ab 95 %, Swap ab 50 %, CPU-Last ab 2,0 je Thread
  (1-Minuten-Last geteilt durch die Zahl der CPU-Threads). Jede Schwelle lässt sich mit 0 abschalten.
- **Dienst ausgefallen** – standardmäßig **aus**. OPNsense liefert nur „läuft / läuft nicht“ und „gesperrt“, aber nicht, ob jemand einen Dienst
  absichtlich gestoppt hat. Deshalb gilt eine **Liste überwachter Dienste** (Standard: unbound, dnsmasq, dhcpd, dhcpd6, kea-dhcp4, kea-dhcp6, ntpd,
  openvpn, wireguard, strongswan), und gemeldet wird nur ein Dienst, der in den letzten rund zwei Stunden lief und jetzt nicht mehr. Ein Dienst, der
  nie lief (etwa DHCPv6 ohne Präfix), bleibt still; einen absichtlich gestoppten Dienst nimmst du aus der Liste. Eine Firewall listet nur Dienste, die
  in ihrer Konfiguration aktiviert sind, ein nicht eingerichteter Dienst taucht also gar nicht auf.

### Regel-Analyse

Unter **Regelwerk › Regel-Analyse** fragst du, welche Regel für eine Verbindung greift: Quell- und Ziel-IP,
Protokoll, Zielport und das Eingangs-Interface („automatisch“ rät es aus der Quell-IP und kennzeichnet das als
Hinweis). Mit „Ausgangsseite prüfen“ werden zusätzlich die Regeln für ausgehenden Verkehr geprüft. Geprüft wird
das erste Paket einer neuen Verbindung gegen den tatsächlich geladenen Regelsatz der Firewall; es wird nur gelesen.
Je Firewall erscheint eine Karte mit dem Urteil (Erlaubt, Blockiert, Abgewiesen), der **Sicherheit** (sicher,
vermutlich, unbekannt; Tooltip mit den Gründen) und der entscheidenden Regel. Ist das Ergebnis „unbekannt“
(zum Beispiel fehlt der Zielport oder ein Alias ist nicht auflösbar), zeigt die Karte bewusst kein Urteil, sondern
die offenen Punkte. Unter „Warum?“ steht jede geprüfte Regel in der Prüfreihenfolge mit ✓/✗/? je Feld.
Die Abfrage steht in der Adresszeile und lässt sich als Link teilen.

#### Bereinigung

Der Tab **Bereinigung** (Regelwerk › Regel-Analyse › Bereinigung) nennt je Firewall die Regeln, die einen Blick wert sind. Du wählst die Firewall
und die **Schwelle** (7, 30, 90 oder 180 Tage); die Auswahl steht in der Adresszeile (`?tab=cleanup&host=…&days=…`). Die Liste selbst wird nur gelesen; Ändern geht über die Auswahl (siehe unten):

| Befund | Bedeutung |
|---|---|
| **Ungenutzt** | OPNexus beobachtet die Regel seit mindestens der Schwelle und hat in dieser Zeit keinen Treffer gesehen (siehe „Regelnutzung“). Eine neue Regel erscheint erst nach der Beobachtungszeit. |
| **Verdeckt** | Eine frühere Quick-Regel trifft schon alles, was diese Regel trifft; die Regel entscheidet nie. Die verdeckende Regel steht daneben. |
| **Doppelt** | Wie verdeckt, aber die frühere Regel hat dieselbe Aktion und trifft genau dasselbe. |
| **Lange deaktiviert** | Die Regel ist deaktiviert und OPNexus hat sie seit mindestens der Schwelle unverändert gesehen (aus dem Änderungsverlauf). |

Jeder Befund trägt eine **Sicherheit**. **Sicher** gibt es nur mit vollständigen, frischen Belegen: bei „ungenutzt“ muss die Regel in pf geladen sein,
ohne Zeitplan, und die Zähler müssen frisch sein; bei „verdeckt/doppelt“ stammt die Reihenfolge von der Firewall, die verdeckende Regel ist geladen
und der Vergleich beruht auf den Regeln selbst. **Vermutlich** heißt: es beruht auf einer Annahme (der Inhalt von Aliasen, eine geschätzte
Reihenfolge, eine Regel mit Zeitplan, ein fehlender pf-Regelsatz). **Unbekannt** heißt: es gibt einen Hinweis ohne Beleg, etwa weil die Regel in pf nicht geladen
ist oder die Zähler veraltet sind. Der Tooltip erklärt es jeweils. Was nicht belegt ist, ist nie „sicher“.

Die Spalte **Aktion** sagt, was eine Auswahl der Regel bewirken würde (**Deaktivieren** oder, bei einer schon deaktivierten Regel, **Löschen**) oder warum
die Regel nicht angeboten wird: automatische Regeln (von OPNsense erzeugt), Regeln im Altformat (zuerst in OPNsense migrieren), von OPNexus ausgerollte Regeln
(die entfernst du über den normalen Ablauf der Regelseite, sonst legt OPNexus sie wieder an) und Regeln mit Befund „unbekannt“. Die Sicherheitslage verlinkt
ihre Befunde „ungenutzt“, „verdeckt“ und „doppelt“ direkt in diese Liste. Per API: `GET /api/hosts/{id}/rule-analysis/cleanup?unused_days=30`, per MCP: `rule_cleanup_candidates`.

##### Verwaltungsquellen (Voraussetzung)

Bevor die Bereinigung etwas ändern darf, trägst du die **Verwaltungsquellen** ein: die IP-Adressen oder Netze (CIDR, keine Hostnamen), von denen aus deine Firewalls verwaltet
werden (OPNexus selbst, dein Arbeitsplatz, ein Jump-Host). Der Tab zeigt bis dahin einen Hinweis und (nur für Administratoren) ein kleines Formular; dasselbe steht unter
**Einstellungen › Regel-Bereinigung**, dort auch die **Obergrenze je Lauf** (1 bis 25, Standard 10) und die **Wartezeit bis zum Löschen** (1 bis 365 Tage, Standard 7).
Für jede gewählte Regel prüft OPNexus, ob sie ein Paket von einer Verwaltungsquelle an eine Adresse der Firewall auf einem Verwaltungsport treffen kann (API-Port aus
der Host-Adresse, 443, 80, 22 und die **weiteren Verwaltungsports**, die du unter Einstellungen › Regel-Bereinigung einträgst, z. B. SSH auf 2222 oder die Weboberfläche
auf einem eigenen Port; bis 20 Ports). Geprüft wird auf jedem Interface, auch für Pakete über nicht zugewiesene Geräte (WireGuard, OpenVPN, IPsec). Trifft die Regel oder
lässt sich das nicht ausschließen, wird sie nie deaktiviert. Dazu kommen Cluster-Regeln (CARP, pfsync), die nie angeboten werden. Eine Sperrregel wieder einzuschalten,
die den Verwaltungszugang treffen könnte, verlangt eine ausdrückliche Bestätigung je Regel.

> **OPNexus selbst ist automatisch geschützt.** Für jede Firewall fragt die Bereinigung beim Planen, von welcher Adresse die Firewall OPNexus kommen sieht (die eigene
> API-Verbindung in der Zustandstabelle, über `diagnostics/firewall/query_states`), und schützt diese Adresse zusätzlich. Die Vorschau zeigt sie je Firewall
> („zusätzlich geschützt“ oder „schon in den Verwaltungsquellen“). Lässt sie sich nicht ermitteln (dem API-Schlüssel fehlt das Recht **Diagnose: Zustände**, oder die
> Firewall hängt am Management-Tunnel), sagt die Vorschau das. Dann trag die Adresse selbst ein (bei NAT die Absenderadresse nach der Übersetzung, über das
> Management-Overlay die Overlay-Adresse). Fehlt sie, kann eine Bereinigung genau die Regel abschalten, über die OPNexus die Firewall erreicht; dann scheitert auch
> das automatische Zurücksetzen. Unter Einstellungen › Regel-Bereinigung schlägt OPNexus außerdem die Adresse deines Browsers vor (wie OPNexus sie sieht; die
> Firewalls können sie anders sehen). Ein „savepoint“ mit Rücknahme durch die Firewall selbst gibt es in OPNsense 26.7 nicht (geprüft im Quellcode und live:
> `firewall/filter/savepoint` antwortet 404); das Zurücksetzen bleibt Aufgabe von OPNexus.
> Quellen, Ports und Firewall-Adressen sehen nur Administratoren; alle anderen sehen nur, ob etwas eingetragen ist.

##### Deaktivieren, Bestätigen, automatisches Zurücksetzen

1. **Auswählen.** Pro Lauf entweder nur Deaktivieren oder nur Löschen, höchstens so viele Regeln wie eingestellt. Bei Befunden „vermutlich“ fragt OPNexus vor der Vorschau je Regel
   mit einem festen Text nach, ob du geprüft hast, dass die Regel nicht gebraucht wird (Zeitplan, Aliasinhalt, kurzes Beobachtungsfenster, Sperrregel ohne Treffer). „Ungenutzt“ ist unter
   30 Tagen Beobachtung und bei Sperrregeln höchstens „vermutlich“.
2. **Vorschau.** OPNexus berechnet alles frisch von der Firewall (im Cluster von allen Knoten; die Regel muss auf jedem Knoten dieselbe UUID haben und es muss genau einen
   Master geben). Je Regel stehen Aktion, Befund, Sicherheit, die Schutzprüfung zum Verwaltungszugang und Hinweise (z. B. „extern geändert“). Die Vorschau gilt 5 Minuten
   und gehört dem, der sie erstellt hat. Offene Wartungen, Ausrollvorgänge oder eine andere Bereinigung im Cluster sperren sie.
3. **Ausrollen** (nur Administratoren, mit Eingabe des Firewall-Namens). OPNexus prüft den Stand erneut und schreibt nichts, wenn er von der Vorschau abweicht. Dann werden die Regeln
   deaktiviert (im Cluster zuerst der Backup) und angewendet.
4. **Bestätigen oder zurücksetzen.** Der Kasten oben im Tab zeigt den Countdown. **Bestätigen** (Operator oder Administrator, **nur in der Browser-Sitzung, nie per API-Token**) liest
   vorher jede Firewall noch einmal und verweigert, wenn sie nicht antwortet: das ist das Signal, dass du dich ausgesperrt haben könntest. Bestätigst du nicht
   rechtzeitig, setzt OPNexus automatisch zurück; mit **Jetzt zurücksetzen** geht das sofort (Administrator, oder Operator, wenn dieser Schalter unter Einstellungen erlaubt ist). Ist
   eine Regel fehlgeschlagen oder unklar (Status *Teilweise*), ist Bestätigen gesperrt. Schlägt das Zurücksetzen selbst fehl, hilft **Zurücksetzen wiederholen**. Nach einem Zurücksetzen beginnt
   das Beobachtungsfenster der Regel neu.

##### Löschen und Wiederherstellen

Gelöscht werden nur Regeln, die schon deaktiviert sind: eine bestätigt deaktivierte Regel nach der Wartezeit (und nur, wenn sie seitdem unverändert und nicht extern geändert wurde)
oder eine von Hand deaktivierte Regel mit Befund „lange deaktiviert“ (sicher). Eine deaktivierte Regel ist in der Firewall nicht geladen, das Löschen ändert dort also nichts
und hat darum kein Bestätigungsfenster. OPNexus sichert vorher den vollständigen Regelinhalt samt Position; ohne Sicherung wird nicht gelöscht. Unter **Verlauf der Bereinigungen**
(am Seitenende des Tabs) zeigt jede Zeile ihre Schritte; bei gelöschten Regeln gibt es **Wiederherstellen** (nur Administratoren, mit Firewall-Namen): die Regel kommt aus der Sicherung neu zurück, **deaktiviert**,
mit neuer UUID (im Cluster auf allen Knoten) und möglichst direkt vor ihrer alten Nachfolgerin. Lässt sich die Position nicht herstellen, steht die Regel am Ende ihrer Gruppe und
OPNexus weist darauf hin. Fehlt der Firewall inzwischen ein Alias oder eine Kategorie der Regel, lehnt sie ab und ihre Meldung steht unverändert beim Ergebnis. Zum Einschalten gibt es
**Wieder aktivieren** (auch bei bestätigt deaktivierten Regeln): ein eigener Lauf mit Bestätigungsfenster, weil das Einschalten einer Sperrregel aussperren kann.

Rechte im Überblick: Vorschau, Abbrechen und Bestätigen dürfen Operatoren und Administratoren; Ausrollen, Wiederholen und Wiederherstellen nur Administratoren; Nur-Lese-Konten sehen die Liste und den Verlauf,
aber keine Knöpfe. Jeder Schritt steht je Regel im Audit-Protokoll und im Änderungsverlauf der Regel (Herkunft „über OPNexus“).

#### Verlauf einer Regel

OPNexus liest alle 15 Minuten die Regeln jeder Firewall (zwei Lesezugriffe) und vergleicht sie mit dem zuletzt gesehenen Stand – auch Regeln, die nicht von OPNexus
stammen. Jede Änderung wird ein Ereignis: **angelegt, geändert** (mit Vorher/Nachher der betroffenen Felder), **verschoben** (nur Reihenfolge oder Gruppe), **aktiviert, deaktiviert, entfernt**. Als **Herkunft**
steht „über OPNexus“, wenn das Audit seit der vorherigen Beobachtung (und noch für keine andere Änderung) ein Ausrollen oder Zurücksetzen dieser Regel auf dieser Firewall kennt (mit dem Benutzer), sonst „extern“ (in der
Oberfläche der Firewall, per Skript oder über einen anderen Zugang). Die erste Beobachtung einer Firewall speichert nur den Ausgangszustand, es entstehen
keine „angelegt“-Ereignisse für vorhandene Regeln; eine leere oder unvollständige Antwort der Firewall wird verworfen, und verschwinden mehr als die Hälfte der Regeln auf einmal, zählt erst die zweite gleiche Beobachtung; eine Änderung zwischen zwei Beobachtungen erscheint zum Zeitpunkt der zweiten. Ereignisse bleiben 365 Tage.

Zu sehen ist der Verlauf in der **Live-Ansicht** (Regelwerk › Live über alle Hosts) über die Zeilenaktion **Verlauf**, in der Bereinigungsliste am Symbol neben der Regel,
und per API (`GET /api/hosts/{id}/live-rules/{uuid}/history`). Bei einer von OPNexus verwalteten Regel stehen die gespeicherten Fassungen und die Audit-Einträge der
Regel dabei. Automatische Regeln werden nicht verfolgt.

### Sicherheitslage

Unter **Monitoring › Sicherheitslage** prüft OPNexus jede Firewall auf typische
Schwachstellen und vergibt einen Wert von 0 bis 100 (ab 90 gut, ab 70 mittel, darunter
schlecht). Die Prüfung liest die Konfiguration der Firewall einmal lesend herunter
und wertet sie im Speicher aus; es wird nichts an der Firewall geändert und die
Konfiguration nicht gespeichert. Alle sechs Stunden läuft die Prüfung von selbst,
„Alle prüfen“ und „Erneut prüfen“ starten sie sofort.

| Prüfung | Gewicht | Wann ein Befund entsteht |
|---|---|---|
| Keine Allow-all-Regel am WAN | kritisch | Eine aktive Regel am WAN (oder an einer Schnittstelle mit Upstream-Gateway) erlaubt jeden Verkehr von überall nach überall |
| Web-Oberfläche nur über HTTPS | hoch | Die Verwaltungsoberfläche läuft über HTTP |
| SSH abgesichert | hoch / mittel | SSH ist an und erlaubt Passwort-Anmeldung (hoch, wenn auch root per Passwort darf) |
| SNMP-Community nicht „public“ | hoch | SNMP ist an und nutzt „public“ |
| Keine Allow-all-Regel mit beliebiger Quelle (intern) | mittel | Eine interne Regel erlaubt beliebige Quellen zu beliebigen Zielen (die Standardregel „LAN to any“ zählt nicht) |
| Keine verdeckten Regeln | mittel | Eine Regel wird nie erreicht, weil eine frühere sie schon abdeckt |
| Firmware aktuell | mittel | Updates stehen bereit |
| Aktuelles Backup | mittel | Das neueste lokale Backup ist 7 Tage oder älter |
| Keine doppelten Regeln, Regeln beschriftet, Zeitserver, externe Logs | niedrig | Doppelte Regel, Regel ohne Beschreibung, kein Zeitserver, kein Syslog-Ziel |
| Einbruchserkennung aktiv | Hinweis | IDS/IPS ist aus (kostet keine Punkte) |

Ein kritischer Befund kostet 30 Punkte, ein hoher 15, ein mittlerer 8, ein niedriger 3.
Nicht entscheidbare Prüfungen (etwa der Updatestatus einer gerade nicht erreichbaren
Firewall) kosten nichts. Verdeckt oder doppelt meldet OPNexus nur, was sich beweisen
lässt: Regeln werden in der Reihenfolge der Firewall (Floating, Interface-Gruppen, Interface-Regeln) Feld für Feld
verglichen, Netze und Portbereiche als Mengen; nur eine frühere Quick-Regel kann eine spätere verdecken, und
Regeln mit Zeitplan, Markierung oder Limits verdecken nichts. Aliase gelten nur bei gleichem Namen als gleich; so entstehen
keine falschen Befunde, aber eine Überschneidung über verschiedene Aliase bleibt hier unentdeckt (die Bereinigung vergleicht
auch statische Alias-Inhalte, dort als „vermutlich“). Beide Regelformate (OPNsense 26.x und ältere Installationen) werden gelesen.
Die Werte stehen auch als Prometheus-Metriken zur Verfügung (siehe unten).

Das **Dashboard** zeigt die Firewalls mit dem niedrigsten Wert. Der Knopf **Bericht** erzeugt eine druckbare Übersicht
aller geprüften Firewalls mit ihren offenen Befunden und Hinweisen zur Behebung; im Browser „Drucken / als PDF speichern“
wählen. In den Einstellungen › Benachrichtigungen lässt sich **„Kritischer oder hoher Sicherheitsbefund“**
einschalten (standardmäßig aus, damit ein bestehender Bestand nicht plötzlich alle alten Befunde auf einmal meldet): Pro
Firewall und Prüfung kommt eine Meldung, sobald sie auftritt, und eine „behoben“-Meldung, wenn sie nach der nächsten
Prüfung verschwunden ist.

### Über OPNexus

Ein Klick auf das Logo öffnet **About**: alle Hosts mit Status, CARP-Rolle, Cluster,
installierter OPNsense-Version und Regelanzahl. Die Werte laden je Host nach; eine
nicht erreichbare Firewall zeigt „nicht abrufbar“. Die eigene Version steht im Footer
jeder Seite und in den Einstellungen.

---

## Automatisierung: API-Tokens, Prometheus, KI-Assistenten

Unter **System › API-Tokens** legst du persönliche Tokens an. Mit einem Token
sprechen Skripte, CI-Jobs, Prometheus und KI-Assistenten die OPNexus-API ohne
Anmeldung an:

```
curl -H "Authorization: Bearer opx_…" https://opnexus.example/api/hosts
```

Ein Token gehört deinem Konto und hat nie mehr Rechte als du. Es wird nur einmal
angezeigt und nur als Hash gespeichert. Zum Anlegen brauchst du dein Passwort. Standard
ist eine Laufzeit von 90 Tagen; du kannst jedes Token jederzeit widerrufen, und ein
Passwort-Reset des Kontos widerruft alle seine Tokens. Höchstens 50 Tokens je Benutzer.

| Bereich | Darf |
|---|---|
| **Lesen** | Alles ansehen (GET), außer Dinge mit Geheimnissen: Roh-Download der Konfigurations-Backups und die Benachrichtigungseinstellungen. Für Dashboards, Prometheus, KI-Assistenten. |
| **Ausrollen** | Zusätzlich ausrollen und warten: Regeln, Aliase, NAT, VPN, Zertifikate, Wartung, Sicherheitsprüfung. Nicht: Benutzer, Einstellungen, Benachrichtigungen, Hosts anlegen oder löschen, OPNexus-Update. |
| **Admin** | Alles, was dein Konto darf, einschließlich der Dinge oben. |

Plan und Export der [Konfiguration als Code](#konfiguration-als-code-yaml-git-ci) gehen
schon mit „Lesen“, das Übernehmen nur mit „Admin“.

Für jeden Bereich gilt: Tokens verwalten und das eigene Konto (Passwort, Zwei-Faktor)
geht nur in einer normalen Anmeldung, nie mit einem Token. Ein Nur-Lese-Konto kann
nur „Lesen“-Tokens anlegen; wird das Konto zurückgestuft, sind alle seine Tokens
sofort nur noch lesend. Im Audit steht bei Änderungen per Token „Benutzer (API: Name)“.

**API-Beschreibung:** das OpenAPI-Schema liegt unter `/api/openapi.json` (mit
Anmeldung oder Token abrufbar) und lässt sich in Postman, einen Code-Generator oder
einen Assistenten laden. Eine interaktive Seite gibt es nicht. Fehler kommen als
`{"detail": {"code": "…", "params": {…}}}` mit stabilem `code`.

### Konfiguration als Code (YAML, Git, CI)

Unter **System › Konfiguration als Code** (oder per `opnexus export` / `opnexus apply`)
lassen sich alle **zentral gepflegten Objekte** als eine YAML-Datei **exportieren** und
wieder **importieren**: Gerätegruppen, Hierarchiegruppen, Aliase, Filterregeln,
Port-Forwards, 1:1-NAT, Outbound-NAT, Hierarchie-Regeln und die Werte der
[Standort-Variablen](#standort-variablen). Für Git-Verwaltung, Reviews, CI-Prüfungen und
die Wiederherstellung auf einer neuen Instanz.

**Was nicht dazugehört:** Hosts selbst (Adresse, API-Schlüssel, Pinning), Benutzer,
Tokens, SSO, Einstellungen, Benachrichtigungen, Wartungsfenster und Rollouts,
Netzwerkobjekte, VPN und Zertifikate. In der Datei stehen keine Zugänge, Schlüssel oder
Passwörter; Hosts erscheinen nur mit ihrem Namen.

**Der Import ändert nur die Entwürfe in OPNexus.** Auf Firewalls kommt weiterhin nichts
ohne Vorschau → Ausrollen → Bestätigen. Der Import-Code ruft kein Firewall-Kommando auf
(ein Test erzwingt das) und löscht nie ein Objekt, das schon einmal ausgerollt wurde.

**Ablauf:**

1. **Plan:** OPNexus vergleicht die Datei mit dem aktuellen Stand und zeigt je Objekt
   *neu*, *geändert* (mit Feldern vorher/nachher), *entfernt* oder *unverändert*,
   dazu Hinweise. Der Plan schreibt nichts und darf auch von Nur-Lese-Konten
   und mit „Lesen“-Token abgerufen werden (zum Beispiel für eine Prüfung in einem Pull Request).
2. **Übernehmen:** nur **Administratoren** (Sitzung oder Admin-Token). Das Übernehmen
   schreibt alles in **einer** Transaktion oder nichts. Es verlangt das Token genau dieses Plans: hat sich seit dem Plan
   etwas geändert (auch nur ein Gruppenmitglied oder ein neues Objekt in der Oberfläche),
   antwortet OPNexus mit „veraltet“ und du planst neu. Zum Schluss prüft OPNexus, dass
   die Datenbank genau dem Plan entspricht; sonst wird alles zurückgenommen.
3. **Danach** erscheinen die Änderungen als Entwürfe und werden wie immer je Objekt
   ausgerollt. Geänderte Objekte, die schon ausgerollt sind, nennt der Plan („auf N
   Firewalls ausgerollt“).

**Teil-Dateien:** Fehlt ein Abschnitt in der Datei, bleibt diese Art unberührt. Steht ein
Abschnitt drin, auch ein leerer, und ist **Prune** an, wird diese Art vollständig
abgeglichen: was in der Datei fehlt, wird aus OPNexus entfernt. Ohne Prune wird nur
angelegt und geändert. Prune ist mächtig und deshalb ein ausdrücklicher Schalter
(`--prune`, in der Oberfläche mit Bestätigung).

**Blockiert** (der Plan lässt sich nicht übernehmen, nichts wird geändert):

- Ein Objekt mit **offenem Deployment** (wartet auf Bestätigung oder Rückrollen) soll
  geändert oder entfernt werden.
- Prune eines Objekts, das **schon ausgerollt** wurde (auch zurückgerollt oder
  fehlgeschlagen). Erst in der Oberfläche entfernen, dort mit Vorschau und Rückbau.
- Prune einer Gruppe, eines Aliasnamens oder einer Hierarchiegruppe, die nach dem
  Import **noch verwendet** wird (von Regeln, VPN, Rollout-Wellen, Hosts, Kindgruppen).
- Fehler in der Datei selbst (unbekannter Host, Gruppe oder Hierarchiegruppe, doppelter
  Schlüssel, Zyklus im Hierarchiebaum, ein Host in zwei Hierarchiegruppen, Platzhalter
  `{{…}}` in Hierarchie-Regeln) lehnen schon den Plan mit Zeile und Pfad ab.

**Schlüssel:** Aliase, Gruppen und Hierarchiegruppen werden über ihren **Namen**
erkannt, Regeln, NAT-Objekte und Hierarchie-Regeln über einen **`key`**
(Kleinbuchstaben, Ziffern, `.` `_` `-`, höchstens 64 Zeichen, eindeutig je Art). Bestehende
Objekte bekamen beim Update automatisch einen (`r-…`, `pf-…`, `nat1-…`, `out-…`, `ph-…`);
in der Datei darfst du eigene vergeben (`allow-dns-lan`), der Import speichert sie. Ein
**Umbenennen** gilt als Entfernen und Neuanlegen, ein ausgerolltes Objekt blockiert
dann mit Prune. Die Reihenfolge der Hierarchie-Regeln ist die Reihenfolge in der Datei.

**Hinweise im Plan:** ein Wert, der wie ein Alias aussieht, aber kein zentraler Alias in OPNexus ist (auf der Firewall von Hand angelegte Aliase sind in Ordnung; OPNexus fragt beim Planen keine Firewall); eine Variable, die entfernt
wird, obwohl ein Objekt sie nutzt (das Ausrollen auf diesen Host scheitert dann sicher);
ein deaktivierter Ziel-Host; neue Objekte mit Zielen ohne Verknüpfung zu bereits auf den
Firewalls vorhandenen Objekten. Letzteres ist wichtig bei einem Import in eine **neue
Instanz** mit denselben Firewalls: Ausrollen legt dort Duplikate an. Besser eine
Vollsicherung (`opnexus backup --full`) zurückspielen oder die Objekte vorher über
„Mit vorhandenem zentralem Objekt verknüpfen“ zuordnen.

**Format:** eine Datei `opnexus-config.yaml`, `opnexus_config: 1`, feste Reihenfolge der
Abschnitte, Objekte nach Schlüssel sortiert, alle Felder immer, kein Zeitstempel: Git
zeigt nur echte Änderungen. Das JSON-Schema liegt in
`backend/app/config_code/opnexus-config-v1.schema.json` (für Editor-Prüfung). Beschreibungen
dürfen mehrzeilig sein, Namen und Schlüssel nicht; Ports dürfen als Zahl oder Text stehen.
Die Datei darf höchstens 1 MiB groß sein (je Art 5000 Objekte, je Host 200 Variablen).

**Strenge Prüfung:** Die Datei wird nur als Daten gelesen. Anker und Aliase (`&`, `*`),
Merge-Schlüssel (`<<`), explizite Tags (`!!python/…`, `!!binary`), Direktiven, mehrere
Dokumente, doppelte Schlüssel, Steuerzeichen und ungültiges UTF-8 werden abgelehnt.
`on`, `yes`, Datumsangaben und `1:30` bleiben Text (kein „YAML 1.1“-Verhalten). Fehler
nennen Zeile und Pfad (`rules[3].destination_port`).

**Auf dem Server (CLI):**

```
opnexus export [--out VERZEICHNIS] [--kinds rules,aliases]
opnexus apply DATEI [--dry-run] [--prune] [--yes] [--json]
```

`apply` zeigt zuerst den Plan und fragt `[j/N]` (ohne Terminal nur mit `--yes`). Mit
`--dry-run` wird nichts übernommen. Exit-Codes für CI: `0` übernommen bzw. keine
Unterschiede, `2` Unterschiede (nur `--dry-run`), `3` Datei ungültig, `4`
blockiert oder veraltet, `1` sonstiger Fehler. Wer die Konsole des Servers bedient, braucht
kein Token; im Audit steht `cli (Host-Konsole)`.

**Git-Rezept** (Cron oder systemd-Timer auf dem OPNexus-Server):

```
opnexus export --out /srv/opnexus-config \
  && git -C /srv/opnexus-config add -A \
  && git -C /srv/opnexus-config commit -m "OPNexus-Stand $(date -u +%F)" \
  && git -C /srv/opnexus-config push
```

OPNexus selbst verbindet sich nie mit einem Git-Server und braucht dafür keine Zugänge.

**CI ohne Zugriff auf den Server:** per API mit einem Token. Der Plan genügt für
Prüfungen in Pull Requests (Token „Lesen“), das Übernehmen braucht „Admin“:

```
curl -s -H "Authorization: Bearer opx_…" -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile y opnexus-config.yaml '{yaml:$y, prune:false}')" \
  https://opnexus.example/api/config/import/plan
```

Die Antwort enthält `summary`, `items`, `warnings`, `applicable` und `token`; dieses
Token gehört zu `POST /api/config/import/apply` (`{"yaml", "prune", "plan_token"}`). Export:
`GET /api/config/export` (`?kinds=rules,aliases`). Im Audit steht bei jedem Übernehmen ein
Sammeleintrag (SHA-256 der Datei, Zähler, Prune) und je geändertem Objekt ein Eintrag; der Inhalt
der Datei steht nicht darin.

### Prometheus

`GET /api/metrics` liefert den Zustand im Prometheus-Textformat. Die Antwort stammt
nur aus der OPNexus-Datenbank (keine Anfragen an die Firewalls), ein häufiges Abfragen
belastet also nichts. Mit einem „Lesen“-Token:

```yaml
scrape_configs:
  - job_name: opnexus
    scheme: https
    metrics_path: /api/metrics
    authorization: { credentials: "opx_…" }
    static_configs: [{ targets: ["opnexus.example"] }]
```

| Metrik | Bedeutung |
|---|---|
| `opnexus_info{version}` | Version, immer 1 |
| `opnexus_hosts` | Anzahl der Firewalls |
| `opnexus_host_up{host,cluster}` | 1, wenn der Host in den letzten 3 Minuten geantwortet hat |
| `opnexus_host_enabled`, `…_last_sample_timestamp_seconds` | Überwachung an; Zeit der letzten Messung |
| `opnexus_host_memory_used_percent`, `opnexus_host_load1`, `opnexus_host_disk_used_percent` | Letzte Messung |
| `opnexus_host_carp_master` | 1 = MASTER, 0 = BACKUP (fehlt, wenn unbekannt) |
| `opnexus_host_swap_used_percent` | Swap-Belegung der letzten Messung (fehlt ohne Swap) |
| `opnexus_host_gateway_up{gateway}` | 1, solange OPNsense das Gateway nicht als down führt |
| `opnexus_host_gateway_loss_percent`, `…_delay_seconds`, `…_stddev_seconds` `{gateway}` | Paketverlust, Laufzeit und Abweichung zum Monitor (fehlen ohne Messung) |
| `opnexus_host_api_rights_missing{read}` | 1, solange die Firewall den Lesezugriff `read` (gateways, services, swap, cpu) wegen fehlender API-Rechte verweigert; sonst fehlt die Zeile |
| `opnexus_host_service_running{service,instance}` | 1 = läuft, nur für Dienste aus der Überwachungsliste |
| `opnexus_host_compliance_score`, `opnexus_host_compliance_findings{severity}` | Sicherheitslage |
| `opnexus_pending_actions{kind,status}` | Offene Aktionen wie auf dem Dashboard |
| `opnexus_problems_open{type}` | Offene Probleme der Benachrichtigungen |
| `opnexus_scheduled_maintenance_pending` | Noch nicht gelaufene geplante Wartungen |
| `opnexus_rollout_runs{status}` | Flotten-Rollouts nach Status (wartend, laufend, pausiert, erfolgreich, …) |

Ein fertiges **Grafana-Dashboard** liegt unter [`docs/grafana-opnexus.json`](grafana-opnexus.json)
(Grafana › Dashboards › Importieren, Prometheus-Datenquelle wählen): erreichbare Firewalls, offene Aktionen
und Probleme, Sicherheitswert und Befunde, Last, Speicher, Swap, Platte und CARP-Rolle sowie Gateways (erreichbar, Paketverlust, Latenz),
gestoppte Dienste, fehlende API-Rechte und Rollouts nach Status.

### KI-Assistenten (MCP)

OPNexus spricht das Model Context Protocol, damit ein KI-Assistent die Flotte
ansehen kann, **nur lesend**: Er kann nichts ändern. Beispiel für Claude Code:

```
claude mcp add --transport http opnexus https://opnexus.example/api/mcp \
  --header "Authorization: Bearer opx_…"
```

Die Werkzeuge: `fleet_overview`, `list_hosts`, `host_resources`, `carp_events`,
`recent_audit`, `search` und `api_get` (liest jeden anderen GET-Endpunkt). Alles läuft
mit den Rechten des Tokens: ein „Lesen“-Token sieht auch hier keine Geheimnisse.

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

**Vollsicherung (empfohlen):** Der Dump allein reicht nicht, um einen OPNexus-Server neu aufzubauen,
denn die Geheimnisse liegen in eigenen Volumes. `./opnexus backup --full [DATEI]` packt Datenbank
und alle Geheimnis-Volumes (Firewall-Zugänge, Zwei-Faktor, VPN- und Zertifikatsschlüssel,
Konfigurationssicherungen) in **eine mit Passphrase verschlüsselte Datei** (`.opxfull`). Die Passphrase
wird abgefragt (oder `OPNEXUS_BACKUP_PASSPHRASE`). Auf einer frischen Installation (gleiche oder
neuere Version) spielt `./opnexus restore DATEI` alles zurück: Es prüft die Datei, fragt nach, stoppt die
Dienste, legt eine Sicherheitskopie des jetzigen Zustands an, spielt Datenbank und Volumes ein und
startet neu. Nicht enthalten: Log-Erfassung, Messwerte und `caddy_data` (die interne CA: nach einer
Wiederherstellung auf neuer Hardware importierst du das CA-Zertifikat im Browser erneut).
Datei und Passphrase getrennt vom Server aufbewahren.

**Was nicht im Dump ist:**
- Die Tabellen der **Log-Erfassung** und der **Systemauslastung** (nur das Schema).
  Nach einer Wiederherstellung sind sie leer und füllen sich aus den Firewalls.
- **Geheimnisse** (API-Secrets, VPN- und Zertifikatsschlüssel, Zwei-Faktor): sie
  liegen in den eigenen Volumes. Für eine vollständige Sicherung `./opnexus backup --full` (siehe oben) verwenden.

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
veröffentlicht ist. Eingeschaltet wird das unter **Einstellungen › OPNexus aktualisieren**
(Schalter „Versionsprüfung aktivieren“, Feed-Adresse `https://updates.opnexus.dev/latest.json`)
— ein Eingriff in die `.env` ist nicht nötig. Wer die Prüfung zentral festlegen will, kann
stattdessen `UPDATE_CHECK_URL=https://updates.opnexus.dev/latest.json` in die `.env`
schreiben; dann ist der Schalter im Webinterface gesperrt.

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
```

Die Versionsprüfung (Feed) schaltest du dazu im Webinterface ein, siehe oben.

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
- **Fremde Dateien:** Der Import von Konfigurationsdateien liest YAML nur als Daten
  (keine Anker, Tags, Merge-Schlüssel oder Mehrfach-Dokumente), begrenzt Größe und
  Tiefe, prüft jedes Feld streng und ist Admins vorbehalten.
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
- **Regeln:** Export und Import als YAML gibt es für die zentralen Objekte
  ([Konfiguration als Code](#konfiguration-als-code-yaml-git-ci)); nicht dabei sind
  Hosts, VPN, Netzwerkobjekte und Zertifikate. Keine Konfigurations-Templates oder
  Policy-Pakete mit atomarem Commit auf den Firewalls. Der Regel-Drift-Abgleich nutzt
  einen Fingerabdruck und ist kein vollständiger Feld-für-Feld-Vergleich.
- **Rollback:** nur für Regeln, Aliase und NAT (Deploys). Löschen, VPN- und
  Netzwerkänderungen haben keinen automatischen Rollback.
- **Wiederherstellung** einer Firewall-Sicherung ist eine geführte Übergabe an
  OPNsense, keine Ausführung durch OPNexus.
- **Logs:** kein Live-Streaming (Polling im Sekundenbereich), die Freitextsuche ist
  eine einfache Teilstring-Suche ohne Index.
- **Geplante Wartung und Rollouts:** Rollouts kennen keine Zielversion „von außen“
  (OPNsense installiert immer die neueste angebotene). Den Rückweg zur alten Version gibt es
  nur auf Firewalls mit ZFS und nur geführt von Hand ([Rückweg](#zurück-zur-vorherigen-firmware)),
  er setzt auch die Konfiguration zurück; ein Rollout macht ihn nie von selbst.
  Major-Upgrade-Rollouts sind nur gegen eine nachgestellte Firewall getestet. Die
  Gesundheitsprüfung sieht die Firewall aus Sicht der Verwaltung; die optionalen
  Erreichbarkeitsproben prüfen, was die Firewall selbst erreicht, nicht den Verkehr
  durch sie hindurch.
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
