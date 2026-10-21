"use client";

import { Logo } from "@/components/Logo";
import { api } from "@/lib/api";
import clsx from "clsx";
import {
  Cable,
  FolderTree,
  Globe,
  GitBranch,
  HardDrive,
  Activity,
  Clock,
  Camera,
  Sliders,
  GitCompare,
  Boxes,
  KeyRound,
  LayoutDashboard,
  Link2,
  Layers,
  ListChecks,
  LogOut,
  Network,
  RefreshCw,
  Route,
  Router,
  ScanSearch,
  ScrollText,
  Search,
  Server,
  Settings,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Tags,
  Users,
  Waypoints,
  Waves,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { OPEN_COMMAND_PALETTE_EVENT } from "@/lib/events";
import { useLanguage } from "@/components/LanguageProvider";

export type NavItem = { href: string; label: string; icon: typeof Network; keywords?: string[] };
export type NavGroup = { key: string; label: string; icon: typeof Network; items: NavItem[] };

// keywords catch searches that use a different word than the page's own
// label/heading (e.g. "darkmode" for the theme radio group under
// Einstellungen → Darstellung, which never spells out that word itself) —
// each list is grounded in what the target page actually shows, not
// invented, so a hit always lands somewhere the term is genuinely relevant.
export const navGroups: NavGroup[] = [
  {
    key: "overview",
    label: "Übersicht",
    icon: Network,
    items: [
      { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, keywords: ["Startseite", "Home", "Offene Aktionen", "Sicherheitswarnungen", "Fleet-Status"] },
      { href: "/hosts", label: "Hosts", icon: Network, keywords: ["Firewalls", "OPNsense-Instanzen"] },
      { href: "/cluster", label: "Cluster", icon: Layers, keywords: ["CARP", "Hochverfügbarkeit", "Failover", "HA", "Master", "Backup"] },
    ],
  },
  {
    key: "policy",
    label: "Regelwerk",
    icon: Shield,
    items: [
      { href: "/rules", label: "Regeln", icon: Shield, keywords: ["Firewall-Regeln", "Filter"] },
      { href: "/analysis", label: "Regel-Analyse", icon: ScanSearch, keywords: ["Welche Regel", "Welche Regel trifft", "Pfad", "Simulation", "Firewall-Regel prüfen", "Paket-Test", "Rule match", "Which rule", "Rule analysis", "Packet path", "Trace"] },
      { href: "/policy-hierarchy", label: "Policy-Hierarchie", icon: GitBranch, keywords: ["Pre-Rules", "Post-Rules", "Vererbung", "Inheritance", "Hierarchische Policies"] },
      { href: "/groups", label: "Gruppen", icon: FolderTree, keywords: ["Gerätegruppen", "Device Groups"] },
      { href: "/aliases", label: "Aliase", icon: Tags, keywords: ["Netzwerk-Objekte", "Netzwerkgruppen"] },
      { href: "/nat", label: "NAT", icon: Network, keywords: ["Port Forward", "Portweiterleitung", "1:1 NAT", "Outbound NAT", "Masquerade"] },
    ],
  },
  {
    key: "network",
    label: "Netzwerk",
    icon: Cable,
    items: [
      { href: "/network/interfaces", label: "Schnittstellen", icon: Cable, keywords: ["Interfaces", "IP-Adressen", "WAN", "LAN", "Zuweisung"] },
      { href: "/network/vlans", label: "VLANs", icon: Layers, keywords: ["VLAN-Tag", "802.1q"] },
      { href: "/network/vips", label: "Virtuelle IPs", icon: Tags, keywords: ["CARP-VIP", "IP-Alias", "VHID"] },
      { href: "/network/gateways", label: "Gateways", icon: Router, keywords: ["Standard-Gateway", "Uplink"] },
      { href: "/network/routes", label: "Routen", icon: Route, keywords: ["Routing", "Statische Routen"] },
      { href: "/network/dns", label: "DNS", icon: Globe, keywords: ["Unbound", "Host-Override", "Hostnamen"] },
      { href: "/network/dhcp", label: "DHCP", icon: Server, keywords: ["Kea", "dnsmasq", "Reservierung", "Statische Zuweisung", "Subnetz", "Bereich", "Range"] },
      { href: "/network/devices", label: "Geräte", icon: Boxes, keywords: ["LAGG", "Link Aggregation", "Bridge", "Brücke", "Loopback", "GRE", "Tunnel"] },
      { href: "/network/compare", label: "Abgleich", icon: GitCompare, keywords: ["Vergleich", "Cluster", "HA", "Abweichung", "Drift", "Unterschiede"] },
    ],
  },
  {
    key: "hostsystem",
    label: "Host-System",
    icon: HardDrive,
    items: [
      { href: "/hostsystem/tunables", label: "Tunables", icon: Sliders, keywords: ["sysctl", "Kernel", "kTLS", "Systemparameter"] },
      { href: "/hostsystem/cron", label: "Cron", icon: Clock, keywords: ["Zeitplan", "Aufgaben", "Jobs", "geplante Aufgaben"] },
      { href: "/hostsystem/syslog", label: "Syslog", icon: ScrollText, keywords: ["Remote-Logging", "Log-Ziel", "Logserver"] },
      { href: "/hostsystem/snapshots", label: "Snapshots", icon: Camera, keywords: ["ZFS", "Boot-Umgebung", "Sicherung vor Änderung"] },
      { href: "/hostsystem/diagnostics", label: "Diagnose", icon: Activity, keywords: ["Ping", "Traceroute", "Port prüfen", "Portprobe", "DNS-Abfrage", "Erreichbarkeit", "Fehlersuche"] },
    ],
  },
  {
    key: "vpn",
    label: "VPN",
    icon: KeyRound,
    items: [
      { href: "/ipsec", label: "IPsec-VPN", icon: KeyRound, keywords: ["Site-to-Site", "PSK", "Pre-Shared Key", "Tunnel"] },
      { href: "/openvpn", label: "OpenVPN", icon: Cable, keywords: ["Server", "Client", "TLS-Auth", "TLS-Crypt"] },
      { href: "/wireguard", label: "WireGuard", icon: Waypoints, keywords: ["Peer", "Tunnel", "Endpoint"] },
    ],
  },
  {
    key: "certificates",
    label: "Zertifikate",
    icon: ShieldCheck,
    items: [{ href: "/certificates", label: "Zertifikate", icon: ShieldCheck, keywords: ["TLS", "SSL", "Let's Encrypt", "ACME", "Trust-Store", "CA"] }],
  },
  {
    key: "monitoring",
    label: "Monitoring",
    icon: ScrollText,
    items: [
      { href: "/logs", label: "Logs", icon: ScrollText, keywords: ["Firewall-Logs", "Geblockt", "Blocked", "Top Ports"] },
      { href: "/security", label: "Sicherheitslage", icon: ShieldAlert, keywords: ["Compliance", "Härtung", "Score", "Regel-Hygiene", "Any-Any", "SSH", "Sicherheit", "Prüfung"] },
      { href: "/audit", label: "Audit", icon: ScrollText, keywords: ["Deploy-Historie", "Protokoll", "Verlauf"] },
    ],
  },
  {
    key: "system",
    label: "System",
    icon: Settings,
    items: [
      {
        href: "/updates",
        label: "Update Center",
        icon: RefreshCw,
        keywords: ["Firmware", "OPNsense-Updates", "Pakete", "Wartung", "Neustart erforderlich", "Versionen"],
      },
      {
        href: "/rollouts",
        label: "Rollouts",
        icon: Waves,
        keywords: ["Update-Ringe", "Canary", "Wellen", "Rollout", "Wartungsfenster", "Firmware-Rollout", "Pilot", "Health-Gate", "Ringe"],
      },
      {
        href: "/settings",
        label: "Einstellungen",
        icon: Settings,
        keywords: [
          "Darstellung",
          "Dark Mode",
          "Darkmode",
          "Light Mode",
          "Theme",
          "Farbschema",
          "Hell",
          "Dunkel",
          "Passwort ändern",
          "Password",
          "Konto",
          "Benachrichtigungen",
          "Webhook",
          "E-Mail",
          "SMTP",
        ],
      },
      {
        href: "/users",
        label: "Benutzer",
        icon: Users,
        keywords: ["User", "Accounts", "Rollen", "Rechte", "Read-Only", "Nur-Lesen", "Admin-Rolle", "Passwort zurücksetzen"],
      },
      {
        href: "/overlay",
        label: "Management-Tunnel",
        icon: Link2,
        keywords: ["Overlay", "WireGuard", "NAT", "CGNAT", "Hub", "hinter NAT", "Fernzugriff", "Außenstelle", "Filiale", "ohne öffentliche IP"],
      },
      {
        href: "/tokens",
        label: "API-Tokens",
        icon: KeyRound,
        keywords: ["API", "Token", "Bearer", "Prometheus", "Metriken", "Automatisierung", "CI", "Skript", "MCP", "Schlüssel"],
      },
      {
        href: "/setup",
        label: "Einrichtung",
        icon: ListChecks,
        keywords: ["Assistent", "Wizard", "Setup", "Erste Schritte", "Onboarding", "Firewall verbinden", "API-Key", "Einführung"],
      },
    ],
  },
];

/** i18n key for a nav item: "/network/vlans" -> nav.items.network-vlans (a slash would read as a nested path). */
export function navLabelKey(href: string): string {
  return `nav.items.${href.slice(1).replace(/\//g, "-")}`;
}

/** True when pathname is the nav item's page or below it ("/hostsystem/x" must NOT match "/hosts"). */
export function matchesNav(pathname: string | null | undefined, href: string): boolean {
  return !!pathname && (pathname === href || pathname.startsWith(href + "/"));
}

export function Nav() {
  const { t } = useLanguage();
  const pathname = usePathname();
  const router = useRouter();
  const [readOnly, setReadOnly] = useState(false);

  const activeGroup = navGroups.find((g) => g.items.some((i) => matchesNav(pathname, i.href))) ?? null;

  useEffect(() => {
    api.me().then((state) => setReadOnly(state.role === "read_only")).catch(() => {});
  }, []);

  async function handleLogout() {
    await api.logout();
    router.push("/login");
  }

  return (
    <header className="border-b border-slate-200 bg-white print:hidden">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-3 gap-y-2 px-3 py-3 sm:px-6">
        <Link href="/about" title={t("nav.aboutTitle")} className="shrink-0">
          <Logo className="h-6 w-auto sm:h-7" />
        </Link>
        <nav aria-label={t("nav.mainNavigation")} className="order-3 flex w-full min-w-0 gap-0.5 overflow-x-auto sm:order-none sm:w-auto">
          {navGroups.map(({ key, icon: Icon, items }) => {
            const active = activeGroup?.key === key;
            return (
              <Link
                key={key}
                href={items[0].href}
                title={t(`nav.groups.${key}`)}
                aria-label={t(`nav.groups.${key}`)}
                aria-current={active ? "true" : undefined}
                className={clsx(
                  "flex shrink-0 items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm font-medium transition-colors",
                  active ? "bg-brand-50 text-brand-600" : "text-slate-600 hover:bg-slate-100"
                )}
              >
                <Icon size={16} aria-hidden="true" />
                <span className="hidden min-[1150px]:inline">{t(`nav.groups.${key}`)}</span>
              </Link>
            );
          })}
        </nav>
        {readOnly && (
          <span
            title={t("nav.readOnlyDescription")}
            className="ml-auto shrink-0 rounded-full bg-flame-50 px-2.5 py-1 text-xs font-medium text-flame-800"
          >
            {t("nav.readOnly")}
          </span>
        )}
        <button
          onClick={() => window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT))}
          title={t("nav.search")}
          aria-label={t("nav.search")}
          className={clsx(
            "flex shrink-0 items-center gap-2 rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 sm:px-3",
            !readOnly && "ml-auto"
          )}
        >
          <Search size={16} aria-hidden="true" />
          <span className="hidden text-xs text-slate-400 sm:inline">⌘K</span>
        </button>
        <button
          onClick={handleLogout}
          title={t("nav.logout")}
          aria-label={t("nav.logout")}
          className="flex shrink-0 items-center rounded-lg px-2.5 py-1.5 text-sm font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700"
        >
          <LogOut size={16} aria-hidden="true" />
        </button>
      </div>
      {activeGroup && activeGroup.items.length > 1 && (
        <div className="border-t border-slate-100 bg-slate-50">
          <nav aria-label={t("nav.subNavigation")} className="mx-auto flex max-w-6xl items-center gap-0.5 overflow-x-auto px-3 py-1.5 sm:px-6 sm:gap-1">
            {activeGroup.items.map(({ href, icon: Icon }) => {
              const active = matchesNav(pathname, href);
              return (
                <Link
                  key={href}
                  href={href}
                  title={t(navLabelKey(href))}
                  aria-label={t(navLabelKey(href))}
                  aria-current={active ? "page" : undefined}
                  className={clsx(
                    "flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-sm transition-colors",
                    active ? "bg-white font-medium text-brand-600 shadow-sm" : "text-slate-500 hover:bg-white hover:text-slate-700"
                  )}
                >
                  <Icon size={14} aria-hidden="true" />
                  <span className="hidden sm:inline">{t(navLabelKey(href))}</span>
                </Link>
              );
            })}
          </nav>
        </div>
      )}
    </header>
  );
}
