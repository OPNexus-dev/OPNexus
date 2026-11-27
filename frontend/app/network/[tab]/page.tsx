"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Column, DataTable } from "@/components/ui/DataTable";
import { MenuAction } from "@/components/ui/ActionMenu";
import { PageHeader } from "@/components/ui/PageHeader";
import { NetworkChangeModal } from "@/components/NetworkChangeModal";
import { NetworkCompare } from "@/components/NetworkCompare";
import { NetworkObjectSection } from "@/components/NetworkObjectSection";
import { Button } from "@/components/ui/Button";
import { api, Host, HostNetwork, NetworkAction, NetworkKind } from "@/lib/api";
import { useReadOnly } from "@/lib/useReadOnly";
import { AlertTriangle, Cable, Pencil, Plus, Trash2 } from "lucide-react";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

type Section = "interfaces" | "vlans" | "vips" | "gateways" | "routes" | "dns" | "dhcp" | "devices" | "compare";
// hostId/hostName are set when the dialog targets another node than the one on screen (transfer to a cluster partner).
type Change = { kind: NetworkKind; action: NetworkAction; uuid?: string; preset?: Record<string, string>; hostId?: string; hostName?: string };
type OpenChange = (change: Change) => void;

const HOST_KEY = "opnexus-network-host";

// Extra generic object lists shown below the main table of a page (and the whole "devices" page).
const EXTRA_SECTIONS: Partial<Record<Section, { kind: NetworkKind; titleKey: string }[]>> = {
  gateways: [{ kind: "gateway_group", titleKey: "network.kinds.gateway_group" }],
  dns: [{ kind: "dns_forward", titleKey: "network.kinds.dns_forward" }],
  dhcp: [{ kind: "dhcp_range", titleKey: "network.kinds.dhcp_range" }],
  devices: [
    { kind: "lagg", titleKey: "network.kinds.lagg" },
    { kind: "bridge", titleKey: "network.kinds.bridge" },
    { kind: "loopback", titleKey: "network.kinds.loopback" },
    { kind: "gre", titleKey: "network.kinds.gre" },
    { kind: "gif", titleKey: "network.kinds.gif" },
    { kind: "vxlan", titleKey: "network.kinds.vxlan" },
  ],
};

const SECTION_KIND: Partial<Record<Section, NetworkKind>> = { interfaces: "interface", vlans: "vlan", vips: "vip", gateways: "gateway", routes: "route", dns: "dns_host" };

function statusTone(status: string): BadgeTone {
  const s = status.toLowerCase();
  if (s === "up" || s === "online") return "success";
  if (s === "down" || s === "offline") return "danger";
  return "neutral";
}

export default function NetworkPage() {
  const { t, errorMessage } = useLanguage();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [hostId, setHostId] = useState<string>("");
  const [network, setNetwork] = useState<HostNetwork | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The sub-navigation in the header picks the section; the path is the single source of truth.
  const { tab } = useParams<{ tab: string }>();
  const section: Section = (["interfaces", "vlans", "vips", "gateways", "routes", "dns", "dhcp", "devices", "compare"].includes(tab) ? tab : "interfaces") as Section;
  const [change, setChange] = useState<Change | null>(null);
  const readOnly = useReadOnly();
  const [reloadKey, setReloadKey] = useState(0);
  const shownHost = useRef("");

  useEffect(() => {
    api
      .listHosts()
      .then((list) => {
        setHosts(list);
        // The chosen firewall survives switching between the sub-pages.
        let remembered = "";
        try { remembered = sessionStorage.getItem(HOST_KEY) ?? ""; } catch { /* storage may be blocked */ }
        setHostId(list.find((h) => h.id === remembered)?.id ?? list[0]?.id ?? "");
      })
      .catch((e) => setError(errorMessage(e, "network.loadFailed")));
  }, [errorMessage]);

  useEffect(() => {
    if (!hostId || section === "compare") return;
    let cancelled = false;
    setError(null);
    // Switching host clears the view; a reload after a change keeps it so an open dialog is not torn down.
    if (shownHost.current !== hostId) {
      setNetwork(null);
      shownHost.current = hostId;
    }
    api
      .getHostNetwork(hostId)
      .then((data) => !cancelled && setNetwork(data))
      .catch((e) => !cancelled && setError(errorMessage(e, "network.loadFailed")))
    return () => {
      cancelled = true;
    };
  }, [hostId, errorMessage, reloadKey, section]);

  if (section === "compare") {
    return (
      <div>
        <PageHeader title={t("nav.items.network-compare")} />
        <NetworkCompare />
      </div>
    );
  }

  const modalHostId = change?.hostId ?? hostId;
  const modalHost = hosts?.find((h) => h.id === modalHostId);
  const current = hosts?.find((h) => h.id === modalHostId);
  const peers = current?.cluster_name
    ? (hosts ?? []).filter((h) => h.cluster_name === current.cluster_name && h.id !== current.id).map((h) => ({ id: h.id, name: h.name }))
    : [];

  if (hosts && hosts.length === 0) {
    return (
      <div>
        <PageHeader title={t(`nav.items.network-${section}`)} />
        <EmptyState icon={Cable} title={t("drift.noHosts")} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title={t(`nav.items.network-${section}`)}
        actions={hosts ? (
          <label className="flex items-center gap-2 text-sm text-slate-600">
            {t("network.host")}
            <select
              value={hostId}
              onChange={(e) => {
                setHostId(e.target.value);
                try { sessionStorage.setItem(HOST_KEY, e.target.value); } catch { /* ignore */ }
              }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
            >
              {hosts.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </select>
          </label>
        ) : undefined}
      />

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}
      {!network && !error && <Skeleton className="h-64 w-full" />}

      {network && (
        <>
          <Notices network={network} />
          {SECTION_KIND[section] && (section !== "interfaces" || network.can_configure_interfaces) && (
            <fieldset disabled={readOnly} className="contents">
              <div className="mb-3 flex justify-end">
                <Button variant="outline" onClick={() => setChange({ kind: SECTION_KIND[section]!, action: "add" })}>
                  <Plus size={16} />
                  {t("network.rowAdd")}
                </Button>
              </div>
            </fieldset>
          )}
          <Card className="overflow-hidden">
            {/* No fieldset around the tables: their read-only controls must keep working; write actions are disabled one by one. */}
            {section === "interfaces" && <InterfacesTable network={network} onChange={setChange} readOnly={readOnly} />}
            {section === "vlans" && <VlansTable network={network} onChange={setChange} readOnly={readOnly} />}
            {section === "vips" && <VipsTable network={network} onChange={setChange} readOnly={readOnly} />}
            {section === "gateways" && <GatewaysTable network={network} onChange={setChange} readOnly={readOnly} />}
            {section === "routes" && <RoutesTable network={network} onChange={setChange} readOnly={readOnly} />}
            {section === "dns" && <DnsTable network={network} onChange={setChange} readOnly={readOnly} />}
            {section === "dhcp" && <DhcpSections network={network} onChange={setChange} readOnly={readOnly} />}
            {(EXTRA_SECTIONS[section] ?? []).map((extra) => (
              <NetworkObjectSection
                key={extra.kind}
                hostId={hostId}
                kind={extra.kind}
                title={t(extra.titleKey)}
                reloadKey={reloadKey}
                readOnly={readOnly}
                onChange={setChange}
              />
            ))}
          </Card>
        </>
      )}
      {change && (
        <NetworkChangeModal
          key={`${modalHostId}:${change.kind}:${change.action}:${change.uuid ?? ""}`}
          hostId={modalHostId}
          hostName={modalHost?.name ?? ""}
          peers={peers}
          onTransfer={(peer, fields) =>
            setChange({ kind: change.kind, action: "add", preset: fields, hostId: peer.id, hostName: peer.name })
          }
          kind={change.kind}
          action={change.action}
          uuid={change.uuid}
          preset={change.preset}
          onClose={() => setChange(null)}
          onDone={() => setReloadKey((k) => k + 1)}
        />
      )}
    </div>
  );
}

function Notices({ network }: { network: HostNetwork }) {
  const { t } = useLanguage();
  return (
    <div className="mb-4 space-y-2">
      {network.management_device ? (
        <p className="flex items-start gap-2 text-sm text-slate-600">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden />
          <span>{t("network.managementNote", { device: network.management_device })}</span>
        </p>
      ) : (
        <p className="text-sm text-slate-500">{t("network.managementUnknown")}</p>
      )}
      {network.unavailable.length > 0 && (
        <p className="text-sm text-amber-700">
          {t("network.unavailable", { sections: network.unavailable.map((s) => t(`network.sections.${s}`)).join(", ") })}
        </p>
      )}
      {!network.can_configure_interfaces && (
        <p className="text-sm text-slate-500">{t("network.cannotConfigure")}</p>
      )}
    </div>
  );
}

/** Edit (shown) and delete (in the menu) of one object; write actions are disabled for read-only accounts. */
function objectActions(kind: NetworkKind, onChange: OpenChange, readOnly: boolean, t: (key: string) => string) {
  return (uuid: string): MenuAction[] => [
    { key: "edit", label: t("network.rowEdit"), icon: Pencil, onSelect: () => onChange({ kind, action: "set", uuid }), disabled: readOnly },
    { key: "delete", label: t("network.rowDelete"), icon: Trash2, tone: "danger", onSelect: () => onChange({ kind, action: "del", uuid }), disabled: readOnly },
  ];
}

function Empty({ text }: { text: string }) {
  return <p className="px-5 py-6 text-sm text-slate-500">{text}</p>;
}

type TableProps = { network: HostNetwork; onChange: OpenChange; readOnly: boolean };
const dash = "–";

function InterfacesTable({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  type Row = HostNetwork["interfaces"][number];
  if (network.interfaces.length === 0) return <Empty text={t("network.empty")} />;
  const edit = objectActions("interface", onChange, readOnly, t);
  const columns: Column<Row>[] = [
    {
      key: "device", header: t("network.col.device"), priority: "primary", isTitle: true, mono: true,
      cell: (i) => (<>{i.device}{i.is_management && <Badge tone="warning" className="ml-2">{t("network.management")}</Badge>}</>),
    },
    { key: "status", header: t("network.col.status"), priority: "primary", cell: (i) => <Badge tone={statusTone(i.status)}>{i.status}</Badge> },
    { key: "name", header: t("network.col.name"), priority: "high", cell: (i) => (i.identifier ? `${i.description || i.identifier} (${i.identifier})` : <span className="text-slate-400">{t("network.unassigned")}</span>) },
    {
      key: "addresses", header: t("network.col.addresses"), priority: "medium", mono: true,
      cell: (i) => i.addresses.length === 0 ? dash : i.addresses.map((a) => (
        <div key={a.address + (a.vhid ?? "")}>
          {a.address}
          {a.vhid && <span className="ml-2 text-slate-500">CARP vhid {a.vhid}{a.carp_status ? ` · ${a.carp_status}` : ""}</span>}
        </div>
      )),
    },
    { key: "type", header: t("network.col.type"), priority: "medium", cell: (i) => [i.ipv4_type, i.ipv6_type].filter(Boolean).join(" / ") || dash },
    { key: "vlan", header: t("network.col.vlan"), priority: "low", cell: (i) => (i.vlan_tag ? `${i.vlan_tag} (${i.vlan_parent ?? "?"})` : dash) },
  ];
  return (
    <DataTable
      bare label={t("nav.items.network-interfaces")} rows={network.interfaces} rowKey={(i) => i.device} columns={columns}
      actions={network.can_configure_interfaces ? (i) => (i.assigned && i.identifier ? edit(i.identifier) : []) : undefined}
    />
  );
}

function VlansTable({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  type Row = HostNetwork["vlans"][number];
  if (network.vlans.length === 0) return <Empty text={t("network.empty")} />;
  const edit = objectActions("vlan", onChange, readOnly, t);
  const columns: Column<Row>[] = [
    { key: "device", header: t("network.col.device"), priority: "primary", isTitle: true, mono: true, cell: (v) => v.device ?? dash },
    { key: "tag", header: t("network.col.tag"), priority: "primary", tabular: true, cell: (v) => v.tag },
    { key: "parent", header: t("network.col.parent"), priority: "high", mono: true, cell: (v) => v.parent },
    { key: "pcp", header: t("network.col.pcp"), priority: "medium", cell: (v) => v.pcp ?? dash },
    { key: "description", header: t("common.description"), priority: "medium", cell: (v) => v.description || dash },
  ];
  return <DataTable bare label={t("nav.items.network-vlans")} rows={network.vlans} rowKey={(v) => v.uuid} columns={columns} actions={(v) => edit(v.uuid)} />;
}

function VipsTable({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  type Row = HostNetwork["vips"][number];
  if (network.vips.length === 0) return <Empty text={t("network.empty")} />;
  const edit = objectActions("vip", onChange, readOnly, t);
  const columns: Column<Row>[] = [
    { key: "address", header: t("network.col.address"), priority: "primary", isTitle: true, mono: true, cell: (v) => v.address },
    { key: "mode", header: t("network.col.mode"), priority: "primary", cell: (v) => v.mode.toUpperCase() },
    { key: "interface", header: t("network.col.interface"), priority: "high", cell: (v) => v.interface_label },
    { key: "vhid", header: "VHID", priority: "medium", tabular: true, cell: (v) => v.vhid ?? dash },
    { key: "advskew", header: t("network.col.advskew"), priority: "medium", tabular: true, cell: (v) => v.advskew ?? dash },
    { key: "description", header: t("common.description"), priority: "medium", cell: (v) => v.description || dash },
  ];
  return <DataTable bare label={t("nav.items.network-vips")} rows={network.vips} rowKey={(v) => v.uuid} columns={columns} actions={(v) => edit(v.uuid)} />;
}

function GatewaysTable({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  type Row = HostNetwork["gateways"][number];
  if (network.gateways.length === 0) return <Empty text={t("network.empty")} />;
  const edit = objectActions("gateway", onChange, readOnly, t);
  const columns: Column<Row>[] = [
    {
      key: "name", header: t("network.col.name"), priority: "primary", isTitle: true,
      cell: (g) => (
        <>
          {g.name}
          {g.is_default && <Badge tone="info" className="ml-2">{t("network.default")}</Badge>}
          {g.disabled && <Badge tone="neutral" className="ml-2">{t("common.statusDisabled")}</Badge>}
        </>
      ),
    },
    { key: "status", header: t("network.col.status"), priority: "primary", cell: (g) => <Badge tone={statusTone(g.status)}>{g.status || dash}</Badge> },
    { key: "interface", header: t("network.col.interface"), priority: "high", cell: (g) => g.interface_label },
    { key: "address", header: t("network.col.address"), priority: "high", mono: true, cell: (g) => g.address || dash },
    { key: "priority", header: t("network.col.priority"), priority: "medium", tabular: true, cell: (g) => g.priority ?? dash },
  ];
  return <DataTable bare label={t("nav.items.network-gateways")} rows={network.gateways} rowKey={(g) => g.uuid} columns={columns} actions={(g) => edit(g.uuid)} />;
}

function RoutesTable({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  type Row = HostNetwork["routes"][number];
  if (network.routes.length === 0) return <Empty text={t("network.empty")} />;
  const edit = objectActions("route", onChange, readOnly, t);
  const columns: Column<Row>[] = [
    { key: "network", header: t("network.col.network"), priority: "primary", isTitle: true, mono: true, cell: (r) => r.network },
    { key: "status", header: t("network.col.status"), priority: "primary", cell: (r) => <Badge tone={r.disabled ? "neutral" : "success"}>{r.disabled ? t("common.statusDisabled") : t("network.active")}</Badge> },
    { key: "gateway", header: t("network.col.gateway"), priority: "high", cell: (r) => r.gateway },
    { key: "description", header: t("common.description"), priority: "medium", cell: (r) => r.description || dash },
  ];
  return <DataTable bare label={t("nav.items.network-routes")} rows={network.routes} rowKey={(r) => r.uuid} columns={columns} actions={(r) => edit(r.uuid)} />;
}

function DnsTable({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  type Row = HostNetwork["dns_hosts"][number];
  if (network.dns_hosts.length === 0) return <Empty text={t("network.empty")} />;
  const edit = objectActions("dns_host", onChange, readOnly, t);
  const columns: Column<Row>[] = [
    {
      key: "hostname", header: t("network.field.hostname"), priority: "primary", isTitle: true,
      cell: (d) => (<>{d.hostname}{!d.enabled && <Badge tone="neutral" className="ml-2">{t("common.statusDisabled")}</Badge>}</>),
    },
    { key: "domain", header: t("network.field.domain"), priority: "high", cell: (d) => d.domain },
    { key: "rr", header: t("network.field.rr"), priority: "medium", cell: (d) => d.record_type },
    { key: "server", header: t("network.field.server"), priority: "high", mono: true, cell: (d) => d.server || dash },
    { key: "description", header: t("common.description"), priority: "medium", cell: (d) => d.description || dash },
  ];
  return <DataTable bare label={t("nav.items.network-dns")} rows={network.dns_hosts} rowKey={(d) => d.uuid} columns={columns} actions={(d) => edit(d.uuid)} />;
}

function SubHeader({ title, kind, onChange, readOnly }: { title: string; kind: NetworkKind; onChange: OpenChange; readOnly: boolean }) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
      <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
      <Button variant="outline" disabled={readOnly} onClick={() => onChange({ kind, action: "add" })}>
        <Plus size={16} />
        {t("network.rowAdd")}
      </Button>
    </div>
  );
}

function DhcpSections({ network, onChange, readOnly }: TableProps) {
  const { t } = useLanguage();
  const hostEdit = objectActions("dhcp_host", onChange, readOnly, t);
  const subnetEdit = objectActions("kea_subnet", onChange, readOnly, t);
  const reservationEdit = objectActions("kea_reservation", onChange, readOnly, t);
  type Host = HostNetwork["dhcp_hosts"][number];
  type Subnet = HostNetwork["kea_subnets"][number];
  type Reservation = HostNetwork["kea_reservations"][number];
  const hostColumns: Column<Host>[] = [
    { key: "host", header: t("network.field.host"), priority: "primary", isTitle: true, cell: (h) => `${h.host}${h.domain ? `.${h.domain}` : ""}` },
    { key: "ip", header: t("network.field.ip"), priority: "primary", mono: true, cell: (h) => h.ip || dash },
    { key: "hwaddr", header: t("network.field.hwaddr"), priority: "high", mono: true, cell: (h) => h.hwaddr || dash },
    { key: "description", header: t("common.description"), priority: "medium", cell: (h) => h.description || dash },
  ];
  const subnetColumns: Column<Subnet>[] = [
    { key: "subnet", header: t("network.field.subnet"), priority: "primary", isTitle: true, mono: true, cell: (s) => s.subnet },
    { key: "pools", header: t("network.field.pools"), priority: "high", mono: true, cell: (s) => s.pools || dash },
    { key: "routers", header: t("network.field.option_data.routers"), priority: "high", mono: true, cell: (s) => s.routers || dash },
    { key: "description", header: t("common.description"), priority: "medium", cell: (s) => s.description || dash },
  ];
  const reservationColumns: Column<Reservation>[] = [
    { key: "ip", header: t("network.field.ip_address"), priority: "primary", isTitle: true, mono: true, cell: (r) => r.ip_address },
    { key: "hw", header: t("network.field.hw_address"), priority: "high", mono: true, cell: (r) => r.hw_address || dash },
    { key: "hostname", header: t("network.field.hostname"), priority: "high", cell: (r) => r.hostname || dash },
    { key: "subnet", header: t("network.field.subnet"), priority: "medium", mono: true, cell: (r) => r.subnet },
  ];
  return (
    <div>
      <SubHeader title={t("network.dhcp.dnsmasq")} kind="dhcp_host" onChange={onChange} readOnly={readOnly} />
      {network.dhcp_hosts.length === 0 ? <Empty text={t("network.empty")} /> : <DataTable bare label={t("network.dhcp.dnsmasq")} rows={network.dhcp_hosts} rowKey={(h) => h.uuid} columns={hostColumns} actions={(h) => hostEdit(h.uuid)} />}
      <SubHeader title={t("network.dhcp.keaSubnets")} kind="kea_subnet" onChange={onChange} readOnly={readOnly} />
      {network.kea_subnets.length === 0 ? <Empty text={t("network.empty")} /> : <DataTable bare label={t("network.dhcp.keaSubnets")} rows={network.kea_subnets} rowKey={(s) => s.uuid} columns={subnetColumns} actions={(s) => subnetEdit(s.uuid)} />}
      <SubHeader title={t("network.dhcp.keaReservations")} kind="kea_reservation" onChange={onChange} readOnly={readOnly} />
      {network.kea_reservations.length === 0 ? <Empty text={t("network.empty")} /> : <DataTable bare label={t("network.dhcp.keaReservations")} rows={network.kea_reservations} rowKey={(r) => r.uuid} columns={reservationColumns} actions={(r) => reservationEdit(r.uuid)} />}
    </div>
  );
}
