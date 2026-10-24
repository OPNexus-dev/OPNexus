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
ansteht): wird beim nächsten MINOR-Release festgelegt (Saker ist mit 1.33.0 vergeben).

## [1.34.0] "Griffon" — 2026-10-24

- **Zum Update:** Das erste Update auf diese Version übernimmt auch die `docker-compose.yml` (siehe unten; braucht wie die
  CLI-Aktualisierung `cosign` und `cosign.pub`), migriert die Datenbank (Revision 0032; ein Rollback spielt die Sicherung davor
  zurück) und stellt das Backend auf einen unprivilegierten Benutzer um. Nichts davon braucht einen Handgriff.
- **Nur ein Backend führt die Hintergrunddienste aus (HA, Stufe H0):** Laufen zwei Backends an derselben Datenbank, verschickten
  beide jeden Alarm und brachen Zyklen mit `UniqueViolation` ab. Jetzt führt nur der Prozess mit der Leader-Sperre in der
  Datenbank die neun Hintergrundschleifen aus; die anderen warten in Bereitschaft und übernehmen innerhalb einer halben Minute,
  wenn der Leader ausfällt. Verliert der Leader seine Datenbankverbindung, hält er alle Schleifen sofort an (geprüft alle 5 Sekunden
  und vor jedem Durchlauf). An einer Replica startet kein Leader. Rolle in `GET /api/health` und als Metrik `opnexus_leader`.
  Zwei gleichzeitige Erststarts legen das Admin-Konto nur einmal an.
- **Umzug und Kaltreserve (HA, Stufe H1):** `opnexus backup --full --with-env --with-ca` nimmt zusätzlich die `.env` und die
  interne CA des Proxys mit, `opnexus restore DATEI --with-env --with-ca` spielt sie zurück (ohne Datenbank-Zugang, Version und
  Adressen des Zielservers; die CA bleibt dieselbe, Browser vertrauen weiter). Neu: `opnexus migrate-host BENUTZER@ZIEL` zieht
  eine Installation in einem Schritt auf einen anderen Server um (Vorprüfung, Quelle anhalten, Sicherung mit Zufalls-Passphrase,
  Restore, Kontrolle). Im Lab 90 Sekunden mit drei Firewalls. `opnexus status` zeigt das Alter der letzten Vollsicherung.
  Handbuch: „Umzug und Notfall“.
- **Betriebseinstellungen im Webinterface:** Was bisher nur in der `.env` oder gar nur im Code stand, lässt sich unter
  Einstellungen › Betriebseinstellungen ändern: Sitzungsdauer und Anmeldesperren, Bestätigungsfrist beim Ausrollen, Zeitlimit für
  Firewall-Anfragen, Messintervalle und Aufbewahrung, Intervall der Versionsprüfung, Heartbeat-Adresse, CA des Mailservers (PEM
  einfügen statt Datei einbinden), Zeitlimit Mailversand. Mit Grenzen, Standardwert, Zurücksetzen und Audit-Eintrag. Ein Wert in der
  `.env` hat weiter Vorrang und wird als „vom Server“ angezeigt. Adressen, TLS und Datenbank-Zugang bleiben bewusst in der `.env`.
- **Heartbeat für einen externen Überwacher:** Die Heartbeat-Adresse (nur `https://`) wird nach jedem erfolgreichen Prüfzyklus
  aufgerufen; bleibt der Aufruf aus, schlägt der Überwacher Alarm. Die Adresse wird nie angezeigt oder protokolliert.
- **Updates bringen die Compose-Datei mit:** Bisher tauschte `opnexus update` nur die Images; Änderungen an der
  `docker-compose.yml` (neue Variablen, Härtung) kamen bei bestehenden Installationen nie an. Jetzt übernimmt das Update die Datei
  des Zielreleases aus dem signierten Updater-Image, sichert die alte als `docker-compose.yml.bak-<version>` und legt sie beim
  Rollback zurück. Eine hier angepasste Datei wird nicht überschrieben (neue Fassung als `docker-compose.yml.new`); eigene
  Anpassungen gehören in `docker-compose.override.yml`.
- **Alarm „Platte des OPNexus-Servers fast voll“** (standardmäßig an, ab 90 %, einstellbar): eine volle Platte hält die Datenbank
  an und damit jeden anderen Alarm. Auch im Dashboard und als Metrik `opnexus_server_disk_used_percent`.
- **Härtung aus einem externen Audit:**
  - **Kein root mehr in den Containern:** Backend (uid 10001) und Frontend (`node`) laufen unprivilegiert, ohne neue Rechte (`no-new-privileges`), das Frontend zusätzlich
    ohne Capabilities. Das Backend startet über `backend/scripts/start.sh` noch kurz als root, stellt die vorhandenen Datenvolumes einmalig um und gibt dann alle Rechte ab:
    ein Update braucht keinen Handgriff, ein Rückweg auf eine ältere Version bleibt möglich. Notausgang für Sonderfälle: `OPNEXUS_RUN_AS_ROOT=1` in der `.env`.
    `wg-hub` (braucht root für WireGuard) und `gui-gateway` (läuft schon als nobody) sind unverändert. Der Proxy (Caddy) und der optionale Updater (Docker-Socket) laufen weiter wie bisher.
  - **Vollsicherung konsistent:** Ändern sich Geheimnisse, während die Sicherung läuft (jemand legt gerade ein Objekt an oder löscht eines), wird sie bis zu dreimal wiederholt;
    gelingt das nicht, bricht sie mit einer klaren Meldung ab, statt ein unstimmiges Archiv zu schreiben.
  - **Zieladress-Schutz überall:** Audit-Export (Webhook und Syslog), Offsite-S3 und der Update-Feed verbinden nur noch zu Adressen, die die Prüfung bestanden haben
    (kein Loopback, Link-Local, Metadaten-Dienst; auch nicht über DNS-Rebinding oder Weiterleitungen). Adressen im eigenen Netz bleiben erlaubt. Der Update-Feed wird beim Lesen auf 1 MB begrenzt, nicht erst danach.
- **Fehlende Basis-Rechte werden sichtbar:** Fehlen dem API-Schlüssel schon die Rechte für die Grundmessungen (Speicher, Last, Platte), schlug das ganze Sampling still fehl und der
  Rechte-Alarm konnte nie auslösen. Jetzt entsteht ein Befund „Grundwerte“ (Recht „Lobby: Dashboard“), der sich nach Korrektur von selbst schließt.
- **Installation:** Die `INSTALL.txt` im Bundle und die Anleitung auf der Website legen die `.env` jetzt mit `install -m 600` an (nur für dich lesbar), statt mit `cp`.
- **Oberfläche:** Die Aktionsknöpfe der Backup-Tabelle auf der Host-Seite werden auf schmalen Bildschirmen als Symbole gezeigt, statt abgeschnitten zu werden.

## [1.33.0] "Saker" — 2026-10-24

- **Aus dem Testlauf 1.31.0 behoben (T2 bis T6, B15):**
  - **Benutzerliste:** `GET /api/users` zeigte jedem angemeldeten Konto (auch Nur-Lese, auch per Token) alle Benutzernamen, Rollen, letzte Anmeldung und 2FA-Status.
    Jetzt sehen **nur Admins** die ganze Liste, alle anderen nur das eigene Konto.
  - **Hosts:** Dieselbe Adresse zweimal einzutragen wird abgelehnt (`HOST_URL_EXISTS`, auch beim Ändern der Adresse). Die Hosts-Seite und der Einrichtungsassistent
    prüfen die Verbindung beim Anlegen (Schlüssel abgelehnt / zu wenig Rechte / Zertifikat / keine Verbindung), speichern bei einem Fehler nichts und bieten
    „Trotzdem anlegen“. Über die API ist das `verify_connection: true` (Standard aus). **Löschen verlangt jetzt auch am Server den Hostnamen:**
    `DELETE /api/hosts/{id}?confirm_name=<Name>` (sonst 409); Skripte, die Hosts löschen, müssen das anpassen.
  - **Zertifikate:** `cert_type: v3_ca` wird nicht mehr angenommen (es entstand ein Zertifikat mit CA-Erweiterung, keine nutzbare CA); die Oberfläche bot es nie an.
  - **Traceroute:** Zwischenstationen ohne Antwort stehen als Zeile „keine Antwort“ in der Liste (vorher sprang die Zählung 1 → 3); ein Weg ganz ohne Antwort wird erklärt.
  - **Netzwerk:** Die Vorschau prüft Zahlenbereiche vorab (VLAN-Tag 1–4094, PCP 0–7, VHID 1–255, Basis 1–254, Skew 0–254, gegen das Modell der OPNsense gelesen): `422` mit dem
    Feldnamen statt `502` von der Firewall.
  - **Gruppen:** Eine Gruppe, die ein VPN-Objekt als Ziel hat, lässt sich nicht mehr löschen (die Verknüpfung fiel still weg).
  - Das Skript `scripts/verify_i18n.mjs` läuft jetzt auch gegen die HTTPS-Test-Instanz.
- **Regel-Bereinigung: OPNexus selbst ist automatisch geschützt (B10).** Beim Planen fragt die Bereinigung jede Firewall, von welcher Adresse sie OPNexus kommen sieht
  (die eigene API-Verbindung in der Zustandstabelle), und schützt diese Adresse zusätzlich zu den Verwaltungsquellen. Vorher konnte eine Regel abgeschaltet werden, über
  die nur OPNexus die Firewall erreicht, wenn in den Quellen nur Arbeitsplätze standen; dann scheiterte auch das automatische Zurücksetzen. Die Vorschau zeigt die Adresse
  je Firewall (nur Admins) oder sagt, dass sie sich nicht ermitteln ließ (API-Recht „Diagnose: Zustände“, Management-Tunnel).
- **Keine verlorenen Regeltreffer mehr (B11):** Jedes Anwenden setzt die Regelzähler der Firewall zurück. OPNexus liest sie jetzt unmittelbar vor jedem Anwenden, das es
  selbst auslöst (Regeln, NAT, Aliase, IPsec/OpenVPN/WireGuard, Netzwerk); vorher konnte eine Regel deshalb fälschlich als „ungenutzt“ gelten.
- **Einstellungen › Regel-Bereinigung:** schlägt die eigene Browser-Adresse als Verwaltungsquelle vor (B12). Geprüft: OPNsense 26.7 hat kein „savepoint“ zum Zurücknehmen
  durch die Firewall selbst (Quellcode und live 404), das automatische Zurücksetzen bleibt bei OPNexus.

- **Installation unter Windows:** Neue Anleitung auf der Website (`/docs/windows/`, auch auf Englisch) und im Handbuch: Docker Desktop mit WSL 2, Zertifizierungsstelle importieren, Zugriff aus dem Netz (Windows-Firewall), Updates mit `opnexus` in WSL und Fehlerbehebung. Entwickelt und getestet wird weiterhin unter Linux; die Anleitung sagt das offen.
- **Sicherheit und Stabilität (aus einer externen Prüfung):**
  - **Rückweg bleibt erhalten:** Wer nach „Konfiguration geschrieben, Anwenden fehlgeschlagen“ erneut ausrollte, überschrieb den ursprünglichen Rückweg; die automatische
    Rücknahme stellte dann den unbestätigten Stand wieder her. Jetzt bleibt der Rückweg der Stand vor dem ersten Versuch. Wartung, Regel-Bereinigung und
    „Konfiguration als Code“ behandeln eine solche Zeile wie eine offene Bestätigung. Gilt für Regeln, Aliase, Port-Forwards, 1:1-NAT, Outbound-NAT und Hierarchie-Regeln.
  - **Anmeldung und 2FA:** Eine Passwort-Anmeldung, die genau während der Aktivierung von 2FA lief, bekam eine Sitzung ohne zweiten Faktor. Jetzt wird sie mit `TOTP_REQUIRED` abgewiesen.
  - **Letzter Administrator:** Zwei Administratoren, die sich gleichzeitig herabstuften oder gegenseitig löschten, konnten alle Administratoren entfernen. Beides ist jetzt serialisiert.
  - **Operatoren:** Mit ausgeschaltetem „Operatoren dürfen ausrollen“ konnten sie noch Snapshots (Boot-Umgebungen) anlegen. Ein Test hält künftig jede schreibende Route
    fest, die keiner Rechte-Regel zugeordnet ist.
  - **Zertifikate und ACME:** Der Ziel-Host ist nach dem ersten Push fest (`409 ZIEL_HOST_NACH_AUSROLLEN_FEST`); vorher meldete ein Host-Wechsel Erfolg für einen Host, der nie
    etwas erhielt. Ein ACME-Konto mit Anfragen lässt sich nicht auf einen anderen Host verschieben (`ACME_KONTO_WIRD_VON_ANFRAGEN_VERWENDET`).
  - **VPN-Ausrollen:** IPsec, OpenVPN und WireGuard sichern jede von der Firewall vergebene Kennung sofort; bei einem Abbruch entstehen beim nächsten Versuch keine Duplikate.
    Pro VPN läuft nur ein Ausrollen oder Entfernen zugleich (`409 VPN_DEPLOY_LAEUFT_BEREITS`).
  - **Konfiguration als Code:** Gültiges YAML mit falsch geformtem Abschnitt (z. B. `aliases: 42`) ergibt `422 CONFIG_INVALID` statt eines Serverfehlers.
  - **`.env`:** Die Anleitungen legen sie mit `install -m 600` an; `opnexus update` und `opnexus status` setzen eine zu offene Datei auf 0600. `opnexus rollback` liest seinen
    Update-Zustand nur noch als Text und führt ihn nie aus.
  - **Abhängigkeiten:** Next.js 15.5.28, PyJWT 2.15.1.

## [1.32.1] "Merlin" — 2026-10-23

- **Korrektur: Rollouts erkannten die Zielversion bei anderen Release-Typen falsch.** OPNexus las die neue Version nur am Kernpaket `opnsense`. Bei der
  Business-Edition (`opnsense-business`) und beim Wechsel des Release-Typs (z. B. auf `opnsense-devel`, dann als neues Paket angeboten) fiel es auf
  `product_latest` zurück; das Gate hielt den Rollout dann fälschlich mit „Version passt nicht“ an. Live gefunden und belegt (Rollout mit Cluster in einer Welle).
- **Plan-Hinweis zu Alias-Namen ehrlich formuliert:** „kein zentraler Alias in OPNexus“ statt „kein Alias dieses Namens“ (von Hand auf der Firewall
  angelegte Aliase sind in Ordnung; der Planer fragt bewusst keine Firewall).

## [1.32.0] "Merlin" — 2026-10-23

- **Konfiguration als Code:** Unter **System › Konfiguration als Code** (und per `opnexus export` / `opnexus apply`) lassen sich Gerätegruppen, Hierarchiegruppen,
  Aliase, Filterregeln, Port-Forwards, 1:1-NAT, Outbound-NAT, Hierarchie-Regeln und Standort-Variablen als **eine YAML-Datei exportieren** (sortiert, ohne Zeitstempel,
  ohne Zugänge oder Schlüssel, für Git und Reviews) und wieder **importieren**: erst ein **Plan** (neu, geändert, entfernt, unverändert, blockiert, mit Hinweisen), dann
  das **Übernehmen** durch einen Administrator in einer Transaktion mit dem Token genau dieses Plans. **Der Import ändert nur die Entwürfe in OPNexus, nie eine Firewall**;
  ausgerollt wird wie immer über Vorschau → Ausrollen → Bestätigen. Ausgerollte oder noch verwendete Objekte entfernt er nie (Prune blockiert den Plan). Die Datei wird
  nur als Daten gelesen (keine Anker, Tags, Merge-Schlüssel, Mehrfach-Dokumente; 1 MiB, strenges Schema mit Zeile und Pfad bei Fehlern). Plan und Export gehen auch mit „Lesen“-Token
  und Nur-Lese-Konten, `opnexus apply --dry-run` liefert Exit-Codes für CI (0 / 2 / 3 / 4). Regeln und NAT-Objekte tragen dafür einen stabilen `key` (Migration, wird für
  Bestand automatisch vergeben). Gruppenänderungen stehen jetzt im Audit.
- **Rückweg zur vorherigen Firmware:** Vor jedem Firmware-Update und Major-Upgrade (manuell, geplant, Rollout, CARP-Übergabe) legt OPNexus auf der Firewall ein
  **ZFS-Boot-Environment** `opnexus-pre-<Version>-<Zeit>` als Rückweg-Punkt an (ohne den Punkt startet das Update nicht; ohne ZFS mit Hinweis). Neue Aktion **Zur vorherigen Firmware**
  (Host-Wartung): Vorschau, Hostname, Downtime-Bestätigung, vorher lokale Sicherung der Konfiguration, dann Aktivieren, Prüfung und Neustart (scheitert ein Schritt, wird das
  vorherige Boot-Environment wieder eingeschaltet). **Der Rückweg setzt auch die Konfiguration zurück** (live belegt): Ausrollvorgänge seit dem Punkt werden als unklar markiert,
  damit das nächste Ausrollen wirklich schreibt. Ein Rollout macht nie von selbst einen Rückweg.
- **Rollout-Gate: Erreichbarkeitsproben.** Bis zu zehn Ping-/TCP-Proben je Plan, von der Firewall selbst ausgeführt, vor der Welle und nach der Beobachtungszeit:
  vorher ok und jetzt nicht mehr hält den Rollout an, schon vorher gestört ist nur ein Hinweis. (Erreichbarkeit von der Firewall aus, nicht der Verkehr durch sie hindurch.)
- Migrationen 0030 (`config_key`) und 0031 (`gate_probes`).

## [1.31.1] "Vulture" — 2026-10-22

- **Korrektur: `opnexus password` und `reset_2fa` stürzten ab.** Seit den Standort-Variablen (1.22) brach das Zurücksetzen des Admin-Passworts
  (`./opnexus password`) bzw. der Zwei-Faktor-Anmeldung (`docker compose exec backend python -m app.reset_2fa <benutzer>`) mit einem
  SQLAlchemy-Fehler ab, weil ein Datenmodell nicht registriert war. Der Weg bei vergessenem Passwort oder verlorenem 2FA-Gerät funktioniert wieder;
  ein Regressionstest sichert ab, dass jedes Hilfsskript alle Modelle kennt.

## [1.31.0] "Vulture" — 2026-10-22

- **Regel-Bereinigung: Entfernen, Bestätigen, Löschen, Wiederherstellen:** Im Tab **Bereinigung** (Regelwerk › Regel-Analyse) lassen sich Kandidaten jetzt auswählen
  (Checkbox, höchstens so viele Regeln je Lauf wie in den Einstellungen; „vermutlich“ nur mit einer ausdrücklichen Einzelbestätigung je Regel, „unbekannt“ nie).
  **Vorschau** zeigt je Firewall (im Cluster zuerst der Backup) die Regeln mit Befund, Sicherheit und Schutzprüfung; **Ausrollen** deaktiviert die Regeln und wendet an.
  Danach läuft ein Countdown: **Bestätigen** heißt „ich erreiche die Firewall noch“, ohne Bestätigung setzt OPNexus **automatisch zurück**; bis dahin geht auch
  **Jetzt zurücksetzen**. Bestätigte Regeln lassen sich nach der Wartezeit (einstellbar, Standard 7 Tage) **löschen**; vorher sichert OPNexus den Regelinhalt, und
  unter **Verlauf der Bereinigungen** kommt die Regel mit **Wiederherstellen** deaktiviert und möglichst an alter Position zurück (sonst mit Hinweis am Ende der Gruppe);
  **Wieder aktivieren** hat wieder ein Bestätigungsfenster. Wichtig: **Ausrollen, Wiederholen und Wiederherstellen darf nur ein Administrator**, eine Vorschau gehört
  dem, der sie erstellt hat (ein Admin öffnet selbst eine neue); **Bestätigen geht nur in der Browser-Sitzung**, nie per API-Token. **Die Bereinigung bleibt gesperrt,
  bis unter Einstellungen die Verwaltungsquellen** (IP-Adressen oder Netze, von denen aus verwaltet wird) **eingetragen sind**: Regeln, die diesen Zugang treffen
  könnten, deaktiviert OPNexus nie. Dort stehen auch die Obergrenze je Lauf, die Wartezeit bis zum Löschen und **weitere Verwaltungsports** (zusätzlich zu
  API-Port, 443, 80, 22). Tragen Sie die Adresse ein, mit der OPNexus selbst bei den Firewalls ankommt (im Cluster für jeden Knoten). Quellen, Ports und
  Firewall-Adressen sehen nur Administratoren. Der Hinweis „Entfernen folgt“ entfällt.
- **Update: neuere CLI aus dem Zielrelease:** `opnexus update` (auch per Klick im Webinterface) holt sich nach dem Pull die CLI aus dem Updater-Image des
  Zielreleases und startet sich damit neu, damit Korrekturen am Update-Ablauf sofort greifen statt erst beim übernächsten Update. Das geschieht nur, wenn
  `cosign` und `cosign.pub` vorhanden sind und die Signatur des Image-**Digests** in diesem Lauf geprüft wurde (ausgeführt wird genau dieser Digest);
  sonst und bei jedem Fehler läuft das Update mit der vorhandenen CLI weiter. Abschalten: `OPNEXUS_NO_CLI_REFRESH=1`. Wirksam wird es ab dem Update
  *von* dieser Version.

## [1.30.0] "Owl" — 2026-10-22

- **Regel-Analyse: Bereinigung (nur Ansicht):** Auf **Regelwerk › Regel-Analyse** gibt es den Tab **Bereinigung** (`?tab=cleanup`). Er listet je Firewall die
  Regeln, die einen Blick wert sind: **ungenutzt** (OPNexus hat in 7/30/90/180 Tagen keinen Treffer gesehen), **verdeckt** (eine frühere Quick-Regel
  trifft schon alles, was sie trifft), **doppelt** und **lange deaktiviert**. Jeder Befund trägt eine Sicherheit (**sicher / vermutlich / unbekannt**, Tooltip mit
  dem Grund): „sicher“ gibt es nur mit vollständigen, frischen Belegen, bei einer Regel mit Zeitplan, ohne geladenen pf-Regelsatz oder bei Aliasen bleibt es
  höchstens „vermutlich“. Die Tabelle zeigt auch den letzten Treffer und ob die Regel entfernbar wäre (nicht: automatische Regeln, Regeln im Altformat, von OPNexus
  verwaltete Regeln – die entfernt man über die Regelseite). Es wird nur gelesen; **Entfernen folgt in einer späteren Version**. Die Sicherheitslage verlinkt ihre
  Befunde „ungenutzt/verdeckt/doppelt“ direkt dorthin; per API `GET /api/hosts/{id}/rule-analysis/cleanup?unused_days=30`, per MCP `rule_cleanup_candidates`.
- **Änderungsverlauf je Regel:** OPNexus liest alle 15 Minuten die Regeln jeder Firewall und merkt sich, was sich geändert hat – auch bei Regeln, die nicht von OPNexus
  stammen: angelegt, geändert (mit Vorher/Nachher), aktiviert, deaktiviert, entfernt, jeweils mit der Herkunft „über OPNexus“ (passender Audit-Eintrag) oder „extern“.
  Auf der Live-Regel-Ansicht (Regelwerk › Live über alle Hosts) öffnet die Zeilenaktion **Verlauf** die Ereignisse zusammen mit den gespeicherten Fassungen und dem Audit der
  verwalteten Regel. Die erste Beobachtung speichert nur den Ausgangszustand; der Verlauf wird 365 Tage aufbewahrt (`GET /api/hosts/{id}/live-rules/{uuid}/history`).
  Eine Umsortierung (andere Reihenfolge oder Gruppe) gilt als „verschoben“, nicht als Änderung. Eine leere oder unvollständige Antwort der Firewall (ohne
  automatische Regeln) wird verworfen; verschwinden mehr als die Hälfte der Regeln auf einmal, zählt erst die zweite gleiche Beobachtung. Die Herkunft „über OPNexus“
  erklärt nur Audit-Einträge seit der vorherigen Beobachtung, und jeder Eintrag nur eine Änderung.
  Schemaänderungen 0027 und 0028 (Tabellen und ein Feld für den Beobachtungszeitraum) laufen beim Start.
- **Regelnutzung genauer:** Die Zähler kommen jetzt aus `ruleStats` (von der Firewall schon je Regel summiert; ältere Versionen weiterhin über den pf-Regelsatz) und werden alle
  15 Minuten statt alle 6 Stunden gelesen. Regeln im Altformat (noch nicht migriert) werden ebenfalls erfasst. Das Beobachtungsfenster für „ungenutzt“ beginnt neu, wenn eine Regel eingeschaltet oder geändert wird, und nach einem pf-Neuladen mit langer Lücke bei den Zählern.
- **Verdeckt/doppelt in der Sicherheitslage genauer:** Der Vergleich kennt jetzt die echte Reihenfolge (Floating vor Interface-Gruppen vor Interface-Regeln), Netze und
  Portbereiche als Mengen (z. B. 10.1.0.0/16 liegt in 10.0.0.0/8), `inet46` und `tcp/udp`, und übergeht Regeln mit Zeitplan, Markierung oder Limits. Es findet dadurch
  mehr verdeckte Regeln als bisher, nie weniger belegte.

## [1.29.0] "Caracara" — 2026-10-22

- **Fehlende API-Rechte werden sichtbar:** Darf der API-Schlüssel einer Firewall den Gateway- oder Dienststatus nicht lesen (OPNsense antwortet
  mit 403), fiel das bisher niemandem auf, und der standardmäßig aktive Gateway-Alarm konnte nie auslösen. Jetzt zeigen die Host-Seite
  (Abschnitt „Gateways“) und die Dashboard-Karte „Fleet-/CARP-Gesundheit“ ein Warn-Badge „API-Rechte fehlen: Gateways, Dienste“; der Tooltip nennt die
  OPNsense-Berechtigung, die beim Benutzer oder bei der Gruppe fehlt (System: Gateways, Status: Services, Lobby: Dashboard). Zusätzlich gibt es
  den neuen Alarm **API-Rechte fehlen** (standardmäßig an, abschaltbar unter Einstellungen › Benachrichtigungen, gleiche Entprellung wie die
  anderen Messungs-Alarme) und die Prometheus-Metrik `opnexus_host_api_rights_missing{host,read}`. Nur eine ausdrückliche Ablehnung zählt;
  Zeitüberschreitungen oder Serverfehler bleiben „unbekannt“.
- **Gateway-Alarm pro Gateway abschaltbar:** In der Gateway-Liste der Host-Seite schaltet eine Glocke den Alarm für ein einzelnes Gateway aus
  (z. B. ruhendes Zweit-WAN oder tote IPv6-Gateways); ein „Alarm aus“-Badge zeigt es an. Ein offener Alarm dieses Gateways löst sich dabei
  auf. Nur Administratoren, die Änderung steht im Audit-Log. Das Dashboard zeigt bei mehr als acht Gateways „+ N weitere“.
- **Firewall-Oberfläche (GUI-Zugriff) härter:** Ein frei gewordener Platz wird für eine **andere** Firewall erst 5 Minuten nach dem Ende der letzten
  Sitzung vergeben (Schutz, falls dort noch ein Tab offen ist, der die Cookies des Platzes teilt); dieselbe Firewall bekommt ihn sofort wieder.
  Sind alle Plätze belegt oder noch in der Abkühlzeit, nennt die Meldung den Grund. Ändern sich Adresse, festgesetztes Zertifikat oder Tunnelweg
  einer Firewall, während ihre Oberfläche offen ist, endet die Sitzung spätestens nach 20 Sekunden. Das Limit von 64 gleichzeitigen Sitzungen
  verdrängt keine laufende Sitzung mehr: eine neue wird abgelehnt (mit Meldung), bis eine endet. **Hinweis:** Das GUI-Gateway-Image muss zur
  Backend-Version passen (wie bisher beim Update beider Container).
- **Anmeldung (Cookies) härter:** Das alte Sitzungs-Cookie `opnexus-session` wird bei HTTPS **nicht mehr akzeptiert**. **Alle, die sich seit der
  Umstellung auf 1.27 nicht neu angemeldet haben, müssen sich einmal neu anmelden**; Skripte mit rohem Sitzungs-Cookie brauchen den Namen
  `__Host-opnexus-session` (API-Tokens sind nicht betroffen, ohne HTTPS bleibt `opnexus-session` der normale Name). Beim Abmelden wird ein altes
  Cookie weiterhin gelöscht. Das Cookie, das die SSO-Anmeldung an den Browser bindet, heißt bei HTTPS jetzt `__Host-opnexus-oidc-state`; der
  Rückweg vom Anbieter wird nur noch als Seitenaufruf (Navigation) von einer fremden Seite zugelassen, und ohne dieses Cookie scheitert er immer.
- **Webhook, Benachrichtigungskanäle und SSO-Anbieter: Schutz vor DNS-Rebinding:** Bisher prüfte OPNexus die Adresse, auf die ein Name zeigt, und
  der HTTP-Client löste den Namen danach noch einmal auf; ein Server unter Fremdkontrolle konnte beim zweiten Mal eine interne Adresse liefern. Jetzt
  verbindet OPNexus genau zu der geprüften Adresse (Host-Name, TLS-Zertifikatsprüfung und Anfrage bleiben beim Namen); zeigt eine Antwort auch nur
  teilweise auf Loopback, Link-Local oder einen Cloud-Metadaten-Dienst (auch AWS-IPv6 und Alibaba), kommt keine Verbindung zustande und die Zustellung meldet „Zieladresse nicht
  erlaubt“. Private Netze bleiben erlaubt (eigener ntfy, Gotify, Matrix). Proxy-Umgebungsvariablen werden für diese Aufrufe nicht mehr beachtet.
- **Benachrichtigungen parallel zugestellt:** Webhook, E-Mail und alle Kanäle werden gleichzeitig bedient, jeder mit eigener Zeitgrenze (20 Sekunden),
  und ein Prüfdurchlauf sendet insgesamt höchstens 90 Sekunden. Ein hängender Kanal verzögert weder die anderen noch die nächsten Prüfungen; im Log
  steht dann „timed out“ (ohne Adresse oder Token). Wie bisher wird einmal je Ereignis zugestellt, ohne Wiederholung, **außer** der Durchlauf war so voll, dass
  für ein Ereignis noch gar kein Versuch stattfand: es wird im nächsten Durchlauf zugestellt. Die Zustellung beginnt jetzt nach allen Prüfungen
  eines Durchlaufs statt mitten darin.
- **Rollouts: Fingerabdruck, verantwortliche Person, überlappende Termine:** Der Fingerabdruck eines aktivierten Plans umfasst jetzt auch die
  ausgelassenen und verschobenen Termine seines Fensters: **wer einen Termin auslässt oder verschiebt, muss einen automatischen Plan danach neu
  aktivieren** (ohne solche Ausnahmen bleiben bestehende Aktivierungen gültig; **Pläne, deren Fenster schon Ausnahmen haben, nach dem Update
  einmal neu aktivieren**, sonst meldet der Plan „Plan geändert“ und startet nicht automatisch). Die verantwortliche Person (Aktivierende, Startende, Freigebende)
  wird zusätzlich als Konto-ID gespeichert (Migration 0026, bestehende Einträge werden über den Namen zugeordnet): ein gelöschtes und unter gleichem
  Namen neu angelegtes Konto gilt nicht mehr als dieselbe Person, der Lauf pausiert bzw. der automatische Start unterbleibt mit „Person darf nicht mehr
  ausrollen“. Angezeigt wird weiter der Benutzername. Überlappen sich zwei Termine eines Wartungsfensters, zählt der zuletzt begonnene (bisher der
  früheste), sodass ein älterer, längst abgeschnittener Termin keinen neuen Start mehr blockiert.
- **Kleinigkeiten:** Ressourcen-Alarme zeigen bei fehlendem Messwert keine Zahl mehr („None %“ entfällt). Der Dialog „Von Firewall übernehmen“ zeigt ohne aktive
  Firewall einen Hinweis statt dauerhaft „Lädt…“. Fehlermeldungen beim Übernehmen und Verknüpfen nennen die Gründe in Klartext (Deutsch/Englisch) statt
  roher Codes wie `name_exists`; dasselbe gilt für „GUI-Zugriff nicht eingerichtet“ und blockierte Netzwerkänderungen. Der Test-Knopf der
  Benachrichtigungskanäle ist für Nicht-Admins ausgeblendet (der Server verweigert den Test ohnehin). Der Tooltip der aktiven Plätze nennt keine
  Umgebungsvariable mehr. Eine Gateway-Sitzung, die der Gateway-Container nicht mehr bestätigt (Absturz), zählt für die Abkühlzeit der Plätze als
  beendet.
- **Grafana-Dashboard erweitert:** neue Felder für Gateways (erreichbar, Paketverlust, Latenz), Swap, gestoppte Dienste, fehlende API-Rechte und
  Rollouts nach Status; dazu die Metrik `opnexus_rollout_runs{status}`.

## [1.28.0] "Sparrowhawk" — 2026-10-21

- **Übernehmen: mit vorhandenem zentralem Objekt verknüpfen:** Dieselbe von Hand angelegte Regel, derselbe Alias oder dieselbe NAT-Regel auf mehreren
  Firewalls (z. B. beide HA-Knoten) wird nicht mehr als zweites Objekt übernommen. Im Dialog „Von Firewall übernehmen“ erscheint bei exakter
  Übereinstimmung (nach Einsetzen der Host-Variablen, bei Aliasen inkl. Verweisen) die Aktion **Verknüpfen**: die Firewall wird weiteres Ziel des
  vorhandenen Objekts, die Vorschau zeigt „keine Änderung“, und der nächste Rollout aktualisiert alle Firewalls an einem Objekt. Auf der Firewall wird
  dabei nichts geändert. Unterscheidet sich ein gleichnamiges Objekt, zeigt der Dialog die abweichenden Felder; verknüpft wird dann nie. „Alle
  übernehmbaren“ fragt vorher nach („3 verknüpfen, 2 neu übernehmen, 1 nicht übernehmbar“). Aliase, die bisher wegen „Name existiert“ abgelehnt wurden,
  lassen sich so übernehmen.
  Wartet das vorhandene Objekt gerade auf eine Bestätigung, wird nicht verknüpft; „Alle übernehmbaren“ lässt den Eintrag dann aus. Verknüpfen
  macht ein gerade geöffnetes Bearbeiten-Formular des Objekts ungültig, damit die neue Firewall nicht versehentlich wieder aus den Zielen fällt.

- **Tooltips überarbeitet:** Der deaktivierte Button „Oberfläche öffnen“ nennt jetzt den Grund im Klartext samt nächstem Schritt (statt nur einer
  Variablen wie `GUI_GATEWAY_TOKEN`); dasselbe gilt für ausgeschaltete Funktion, deaktivierte Firewall, Nicht-HTTPS-Adresse und ausgeschaltete
  Zertifikatsprüfung. Texte durchgehend in Du-Form, kürzere VPN-Einleitungen, verständlichere Hinweise bei gesperrten Buttons (Zertifikate, Benutzer,
  Dienste). Icon-Buttons (Bearbeiten, Löschen, Schließen, Reihenfolge) zeigen beim Darüberfahren ihren Namen.

## [1.27.2] "Buzzard" — 2026-10-21

- **`opnexus update` prüft den Proxy vor dem Wechsel:** Nach dem Ziehen der Images validiert das Werkzeug die Caddy-Konfiguration des neuen
  Proxy-Images mit den Umgebungsvariablen *dieser* Installation. Ist sie ungültig, bricht das Update ab, bevor etwas gewechselt wurde.
  Der Fall aus 1.27.0 (Compose-Datei mit leerem `OPNEXUS_GUI_SITES`) wird dabei automatisch repariert: Das Werkzeug trägt den Platzhalter
  `OPNEXUS_GUI_SITES=https://localhost:9441` in die `.env` ein und sagt es. Installationen, die noch die Compose-Datei aus 1.27.0 haben,
  müssen also nur einmal `opnexus update` ausführen.
- **`opnexus update` erkennt einen abstürzenden Proxy:** Ein Dienst ohne Healthcheck (der Proxy) gilt jetzt erst nach 10 Sekunden
  durchgehender Laufzeit als gesund. Bisher meldete das Update „Fertig“, obwohl der Proxy in einer Neustart-Schleife hing, und das
  automatische Zurückrollen griff nicht.

## [1.27.1] "Buzzard" — 2026-10-21

- **Fix (1.27.1): Proxy startete mit der neuen Compose-Datei nicht.** Mit der `docker-compose.yml` aus 1.27.0 gab Compose die Variable
  `OPNEXUS_GUI_SITES` als leeren Text an den Proxy weiter; Caddy nutzt seinen Standardwert aber nur bei *nicht gesetzter* Variable und
  brach mit „server block without any key“ ab, OPNexus war dann nicht mehr erreichbar. Die Compose-Datei setzt jetzt einen gültigen
  Platzhalter als Standard. Betroffen waren nur Installationen, die die neue Compose-Datei aus 1.27.0 übernommen hatten (ohne
  `OPNEXUS_GUI_SITES` in der `.env`); wer nur die Images aktualisiert hatte, war nicht betroffen. Sofort-Abhilfe ohne Update:
  `OPNEXUS_GUI_SITES=https://localhost:9441` in die `.env` eintragen und `docker compose up -d proxy` ausführen.

## [1.27.0] "Buzzard" — 2026-10-21

Vorgesehen als MINOR-Release **1.27.0** „Firewall-Oberfläche über OPNexus“.

- **Firewall-Oberfläche über OPNexus:** Auf der Host-Seite öffnet **Oberfläche öffnen** die Weboberfläche der Firewall
  in einem neuen Tab, auch bei Firewalls hinter NAT im Management-Tunnel. Nur für Admins; jede Firewall läuft in einer
  eigenen Browser-Origin (Platz = Port), Cookies bleiben auf dem Server, das Ticket gilt einmalig 30 Sekunden,
  Inaktivitäts- und Gesamtlimit sind einstellbar, Ticket, Start und Ende stehen im Audit (Art „GUI-Zugriff“).
  Standardmäßig **aus**: braucht das Compose-Profil `gui`, `OPNEXUS_GUI_SITES`, `GUI_PORTS`, `GUI_GATEWAY_TOKEN`,
  `COOKIE_SECURE=true` und den Schalter unter Einstellungen (Handbuch, „Firewall-Oberfläche über OPNexus“).
  Live getestet auf OPNsense 26.7.
- **Einschränkungen:** kein WebSocket, keine automatische Anmeldung, höchstens 4 Plätze gleichzeitig. OPNexus-Adresse
  vorher in die Whitelist des Anmeldeschutzes (sshguard) der Firewall eintragen, sonst können Fehlanmeldungen die
  API-Zugriffe von OPNexus aussperren. Nach einem Neustart des wg-hub braucht der Tunnel 2 bis 4 Minuten.
- **Hinweis für Betreiber mit eigenen Skripten:** Bei HTTPS heißt das Sitzungs-Cookie jetzt `__Host-opnexus-session`
  (bestehende Sitzungen werden ohne Abmelden übernommen), und Cookie-Anfragen an `/api` mit `Sec-Fetch-Site: same-site/cross-site`
  werden abgelehnt. Zugriffe mit API-Token sind nicht betroffen.

## [1.26.0] "Kite" — 2026-10-21

Geplant als MINOR-Release **1.26.0** (Regel-Analyse). Backend, MCP und Oberfläche.

- **Regel-Analyse „Welche Regel greift?“ (API):** `GET /api/hosts/{id}/rule-analysis/match` und
  `GET /api/rule-analysis/match` (ganze Flotte, Auswahl oder Gerätegruppe) beantworten für Quelle, Ziel, Protokoll
  und Port, ob eine Firewall das erste Paket einer neuen Verbindung durchlässt oder blockiert, welche Regel das
  entscheidet und warum (Prüfprotokoll Regel für Regel). Grundlage ist der tatsächlich geladene pf-Regelsatz
  inklusive automatischer Regeln, Floating- und Gruppenregeln, `quick`/letzter Treffer, Negation, verschachtelter
  Aliase (pf-Tabellentest der Firewall), Port-Aliase und Adressfamilien. Jede Antwort trägt eine ehrliche
  Konfidenz **sicher / vermutlich / unbekannt** mit Begründungscodes; was nicht belegbar ist (z. B. `tagged`,
  nicht auflösbare Aliase, geratenes Interface, NAT in der Nähe), wird nie als „sicher“ gemeldet. Nur lesend,
  auch mit `read`-Token nutzbar; es wird nichts gespeichert und nichts an der Firewall geändert.
- **MCP:** neues Werkzeug `rule_match` für KI-Assistenten (gleiche Abfrage, ohne Prüfprotokoll).
- **Seite „Regel-Analyse“ (Regelwerk):** Formular für Hosts oder Gerätegruppe, Quell-/Ziel-IP, Protokoll, Ports,
  Eingangs-Interface („automatisch“ oder Liste) und optional die Ausgangsseite. Die Abfrage steht in der Adresszeile
  (teilbar, wird beim Öffnen sofort ausgeführt). Je Firewall eine Karte mit Erlaubt/Blockiert/Abgewiesen, Sicherheit
  des Urteils, entscheidender Regel und Hinweisen; ein „unbekannt“-Ergebnis erscheint bewusst nicht als Urteil.
  Aufklappbares „Warum?“ zeigt jede geprüfte Regel mit ✓/✗/? je Feld, überstimmte Regeln durchgestrichen. Auch mit
  Nur-Lesen-Rolle nutzbar.

## [1.25.0] "Goshawk" — 2026-10-21

Geplant als MINOR-Release **1.25.0** (Flotten-Rollout).

- **Rollouts in Wellen (System › Rollouts):** Firmware-Updates für die ganze Flotte in Ringen, z. B. erst
  `ring-canary`, dann `ring-pilot`, dann `ring-prod` (Wellen aus Gerätegruppen). Jeder Host läuft über die
  bestehende geplante Wartung (Backup vor dem Update, CARP-Übergabe für den MASTER, Audit), immer nur ein Host
  gleichzeitig und nur ein Rollout zur selben Zeit; im Cluster zuerst der BACKUP, dann der MASTER. Nach jeder Welle
  folgen eine Beobachtungszeit und eine **Gesundheitsprüfung** (erreichbar, Zielversion installiert, CARP je VHID,
  Gateways und Dienste wie vorher, keine offenen Sperren, optional Sicherheits-Score). Rot hält den Rollout an
  (nach einer Nachprüfung zwei Minuten später) und meldet sich. Anhalten, Fortsetzen (prüft zuerst alles bisher
  Aktualisierte erneut), Welle freigeben, Host überspringen und Abbrechen auf der Lauf-Seite; die Seite zeigt
  Wellen, Hosts (Version vorher → nachher, aktueller Schritt) und die Prüfmatrix.
- **Sicherheitsnetz:** Weicht die angebotene Version in einem späteren Ring von der des ersten Rings ab, hält der
  Rollout an (OPNsense installiert immer die neueste). Ein Host ohne Update wird übersprungen, ein fehlgeschlagener
  nie von selbst wiederholt. Angehalten wird auch, wenn die verantwortliche Person das Recht zum Ausrollen verliert,
  eine fremde Wartung im Cluster länger als 15 Minuten offen ist, der MASTER ohne Übergabe dran wäre oder ein Lauf
  älter als 14 Tage ist. Höchstens 10 Wellen und 100 Hosts je Plan.
- **Wiederkehrende Wartungsfenster:** wöchentlich oder monatlich (n-ter Wochentag, letzter, fester Tag) in einer
  Zeitzone, mit Sommerzeit-Regel, Abschneidezeit („neue Hosts nur in den ersten N Minuten“) und einzelnen Terminen
  zum Auslassen oder Verschieben. Ein Plan mit Fenster startet neue Hosts nur darin; mit **automatischem Start**
  (nur stabile Updates) beginnt er zu jedem Termin von selbst, aber nur, solange Plan, Gruppen und Fenster seit
  der Aktivierung unverändert sind. Jede Änderung deaktiviert ihn.
- **Geplante Wartung prüft sich selbst:** Neue Option „Ergebnis automatisch prüfen und abschließen“ (im Dialog
  vorausgewählt, bei Rollouts immer an). Nach dem Auftrag prüft OPNexus, ob die Firmware wieder bereit ist, der
  erwartete Neustart stattfand und die Version lesbar ist, und schließt die Wartung dann selbst ab. Bisher blieb
  sie auf „übergeben“ und sperrte Host und Cluster, bis jemand sie von Hand abschloss.
- **Benachrichtigungen:** „Rollout angehalten“ und „Rollout nicht automatisch gestartet“ (ein Schalter, standardmäßig an).
- **Rechte:** Rollout starten/aktivieren/fortsetzen/freigeben/überspringen/abbrechen und Wartungsfenster ändern
  gelten als Ausrollen (Aufgabentrennung); **Anhalten** darf jeder mit Schreibrecht. Update Center: Hosts im
  laufenden Rollout sind für manuelle Firmware-Aktionen gesperrt. Ein OPNexus-Update während eines Rollouts ist
  sicher, wird aber angekündigt.
- **Getestet:** live auf den Test-Firewalls (OPNsense 26.7): automatischer Start im Fenster, Canary-Update
  26.7.1_1 → 26.7.7, absichtlich gestoppter Dienst → Gate rot → angehalten (zweiter Ring unberührt,
  Benachrichtigung), Fortsetzen, Pilot-Update 26.7.6 → 26.7.7 auf dem damaligen MASTER mit CARP-Übergabe und einem
  Neustart des OPNexus-Backends mitten im Auftrag (genau ein Update-Auftrag), Freigabe, Host ohne Update
  übersprungen, Anhalten/Fortsetzen/Abbrechen von Hand, Termin auslassen. **Nur gemockt:** Major-Upgrade als
  Rollout, ein Cluster in *einer* Welle, Cluster-Prüfung vor dem zweiten Knoten, abweichende Zielversion,
  Zeitüberschreitungen, Rechteverlust, Fenster schließt mitten im Lauf; die Sommerzeit-Regeln sind Rechentests.

## [1.24.0] "Merlin" — 2026-10-21

- **Alarme für Gateways, Ressourcen und Dienste:** Neue Ereignisse in Einstellungen › Benachrichtigungen.
  **Gateway gestört** (standardmäßig **an**) meldet ein Gateway, das OPNsense als „down“ führt oder dessen Paketverlust (ab 20 %) bzw.
  Latenz (ab 500 ms) über der Schwelle liegt; bewusst abgeschaltete und nicht überwachte Gateways bleiben still. **Ressourcen-Schwelle**
  (standardmäßig aus) prüft Festplatte (ab 90 %), Arbeitsspeicher (ab 95 %), Swap (ab 50 %) und CPU-Last (ab 2 pro Thread), **Dienst ausgefallen**
  (standardmäßig aus) eine einstellbare Liste (Unbound, Dnsmasq, DHCP, NTP, OpenVPN, WireGuard, IPsec …) und meldet nur Dienste, die zuvor
  liefen. Alle drei stützen sich auf die Minutenmessung und lösen erst aus, wenn die letzten 3 (Ressourcen: 5) Messungen übereinstimmen, und
  enden erst, wenn alle wieder in Ordnung sind: Einzelspitzen und flackernde Verbindungen schicken nichts. Der Gateway-Status (Verlust, Latenz,
  Abweichung) steht außerdem auf der Host-Seite, als Kachel auf dem Dashboard und als Prometheus-Metriken (`opnexus_host_gateway_*`,
  `opnexus_host_swap_used_percent`, `opnexus_host_service_running`). Die Messung speichert dafür zusätzlich Swap, CPU-Threads, Gateways
  und Dienste in der vorhandenen Messreihe (keine neue Tabelle).
- **Weitere Benachrichtigungskanäle:** ntfy, Gotify, Telegram, Microsoft Teams (Workflows-/Incoming-Webhook) und Matrix, beliebig viele
  je Typ, mit „Testen“-Knopf pro Kanal. Zugangsdaten (Tokens, die Teams-Adresse) liegen wie das SMTP-Passwort in der Geheimnis-Ablage, nie
  in der Datenbank oder in Antworten, und tauchen nicht in Fehlermeldungen oder Logs auf. Webhook und neue Kanäle prüfen die Zieladresse:
  nur http(s), keine Zugangsdaten in der Adresse, keine lokalen/Link-Local-/Metadaten-Adressen; Weiterleitungen werden nicht verfolgt.
  Betrifft die bestehende Webhook-Adresse: `localhost`, Loopback, 169.254.x.x und Zugangsdaten in der Adresse (`https://user:pass@…`)
  werden jetzt abgelehnt. Eine solche gespeicherte Adresse wird nicht mehr beliefert (nur ein Log-Eintrag), und die
  Benachrichtigungs-Einstellungen lassen sich erst wieder speichern, wenn sie korrigiert oder geleert ist (die Meldung nennt den Grund).
  Neue Einstellungen kommen mit Migration 0020.

- **Bestand übernehmen für Aliase und NAT:** Auf den Seiten Aliase und NAT (Port Forward, 1:1, Outbound) gibt es jetzt **Von Firewall übernehmen**
  wie bei den Regeln. Der Dialog zeigt, was von Hand in OPNsense angelegt wurde, einzeln zum Übernehmen oder mit „Alle übernehmbaren“ in einem Schritt;
  verschachtelte Aliase werden in der richtigen Reihenfolge übernommen. Die Übernahme schreibt nichts auf die Firewall: Die Vorschau zeigt direkt
  danach „keine Änderung“, der nächste Rollout aktualisiert dasselbe Objekt (kein Duplikat). Nicht übernommen werden OPNsense-eigene Aliase,
  Typen, die OPNexus nicht kennt (z. B. URL, GeoIP), Aliase mit Verweis auf nicht übernommene Aliase sowie NAT-Regeln, die das Modell verfälschen würden
  (z. B. „Kein Redirect“, verknüpfte Filterregel, mehrere Schnittstellen, „Kein NAT“); die Oberfläche nennt den Grund je Objekt.
  Outbound-NAT lässt sich nur übernehmen, wenn der Modus der Firewall Hybrid oder Manuell ist. Einstellungen, die OPNexus nicht verwaltet (Protokollierung,
  Pool-Optionen, Tags, Kategorien), bleiben unverändert und werden angezeigt.

- **Images für ARM:** Alle Release-Images (backend, frontend, proxy, updater) gibt es jetzt für `linux/amd64` und `linux/arm64`
  (Raspberry Pi 4/5, ARM-Server, Apple-Silicon-Docker).

## [1.23.11] "Harrier" — 2026-10-20

- **Updates per Klick prüfen jetzt ebenfalls die Signatur:** Der Updater-Container enthält `cosign` (feste Version, Prüfsumme beim Bau
  geprüft) und verifiziert die neuen Images vor dem Wechsel, wenn `cosign.pub` im Projektverzeichnis liegt. Das Image wird dadurch größer
  (rund 266 statt 123 MB).

## [1.23.10] "Harrier" — 2026-10-20

- **Signierte Release-Images:** Die Images (backend, frontend, proxy, updater) werden mit cosign signiert, beginnend mit 1.23.9. Der öffentliche
  Schlüssel liegt als `cosign.pub` im Repository und im Release-Bundle. `opnexus update` prüft die Signaturen vor dem Wechsel, wenn `cosign`
  installiert ist. Anleitung: Handbuch, „Images prüfen“.

## [1.23.9] "Harrier" — 2026-10-20

- Update-Karte: Der Hinweis für Installationen ohne Release-Images nennt jetzt die tatsächliche Ursache („OPNEXUS_IMAGE_REPO ist nicht gesetzt“)
  statt „baut aus dem Quellcode“.
- `cosign.pub`: öffentlicher Schlüssel für die Signaturen der Release-Images (das Release-Skript kann Images jetzt signieren).

## [1.23.8] "Harrier" — 2026-10-20

- **Einstellungen › OPNexus aktualisieren:** Auf einer Installation, die aus dem Quellcode gebaut wird (kein `OPNEXUS_IMAGE_REPO`), erscheint nicht mehr
  die Anleitung zum Aktivieren des Updaters – Updates per Klick gibt es dort ohnehin nicht –, sondern nur der Hinweis auf `git pull` und
  `docker compose up -d --build`. Das Backend erkennt den Modus jetzt selbst (neue Umgebungsvariable im Compose-File).

## [1.23.7] "Harrier" — 2026-10-20

- Hinweistext der Versionsprüfung verweist auf den Schalter „oben“ statt „unten“.

## [1.23.6] "Harrier" — 2026-10-20

- **Versionsprüfung im Webinterface:** Unter Einstellungen › OPNexus aktualisieren lässt sich die Prüfung auf neue Versionen jetzt per Schalter
  und Feed-Adresse einstellen – kein Eingriff in die `.env` mehr nötig (Migration `0019`). Standardmäßig bleibt sie aus. Ein gesetztes
  `UPDATE_CHECK_URL` in der `.env` hat weiter Vorrang und sperrt den Schalter (mit Hinweis).

## [1.23.5] "Harrier" — 2026-10-20

- **Fix (Ursache des Vorfalls vom 20.10. in 1.23.4):** OPNsense merkt ein CARP-VIP zum Löschen vor, wenn eine Änderung kein `interface` mitschickt,
  und der anschließende „Anwenden“-Schritt entfernt es dann live, ohne es wieder anzulegen. OPNexus schickt bei jeder VIP-Änderung jetzt die
  bisherige Schnittstelle mit (wie schon die Adresse). Auf dem Test-Cluster mit echten Firewalls nachgestellt und bestätigt.
- **Einstellungen › OPNexus aktualisieren:** Die Anleitung zum Aktivieren nennt jetzt alles Nötige in einem Block (auch `UPDATE_CHECK_URL`) und weist
  darauf hin, dass Updates per Klick Release-Images brauchen; wer aus dem Quellcode baut, aktualisiert mit `git pull` und `docker compose up -d --build`.

## [1.23.4] "Harrier" — 2026-10-20

- **Fix (Produktiv-Vorfall 2026-10-20):** Das Ändern der Beschreibung eines CARP-VIP wendete das VIP neu an und ließ es auf beiden Firewalls
  im Zustand INIT hängen – OPNexus zeigte trotzdem „alles in Ordnung“. Nur die Beschreibung zu ändern, wendet jetzt nichts mehr an. Nach jeder
  anderen CARP-VIP-Änderung prüft OPNexus den Live-Zustand (MASTER/BACKUP), wendet bei Bedarf einmal erneut an und meldet sonst „Angewendet,
  Prüfung weicht ab“.
- **CARP-Überwachung je VHID:** Neue Warnungen `vip_down` (ein VIP ist auf einem Node nicht aktiv) und `role_mismatch` (ein Node ist nur für
  einen Teil der VIPs MASTER). Bisher zählte der Host schon als MASTER, sobald ein einziges VIP MASTER war.

## [1.23.3] "Harrier" — 2026-10-20

- **SSO:** Liefert der Anbieter `preferred_username`/`email` nicht im ID-Token (Authelia), fragt OPNexus sie beim Userinfo-Endpunkt ab; der neue
  Benutzer heißt dann `alice` statt wie die kryptische Benutzerkennung des Anbieters. Geprüft gegen Keycloak 26 und Authelia 4.39; S3-Offsite gegen
  Garage, MinIO, RustFS und Versity S3 Gateway.

## [1.23.2] "Harrier" — 2026-10-20

- **SSO-Fix (gefunden beim Test gegen ein echtes Keycloak):** Die Standard-Scopes enthalten nicht mehr `groups` – Keycloak und Entra ID
  lehnten die Anmeldung damit mit `invalid_scope` ab. Bestehende Einstellungen mit dem alten Standardwert werden umgestellt (Migration
  `0018`); Anbieter wie Okta tragen `groups` bei Bedarf selbst ein (Hinweis in den Einstellungen).

## [1.23.1] "Harrier" — 2026-10-20

- **Sicherheit (Ergebnis eines unabhängigen Reviews der Versionen 1.18–1.23):**
  - Aufgabentrennung: Mit „nur Admins rollen aus“ ist jetzt auch das Löschen verwalteter Objekte (entfernt sie von den Firewalls), Zertifikat-/ACME-Push, Dienste starten/stoppen und „Reihenfolge erneut durchsetzen“ den Admins vorbehalten, nicht nur `/deploy`.
  - Backup-Änderungsansicht: weitere Geheimnis-Felder (`otp_seed`, `pass`, `pwd`, …) werden ausgeblendet, die Platzhalter sind pro Abruf gesalzen (kein Wörterbuchabgleich, kein Vergleich über Abrufe/Firewalls).
  - SSO: der Anmeldeversuch ist per Cookie an den Browser gebunden (kein Login-CSRF, abgefangene Code/State-Paare sind nutzlos); SSO-Sitzungen dauern 12 Stunden statt 30 Tage; Gruppen-Strings werden nur an Komma/Zeilenumbruch getrennt (Gruppen mit Leerzeichen); Fehlertext des Anbieters wird für das Log bereinigt.
  - Das Ändern des eigenen Passworts widerruft jetzt auch die eigenen API-Tokens.
  - Management-Tunnel: der Hub routet nicht mehr zwischen Firewalls oder ins Compose-Netz (`FORWARD` verworfen, `ip_forward=0`); loopback-/link-local-Netze sind als Tunnel-Netz nicht erlaubt.
  - Sicherheitslage: eine defekte `config.xml` stoppt die Prüfung der übrigen Firewalls nicht mehr; die Auswertung läuft außerhalb der Ereignisschleife.
  - Behoben: S3-Offsite mit Port 443 im Endpunkt (Signatur passte nicht); Variablenwerte mit angehängtem Zeilenumbruch werden abgelehnt.
- Fertiges **Grafana-Dashboard** (`docs/grafana-opnexus.json`) für die Prometheus-Metriken von OPNexus.

## [1.23.0] "Harrier" — 2026-10-20

- **Sicherheitslage im Alltag**: Dashboard-Karte mit den Firewalls mit dem niedrigsten Wert; Benachrichtigung bei kritischen und hohen
  Befunden (je Firewall und Prüfung, mit „behoben“-Meldung; standardmäßig aus; Migration `0017`); druckbarer **Sicherheitsbericht**
  (Knopf „Bericht“, Drucken/PDF über den Browser).

## [1.22.0] "Peregrine" — 2026-10-20

- **Standort-Variablen**: `{{NAME}}`-Platzhalter in Regeln, Aliasen, Portweiterleitungen, 1:1- und Outbound-NAT, je Host mit eigenem Wert
  (Host-Seite › Standort-Variablen, listet die verwendeten Platzhalter automatisch). Je Host aufgelöst in Vorschau, Ausrollen, Zählern und
  Audit; fehlt ein Wert, wird nichts gesendet (Vorschau „Variable fehlt“, Ausrollen wird vor jedem Schreiben abgelehnt, zusätzlich eine
  Sperre im API-Client). Neue Tabelle `host_variables` (Migration `0016`).

## [1.21.0] "Osprey" — 2026-10-20

- **Management-Tunnel** (System › Management-Tunnel): Firewalls hinter NAT/CGNAT verwalten. Ein optionaler WireGuard-Hub auf dem
  OPNexus-Server (Compose-Profil `overlay`, Container `wg-hub` aus dem Backend-Image mit `NET_ADMIN`) nimmt die Firewalls entgegen, die sich
  nach außen einwählen; OPNexus erreicht deren API über einen kleinen CONNECT-Proxy des Hubs, der nur ins Tunnel-Netz verbindet. Je Firewall
  eine Tunnel-Adresse und ein eigener Pre-Shared Key, Anleitung mit kopierbaren Werten, Live-Status (Handshake, Datenmenge). Der Hub nimmt
  keine neuen Verbindungen aus dem Tunnel an. Ende-zu-Ende gegen eine echte OPNsense 26.7 geprüft (Handshake, API, Zertifikat festsetzen).
  Neue Tabellen `overlay_settings`, `overlay_peers` (Migration `0015`); das Backend-Image enthält jetzt `wireguard-tools`, `iproute2`, `iptables`.

## [1.20.0] "Lilli" — 2026-10-20

- **Single Sign-on (OIDC)**: Anmeldung über Authentik, Keycloak, Entra ID, Google u. a. (Authorization Code mit PKCE). Die Rolle
  kommt bei jeder Anmeldung aus den Gruppen des Anbieters, ohne zugeordnete Gruppe kommt niemand hinein; SSO-Konten werden nie mit
  lokalen Konten verknüpft; lokale Anmeldung bleibt als Notzugang. ID-Token werden streng geprüft (Signatur nur asymmetrisch, Aussteller,
  Zielgruppe, Ablauf, Nonce, einmaliger State), gegen einen nachgebauten Anbieter getestet (inkl. Fälschungsversuche). Neue Tabellen
  `oidc_settings`, `oidc_login_states`, Spalten `admin_users.auth_source/oidc_subject` (Migration `0013`). Neue Abhängigkeit: PyJWT.
- **Behoben:** `opnexus backup --full` und `opnexus restore` (Vollsicherung, in 1.19.0 beschrieben) fehlten im 1.19.0-Bundle und -Updater-Image,
  weil die Änderung am Skript nicht mit eingecheckt war; sie sind erst ab dieser Version enthalten.
- **Aufgabentrennung**: Schalter, mit dem nur Admins ausrollen dürfen, Operatoren aber vorbereiten (Migration `0014`).

## [1.19.0] "Lanner" — 2026-10-20

Backups, Governance, Regelnutzung. Auf einem Test-Stack mit drei Test-Firewalls geprüft; die Vollsicherung Ende-zu-Ende
(sichern, Schaden anrichten, zurückspielen), siehe Handbuch „Konfigurationssicherungen“, „Sichern und Wiederherstellen“.

- **Rolle „Operator“**: rollt aus und betreibt (Regeln, NAT, VPN, Wartung, Sicherungen), verwaltet aber keine
  Benutzer, Einstellungen oder Hosts und lädt keine Roh-Backups herunter. Abgelehnte Versuche stehen im Audit.
  Tokens eines Operators gehen bis zum Bereich „Ausrollen“.
- **Audit-Export** (Einstellungen): Syslog (RFC 5424, UDP/TCP/TLS) und/oder signierter Webhook, mindestens
  einmal zugestellt, ohne Objektinhalte. Neue Tabelle `audit_export_settings` (Migration `0010`).
- **Geplante Backups** mit Aufbewahrung, **verschlüsseltem Offsite-Ziel** (S3-kompatibel, AES-256-GCM,
  Passphrase bleibt auf dem Server), Benachrichtigungen bei Problemen und einer **Änderungsansicht** zwischen
  zwei Sicherungen (Geheimnisse nur als Hash). Neue Tabellen `backup_settings`, `backup_uploads`, Spalte
  `notification_settings.backup_failed_enabled` (Migration `0011`). Die S3-Signatur ist gegen die offiziellen
  AWS-Beispiele geprüft; ein Lauf gegen einen echten Anbieter steht noch aus.
- **Vollsicherung** `opnexus backup --full` und `opnexus restore`: Datenbank und alle Geheimnis-Volumes in einer
  verschlüsselten Datei; Ende-zu-Ende auf dem Test-Stack geprüft (Sichern, Schaden anrichten, Zurückspielen).
- **Regelnutzung**: wann hat jede Regel zuletzt etwas getroffen (aus den pf-Zählern, überlebt Neuladen und
  Neustart); Befund „Regeln in Benutzung“ in der Sicherheitslage nach 30 Tagen ohne Treffer. Neue Tabelle
  `rule_hits` (Migration `0012`).
- Änderungen an Einstellungen (`/api/settings/…`) und das Auslösen von Sicherungen stehen jetzt im Audit.

## [1.18.0] "Kestrel" — 2026-10-19

Automatisierung und Sicherheitslage. Ende-zu-Ende gegen einen Test-Stack und drei Test-Firewalls geprüft,
siehe Handbuch „Sicherheitslage“ und „Automatisierung“.

- **API-Tokens** (System › API-Tokens): persönliche Tokens für Skripte, CI, Prometheus und KI-Assistenten,
  Bereiche *Lesen*, *Ausrollen*, *Admin*. Nur als Hash gespeichert, einmal angezeigt, widerrufbar, mit
  Ablaufdatum; Anlegen verlangt das Passwort. Ein Token überschreitet nie die Rechte seines Besitzers.
  Tokens verwalten und das eigene Konto geht nur in einer normalen Anmeldung. Geheimnisse (Roh-Backups,
  Benachrichtigungseinstellungen) liest nur der Bereich *Admin*. Das Audit nennt das Token. Neue Tabelle
  `api_tokens` (Migration `0008`).
- **Prometheus-Metriken** unter `GET /api/metrics` (Host erreichbar, Last, Speicher, Platte, CARP-Rolle,
  offene Aktionen und Probleme, Sicherheitswert), nur aus der Datenbank, ohne Anfragen an die Firewalls.
- **MCP-Endpunkt** `POST /api/mcp` (nur lesend) für KI-Assistenten: Flottenübersicht, Hosts, Auslastung, CARP-Ereignisse,
  Audit, Suche und ein Leser für jeden GET-Endpunkt, mit den Rechten des Tokens.
- **OpenAPI-Schema** jetzt unter `/api/openapi.json` (bisher unter `/openapi.json`, vom Proxy nicht
  erreichbar); die Swagger-/ReDoc-Seiten entfallen (ihre CDN-Skripte verbietet die CSP).
- **Sicherheitslage** (Monitoring › Sicherheitslage): Härtungs- und Regel-Hygiene-Prüfungen je Firewall mit Wert
  0–100: Allow-all am WAN, HTTPS, SSH, SNMP, verdeckte/doppelte/unbeschriftete Regeln, Zeitserver, externe
  Logs, Firmware, Backup-Alter. Liest die Konfiguration nur lesend, speichert sie nicht, alle 6 Stunden und
  auf Knopfdruck. Neue Tabelle `compliance_results` (Migration `0009`).
- Behoben: Änderungen am Benutzer in einer Anfrage (z. B. 2FA aktivieren) gingen verloren, wenn die
  Middleware den Benutzer aus einer anderen Datenbank-Sitzung lieferte; er wird jetzt in der Sitzung der
  Anfrage neu geladen.

## [1.17.0] "Imperial" — 2026-10-19

Geplante Wartung: Fehler zeigen sich sofort statt erst um 3 Uhr nachts, und der Master lässt sich
unbeaufsichtigt aktualisieren. Auf einem Test-Cluster live geprüft (Neustart und ein echtes Update
mit Umschaltung), siehe Handbuch „Geplante Wartung“.

- **Rollenprüfung beim Einplanen:** Ein Host, der in einem Cluster gerade CARP-MASTER ist,
  lässt sich nicht mehr einplanen (Neustart/Update/Major-Upgrade). Bisher wurde der Plan
  angenommen und scheiterte erst zur Uhrzeit. Der Planungsdialog zeigt den Grund an und
  sperrt den Button. Ist die Rolle nicht feststellbar (Host gerade nicht erreichbar), wird
  weiter angenommen; die volle Prüfung läuft wie bisher zum Zündzeitpunkt.
- **Benachrichtigung „Geplante Wartung nicht ausgeführt“:** Eine fehlgeschlagene geplante
  Wartung meldet sich per Webhook/E-Mail (einzeln schaltbar in den Einstellungen, standardmäßig
  an). Offen für drei Tage nach dem geplanten Zeitpunkt, dann ohne „behoben“-Meldung geschlossen.
  Neue Spalte `notification_settings.scheduled_maintenance_failed_enabled` (Migration `0006`).
- **Automatische CARP-Umschaltung (Option pro Plan, standardmäßig aus):** Ein geplanter Neustart,
  Update oder Major-Upgrade auf dem MASTER schaltet vorher auf den Partner um, prüft die
  Übernahme, führt die Aktion aus, wartet auf die Rückkehr des Hosts und schaltet zurück. Fehler
  vor dem Start der Aktion stellen den Normalbetrieb wieder her; danach bleibt der Host bewusst im
  Wartungsmodus (Partner trägt den Verkehr) und es kommt eine Benachrichtigung. Der Ablauf steht in
  der Datenbank und überlebt einen Neustart des Backends. Neue Spalten an `scheduled_maintenance`
  (Migration `0007`).
- **Backup vor geplantem Update:** Ein geplantes Update oder Major-Upgrade zieht unmittelbar vor
  dem Start selbst ein Konfigurationsbackup (nur lesend auf der Firewall). Bisher scheiterte jeder
  Plan, der mehr als 24 Stunden in der Zukunft lag, an der Backup-Aktualität. Schlägt das Backup
  fehl, wird nicht aktualisiert.

## [1.16.0] "Hobby" — 2026-10-10

Erste Schritte nach der Installation. Ende-zu-Ende gegen einen isolierten Stack getestet, siehe
Handbuch „Einrichtungsassistent und Passwort-Reset“:

- **Einrichtungsassistent** (`/setup`, System › Einrichtung): vier Schritte mit Erklärungen —
  Willkommen und Sprache, Konto (eigenes Passwort, Zwei-Faktor-Anmeldung), erste Firewall
  verbinden (inklusive Anleitung für den API-Key in OPNsense, Zertifikat festsetzen,
  Verbindungsstatus) und eine Übersicht, was als Nächstes sinnvoll ist. Auf einer frischen
  Installation ohne Hosts öffnet er sich nach der ersten Anmeldung von selbst; „Assistent
  überspringen“ merkt sich das pro Benutzer, erneut öffnen geht jederzeit über System › Einrichtung
  oder die Einstellungen. Nur für Admins. Neue Spalte `user_preferences.setup_done`
  (Migration `0005`).
- **`opnexus password [NAME]`** setzt ein neues Zufallspasswort für den Admin und zeigt es an
  (`--yes` ohne Rückfrage; Rückfrage nur, wenn sich schon jemand angemeldet hat). Ersetzt das
  Suchen im Backend-Log nach dem beim ersten Start erzeugten Passwort und taugt zugleich für ein
  vergessenes Admin-Passwort. Alle Sitzungen des Kontos enden. Dahinter steht
  `python -m app.reset_password`. Die Meldung beim ersten Start verweist darauf.

## [1.15.0] "Gyrfalcon" — 2026-10-10

Updates aus dem Webinterface. Auf Klick, mit Fortschritt und automatischem Rollback; ein eigener,
optionaler Container erledigt die Arbeit. Ende-zu-Ende mit echten Images getestet, siehe Handbuch
„Update aus dem Webinterface (optional)“:

- **Updates aus dem Webinterface (optional):** Einstellungen › „OPNexus aktualisieren“ zeigt die
  neueste Version samt Release-Notes und aktualisiert auf Klick — mit Fortschrittsanzeige,
  automatischem Rollback bei einem fehlgeschlagenen Update und „Auf Vorversion zurücksetzen“
  (mit Hinweis, ob dabei Daten verloren gehen). Dahinter arbeitet ein eigener, kleiner
  **Updater-Container** (`updater/`, Compose-Profil `updater`, mit `COMPOSE_PROFILES=updater`
  in `.env` aktivieren), der dieselbe Logik wie `opnexus update` ausführt. Er ist standardmäßig
  **aus** und der einzige Container mit Docker-Socket; das Backend bekommt ihn nie. Der Updater
  hat keinen veröffentlichten Port, ist nur mit einem selbst erzeugten Token ansprechbar und kann
  ausschließlich „aktualisieren auf die vom Release-Feed gemeldete neueste stabile Version“,
  „zurückrollen“ und „Status“. Hauptversions-Sprünge bleiben der Kommandozeile vorbehalten.
  Anfragen stehen im Audit-Log (neuer Typ „OPNexus-System“). Gilt nur für Release-Images.
- `opnexus` (jetzt in `updater/opnexus`, `scripts/opnexus` ist ein Symlink): `status --porcelain`,
  Exit-Code 3 für „fehlgeschlagen und sauber zurückgerollt“. Release-Images und -Bundle enthalten
  zusätzlich `opnexus-updater`.
- Der Banner „Neue Version verfügbar“ verlinkt auf den neuen Abschnitt in den Einstellungen.

## [1.14.0] "Eagle" — 2026-10-09

Diagnose von der Firewall aus sowie VXLAN und GIF. Gegen die OPNsense-Core-Quellen
geprüft und live auf den Test-Firewalls getestet, siehe Handbuch „Erweiterung 1.14.0“:

- **Host-System › Diagnose:** Ping, Traceroute, Port-Prüfung und DNS-Abfrage, ausgeführt von der
  ausgewählten Firewall. Nur für Admins, strenge Eingabeprüfung, jeder Lauf im Audit-Log,
  Ping-Jobs werden immer wieder aufgeräumt, der Banner der Gegenstelle wird nicht weitergegeben.
  Packet-Capture gibt es bewusst nicht (Mitschnitte enthalten fremde Nutzdaten).
- **Netzwerk › Geräte:** VXLAN und GIF-Tunnel (anlegen, ändern, löschen) über dieselbe abgesicherte
  Engine wie die übrigen Geräte.
- **Fehlerkorrektur:** Auf den Host-System-Seiten war „Übersicht › Hosts“ markiert und der Seitentitel
  falsch, weil `/hostsystem` mit `/hosts` beginnt.

## [1.13.0] "Buzzard" — 2026-10-09

Mehr Verwaltung pro Firewall und ein Abgleich der Cluster-Knoten. Alles über
dieselbe abgesicherte Engine wie in 1.12.0 und live auf drei Test-Firewalls
geprüft, siehe Handbuch „Erweiterung 1.13.0“:

- **Netzwerk › Geräte:** LAGG, Bridge, Loopback und GRE. Dazu Gateway-Gruppen,
  DNS-Weiterleitungen (Unbound) und DHCP-Bereiche (dnsmasq).
- **Neue Gruppe „Host-System“:** Tunables (immer mit Bestätigung, Vorgaben nur per
  „Überschreiben“), Cron, Syslog-Ziele und ZFS-Snapshots (anlegen und löschen; der
  aktive Snapshot ist geschützt, das Aktivieren bleibt OPNsense selbst überlassen).
- **Netzwerk › Abgleich:** zeigt Unterschiede in der Netzwerk-Konfiguration zwischen den
  Knoten eines Clusters. Neuer Alarm „Netzwerk-Abweichung im Cluster“ (Einstellungen ›
  Benachrichtigungen, Migration 0004).
- **Auf den Partner übertragen:** Nach dem Anlegen eines Objekts lässt es sich mit
  einem Klick auch auf dem anderen Cluster-Knoten anlegen (eigene Vorschau, eigenes Backup).
- **Kopfzeile:** Mit acht Gruppen zeigt die Navigation die Namen ab 1150 px, darunter
  nur Icons, damit Suche und Abmelden in einer Zeile bleiben.

## [1.12.0] "Condor" — 2026-10-09

Netzwerk-Verwaltung und Übernahme von Hand angelegter Regeln. Alles gegen die
OPNsense-Core-Quellen geprüft und live auf drei Test-Firewalls (26.7.1 und
26.7.6) getestet, siehe Handbuch „Netzwerk, DNS und DHCP“:

- **Neuer Bereich „Netzwerk“:** Inventar je Firewall (Schnittstellen mit
  Adressen und CARP-Zustand, VLANs, virtuelle IPs, Gateways, Routen, DNS-Einträge,
  DHCP) und Änderungen daran: anlegen, bearbeiten, löschen für VLANs, virtuelle
  IPs, Gateways, statische Routen, Schnittstellen-Zuweisung samt IP-Konfiguration,
  Unbound-DNS-Einträge, dnsmasq-Hosts sowie Kea-Subnetze und -Reservierungen.
- **Abgesichert:** Jede Änderung läuft über eine Vorschau mit Diff und einem Token,
  der den Firewall-Stand erfasst. Vor dem Schreiben wird ein Konfig-Backup
  gespeichert (ohne Backup keine Änderung), danach angewendet und zurückgelesen.
  OPNexus erkennt, über welche Schnittstelle es die Firewall erreicht; Änderungen,
  die diese Verbindung kappen können (Verwaltungs-Schnittstelle, Standard-Gateway,
  Standardroute, Route über die Firewall-Adresse), brauchen eine ausdrückliche
  Bestätigung mit Eingabe des Firewall-Namens. Es gibt bewusst **keinen**
  automatischen Rollback: OPNexus kann eine Änderung nicht zurücknehmen, wenn sie
  seine eigene Verbindung trennt. Das Backup vor der Änderung ist der Rückweg.
- **Schnittstellen-Löschen:** OPNsense entfernt dabei selbst alle Regeln, die die
  Schnittstelle nutzen. Die Vorschau listet sie, die Bestätigung ist Pflicht.
  Bereits auf der Firewall vorgemerkte Änderungen blockieren eine neue Änderung.
- **IP-Konfiguration per API gibt es erst ab OPNsense 26.7.6.** Auf 26.7.1 ist die
  Zuweisungsliste nur lesbar; OPNexus erkennt das an den Daten und sperrt die
  Änderung mit Begründung.
- **Von der Firewall übernehmen:** Regeln, die direkt in OPNsense angelegt wurden,
  lassen sich in OPNexus übernehmen, bearbeiten und ausrollen. Die Übernahme schreibt
  nichts auf die Firewall. Regeln, die das Modell verfälschen würde (invertierte
  Quelle/Ziel, mehrere Schnittstellen, keine Quick-Regel, …), werden mit Grund
  abgelehnt. Neu: Regeln haben ein Protokollieren-Flag (`log`, Migration 0003),
  damit es beim Bearbeiten nicht still verloren geht; nicht modellierte Felder
  (Gateway, Zustandsoptionen, …) bleiben auf der Firewall unverändert.
- **Geheimnisse:** Das CARP-Passwort (liefert OPNsense im Klartext) und der
  Kea-DDNS-Schlüssel werden nie zurückgegeben, gespeichert oder auditiert.
- **Host-Adresse änderbar:** Hosts › Bearbeiten hat jetzt das Feld „Adresse“. Nach
  einer Änderung der Verwaltungs-IP muss der Host nicht mehr gelöscht und neu
  angelegt werden; Verlauf und Zuordnungen bleiben erhalten. Meldet eine Änderung
  „angewendet, aber nicht prüfbar“, weist die Oberfläche darauf hin.
- **Fehler aus den Live-Tests, die Mocks nicht gezeigt haben:** Jede Änderung einer
  virtuellen IP muss die Adresse (`network`) wiederholen, sonst leert OPNsense sie
  bzw. lehnt ab; die Prüfung nach dem Anwenden vergleicht jetzt den vollständigen
  Soll-Zustand statt nur der gesendeten Felder. Dazu: Gateway-Priorität als Zahl,
  Routen mit `enabled` statt `disabled`, Beschreibungsmuster der Schnittstellen,
  gesperrte Schnittstellen, Pflicht-Backup vor jeder Änderung.
- **Hinweis zu den Versionen:** Die IP-Konfiguration von Schnittstellen per API gibt
  es erst ab OPNsense 26.7.6. Mit 26.7.5 (die Prod-Firewalls) sind VLANs, virtuelle
  IPs, Gateways, Routen, DNS und DHCP verwaltbar, Schnittstellen nur lesbar.

## [1.11.1] "Merlin" — 2026-10-09

Fehler aus dem ersten Live-Test der bisher nur gemockten Funktionen auf drei
echten OPNsense-Firewalls (alle in den Tests mit Mocks unsichtbar geblieben).
Live bestätigt sind jetzt auch das Major-Upgrade über den Update Center
(25.7 → 26.1 → 26.7, je mit Backup und Abschluss-Bestätigung), WireGuard,
OpenVPN, IPsec, Zertifikate, ACME (Staging), hierarchische Policies,
Benachrichtigungen und der HA-Config-Sync:

- **Zertifikate (Trust-Store) und ACME ließen sich nicht pushen:** Die URLs
  endeten auf `…Action` (`/trust/cert/addAction`), OPNsense kennt aber nur
  `/trust/cert/add`, `set`, `del` bzw. `/acmeclient/…/add`, `update`, `del` (404).
  Die Mocks hatten denselben Tippfehler. Ein neuer Test prüft den Client-Quelltext
  gegen dieses Muster.
- **OpenVPN-Deploy scheiterte immer:** Die Firewall verlangt `vpnid` und vergibt
  es nicht selbst. OPNexus wählt jetzt die nächste freie. Außerdem erwarten
  `cert`/`ca` (OpenVPN) und `caref` (Zertifikat signieren) die **refid** der
  Trust-Store-Einträge, der Picker lieferte die Zeilen-UUID. Die Listen
  `/hosts/{id}/certificates` und `/cas` liefern jetzt `refid`; im
  Zertifikatsformular gibt es ein CA-Dropdown statt eines Freitextfelds.
- **WireGuard meldete „Erfolg“ ohne laufenden Tunnel:** Ist der globale Dienst
  (VPN › WireGuard › Allgemein) aus, entsteht trotz geschriebener Konfiguration
  kein Interface. Der Deploy schaltet den Dienst jetzt ein.
- **Dienststeuerung:** Das Backend prüfte weder, ob der Dienst existiert, noch ob
  er gesperrt ist (pf, webgui, routing …). Beides war nur in der UI gesperrt;
  unbekannte Namen meldete die Firewall trotzdem als „ok“. Jetzt 404
  `SERVICE_NOT_FOUND` bzw. 409 `SERVICE_LOCKED`.
- **Fehlertexte der Firewall unleserlich:** Die Redaktion maskierte jede
  Validierungsmeldung, deren Feldname `cert`, `crt` oder `auth` enthält
  („instance.cert: [redacted]“). In `validations` lösen diese drei Namen die
  Maskierung nicht mehr aus; echte Geheimnisfelder bleiben maskiert. Das
  Löschen eines benutzten Zertifikats nennt jetzt den Grund der Firewall
  („Item in use by OpenVPN …“) statt eines allgemeinen Fehlers.
- **Drift-Alarme flackerten beim Neustart eines Cluster-Mitglieds:** Mit nur
  einem erreichbaren Host gab es nichts zu vergleichen, offene Regel-/NAT-Drift-
  und Zertifikatsprobleme wurden „gelöst“ und danach neu geöffnet (je ein
  Meldungspaar pro Problem). Offene Probleme bleiben jetzt unverändert, solange
  sie nicht beurteilbar sind.
- **IPsec lief nach dem Deploy nicht:** Der globale IPsec-Schalter war aus, die
  Verbindungen wurden gespeichert, `reconfigure` antwortete „ok“, OPNexus meldete
  „Erfolg“ (gleiche Klasse wie bei WireGuard). Der Deploy schaltet ihn jetzt ein;
  zwischen zwei Test-FWs steht der Tunnel (Phase 1 verbunden).
- **Löschen scheiterte ohne Grund:** Wenn die Firewall ein Löschen ablehnt (Alias
  noch in einer Regel, Zertifikat in Benutzung), nennt die Meldung jetzt den Grund
  der Firewall (`UPSTREAM_REJECTED`) statt „Anfrage fehlgeschlagen“.
- **Kaputte Eingaben:** Ein NUL-Zeichen in einem Textfeld ergab einen 500er samt
  abgebrochener Verbindung, ebenso ein langes oder NUL-haltiges Pfadsegment (der
  Audit-Eintrag übernahm es ungeprüft in `VARCHAR(36)`). Jetzt 422 bzw. 404/422;
  ein Fuzz-Lauf über alle 188 Routen findet keine 5xx mehr.
- **`opnexus rollback` landete auf der falschen Version:** Zurückgesetzt wurde auf
  die vom Backend gemeldete Programmversion statt auf den vorher eingestellten
  Image-Tag. Bei Release-Candidates (rc1-Images melden schon `1.11.0`) führte das
  auf das neuere Release. Jetzt wird der vorherige exakte Tag wiederhergestellt.
- **Handy-Ansicht (375px) zoomte aus:** Die ausgeblendete Überschrift der
  Aktionsspalte (`sr-only`, absolut positioniert) lag außerhalb der scrollbaren
  Tabellenkarte und machte die Seite bis zu doppelt so breit (Hosts, Benutzer,
  Host-Details). Scroll-Container sind jetzt der Bezugsrahmen.
- **Überschriften-Reihenfolge:** Kartentitel in den Listen (OpenVPN, IPsec,
  WireGuard, NAT) sind `h2` statt `h3`, sichtbar erst mit Daten; axe meldet auf
  allen Seiten, hell und dunkel, nichts mehr.

- **CARP-Wartungsfenster mit Auto-Revert:** Nach dem automatischen Zurückschalten
  blieb die Aktion „CARP-Übergabe“ (`carp_enter`) offen. Sie ließ sich nicht mehr
  bestätigen (der Host ist ja nicht mehr im Wartungsmodus) und blockierte alle
  Deploys und Updates im Cluster (`OPEN_HOST_MAINTENANCE_BLOCKS_DEPLOY`), bis
  jemand sie von Hand abbrach. Der Auto-Revert schließt die Aktion jetzt selbst
  (Status „abgebrochen“, Audit-Eintrag). Schlägt das Zurückschalten fehl, bleibt
  sie bewusst offen. Gefunden beim Live-Test gegen zwei OPNsense-26.7-Firewalls.
- **Update Center, Protokoll-Fenster:** Nach einem Update mit Neustart ist das
  Protokoll der Firewall leer; angezeigt wurde dann nur das Wort „error“, obwohl
  das Update erfolgreich war. Jetzt steht dort ein erklärender Hinweis.

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
