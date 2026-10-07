// German server text → English. The backend still emits some German prose
// inside successful responses (deploy previews, host messages, revision notes,
// blockers) and in Pydantic validation errors. German is the source text, so
// only English needs a lookup; anything unknown passes through unchanged.
//
// Whole-string entries match literally; RegExp entries use named groups that
// fill {placeholders} in the English template. Placeholder values are looked up
// again (sentences, patterns and serverWordsEn), so a captured German noun or
// operation name is translated too.

// Nouns and small words that only appear as captured placeholder values
// (operation names, host kinds, …); never used for substring replacement.
export const serverWordsEn: Record<string, string> = {
  "1:1 NAT-Regel": "1:1 NAT rule",
  "ACME-Account": "ACME account",
  "ACME-Zertifikatsanfrage": "ACME certificate request",
  "Alias": "alias",
  "Aliase": "aliases",
  "IPsec-Identität (lokal)": "IPsec identity (local)",
  "IPsec-Identität (remote)": "IPsec identity (remote)",
  "IPsec Phase-2 (Child)": "IPsec phase 2 (child)",
  "IPsec Pre-Shared-Key": "IPsec pre-shared key",
  "IPsec-Verbindung": "IPsec connection",
  "IPsec-Dienst": "IPsec service",
  "OpenVPN-Instanz": "OpenVPN instance",
  "OpenVPN Static-Key": "OpenVPN static key",
  "OpenVPN-Dienst": "OpenVPN service",
  "Outbound NAT-Regel": "outbound NAT rule",
  "Port-Forward-Regel": "port forward rule",
  "Regel": "rule",
  "WireGuard-Peer": "WireGuard peer",
  "WireGuard-Server": "WireGuard server",
  "WireGuard-Dienst": "WireGuard service",
  "Zertifikat": "certificate",
  "Tunnel": "Tunnel",
  "Instanz": "Instance",
  "unbekannt": "unknown",
};

const NOUNS = Object.keys(serverWordsEn).map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
const operationEntries: Array<[RegExp, string]> = [
  [new RegExp(`^(?<noun>${NOUNS}) ändern$`), "Change {noun}"],
  [new RegExp(`^(?<noun>${NOUNS}) anlegen$`), "Create {noun}"],
  [new RegExp(`^(?<noun>${NOUNS}) entfernen$`), "Remove {noun}"],
  [new RegExp(`^(?<noun>${NOUNS}) neu laden$`), "Reload {noun}"],
];

export const serverMessagesEn: Array<[string | RegExp, string]> = [
  // --- deploy previews -----------------------------------------------------
  ["Zuletzt von OPNexus gespeicherter Deploy-Stand; kein Live-Abgleich.", "Last deploy state stored by OPNexus; not a live comparison."],
  ["Offene Bestätigung oder fehlgeschlagener Rollback: Push gesperrt.", "Open confirmation or failed rollback: push blocked."],
  ["Deaktivierte Hosts werden übersprungen.", "Disabled hosts are skipped."],
  ["Vorheriger Push fehlgeschlagen. Vor erneutem Push den Firewall-Stand prüfen.", "Previous push failed. Check the firewall state before pushing again."],
  ["Frühere Ziele behalten ihre Regel, bis du sie hier gezielt entfernst. Diese Löschung hat keinen automatischen Rollback.", "Former targets keep their rule until you remove them here selectively. This removal has no automatic rollback."],
  ["Frühere Ziele behalten diesen Alias, bis du sie hier gezielt entfernst. Diese Löschung hat keinen automatischen Rollback.", "Former targets keep this alias until you remove them here selectively. This removal has no automatic rollback."],
  ["Frühere Ziele behalten diese Regel, bis du sie hier gezielt entfernst. Diese Löschung hat keinen automatischen Rollback.", "Former targets keep this rule until you remove them here selectively. This removal has no automatic rollback."],
  ["Frühere Ziele behalten ihre Instanz, bis du sie hier gezielt entfernst.", "Former targets keep their instance until you remove them here selectively."],
  ["Frühere Ziele behalten ihren Tunnel, bis du sie hier gezielt entfernst.", "Former targets keep their tunnel until you remove them here selectively."],
  ["Für mindestens einen Ziel-Host fehlt eine Zertifikat-/CA-Auswahl. Dieser Host wird beim Push mit einem Fehler übersprungen.", "A certificate/CA selection is missing for at least one target host. That host is skipped with an error during the push."],
  ["Kein Zertifikat/CA für diesen Host ausgewählt.", "No certificate/CA selected for this host."],
  ["TLS-Hardening (tls-auth/tls-crypt) ist aktiviert, aber es wurde noch kein Schlüsselmaterial hinterlegt.", "TLS hardening (tls-auth/tls-crypt) is enabled, but no key material has been stored yet."],
  ["Für diesen Tunnel ist noch kein Server-Privatschlüssel hinterlegt.", "No server private key has been stored for this tunnel yet."],
  ["Peer-Pre-Shared-Key ist aktiviert, aber es wurde noch kein Schlüsselmaterial hinterlegt.", "The peer pre-shared key is enabled, but no key material has been stored yet."],
  ["Für diesen Tunnel ist noch kein Pre-Shared-Key hinterlegt.", "No pre-shared key has been stored for this tunnel yet."],
  ["Kein automatischer Rollback: falls dieser Host nur über den VPN-Tunnel erreichbar ist, den du gerade änderst, kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen.", "No automatic rollback: if this host is only reachable through the VPN tunnel you are changing, OPNexus can NOT restore the connection automatically after an error."],
  ["Kein automatischer Rollback: falls dieser Host nur über den OpenVPN-Tunnel erreichbar ist, den du gerade änderst, kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen.", "No automatic rollback: if this host is only reachable through the OpenVPN tunnel you are changing, OPNexus can NOT restore the connection automatically after an error."],
  ["Kein automatischer Rollback: falls dieser Host nur über den WireGuard-Tunnel erreichbar ist, den du gerade änderst, kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen.", "No automatic rollback: if this host is only reachable through the WireGuard tunnel you are changing, OPNexus can NOT restore the connection automatically after an error."],
  ["Server-Privatschlüssel", "Server private key"],

  // --- revision notes ------------------------------------------------------
  ["Regel angelegt", "Rule created"],
  ["Alias angelegt", "Alias created"],
  ["Tunnel angelegt", "Tunnel created"],
  ["Instanz angelegt", "Instance created"],
  ["Vor erster Bearbeitung", "Before first edit"],
  ["Entwurf gespeichert", "Draft saved"],
  ["Reihenfolge per Drag-and-drop geändert", "Order changed by drag and drop"],
  ["Reihenfolge geändert", "Order changed"],

  // --- deployment results --------------------------------------------------
  ["Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen", "Configuration was written, but the service reload was not confirmed — check reachability manually"],
  [/^(?<kind>Alias|Regel|Outbound NAT|1:1 NAT|Port Forward) wurde übernommen, Hierarchie-Reihenfolge aber nicht bestätigt: (?<detail>[\s\S]*)$/, "{kind} was applied, but the hierarchy ordering was not confirmed: {detail}"],
  [/^Regel wurde übernommen, Positionierung \('quick'\) aber nicht bestätigt: (?<detail>[\s\S]*)$/, "Rule was applied, but its positioning ('quick') was not confirmed: {detail}"],
  [/^Firewall hat keine (?<what>[\w-]+)-UUID zurückgegeben; Stand manuell prüfen$/, "The firewall returned no {what} UUID; check the state manually"],
  [/^(?<what>[\s\S]+) konnte nicht von allen Hosts entfernt werden: (?<detail>[\s\S]*)$/, "{what} could not be removed from all hosts: {detail}"],
  ["Verknüpfter ACME-Account wurde noch nicht auf die Firewall geschrieben", "The linked ACME account has not been written to the firewall yet"],
  ["Account wurde noch nicht auf die Firewall geschrieben", "The account has not been written to the firewall yet"],
  ["Zertifikatsanfrage wurde noch nicht auf die Firewall geschrieben", "The certificate request has not been written to the firewall yet"],

  // --- OPNsense client -----------------------------------------------------
  [/^(?<op>[\s\S]+) von Firewall nicht bestätigt: (?<detail>[\s\S]*)$/, "{op}: not confirmed by the firewall — {detail}"],
  [/^(?<op>[\s\S]+): Keine auswertbare Bestätigung der Firewall$/, "{op}: no evaluable confirmation from the firewall"],
  [/^(?<op>[\s\S]+): keine auswertbare Antwort der Firewall$/, "{op}: no evaluable response from the firewall"],
  [/^(?<op>[\s\S]+): von der Firewall abgelehnt \(Objekt nicht gefunden oder keine POST-Anfrage\)$/, "{op}: rejected by the firewall (object not found or not a POST request)"],
  [/^(?<op>[\s\S]+): unerwartete Antwortform der Firewall: (?<detail>[\s\S]*)$/, "{op}: unexpected response shape from the firewall: {detail}"],
  [/^Verbindung zu (?<host>[\s\S]+?) fehlgeschlagen: (?<detail>[\s\S]*)$/, "Connection to {host} failed: {detail}"],
  [/^Kein API-Secret für Host '(?<host>[\s\S]+)' hinterlegt$/, "No API secret stored for host '{host}'"],
  [/^Ungültige API-Antwort von (?<host>[\s\S]+?) bei (?<path>\S+)$/, "Invalid API response from {host} at {path}"],
  ["Ungültige NAT-Suchantwort", "Invalid NAT search response"],
  [/^Outbound-Modus auf (?<host>[\s\S]+?) nicht lesbar: (?<detail>[\s\S]*)$/, "Outbound mode on {host} not readable: {detail}"],
  ...operationEntries,
  [/^(?<noun>ACME-Account) registrieren$/, "Register ACME account"],
  ["ACME-Zertifikat ausstellen", "Issue ACME certificate"],
  ["Änderungen anwenden", "Apply changes"],
  ["Aliase anwenden", "Apply aliases"],
  ["Regel verschieben", "Move rule"],
  ["CARP-Wartungsmodus umschalten", "Toggle CARP maintenance mode"],

  // --- maintenance ---------------------------------------------------------
  ["An Firewall übergeben. Abschluss und Erreichbarkeit prüfen; anschließend Wartung abschließen.", "Submitted to the firewall. Check completion and reachability, then complete the maintenance."],
  ["Geplant übergeben. Abschluss und Erreichbarkeit prüfen; anschließend Wartung abschließen.", "Submitted as scheduled. Check completion and reachability, then complete the maintenance."],
  [/^Ausgang unklar: (?<detail>[\s\S]*)\. Nicht automatisch wiederholen\.$/, "Outcome unclear: {detail}. Do not retry automatically."],
  ["CARP-Übergabe angefordert. Prüfen, dass der Partner MASTER und dieser Host BACKUP ist.", "CARP handover requested. Check that the partner is MASTER and this host is BACKUP."],
  ["Normalen CARP-Betrieb angefordert. Rollen und Erreichbarkeit beider Hosts prüfen.", "Normal CARP operation requested. Check the roles and reachability of both hosts."],
  [/^CARP-Ausgang unklar: (?<detail>[\s\S]*)\. Nicht erneut klicken; Rollen zuerst prüfen\.$/, "CARP outcome unclear: {detail}. Do not click again; check the roles first."],
  ["CARP-Antwort passt nicht zur angeforderten Richtung", "CARP response does not match the requested direction"],
  ["CARP-Antwort passt nicht zur erwarteten Richtung (leave_maintenance)", "CARP response does not match the expected direction (leave_maintenance)"],
  ["Abschluss manuell geprüft und bestätigt.", "Completion manually verified and confirmed."],
  [/^Manuell abgebrochen, kein bestätigter Abschluss\. Grund: (?<reason>[\s\S]*)$/, "Manually cancelled, no confirmed completion. Reason: {reason}"],
  ["Unerwarteter Fehler beim Ausführen", "Unexpected error during execution"],
  ["Host nicht gefunden oder deaktiviert", "Host not found or disabled"],
  [/^Host nicht erreichbar: (?<detail>[\s\S]*)$/, "Host unreachable: {detail}"],
  ["Manuell vor Ablauf des Zeitfensters in den Normalbetrieb zurückgeschaltet.", "Manually switched back to normal operation before the window expired."],
  ["Host wurde gelöscht, bevor das Zeitfenster ablief.", "The host was deleted before the window expired."],
  ["Host war beim Ablauf bereits nicht mehr im CARP-Wartungsmodus.", "The host was no longer in CARP maintenance mode when the window expired."],
  ["Automatisch nach Ablauf des Zeitfensters in den Normalbetrieb zurückgeschaltet.", "Automatically switched back to normal operation after the window expired."],
  [/^Automatischer Rückschalt-Versuch fehlgeschlagen: (?<detail>[\s\S]*)$/, "Automatic switch-back attempt failed: {detail}"],
  [/^Automatischer Rückschalt-Zeitpunkt: (?<time>[\s\S]*)$/, "Automatic switch-back time: {time}"],
  [/^Dienst '(?<name>[\s\S]+)' (?<action>\S+) übergeben\.$/, "Service '{name}' {action} submitted."],
  ["CARP-Übergabe", "CARP handover"],

  // --- maintenance blockers ------------------------------------------------
  ["Offene Bestätigung oder fehlgeschlagener Rollback im Host/Cluster.", "Open confirmation or failed rollback in the host/cluster."],
  ["Eine frühere Wartungsaktion im Host/Cluster muss zuerst geprüft und abgeschlossen werden.", "An earlier maintenance action in the host/cluster must be checked and completed first."],
  ["Eine Wiederherstellung im Host/Cluster ist offen und muss zuerst bestätigt oder abgebrochen werden.", "A restore in the host/cluster is open and must be confirmed or cancelled first."],
  ["Firmware-Auftrag läuft oder Status ist unbekannt.", "A firmware job is running or the status is unknown."],
  ["Cluster-Wartung nur auf dem CARP-BACKUP. Rollenwechsel separat in OPNsense durchführen.", "Cluster maintenance only on the CARP BACKUP. Perform the role switch separately in OPNsense."],
  ["Kein erreichbarer aktiver Cluster-Partner bestätigt.", "No reachable active cluster partner confirmed."],
  ["Kein stabiles Update zur Installation gemeldet. Update-Suche ausführen.", "No stable update reported for installation. Run the update check."],
  ["Kein Major-Upgrade zur Installation gemeldet. Update-Suche ausführen.", "No major upgrade reported for installation. Run the update check."],
  ["Vor der Installation ein lokales Konfigurationsbackup erstellen (maximal 24 Stunden alt).", "Create a local configuration backup (at most 24 hours old) before installing."],
  ["CARP-Rollenwechsel benötigt einen Cluster mit mindestens zwei Hosts.", "A CARP role switch requires a cluster with at least two hosts."],
  ["Der ausgewählte Host ist nicht erreichbar.", "The selected host is not reachable."],
  ["CARP ist auf dem ausgewählten Host deaktiviert.", "CARP is disabled on the selected host."],
  ["Kein erreichbarer CARP-BACKUP als Übernahmeziel bestätigt.", "No reachable CARP BACKUP confirmed as takeover target."],
  ["Kein erreichbarer CARP-MASTER bestätigt, der den Verkehr aktuell trägt.", "No reachable CARP MASTER confirmed that currently carries the traffic."],
  ["Auf mindestens einem Cluster-Knoten läuft ein Firmware-Auftrag oder sein Zustand ist unbekannt.", "A firmware job is running on at least one cluster node or its state is unknown."],
  ["Nur der aktuelle CARP-MASTER kann auf den Partner umgeschaltet werden.", "Only the current CARP MASTER can be switched to the partner."],
  ["Host ist deaktiviert.", "Host is disabled."],
  ["Host ist nicht erreichbar.", "Host is not reachable."],
  [/^(?<name>[\s\S]+?) steht bereits im CARP-Wartungsmodus\. Zuerst dort „CARP-Normalbetrieb herstellen“ verwenden, nicht hier erneut umschalten\.$/, "{name} is already in CARP maintenance mode. Use “Restore CARP normal operation” there first instead of switching again here."],

  // --- restore -------------------------------------------------------------
  ["Zur manuellen Wiederherstellung übergeben. Sicherung jetzt im OPNsense-Webinterface importieren (System → Konfiguration → Historie/Wiederherstellen), Interface-/Konsolenzuordnung prüfen, danach hier bestätigen.", "Handed over for manual restore. Now import the backup in the OPNsense web interface (System → Configuration → History/Restore), check the interface/console assignment, then confirm here."],
  [/^(?<message>[\s\S]*) Hinweis: Zentral verwaltete Regeln\/Aliase\/NAT für diesen Host vor dem nächsten Push neu abgleichen\.$/, "{message} Note: re-check centrally managed rules/aliases/NAT for this host before the next push."],
  ["Wiederherstellung manuell geprüft und bestätigt.", "Restore manually verified and confirmed."],
  [/^Hostname in der Sicherung \(„(?<hostname>[\s\S]+?)“\) unterscheidet sich deutlich vom verwalteten Host „(?<host>[\s\S]+?)“\. Bitte prüfen, ob dies wirklich die richtige Sicherung für diesen Host ist\.$/, "The host name in the backup (“{hostname}”) differs significantly from the managed host “{host}”. Please check that this is really the right backup for this host."],
  ["Die Sicherung enthält eine HA-Synchronisationskonfiguration, der Host ist aber aktuell keinem Cluster zugeordnet.", "The backup contains an HA sync configuration, but the host is currently not assigned to a cluster."],
  ["Keine geheimen Felder (Passwörter/Schlüssel) in der Sicherung gefunden — bitte prüfen, ob die Datei vollständig ist.", "No secret fields (passwords/keys) found in the backup — please check that the file is complete."],
  [/^Frische Sicherheitssicherung vor dem Restore fehlgeschlagen: (?<detail>[\s\S]*)$/, "Fresh safety backup before the restore failed: {detail}"],

  // --- audit / auth ---------------------------------------------------------
  ["Anfrage nicht abgeschlossen; Zustand vor Wiederholung prüfen.", "Request not completed; check the state before retrying."],
  ["Rollback-Ausgang unklar; gespeicherten und tatsächlichen Stand prüfen.", "Rollback outcome unclear; check the stored and the actual state."],
  ["Nicht authentifiziert", "Not authenticated"],
  ["Nur-Lese-Zugriff: Diese Aktion erfordert die Rolle \"Admin\".", "Read-only access: this action requires the “Admin” role."],
  ["Dies ist eine Testbenachrichtigung von OPNexus.", "This is a test notification from OPNexus."],

  // --- validation (Pydantic) -----------------------------------------------
  [/^Value error, (?<message>[\s\S]+)$/, "{message}"],
  ["Ungültige Eingabe", "Invalid input"],
  ["Name darf nicht leer sein", "Name must not be empty"],
  ["Feld darf nicht nur Leerzeichen enthalten", "Field must not consist of whitespace only"],
  ["Port muss eine Zahl zwischen 1 und 65535 sein", "Port must be a number between 1 and 65535"],
  ["Port muss leer sein oder eine Zahl zwischen 1 und 65535", "Port must be empty or a number between 1 and 65535"],
  ["Nur alphanumerische Zeichen, Punkt, Bindestrich und Unterstrich, 1-64 Zeichen", "Only alphanumeric characters, dot, hyphen and underscore, 1–64 characters"],
  ["Endpoint-Port wird benötigt, wenn eine Endpoint-Adresse gesetzt ist", "An endpoint port is required when an endpoint address is set"],
  [/^Zertifikat-\/CA-Auswahl für Host (?<host>[\s\S]+?) darf nicht leer sein$/, "The certificate/CA selection for host {host} must not be empty"],
  [/^Kein Zertifikat\/CA für Host (?<host>[\s\S]+?) ausgewählt$/, "No certificate/CA selected for host {host}"],
  ["Server-Rolle benötigt ein Tunnelnetz (server_network)", "The server role requires a tunnel network (server_network)"],
  ["Client-Rolle benötigt ein Remote-Ziel (remote)", "The client role requires a remote target (remote)"],
  ["Für diese Aktion wird ein Common Name benötigt", "A common name is required for this action"],
  ["Für 'CSR signieren' wird ein CSR (PEM) benötigt", "A CSR (PEM) is required for “sign CSR”"],
  ["Für diese Aktion wird ein Zertifikat (PEM) benötigt", "A certificate (PEM) is required for this action"],
  ["Ein privater Schlüssel kann nur bei Aktion 'import' hinterlegt werden", "A private key can only be stored for the “import” action"],
  ["Für CA 'custom' wird eine Custom-CA-URL benötigt", "A custom CA URL is required for CA “custom”"],
];
