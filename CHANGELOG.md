# Changelog

Alle nennenswerten Änderungen an OPNexus, mit Version und Datum. Für die
vollständige Feature-Beschreibung (Screenshots, Verifikationsnachweise,
v1-Scope-Grenzen) siehe die datierten Abschnitte in [Handbuch](docs/HANDBUCH.md)
— dieses Dokument ist die kurze Versions-/Namens-Übersicht, nicht der
Ersatz dafür.

## Versionierung

OPNexus folgt [Semantic Versioning](https://semver.org/lang/de/)
(`MAJOR.MINOR.PATCH`):

- **MAJOR** — Breaking Changes (z.B. inkompatible API-/Schema-Änderungen,
  entfernte Features).
- **MINOR** — neue Features, rückwärtskompatibel (der Regelfall bei einem
  Projekt, das noch aktiv wächst).
- **PATCH** — Bugfixes und kleine Korrekturen ohne neues Feature.

Zusätzlich bekommt **jede MINOR- und MAJOR-Version einen Codenamen** —
Raubvögel, alphabetisch aufsteigend über die Releases hinweg (ähnliches
Prinzip wie Ubuntus alliterierende Tier-Codenamen, nur ohne den doppelten
Anfangsbuchstaben-Zwang). Ein PATCH-Release behält den Codenamen seiner
MINOR-Version. Die aktuelle Version steht in `frontend/lib/version.ts`
(`APP_VERSION`/`APP_CODENAME`) und `backend/app/version.py` — beide von
Hand gepflegt, keine Build-Pipeline/kein Git-Hash-Tagging, bewusst analog
zur bisherigen Konvention (siehe Handbuch, "Login-Infos" 2026-09-09). Sichtbar
in der App auf der Login-Seite, im Footer jeder Seite, auf `/about` und in
den Einstellungen.

Nächste Codenamen (der Reihe nach, wenn die jeweils nächste MINOR-Version
ansteht): Merlin …

## [1.11.0] "Merlin" — 2026-10-07

Sicherheitsüberprüfung vom 2026-10-07, umgesetzte Korrekturen.
**Verhaltensänderungen**, die beim Update auffallen können:

- **Backup-Rohdownload nur für Admins**. Nur-Lese-Konten sehen weiter die
  Liste der Snapshots, bekommen die `config.xml` (enthält Passwort-Hashes, VPN-Schlüssel)
  aber nicht mehr; jeder Versuch steht im Audit-Verlauf (neuer Status „abgelehnt“).
- **Passwortwechsel/-reset beendet alle Sitzungen** des Kontos; ebenso
  2FA-Reset durch einen Admin. 2FA aktivieren/deaktivieren beendet die *anderen* Sitzungen.
- **SMTP über SSL/STARTTLS prüft Zertifikat und Hostname**. Mailserver mit
  privater CA: `SMTP_CA_FILE` setzen. SMTP-Anmeldung ohne Verschlüsselung ist gesperrt
  (`ALLOW_INSECURE_SMTP_AUTH=true` hebt das bewusst auf).
- **Firewalls nur noch per `https://`** — beim Anlegen und bei jeder Anfrage;
  ein vorhandener `http://`-Host zeigt „offline“, statt Zugangsdaten im Klartext zu senden
  (`ALLOW_INSECURE_FIREWALL_URLS=true` nur für Labor-Setups).
- **VPN-Push (IPsec/OpenVPN/WireGuard) verlangt das Vorschau-Token** und das
  Token ändert sich, sobald nur das Geheimnis (PSK/Schlüssel) rotiert wurde. Die Vorschau
  zeigt „unverändert“ jetzt korrekt, statt jeden Tunnel dauerhaft als geändert.
- **Host löschen** ist auch bei VPN-, Zertifikats- und ACME-Objekten auf der Firewall
  gesperrt, bis diese entfernt sind; Schlüsseldateien nie bereitgestellter
  Entwürfe werden mit entfernt.
- Benachrichtigungs-Geheimnisse liegen jetzt im Volume `notification_secrets`
 . Ohne lesbares Signatur-/SMTP-Geheimnis wird **nichts** mehr gesendet
  (vorher: unsigniert/ohne Login). Wer Geheimnisse gesetzt hat, muss sie nach dem
  Update einmal neu eingeben, falls der Container zwischenzeitlich ersetzt wurde.

Weitere Korrekturen:

- Oberfläche: Der „Bestätigen“-Button steht jetzt am Ende der Deploy-Ergebnis-Zeile statt
  in der Aktionszeile (Regeln, Aliase, Port-Forwards, 1:1-NAT, Outbound-NAT,
  Hierarchie-Regeln); die Aktionszeile springt nicht mehr, wenn ein Deployment offen wird.

- **Deployments hinterlassen vor dem ersten Firewall-Aufruf eine dauerhafte Spur**
  (gemeinsamer Ablauf `deploy_flow.py` für Regeln, Aliase, Port-Forwards, 1:1-NAT,
  Outbound-NAT und Hierarchie-Regeln): Bei Absturz, verlorener Antwort oder Timeout bleibt
  die Rollback-Frist erhalten und der Sweeper macht die Änderung rückgängig. Wo die Firewall
  eine neue Regel möglicherweise angelegt hat, ohne dass eine UUID bekannt ist, steht der
  Eintrag auf „Rollback fehlgeschlagen“ mit dem Hinweis „Ausgang unklar“ (blockiert
  erneutes Deployen, löst die Benachrichtigung aus); „Rollback wiederholen“ entfernt ihn.
  Von der Firewall beantwortete Ablehnungen und Verbindungsfehler bleiben einfache Fehler.
- TOTP- und Recovery-Codes sind auch bei gleichzeitigen Anfragen genau einmal
  verwendbar (Zeilensperre).
- Login-Sperre: begrenzter Speicher, Argon2 läuft außerhalb des Event-Loops mit
  Parallelitätsgrenze, 2FA-Einrichtung/-Aktivierung/-Deaktivierung und Passwortwechsel
  haben ein Versuchslimit; offene 2FA-Einrichtung läuft nach 15 Minuten ab; Proxy
  begrenzt API-Anfragen auf 2 MB.
- Fehlermeldungen der Firewall werden vor Speicherung/Anzeige von Geheimnissen
  bereinigt und gekürzt; Validierungsfehler (422) geben Eingaben nicht mehr
  zurück; Webhook-URLs (oft mit Token) sieht ein Nur-Lese-Konto nur noch als
  Host.
- Audit: MFA/Passwort, VPN, Zertifikate/ACME, Benachrichtigungen und Backup-Exporte
  werden aufgezeichnet, ebenso abgelehnte Schreibversuche von Nur-Lese-Konten.
- Same-Origin-Prüfung berücksichtigt das Schema bei `COOKIE_SECURE=true`;
  Warnung beim Start, wenn `FRONTEND_ORIGIN` https ist, das Cookie aber nicht `Secure`
 . Geheimnisdateien werden atomar mit Modus 0600 angelegt.
- Frontend-Abhängigkeiten (`sharp`, `source-map-js`, `brace-expansion`) aktualisiert.
  Offen: `braces`/`postcss-selector-parser` nur in Build-Werkzeugen (Tailwind 3 /
  ESLint), nicht im Laufzeit-Image; für `braces` gibt es keine gepatchte Version.

## [1.10.1] "Lanner" — 2026-10-06

PATCH-Release (Bugfixes, Dokumentation, Lizenz/Repo-Umstellung) nach dem ersten Live-Test
gegen frische OPNsense-26.7-Firewalls; löst die Release Candidates
`1.10.0-rc1`/`-rc2` ab.

- **Fehlerbehebung aus dem ersten Live-Test mit OPNsense 26.7:**
  - Beschädigte HTTP-Antworten einer Firewall (bekannter kTLS-Fehler in
    OPNsense 26.x) werden als solche gemeldet, mit dem Workaround
    `kern.ipc.tls.enable=0` im Log (vorher nur „Verbindung fehlgeschlagen“).
  - Die Log-Ingest-Warnung nennt den echten Grund statt pauschal „nicht
    erreichbar?“ – die Firewall war in diesem Fall erreichbar.
  - `BACKEND_PORT`/`FRONTEND_PORT` in der `.env`, damit eine zweite (Test-)Instanz
    auf demselben Rechner ohne Änderung am Compose-File läuft.
  - Neues Handbuch-Kapitel „Fehlerbehebung“ (kTLS, zweite Instanz,
    `OUTBOUND_MODE_BLOCKS_PUSH`, Erkennungstakt der CARP-Historie) und zwei
    Regressionstests.
- **Datenbank-Namen:** Standard für Datenbank und Benutzer ist jetzt `opnexus`
  (`POSTGRES_DB`/`POSTGRES_USER` in der `.env`, auch von Backup-Dienst,
  `opnexus`-Kommando und CI gelesen). Ältere Installationen tragen ihre
  bisherigen Namen in der `.env` ein (Handbuch, „Datenbank-Namen“). Alte
  Projektnamen sind aus Quelltext, Handbuch und Changelog entfernt.
- **Lizenzwechsel zur PolyForm Shield License 1.0.0** (siehe Handbuch, „Lizenz“):
  Download, Quellcode-Einsicht und eigener Einsatz bleiben kostenlos, der
  Weiterverkauf und konkurrierende Produkte durch Dritte sind ausgeschlossen.
  Bereits unter AGPL-3.0 veröffentlichte Stände bleiben unter der AGPL.
- Website: „Open Source“-Aussagen entfernt, Texte zur Lizenz angepasst.
- **Öffentliches Repo** `git.opnexus.dev/opnexus/opnexus`: pro Release ein sauberer
  Snapshot-Commit (`scripts/publish_public.sh`), Images in der Registry derselben
  Gitea (`scripts/build_release_images.sh`).
- **Neue kurze `README.md`** (Englisch) und `README.de.md`; die ausführliche
  Dokumentation ist jetzt das **Handbuch** `docs/HANDBUCH.md`. Dazu `SECURITY.md`.
- Update-Feed liegt unter `website/latest.json` (ausgeliefert als
  `https://updates.opnexus.dev/latest.json`); Website-Auslieferung über
  Cloudflare Pages (`website/_headers`).

## [1.10.0] "Lanner" — 2026-10-05

Update-Fähigkeit, Teil 2: Release-Images (siehe Handbuch „Release-Images & CI“):

- **CI-Workflows** (`.github/workflows/`): Tests bei Push/PR; bei einem Tag
  `vX.Y.Z` baut der Release-Workflow Multi-Arch-Images (amd64/arm64) für
  Backend, Frontend und Proxy und pusht sie. Die Registry ist reine
  Repository-Konfiguration (GitHub oder Forgejo).
- **`docker-compose.yml`:** jeder Dienst hat `build:` und `image:`
  (`OPNEXUS_IMAGE_REPO`/`OPNEXUS_VERSION`, Image-Namen `opnexus-backend`/`-frontend`/`-proxy`); Update aus Release-Images ist
  `docker compose pull && docker compose up -d --no-build`.
- **Proxy-Image** mit eingebackener Caddyfile statt Bind-Mount.
- `scripts/check_version.sh` prüft Tag, Versionsdateien und CHANGELOG.

Update-Fähigkeit, Teil 3: Update-Werkzeug (siehe Handbuch „Update und Rollback mit
`opnexus`“):

- **`scripts/opnexus`** mit `status`, `update`, `rollback`, `backup`: Pull vor
  dem Eingriff, Warten auf „gesund“, automatisches Zurückrollen, Rollback
  schließt die Datenbank-Wiederherstellung aus dem Pre-Upgrade-Backup ein.
- **Behoben — Pre-Upgrade-Backup war nicht zurückspielbar:** das Backend-Image
  hatte `pg_dump` 17 (Debian 13) gegen einen Postgres-16-Server, solche Dumps
  lassen sich dort nicht einspielen. Das Image installiert jetzt den Client 16
  (`backend/scripts/install_pg_client.sh`); der Start bricht vor der Migration ab,
  wenn Client- und Server-Hauptversion nicht übereinstimmen. Ein neuer Test
  spielt das Backup tatsächlich zurück.
- `PROXY_HTTPS_PORT` und `PROXY_HTTP_PORT` machen die Proxy-Ports konfigurierbar.
- **Compose getrennt:** `docker-compose.yml` nur mit Images (= Release-Compose),
  `docker-compose.override.yml` mit den Build-Kontexten für den Repo-Checkout.
- **`scripts/make_release_bundle.sh`** erzeugt ein Release-Bundle (Compose mit
  eingebackener Registry/Version, `.env.example`, `opnexus`, Anleitung).
- ESLint ignoriert die generierte `next-env.d.ts`.

Update-Fähigkeit, Teil 4: Hinweis auf neue Versionen:

- **„Neue Version verfügbar“-Banner** für Administratoren. Quelle ist ein
  Release-Feed im Format von GitHub/Forgejo `releases/latest`
  (`UPDATE_CHECK_URL`); ohne URL ist die Funktion aus und OPNexus macht keine
  ausgehende Anfrage. Neuer Endpunkt `GET /api/system/version-check`
  (Ergebnis 12 Stunden gecacht, Fehler werden leise abgefangen und nicht
  sofort wiederholt, Vorabversionen werden nie angeboten, nur http(s)-Links
  werden weitergegeben). Pro Version wegklickbar. Aktualisiert wird nie
  automatisch, sondern mit `opnexus update`.

## [1.9.0] "Kite" — 2026-10-04

Update-Fähigkeit, Teil 1: Schema-Migrationen (siehe Handbuch „Schema-Migrationen &
Upgrade“):

- **Alembic ersetzt `schema_patches.py`.** Das Schema wird versioniert
  (`backend/migrations/`); `app/migrate.py` migriert beim Start unter einem
  Advisory-Lock und in einer Transaktion. Frische Installationen bauen das
  Schema aus der Baseline `0001` auf, Installationen bis 1.8.x werden einmalig
  überbrückt und gestempelt (`0002` gleicht dabei fehlende Server-Defaults und
  einen fehlenden Index an).
- **Automatisches Backup vor jeder Migration:** `pg_dump` nach
  `/backups/pre-upgrade-…dump`; schlägt es fehl, wird nicht migriert.
- Eine Datenbank, die von einer neueren Version migriert wurde, wird nicht mehr
  stillschweigend weiterbenutzt — der Start bricht mit klarer Meldung ab.
- `app/schema_patches.py` ist jetzt `app/legacy_schema_patches.py`
  (eingefroren, nur noch die Brücke).
- Neue Tests (`tests/test_migrations.py`): Frisch-Aufbau gleicht den Modellen,
  Altinstallation behält ihre Daten, Backup-Pflicht, parallele Starts.
- Server-Defaults, die bisher nur auf gepatchten Installationen existierten,
  stehen jetzt auch in den Modellen.

## [1.8.1] "Harris" — 2026-10-04

- **Laufende Aktionen sind sichtbar:** Während ein Push, Speichern oder eine
  andere Aktion läuft, zeigt der Button jetzt einen drehenden Spinner vor dem
  Text („Push läuft…“) statt nur den Text zu wechseln (neue Prop `loading` an
  `Button`, gesetzt in allen Review-Dialogen, Formularen und Bestätigungen).
  Lade-Hinweise („Wird geladen…“) nutzen die neue Komponente
  `components/ui/Loading.tsx` mit Spinner. Bei „Bewegung reduzieren“ im
  Betriebssystem bleibt der Spinner stehen.

## [1.8.0] "Harris" — 2026-10-04

Barrierefreiheit (siehe Handbuch „Barrierefreiheit“):

- **Kontrast:** Die Grautöne für Nebentext (`slate-400`/`slate-500`) erfüllen
  im Hellmodus jetzt WCAG AA (4,5:1); Links in `sky-700` statt `sky-600`;
  im Dunkelmodus war der aktive Menüpunkt hell auf hell (Kontrast 1,99) und
  ist behoben.
- **Tastatur:** sichtbarer Fokusring für alle Elemente, „Zum Inhalt springen“-
  Link, `aria-current` im Menü, Bereichsnavigation als eigene Landmarke,
  scrollbare Tabellenbereiche per Tab erreichbar.
- **Screenreader:** sprechende Seitentitel pro Seite („Regeln · OPNexus“),
  genau eine `h1` je Seite (Login und Über-Seite hatten keine),
  korrekte Überschriftenreihenfolge in Regeln, Aliasen und Logs, Icons nur
  dekorativ, leere Tabellenköpfe tragen „Aktionen“, Host-Status wird
  jetzt übersetzt statt fest deutsch.
- Neues Prüfskript `scripts/verify_a11y.mjs` (axe-core, alle Seiten, Hell/
  Dunkel/Mobil): 0 Verstöße.

## [1.7.0] "Goshawk" — 2026-10-04

Sichere Übertragung (siehe Handbuch „Sichere Übertragung“):

- **HTTPS-Proxy (Caddy)** mit interner CA: Zertifikate auch für reine IPs,
  Zugriff per IP und Namen ohne DNS, CA-Download unter `/opnexus-ca.crt`.
  Backend/Frontend nur noch auf `127.0.0.1`; API same-origin.
- **Zertifikats-Pinning pro Firewall** (Hosts → Bearbeiten, Fingerprint-
  Bestätigung); `verify_tls=false` ist nicht mehr nötig. Neue Spalte
  `opnsense_hosts.tls_ca_pem`, Endpunkt `POST /api/hosts/tls-probe`.
- **Zwei-Faktor-Anmeldung (TOTP)** optional pro Benutzer mit
  Wiederherstellungscodes, Replay-Schutz, Admin-Reset und Notfall-CLI
  `python -m app.reset_2fa`. Neue Spalten `admin_users.totp_*`.
- **Origin-Prüfung** gegen CSRF, **Sicherheits-Header** (HSTS, CSP, …),
  `FRONTEND_ORIGIN` akzeptiert mehrere Ursprünge, API-Antworten `no-store`.
- Login-Screen fragt bei aktivem 2FA den Code ab; Hosts zeigen ein Schild für
  den TLS-Status.
- Abhängigkeiten: PostCSS-Lücken per `overrides` behoben (`npm audit`: 0),
  `pip-audit`: keine bekannten Lücken; neue Abhängigkeiten `cryptography`
  (Backend) und `qrcode` (Frontend).
- Tests laufen unabhängig von den Transport-Einstellungen in `.env`.
- Fehler behoben: Kehrte ein bereits gelöstes Problem zurück (z. B. „Rollback
  fehlgeschlagen“), scheiterte die Benachrichtigungsprüfung an der
  Unique-Constraint, und der Alarm ging verloren. Die Zeile wird jetzt
  wiederverwendet (`notification_dedup.py`).

## [1.6.0] "Peregrine" — 2026-10-04

Bedienbarkeit: Hinweise statt Textblöcke.

- Neue Tooltip-Komponente (`components/ui/Tooltip.tsx`): erscheint bei Hover,
  Tastaturfokus und Tippen, schließt mit Esc, wird nie von Karten oder
  Dialogen abgeschnitten und funktioniert auch auf deaktivierten Buttons.
- Update Center: Die großen Hinweis- und Sperrgrund-Kästen sind entfallen.
  Warum „Update vorbereiten“ gesperrt ist, steht jetzt im Tooltip des
  Buttons (mit Schloss-Symbol); Intro, Backup-Regel, Neustart- und
  CARP-Hinweise sind Tooltips bzw. kompakte Badges. Erfolgsmeldungen
  erscheinen als Toast. Die Bestätigungsdialoge (Hostname + Häkchen) bleiben
  unverändert, sind aber kürzer, fokussieren das Eingabefeld und sagen am
  deaktivierten Button, was noch fehlt.
- Dauerhafte Hinweisabsätze auf Audit, Einstellungen, Host-Wartung,
  Live-NAT, Review-Verläufen und in Formularen (Hierarchie, Private Key)
  sind zu Info-Tooltips geworden (`FieldGroup` hat dafür ein `hint`).
- Zeilenaktionen vereinheitlicht (Regeln, NAT, Aliase, VPN, Hosts, Benutzer):
  eine Hauptaktion („Prüfen & Push“), Bearbeiten als Outline-Button, Löschen
  dezent in Rot, Verlauf/Duplizieren als Icon mit Tooltip. Deploy-Ergebnisse
  stehen kompakt in einer Zeile.
- Seitenintros auf allen Seiten sind Info-Tooltips am Seitentitel; der
  Push-Hinweis (Bestätigungsfrist, automatischer Rollback) ist eine kurze
  Zeile mit Tooltip.
- Host-Detail: Live-Regeln und Live-Aliase sind einklappbar (mit Anzahl) und
  scrollen intern; die Seite ist von ~10.900 px auf ~2.300 px geschrumpft.
- Restore-Wizard: ein Schritt statt zwei, Warnung bleibt sichtbar, Übergabe
  nur nach Eingabe des Hostnamens.
- Dashboard-Aktivität zeigt lesbare Aktionsnamen („Update-Suche“ statt
  `check-updates`); große Zahlen in den Logs sind mit Tausendertrennung.
- Log-Erfassung: neues Größenlimit in GB (Einstellungen → Log-Erfassung,
  Standard 10 GB, 0 = unbegrenzt). Ist es erreicht, werden die ältesten
  Einträge gelöscht; die Einstellungen zeigen den belegten Speicher mit
  Balken. Neue Spalte `log_settings.max_size_gb` (per `schema_patches.py`).
  Beschriftungen klargestellt: „gespeichert“ (ganze Tabelle) in den
  Einstellungen, „Treffern“ (aktueller Filter) auf der Logs-Seite.
- UI-Korrekturen: Karten in Regel-/Alias-/NAT-Listen haben wieder Abstand
  (`space-y` wirkte nicht über `fieldset.contents`), `Input`/`Select`
  behalten ihre Standardstyles auch mit eigener `className`, alle Buttons
  sind gleich hoch, Einstellungen mit klaren Feldabständen.
- Dark Mode jetzt in neutralem Anthrazit statt Navy-Blau (Skala zentral in
  `globals.css`); Info-/Neutral-Badges, Toasts und der Sekundär-Button sind
  entsättigt, der Dialog-Hintergrund ist ein dunkler Schleier.
- Login-Screen: animierter Netzwerk-Hintergrund (Knoten, Verbindungen,
  wandernde Datenpakete – blau = erlaubt, gelegentlich orange = blockiert),
  gestaffelte Eingangsanimation, Schütteln bei falschen Zugangsdaten,
  Ausblenden nach erfolgreichem Login. Respektiert `prefers-reduced-motion`
  (dann statisch) und pausiert bei verstecktem Tab.
- Dialoge halten den Tastaturfokus (Tab-Falle) und geben ihn beim Schließen
  zurück; Login-Felder haben Autocomplete-Attribute.

## [1.5.0] "Osprey" — 2026-09-19

Mehrsprachigkeit (Deutsch/Englisch) vollständig — siehe Handbuch,
"Mehrsprachigkeit (Deutsch/Englisch)":

- Alle Seiten und Komponenten der Oberfläche sind auf Katalogschlüssel
  umgestellt (vorher nur Login, Navigation, Dashboard, Hosts und ein Teil
  der Regeln); Datums-/Zeitformate folgen der gewählten Sprache.
- API-Fehler bleiben sprachneutral (`detail: {code, params}`): die letzten
  rohen `HTTPException`s in `services/` sowie die Middleware-Antworten
  (401 „nicht angemeldet“, 403 „Nur-Lese“) liefern jetzt ebenfalls Codes.
- Deutsche Fließtexte in Erfolgsantworten (Deploy-Vorschau-Warnungen,
  Wartungs-/Deploy-Meldungen, Revisionsnotizen, Validierungsfehler) werden im
  Client über `frontend/lib/i18n/serverMessages.ts` ins Englische übersetzt.
- Neu: `scripts/check_i18n.py` prüft Katalog-Parität, Platzhalter,
  `ErrorCode`-Abdeckung und referenzierte Schlüssel.
- `tsconfig.json`: `target` auf ES2018 (nur Typprüfung, `noEmit`) wegen
  benannter Regex-Gruppen.

378 Backend-Tests grün, `tsc --noEmit`/`next build` sauber.

## [1.4.0] "Merlin" — 2026-09-17

Lizenz und dritte Härtungsrunde:

- **AGPL-3.0** als Projektlizenz festgelegt — komplett kostenlos nutzbar,
  Netzwerk-Klausel verhindert unveränderten/leicht angepassten Weiterverkauf
  als proprietären Hosted-Dienst (siehe Handbuch, "Lizenz").
- `/docs`, `/redoc`, `/openapi.json` erfordern jetzt eine Anmeldung statt
  öffentlich erreichbar zu sein.
- Login-Sperrschutz kann jetzt optional (`TRUST_PROXY_HEADERS`) die echte
  Client-IP aus `X-Forwarded-For` lesen, für den Betrieb hinter einem
  Reverse-Proxy — standardmäßig aus, nur mit explizitem Vertrauen in den
  Proxy zu aktivieren.

378 Backend-Tests grün.

## [1.3.0] "Kestrel" — 2026-09-17

Zweite Härtungsrunde, ausgelöst durch das Live-Rollout von 1.2.0 selbst
(siehe Handbuch, "Zweite Härtungsrunde"):

- Keine rohen OPNsense-Fehlertexte mehr in 502-Antworten (Info-Leckage
  an `read_only`-Nutzer).
- Healthchecks für Backend/Frontend ergänzt; dabei einen echten Next.js-
  Bindungs-Bug gefunden und behoben (`HOSTNAME=0.0.0.0`).
- Nächtliches Backup der eigenen OPNexus-Datenbank (neuer
  `postgres-backup`-Service), plus Fix für zu kleines Postgres-`/dev/shm`.
- Log-Erfassungs-Einstellungen (Intervall/Aufbewahrung) erstmals über
  UI/API sicht- und editierbar; Default-Aufbewahrung 30→7 Tage.
- Realer Disk-Vorfall behoben: `firewall_log_entries` war bei den zwei
  echten Firewalls in 9 Tagen auf ~9,7GB angewachsen, hat zusammen mit
  Rebuilds zweimal kurz die Disk gefüllt — bereinigt, keine Datenverluste.

375 Backend-Tests grün, `tsc --noEmit`/`next build` sauber.

## [1.2.0] "Harrier" — 2026-09-17

Sicherheits- und Stabilitäts-Härtung vor dem geplanten öffentlichen
Release, aus einem vollständigen Audit (siehe Handbuch, "Sicherheits- und
Stabilitäts-Härtung vor dem öffentlichen Release"):

- CORS-Default auf `localhost`/`127.0.0.1` verengt, neue `FRONTEND_ORIGIN`-
  Variable für alle anderen Deployments.
- `POSTGRES_PASSWORD` ohne unsicheren Fallback — muss explizit gesetzt sein.
- Ports standardmäßig nur auf `127.0.0.1`, steuerbar über neue
  `BIND_ADDRESS`-Variable statt manuellem Editieren von
  `docker-compose.yml`.
- TLS-/Reverse-Proxy-Betrieb dokumentiert (Caddy-Beispiel).
- Backend-Logging aktiviert (war zuvor komplett stumm) + Log-Rotation für
  alle Container.
- Neuer automatischer Schema-Patch-Mechanismus (`app/schema_patches.py`)
  ersetzt die bisherigen manuellen `ALTER TABLE`-Schritte bei jedem Start.
- Reproduzierbare Builds: Frontend-Lockfile committet (`npm ci`),
  Backend-Abhängigkeiten exakt gepinnt.

Live auf der Produktivinstanz ausgerollt und verifiziert, 371 Backend-Tests
grün.

## [1.1.0] "Goshawk" — 2026-09-17

Vier funktionale Lücken gegenüber DynFi Manager geschlossen (dem einzigen
echten Software-Wettbewerber in der Nische zentrales OPNsense-Fleet-
Management) — Multi-Tenancy/MSP-Betrieb bewusst ausgeklammert, Zielgruppe
bleiben Privatleute/kleine Unternehmen mit eigener Firewall:

- **Direct-View-Link**: Ein-Klick-Button öffnet die native OPNsense-GUI
  eines Hosts in einem neuen Tab.
- **Generische Dienst-Steuerung**: Beliebigen von OPNsense gemeldeten
  Dienst direkt aus OPNexus starten/stoppen/neustarten.
- **Resource-Monitoring**: Kompakte 24h-Zeitverlaufs-Charts für CPU-Last,
  Speicher- und Plattenbelegung je Host, eigene minimale
  Inline-SVG-Sparkline-Komponente, live gegen die reale Flotte im
  Hintergrund alle 60s abgetastet.
- **Geplante Wartung**: Neustart/Update/Major-Upgrade auf einen
  zukünftigen Zeitpunkt statt sofort legen (einmalig, kein Recurring).

14 neue Backend-Tests (371 gesamt grün). Siehe Handbuch, "Direct-View-Link,
Dienst-Steuerung, Resource-Monitoring, geplante Wartung".

## [1.0.0] "Falcon" — 2026-09-17

Erstes Release unter dem neuen Versionsschema — markiert den aktuellen,
bereits sehr ausgereiften Stand des Projekts (siehe Handbuch für die lange
Vorgeschichte des Projekts). Fasst
zwei zuvor unversionierte Arbeits-Batches zusammen, die mit diesem Release
erstmals committet wurden:

- **Fleet-Dashboard, neue UI-Bausteine, Konsistenz-Pass & Toast-Feedback**
  (`/dashboard` als neue Startseite; `Badge`/`Tabs`/`Table`/`EmptyState`/
  `Toast`-Primitives; CSS-Variablen-basierter Dark Mode für die
  Slate-Palette; Erfolgs-Toasts für praktisch alle schreibenden Aktionen
  inkl. aller Deploy-Push-Overlays). Siehe Handbuch, "Fleet-Dashboard, neue
  UI-Bausteine und Konsistenz-Pass".
- **CARP/HA vertieft**: Ereignis-Historie pro Cluster, Split-Brain-/
  Kein-Master-Alarm, NAT-Drift-Erkennung (Port Forward/1:1/Outbound),
  optionaler Auto-Revert-Timer für den CARP-Wartungsmodus. Siehe Handbuch,
  "CARP/HA vertiefen".
- **Versionierung & Update-System** (dieses Release selbst): Semantic
  Versioning mit Raubvogel-Codenamen, `CHANGELOG.md`, Versionsanzeige an
  vier Stellen in der App (Login, Footer, `/about`, Einstellungen),
  Backend-`FastAPI`-App trägt dieselbe Version. Kleiner Nebenfund dabei
  behoben: Login leitete nach erfolgreicher Anmeldung noch auf `/hosts`
  weiter statt auf die seit dem Dashboard-Release neue Startseite
  `/dashboard`.

357 Backend-Tests grün, `tsc --noEmit`/`next build` sauber. Details, offene
Punkte und Verifikationsstand: siehe docs/HANDBUCH.md.
