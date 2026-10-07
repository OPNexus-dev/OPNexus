"use client";

import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { api, AliasOption, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { groupHostsByCluster } from "@/lib/clusters";
import { Network, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

// Unlike rules, aliases have a natural cross-host identity: their name.
// So drift here means either "missing on some hosts" or "same name,
// different content" — both computed directly by name, no fingerprinting.
interface AliasRow {
  name: string;
  type: string;
  byHost: Map<string, AliasOption>;
  drifted: boolean;
}

interface ClusterState {
  hosts: Host[];
  loading: boolean;
  rows: AliasRow[];
  driftedCount: number;
}

async function computeClusterDrift(clusterHosts: Host[]): Promise<Omit<ClusterState, "hosts" | "loading">> {
  const perHostAliases = await Promise.all(
    clusterHosts.map(async (host) => {
      try {
        return { host, aliases: await api.getHostAliases(host.id) };
      } catch {
        return { host, aliases: null as AliasOption[] | null };
      }
    })
  );

  const reachable = perHostAliases.filter((r): r is { host: Host; aliases: AliasOption[] } => r.aliases !== null);
  const byName = new Map<string, Omit<AliasRow, "drifted">>();
  for (const { host, aliases } of reachable) {
    for (const alias of aliases) {
      const row = byName.get(alias.name) ?? { name: alias.name, type: alias.type, byHost: new Map<string, AliasOption>() };
      row.byHost.set(host.id, alias);
      byName.set(alias.name, row);
    }
  }

  const rows = Array.from(byName.values())
    .map((row) => {
      const contents = new Set(Array.from(row.byHost.values()).map((a) => a.content));
      const drifted = row.byHost.size < reachable.length || contents.size > 1;
      return { ...row, drifted };
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  return { rows, driftedCount: rows.filter((row) => row.drifted).length };
}

export function LiveAliasesDrift() {
  const { t, errorMessage } = useLanguage();
  const [clusterStates, setClusterStates] = useState<Record<string, ClusterState> | null>(null);
  const [ungrouped, setUngrouped] = useState<Host[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listHosts()
      .then((hosts) => {
        const { clusters, ungrouped: solo } = groupHostsByCluster(hosts);
        setUngrouped(solo);

        const initial: Record<string, ClusterState> = {};
        for (const [clusterName, clusterHosts] of Object.entries(clusters)) {
          initial[clusterName] = { hosts: clusterHosts, loading: true, rows: [], driftedCount: 0 };
        }
        setClusterStates(initial);

        for (const [clusterName, clusterHosts] of Object.entries(clusters)) {
          computeClusterDrift(clusterHosts).then((result) => {
            setClusterStates((prev) => ({
              ...prev,
              [clusterName]: { hosts: clusterHosts, loading: false, ...result },
            }));
          });
        }
      })
      .catch((e) => setError(errorMessage(e, "drift.loadAliasesFailed")));
  }, [errorMessage]);

  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (!clusterStates) return <LiveAliasesSkeleton />;

  const clusterEntries = Object.entries(clusterStates);

  return (
    <div className="space-y-8">
      {clusterEntries.length === 0 && ungrouped.length === 0 && (
        <EmptyState icon={Network} title={t("drift.noHosts")} />
      )}

      {clusterEntries.map(([clusterName, cluster]) => (
        <div key={clusterName}>
          <div className="mb-1 flex items-baseline gap-2">
            <h3 className="text-lg font-semibold text-slate-900">{clusterName}</h3>
            {cluster.loading ? (
              <span className="flex items-center gap-1.5 text-sm text-slate-400">
                <Spinner size={13} />
                {t("drift.comparingAliases", { count: cluster.hosts.length })}
              </span>
            ) : (
              <span className="text-sm text-slate-500">
                {t("drift.aliasesIdentical", { same: cluster.rows.length - cluster.driftedCount, total: cluster.rows.length })}
                {cluster.driftedCount > 0 && t("drift.someDiffer", { count: cluster.driftedCount })}
              </span>
            )}
          </div>

          {cluster.loading ? (
            <Card className="p-5">
              <Skeleton className="mb-2 h-3 w-full" />
              <Skeleton className="mb-2 h-3 w-5/6" />
              <Skeleton className="h-3 w-3/4" />
            </Card>
          ) : cluster.rows.length === 0 ? (
            <p className="mb-4 text-sm text-slate-500">{t("drift.noAliases")}</p>
          ) : (
            <Card className="overflow-x-auto">
              {cluster.driftedCount === 0 && (
                <p className="border-b border-slate-100 px-5 py-2 text-sm text-emerald-600">
                  {t("drift.noDiffs")}
                </p>
              )}
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium">{t("drift.colName")}</th>
                    <th className="px-5 py-3 font-medium">{t("drift.colType")}</th>
                    {cluster.hosts.map((host) => (
                      <th key={host.id} className="px-5 py-3 font-medium">
                        {host.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {cluster.rows.map((row) => (
                    <tr
                      key={row.name}
                      className={`border-b border-slate-100 last:border-0 ${row.drifted ? "bg-flame-50" : ""}`}
                    >
                      <td className="px-5 py-3 font-mono text-slate-900">{row.name}</td>
                      <td className="px-5 py-3 text-slate-500">{row.type}</td>
                      {cluster.hosts.map((host) => {
                        const alias = row.byHost.get(host.id);
                        return (
                          <td key={host.id} className="px-5 py-3">
                            {alias ? (
                              <span className="whitespace-pre-wrap font-mono text-xs text-slate-700">
                                {alias.content || t("drift.empty")}
                              </span>
                            ) : (
                              <X size={16} className="text-red-400" />
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </div>
      ))}

      {ungrouped.length > 0 && (
        <div>
          <h3 className="mb-2 text-lg font-semibold text-slate-900">{t("drift.noCluster")}</h3>
          <p className="mb-3 text-sm text-slate-500">
            {t("drift.noClusterAliasesHint")}
          </p>
          <div className="flex flex-wrap gap-2">
            {ungrouped.map((host) => (
              <Link
                key={host.id}
                href={`/hosts/${host.id}`}
                className="rounded-full bg-slate-100 px-3 py-1 text-sm text-sky-700 hover:bg-slate-200"
              >
                {host.name}
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function LiveAliasesSkeleton() {
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
