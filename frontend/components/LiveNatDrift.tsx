"use client";

import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { api, Host, NatLiveRow } from "@/lib/api";
import { groupHostsByCluster } from "@/lib/clusters";
import { useLanguage } from "@/components/LanguageProvider";
import { Network, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

// Mirrored in backend/app/services/nat_drift_service.py — keep these three
// fingerprint functions in exact sync, field-for-field, with their backend
// counterparts (field names verified against OPNsense core source, see
// that module's docstring: Port Forward uses "descr" and dotted
// "source.network"/"destination.network" keys, One-to-One/Outbound NAT use
// flat "description"/"source_net"/"destination_net").
type NatType = "port_forward" | "one_to_one" | "outbound_nat";

const NAT_TYPES: { key: NatType; label: string; fetch: (hostId: string) => Promise<NatLiveRow[]> }[] = [
  { key: "port_forward", label: "Port Forward", fetch: async (id) => (await api.getHostPortForwards(id)).rules },
  { key: "one_to_one", label: "1:1 NAT", fetch: async (id) => (await api.getHostOneToOne(id)).rules },
  { key: "outbound_nat", label: "Outbound NAT", fetch: async (id) => (await api.getHostOutboundNat(id)).rules },
];

function fingerprint(natType: NatType, row: NatLiveRow): string {
  const get = (key: string) => String(row[key] ?? "");
  if (natType === "port_forward") {
    return [
      get("descr"), get("interface"), get("protocol"), get("source.network"), get("source.port"),
      get("destination.network"), get("destination.port"), get("target"), get("local-port"),
    ].join("|");
  }
  if (natType === "one_to_one") {
    return [get("description"), get("interface"), get("type"), get("source_net"), get("destination_net"), get("external")].join("|");
  }
  return [
    get("description"), get("interface"), get("protocol"), get("source_net"), get("source_port"),
    get("destination_net"), get("destination_port"), get("target"), get("target_port"),
  ].join("|");
}

function describe(natType: NatType, row: NatLiveRow, fallback: string): string {
  return String(row[natType === "port_forward" ? "descr" : "description"] ?? fallback);
}

interface DriftRow {
  fingerprint: string;
  sample: NatLiveRow;
  presentOn: Set<string>;
}

interface TypeState {
  loading: boolean;
  total: number;
  driftedRows: DriftRow[];
}

interface ClusterState {
  hosts: Host[];
  byType: Record<NatType, TypeState>;
}

async function computeTypeDrift(natType: NatType, fetchFn: (id: string) => Promise<NatLiveRow[]>, clusterHosts: Host[]): Promise<TypeState> {
  const perHost = await Promise.all(
    clusterHosts.map(async (host) => {
      try { return { host, rows: await fetchFn(host.id) }; }
      catch { return { host, rows: null as NatLiveRow[] | null }; }
    })
  );
  const reachable = perHost.filter((r): r is { host: Host; rows: NatLiveRow[] } => r.rows !== null);
  const byFingerprint = new Map<string, { sample: NatLiveRow; presentOn: Set<string> }>();
  for (const { host, rows } of reachable) {
    for (const row of rows) {
      const fp = fingerprint(natType, row);
      const entry = byFingerprint.get(fp) ?? { sample: row, presentOn: new Set<string>() };
      entry.presentOn.add(host.id);
      byFingerprint.set(fp, entry);
    }
  }
  const all = Array.from(byFingerprint.entries()).map(([fp, entry]) => ({ fingerprint: fp, sample: entry.sample, presentOn: entry.presentOn }));
  return { loading: false, total: all.length, driftedRows: all.filter((row) => row.presentOn.size < reachable.length) };
}

export function LiveNatDrift() {
  const { t, errorMessage } = useLanguage();
  const [clusterStates, setClusterStates] = useState<Record<string, ClusterState> | null>(null);
  const [ungrouped, setUngrouped] = useState<Host[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listHosts()
      .then((hosts) => {
        const { clusters, ungrouped: solo } = groupHostsByCluster(hosts);
        setUngrouped(solo);

        const initial: Record<string, ClusterState> = {};
        for (const [clusterName, clusterHosts] of Object.entries(clusters)) {
          initial[clusterName] = {
            hosts: clusterHosts,
            byType: Object.fromEntries(NAT_TYPES.map((t) => [t.key, { loading: true, total: 0, driftedRows: [] }])) as unknown as Record<NatType, TypeState>,
          };
        }
        setClusterStates(initial);

        for (const [clusterName, clusterHosts] of Object.entries(clusters)) {
          for (const natType of NAT_TYPES) {
            computeTypeDrift(natType.key, natType.fetch, clusterHosts).then((result) => {
              setClusterStates((prev) => prev && {
                ...prev,
                [clusterName]: { ...prev[clusterName], byType: { ...prev[clusterName].byType, [natType.key]: result } },
              });
            });
          }
        }
      })
      .catch((e) => setError(errorMessage(e, "drift.loadNatFailed")));
  }, []);

  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (!clusterStates) return <LiveNatDriftSkeleton />;

  const clusterEntries = Object.entries(clusterStates);

  return (
    <div className="space-y-8">
      {clusterEntries.length === 0 && ungrouped.length === 0 && (
        <EmptyState icon={Network} title={t("drift.noHosts")} />
      )}

      {clusterEntries.map(([clusterName, cluster]) => (
        <div key={clusterName}>
          <h3 className="mb-3 text-lg font-semibold text-slate-900">{clusterName}</h3>
          {NAT_TYPES.map((natType) => {
            const state = cluster.byType[natType.key];
            return (
              <div key={natType.key} className="mb-5">
                <div className="mb-1 flex items-baseline gap-2">
                  <h4 className="font-medium text-slate-700">{natType.label}</h4>
                  {state.loading ? (
                    <span className="flex items-center gap-1.5 text-sm text-slate-400">
                      <Spinner size={13} /> {t("drift.comparing")}
                    </span>
                  ) : (
                    <span className="text-sm text-slate-500">
                      {t("drift.natIdentical", { same: state.total - state.driftedRows.length, total: state.total })}
                      {state.driftedRows.length > 0 && t("drift.someDiffer", { count: state.driftedRows.length })}
                    </span>
                  )}
                </div>
                {state.loading ? (
                  <Skeleton className="h-10 w-full" />
                ) : state.driftedRows.length === 0 ? (
                  <p className="text-sm text-emerald-600">{t("drift.noDiffs")}</p>
                ) : (
                  <Card className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                        <tr>
                          <th className="px-5 py-3 font-medium">{t("drift.colDescription")}</th>
                          {cluster.hosts.map((host) => (
                            <th key={host.id} className="px-5 py-3 font-medium">{host.name}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {state.driftedRows.map((row) => (
                          <tr key={row.fingerprint} className="border-b border-slate-100 last:border-0">
                            <td className="px-5 py-3 font-mono text-slate-900">{describe(natType.key, row.sample, t("liveNat.noDescription"))}</td>
                            {cluster.hosts.map((host) => (
                              <td key={host.id} className="px-5 py-3">
                                {row.presentOn.has(host.id) ? "✓" : <X size={16} className="text-red-400" />}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Card>
                )}
              </div>
            );
          })}
        </div>
      ))}

      {ungrouped.length > 0 && (
        <div>
          <h3 className="mb-3 flex items-center gap-2 text-lg font-semibold text-slate-900">{t("drift.noCluster")}<InfoTip text={t("drift.noClusterNatHint")} /></h3>
          <div className="flex flex-wrap gap-2">
            {ungrouped.map((host) => (
              <Link key={host.id} href={`/hosts/${host.id}`} className="rounded-full bg-slate-100 px-3 py-1 text-sm text-sky-700 hover:bg-slate-200">
                {host.name}
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function LiveNatDriftSkeleton() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-5 w-32" />
      <Card className="p-5">
        <Skeleton className="mb-2 h-3 w-full" />
        <Skeleton className="mb-2 h-3 w-5/6" />
        <Skeleton className="h-3 w-3/4" />
      </Card>
    </div>
  );
}
