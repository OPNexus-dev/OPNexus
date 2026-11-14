# OPNsense-Rechte für den API-Benutzer von OPNexus

OPNexus arbeitet mit dem API-Schlüssel eines OPNsense-Benutzers. Der einfachste Weg ist ein Benutzer in der Gruppe `admin`
(oder mit dem Recht **All pages**). Wer das nicht möchte, kann dem Benutzer einzelne Rechte geben. Diese Seite sagt, welche.

> **Wichtig:** OPNsense-Rechte gelten für ganze Bereiche und sind **nicht „nur lesend“**. „Firewall: Rules [new]“ erlaubt
> auch das Anlegen und Löschen von Regeln, „Diagnostics: Configuration History“ auch das Herunterladen der kompletten
> Konfiguration. Ein reines Lese-Profil lässt sich in OPNsense deshalb nicht bauen. Gib einem API-Benutzer nur die Rechte
> der Funktionen, die du in OPNexus wirklich nutzt.

## So gibst du die Rechte

In OPNsense unter **System › Access › Users** den Benutzer öffnen, bei **Effective Privileges** die Rechte aus der Tabelle
auswählen und speichern. Der Name steht in der Spalte „Recht“, die interne ID in der Spalte daneben. Bei einem HA-Paar
prüfst du, dass der Benutzer mit denselben Rechten auf **beiden** Knoten vorhanden ist: OPNexus trägt jeden Knoten mit
einem eigenen Schlüssel ein.

Fehlt ein Recht, zeigt OPNexus das als Alarm **„API-Rechte fehlen“** (siehe Handbuch, Alarme) mit dem Namen des Rechts.

## Die Rechte je Funktion

Ein Recht in der Tabelle genügt für alle genannten Aktionen der Funktion. Steht dort „oder“, reicht eines der Rechte.

### Verbindung, Status und Überwachung (Firewall anlegen, Messwerte, Alarme)

| Wofür | Recht | ID |
| --- | --- | --- |
| Firewall anlegen und Erreichbarkeit, CARP-Status | Interfaces: Virtual IPs: Status | `page-status-carp` |
| Speicher, Last, Datenträger, Swap, CPU, Verbindungsliste | Lobby: Dashboard | `page-system-login-logout` |
| Schnittstellen-Zähler | Diagnostics: Netstat | `page-diagnostics-netstat` |
| Schnittstellen-Übersicht | Status: Interfaces | `page-status-interfaces` |
| Gateway-Zustand | System: Gateways **oder** System: Static Routes | `page-system-gateways` / `page-system-staticroutes` |
| Dienste anzeigen (und starten, stoppen, neu starten) | Status: Services | `page-status-services` |
| Firewall-Log einlesen | Diagnostics: Logs: Firewall: Live View | `page-diagnostics-logs-firewall-dynamic` |

Das sind die Rechte, mit denen die Überwachung und die Alarme laufen.

### Regeln, Aliase, NAT

| Wofür | Recht | ID |
| --- | --- | --- |
| Regeln lesen, anlegen, ändern, löschen, verschieben, anwenden | Firewall: Rules [new] | `page-filter-api` |
| Regel-Nutzung (Treffer-Verlauf, Regel-Analyse): Zähler je Regel | Firewall: Rules | `page-firewall-rules` |
| Regel-Nutzung: Regelwerk des Paketfilters | Diagnostics: Firewall statistics | `page-diagnostics-pf-info` |
| Aliase **lesen** | Firewall: Aliases **oder** Firewall: Alias: Edit | `page-firewall-aliases` / `page-firewall-alias-edit` |
| Aliase anlegen, ändern, löschen, anwenden | Firewall: Alias: Edit | `page-firewall-alias-edit` |
| Verwendung eines Alias suchen | Diagnostics: PF Table IP addresses | `page-diagnostics-tables` |
| Schnittstellen-Gruppen lesen | Firewall: Groups | `page-interfaces-groups-edit` |
| Port-Forwarding (Destination NAT) | Firewall: NAT: Destination NAT | `page-firewall-nat-portforward-edit` |
| 1:1-NAT | Firewall: NAT: 1:1 | `page-firewall-nat-1-1-edit` |
| Outbound-NAT (Source NAT) | Firewall: NAT: Source NAT | `page-filter-snat-api` |

Das getrennte Recht „Firewall: Rules“ (ohne „[new]“) deckt die Regel-Endpunkte **nicht** ab, nur die Regel-Zähler und die
Kategorien. Ohne die zwei Rechte zur Regel-Nutzung fehlen Treffer-Verlauf und Regel-Analyse, der Rest läuft.

### Netzwerk (Schnittstellen, Routing, DNS, DHCP)

| Wofür | Recht | ID |
| --- | --- | --- |
| Schnittstellen zuweisen | Interfaces: Assign network ports | `page-interfaces-assignnetworkports` |
| VLAN | Interfaces: VLAN | `page-interfaces-vlan-edit` |
| Virtuelle IPs (CARP, Alias) anlegen und ändern | Interfaces: Virtual IPs: Settings | `page-firewall-virtualipaddress-edit` |
| Bridge / LAGG / Loopback / GRE / GIF / VXLAN | Interfaces: Bridge / LAGG: Edit / Loopback / GRE / GIF / VXLAN | `page-interfaces-bridge-edit`, `page-interfaces-lagg-edit`, `page-interfaces-loopback`, `page-interfaces-gre-edit`, `page-interfaces-gif-edit`, `page-interfaces-vxlan` |
| Gateways | System: Gateways | `page-system-gateways` |
| Gateway-Gruppen | System: Gateway Groups | `page-system-gatewaygroups` |
| Statische Routen | System: Static Routes | `page-system-staticroutes` |
| Dnsmasq (DHCP-Hosts, Bereiche, DNS-Weiterleitung) | Services: Dnsmasq DNS/DHCP: Settings | `page-services-dnsforwarder` |
| Kea DHCPv4 | Services: DHCP: Kea(v4) | `page-dhcp-kea-v4` |
| Unbound: Host-Überschreibungen | Services: Unbound DNS: Edit Host and Domain Override **oder** Services: Unbound | `page-services-dnsresolver-overrides` / `page-services-unbound` |
| Unbound: Weiterleitungen (DNS over TLS) | Services: Unbound | `page-services-unbound` |

Für die Unbound-Weiterleitungen gibt es in OPNsense kein schmaleres Recht als „Services: Unbound“.

### System

| Wofür | Recht | ID |
| --- | --- | --- |
| Tunables | System: Advanced: Tunables | `page-system-advanced-sysctl` |
| Cron-Jobs | System: Settings: Cron | `page-system-cron` |
| Syslog-Ziele | System: Settings: Logging | `page-diagnostics-logs-settings-targets` |
| Konfigurations-Backups lesen und herunterladen | Diagnostics: Configuration History | `page-diagnostics-configurationhistory` |
| Firmware-Stand, Updates, Upgrades | System: Firmware | `page-system-firmware-manualupdate` |
| ZFS-Snapshots | System: Snapshots | `page-snapshots` |
| HA-Einstellungen lesen | System: High Availability | `page-system-hasync` |
| Neustart der Firewall | Diagnostics: Reboot System | `page-diagnostics-rebootsystem` |
| CARP-Wartungsmodus umschalten | Interfaces: Virtual IPs: Status | `page-status-carp` |

„System: Configuration: Backups“ (`page-diagnostics-backup-restore`) wird für die Backups **nicht** gebraucht: Es hat keine
API-Endpunkte.

### VPN und Zertifikate

| Wofür | Recht | ID |
| --- | --- | --- |
| IPsec: Verbindungen, Identitäten, Phase 2 | VPN: IPsec: Connections | `page-vpn-ipsec-connections` |
| IPsec: Pre-Shared Keys | VPN: IPsec: Edit Pre-Shared Keys | `page-vpn-ipsec-editkeys` |
| OpenVPN: Instanzen und Static Keys | VPN: OpenVPN: Instances | `page-openvpn-instances` |
| OpenVPN-Dienst neu laden | Status: OpenVPN | `page-status-openvpn` |
| WireGuard: Server und Peers | VPN: WireGuard: Configuration | `page-wireguard-config` |
| Zertifikate lesen, anlegen, löschen | System: Trust: Certificates | `page-system-certmanager` |
| Zertifizierungsstellen lesen | System: Trust: Authorities | `page-system-camanager` |

Das ACME-Plugin (`os-acme-client`) bringt eigene Rechte mit, die nicht im OPNsense-Kern stehen. Sie sind hier **nicht**
geprüft.

### Diagnose-Werkzeuge

| Wofür | Recht | ID |
| --- | --- | --- |
| Verbindungen (Zustände) ansehen | Diagnostics: Show States | `page-diagnostics-showstates` |
| Ping | Diagnostics: Ping | `page-diagnostics-ping` |
| Traceroute | Diagnostics: Traceroute | `page-diagnostics-traceroute` |
| Port-Test | Diagnostics: Test Port | `page-diagnostics-testport` |
| DNS-Abfrage | Interfaces: Diagnostics: DNS Lookup | `page-diagnostics-dns_diagnostics` |

## Wie das geprüft wurde

Am 14.11.2026 auf einer OPNsense **26.7**: je ein Test-Benutzer mit **genau einem** Recht, 57 Rechte, jeweils alle
OPNexus-Endpunkte aufgerufen (mit und ohne Query-Zeichenkette), insgesamt 16.815 Abfragen. In jedem Fall passte das
Ergebnis zu dem, was die OPNsense-Rechte-Definitionen (ACL.xml) vorhersagen. Zusätzlich liefen alle lesenden Funktionen von
OPNexus mit einem Benutzer, der nur die schmalen Rechte hatte; mit einem Benutzer ohne passende Rechte schlug jede
Abfrage mit „403“ fehl.

**Nur aus den ACL-Definitionen abgeleitet, nicht live aufgerufen:** alles, was etwas auslöst: Neustart, Firmware-Update,
Snapshot aktivieren, CARP-Wartungsmodus, Dienste starten/stoppen, „Anwenden“ und „Neu laden“ der Bereiche sowie
Ping/Traceroute/Port-Test/DNS-Abfrage. Schreibende Endpunkte wurden ohne Inhalt aufgerufen (OPNsense lehnt das ab, es
wird nichts angelegt); ob ein Anlegen mit gültigen Daten gelingt, hängt dann nur noch vom Recht ab.

## Ältere OPNsense-Versionen

OPNsense prüft die Rechte gegen die Adresse **genau so, wie sie geschrieben ist**, und die Schreibweise in den
Rechte-Definitionen hat sich geändert: Bis OPNsense 25.1 stehen vier Diagnose-Abfragen (`systemResources`, `systemTime`,
`systemDisk`, `getInterfaceStatistics`) in `camelCase`, ab 25.7 in `snake_case`. OPNexus fragt die neuere Schreibweise zuerst
und weicht bei „403“ oder „404“ auf die ältere aus (ab Version 1.37.8). Auf Firewalls bis 25.1 wurde das nur mit
Nachbildungen getestet, nicht mit einer echten Firewall.
