"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Table, TBody, TD, TH, THead, TRow } from "@/components/ui/Table";
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
        <h1 className="mb-6 text-2xl font-semibold text-slate-900">{t("nav.items.network-compare")}</h1>
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
        <h1 className="mb-6 text-2xl font-semibold text-slate-900">{t(`nav.items.network-${section}`)}</h1>
        <EmptyState icon={Cable} title={t("drift.noHosts")} />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900">{t(`nav.items.network-${section}`)}</h1>
        {hosts && (
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
        )}
      </div>

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
          <Card className="overflow-x-auto">
            <fieldset disabled={readOnly} className="contents">
              {section === "interfaces" && <InterfacesTable network={network} onChange={setChange} />}
              {section === "vlans" && <VlansTable network={network} onChange={setChange} />}
              {section === "vips" && <VipsTable network={network} onChange={setChange} />}
              {section === "gateways" && <GatewaysTable network={network} onChange={setChange} />}
              {section === "routes" && <RoutesTable network={network} onChange={setChange} />}
              {section === "dns" && <DnsTable network={network} onChange={setChange} />}
              {section === "dhcp" && <DhcpSections network={network} onChange={setChange} />}
            </fieldset>
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

function RowActions({ kind, uuid, onChange }: { kind: NetworkKind; uuid: string; onChange: OpenChange }) {
  const { t } = useLanguage();
  return (
    <div className="flex justify-end gap-1">
      <Button variant="ghost" aria-label={t("network.rowEdit")} onClick={() => onChange({ kind, action: "set", uuid })}>
        <Pencil size={14} />
      </Button>
      <Button variant="dangerSubtle" aria-label={t("network.rowDelete")} onClick={() => onChange({ kind, action: "del", uuid })}>
        <Trash2 size={14} />
      </Button>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="px-5 py-6 text-sm text-slate-500">{text}</p>;
}

function InterfacesTable({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  if (network.interfaces.length === 0) return <Empty text={t("network.empty")} />;
  return (
    <Table>
      <THead>
        <TRow>
          <TH>{t("network.col.device")}</TH>
          <TH>{t("network.col.name")}</TH>
          <TH>{t("network.col.status")}</TH>
          <TH>{t("network.col.addresses")}</TH>
          <TH>{t("network.col.type")}</TH>
          <TH>{t("network.col.vlan")}</TH>
          {network.can_configure_interfaces && <TH><span className="sr-only">{t("common.actions")}</span></TH>}
        </TRow>
      </THead>
      <TBody>
        {network.interfaces.map((i) => (
          <TRow key={i.device}>
            <TD className="font-mono">
              {i.device}
              {i.is_management && (
                <Badge tone="warning" className="ml-2">
                  {t("network.management")}
                </Badge>
              )}
            </TD>
            <TD>
              {i.identifier ? `${i.description || i.identifier} (${i.identifier})` : <span className="text-slate-400">{t("network.unassigned")}</span>}
            </TD>
            <TD>
              <Badge tone={statusTone(i.status)}>{i.status}</Badge>
            </TD>
            <TD className="font-mono text-xs">
              {i.addresses.length === 0 ? "–" : i.addresses.map((a) => (
                <div key={a.address + (a.vhid ?? "")}>
                  {a.address}
                  {a.vhid && (
                    <span className="ml-2 text-slate-500">
                      CARP vhid {a.vhid}{a.carp_status ? ` · ${a.carp_status}` : ""}
                    </span>
                  )}
                </div>
              ))}
            </TD>
            <TD>{[i.ipv4_type, i.ipv6_type].filter(Boolean).join(" / ") || "–"}</TD>
            <TD>{i.vlan_tag ? `${i.vlan_tag} (${i.vlan_parent ?? "?"})` : "–"}</TD>
            {network.can_configure_interfaces && (
              <TD>{i.assigned && i.identifier ? <RowActions kind="interface" uuid={i.identifier} onChange={onChange} /> : null}</TD>
            )}
          </TRow>
        ))}
      </TBody>
    </Table>
  );
}

function VlansTable({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  if (network.vlans.length === 0) return <Empty text={t("network.empty")} />;
  return (
    <Table>
      <THead>
        <TRow>
          <TH>{t("network.col.device")}</TH>
          <TH>{t("network.col.parent")}</TH>
          <TH>{t("network.col.tag")}</TH>
          <TH>{t("network.col.pcp")}</TH>
          <TH>{t("common.description")}</TH>
          <TH><span className="sr-only">{t("common.actions")}</span></TH>
        </TRow>
      </THead>
      <TBody>
        {network.vlans.map((v) => (
          <TRow key={v.uuid}>
            <TD className="font-mono">{v.device ?? "–"}</TD>
            <TD className="font-mono">{v.parent}</TD>
            <TD>{v.tag}</TD>
            <TD>{v.pcp ?? "–"}</TD>
            <TD>{v.description || "–"}</TD>
            <TD><RowActions kind="vlan" uuid={v.uuid} onChange={onChange} /></TD>
          </TRow>
        ))}
      </TBody>
    </Table>
  );
}

function VipsTable({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  if (network.vips.length === 0) return <Empty text={t("network.empty")} />;
  return (
    <Table>
      <THead>
        <TRow>
          <TH>{t("network.col.address")}</TH>
          <TH>{t("network.col.interface")}</TH>
          <TH>{t("network.col.mode")}</TH>
          <TH>VHID</TH>
          <TH>{t("network.col.advskew")}</TH>
          <TH>{t("common.description")}</TH>
          <TH><span className="sr-only">{t("common.actions")}</span></TH>
        </TRow>
      </THead>
      <TBody>
        {network.vips.map((v) => (
          <TRow key={v.uuid}>
            <TD className="font-mono">{v.address}</TD>
            <TD>{v.interface_label}</TD>
            <TD>{v.mode.toUpperCase()}</TD>
            <TD>{v.vhid ?? "–"}</TD>
            <TD>{v.advskew ?? "–"}</TD>
            <TD>{v.description || "–"}</TD>
            <TD><RowActions kind="vip" uuid={v.uuid} onChange={onChange} /></TD>
          </TRow>
        ))}
      </TBody>
    </Table>
  );
}

function GatewaysTable({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  if (network.gateways.length === 0) return <Empty text={t("network.empty")} />;
  return (
    <Table>
      <THead>
        <TRow>
          <TH>{t("network.col.name")}</TH>
          <TH>{t("network.col.interface")}</TH>
          <TH>{t("network.col.address")}</TH>
          <TH>{t("network.col.priority")}</TH>
          <TH>{t("network.col.status")}</TH>
          <TH><span className="sr-only">{t("common.actions")}</span></TH>
        </TRow>
      </THead>
      <TBody>
        {network.gateways.map((g) => (
          <TRow key={g.uuid}>
            <TD>
              {g.name}
              {g.is_default && (
                <Badge tone="info" className="ml-2">
                  {t("network.default")}
                </Badge>
              )}
              {g.disabled && (
                <Badge tone="neutral" className="ml-2">
                  {t("common.statusDisabled")}
                </Badge>
              )}
            </TD>
            <TD>{g.interface_label}</TD>
            <TD className="font-mono">{g.address || "–"}</TD>
            <TD>{g.priority ?? "–"}</TD>
            <TD>
              <Badge tone={statusTone(g.status)}>{g.status || "–"}</Badge>
            </TD>
            <TD><RowActions kind="gateway" uuid={g.uuid} onChange={onChange} /></TD>
          </TRow>
        ))}
      </TBody>
    </Table>
  );
}

function RoutesTable({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  if (network.routes.length === 0) return <Empty text={t("network.empty")} />;
  return (
    <Table>
      <THead>
        <TRow>
          <TH>{t("network.col.network")}</TH>
          <TH>{t("network.col.gateway")}</TH>
          <TH>{t("common.description")}</TH>
          <TH>{t("network.col.status")}</TH>
          <TH><span className="sr-only">{t("common.actions")}</span></TH>
        </TRow>
      </THead>
      <TBody>
        {network.routes.map((r) => (
          <TRow key={r.uuid}>
            <TD className="font-mono">{r.network}</TD>
            <TD>{r.gateway}</TD>
            <TD>{r.description || "–"}</TD>
            <TD>
              <Badge tone={r.disabled ? "neutral" : "success"}>{r.disabled ? t("common.statusDisabled") : t("network.active")}</Badge>
            </TD>
            <TD><RowActions kind="route" uuid={r.uuid} onChange={onChange} /></TD>
          </TRow>
        ))}
      </TBody>
    </Table>
  );
}

function DnsTable({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  if (network.dns_hosts.length === 0) return <Empty text={t("network.empty")} />;
  return (
    <Table>
      <THead>
        <TRow>
          <TH>{t("network.field.hostname")}</TH>
          <TH>{t("network.field.domain")}</TH>
          <TH>{t("network.field.rr")}</TH>
          <TH>{t("network.field.server")}</TH>
          <TH>{t("common.description")}</TH>
          <TH><span className="sr-only">{t("common.actions")}</span></TH>
        </TRow>
      </THead>
      <TBody>
        {network.dns_hosts.map((d) => (
          <TRow key={d.uuid}>
            <TD>
              {d.hostname}
              {!d.enabled && <Badge tone="neutral" className="ml-2">{t("common.statusDisabled")}</Badge>}
            </TD>
            <TD>{d.domain}</TD>
            <TD>{d.record_type}</TD>
            <TD className="font-mono">{d.server || "–"}</TD>
            <TD>{d.description || "–"}</TD>
            <TD><RowActions kind="dns_host" uuid={d.uuid} onChange={onChange} /></TD>
          </TRow>
        ))}
      </TBody>
    </Table>
  );
}

function SubHeader({ title, kind, onChange }: { title: string; kind: NetworkKind; onChange: OpenChange }) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
      <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
      <Button variant="outline" onClick={() => onChange({ kind, action: "add" })}>
        <Plus size={16} />
        {t("network.rowAdd")}
      </Button>
    </div>
  );
}

function DhcpSections({ network, onChange }: { network: HostNetwork; onChange: OpenChange }) {
  const { t } = useLanguage();
  return (
    <div>
      <SubHeader title={t("network.dhcp.dnsmasq")} kind="dhcp_host" onChange={onChange} />
      {network.dhcp_hosts.length === 0 ? <Empty text={t("network.empty")} /> : (
        <Table>
          <THead>
            <TRow>
              <TH>{t("network.field.host")}</TH>
              <TH>{t("network.field.ip")}</TH>
              <TH>{t("network.field.hwaddr")}</TH>
              <TH>{t("common.description")}</TH>
              <TH><span className="sr-only">{t("common.actions")}</span></TH>
            </TRow>
          </THead>
          <TBody>
            {network.dhcp_hosts.map((h) => (
              <TRow key={h.uuid}>
                <TD>{h.host}{h.domain ? `.${h.domain}` : ""}</TD>
                <TD className="font-mono">{h.ip || "–"}</TD>
                <TD className="font-mono text-xs">{h.hwaddr || "–"}</TD>
                <TD>{h.description || "–"}</TD>
                <TD><RowActions kind="dhcp_host" uuid={h.uuid} onChange={onChange} /></TD>
              </TRow>
            ))}
          </TBody>
        </Table>
      )}
      <SubHeader title={t("network.dhcp.keaSubnets")} kind="kea_subnet" onChange={onChange} />
      {network.kea_subnets.length === 0 ? <Empty text={t("network.empty")} /> : (
        <Table>
          <THead>
            <TRow>
              <TH>{t("network.field.subnet")}</TH>
              <TH>{t("network.field.pools")}</TH>
              <TH>{t("network.field.option_data.routers")}</TH>
              <TH>{t("common.description")}</TH>
              <TH><span className="sr-only">{t("common.actions")}</span></TH>
            </TRow>
          </THead>
          <TBody>
            {network.kea_subnets.map((s) => (
              <TRow key={s.uuid}>
                <TD className="font-mono">{s.subnet}</TD>
                <TD className="font-mono text-xs">{s.pools || "–"}</TD>
                <TD className="font-mono">{s.routers || "–"}</TD>
                <TD>{s.description || "–"}</TD>
                <TD><RowActions kind="kea_subnet" uuid={s.uuid} onChange={onChange} /></TD>
              </TRow>
            ))}
          </TBody>
        </Table>
      )}
      <SubHeader title={t("network.dhcp.keaReservations")} kind="kea_reservation" onChange={onChange} />
      {network.kea_reservations.length === 0 ? <Empty text={t("network.empty")} /> : (
        <Table>
          <THead>
            <TRow>
              <TH>{t("network.field.ip_address")}</TH>
              <TH>{t("network.field.hw_address")}</TH>
              <TH>{t("network.field.hostname")}</TH>
              <TH>{t("network.field.subnet")}</TH>
              <TH><span className="sr-only">{t("common.actions")}</span></TH>
            </TRow>
          </THead>
          <TBody>
            {network.kea_reservations.map((r) => (
              <TRow key={r.uuid}>
                <TD className="font-mono">{r.ip_address}</TD>
                <TD className="font-mono text-xs">{r.hw_address || "–"}</TD>
                <TD>{r.hostname || "–"}</TD>
                <TD className="font-mono">{r.subnet}</TD>
                <TD><RowActions kind="kea_reservation" uuid={r.uuid} onChange={onChange} /></TD>
              </TRow>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}
