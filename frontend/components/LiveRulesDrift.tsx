"use client";

import { HistoryTarget, RuleHistoryModal } from "@/components/RuleHistoryModal";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { api, Host, LiveRule } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { groupHostsByCluster } from "@/lib/clusters";
import clsx from "clsx";
import { Check, History, Network, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

// Rules have no stable cross-host id (each OPNsense instance mints its own
// UUID independently) — matched by a best-effort content fingerprint
// instead. Good enough to spot "this rule exists on A but not B"; a rule
// edited differently on two hosts in a way that doesn't touch any of
// these fields won't show up as drift.
//
// Mirrored in backend/app/services/rule_drift_service.py for the
// notification checker — keep both fingerprint() implementations in exact
// sync, field-for-field, or the UI and notifications will disagree about
// what counts as drift.
function fingerprint(rule: LiveRule): string {
  return [
    rule.description,
    rule.interface ?? "",
    rule.direction,
    rule.protocol ?? "",
    rule.source_net,
    rule.source_port ?? "",
    rule.destination_net,
    rule.destination_port ?? "",
    rule.action,
  ].join("|");
}

interface DriftRow {
  fingerprint: string;
  sample: LiveRule;
  presentOn: Set<string>;
  // The rule's id on each firewall (they differ between firewalls); the change history is kept per firewall and id.
  uuids: Map<string, string>;
}

interface ClusterState {
  hosts: Host[];
  loading: boolean;
  allRows: DriftRow[];
  driftedRows: DriftRow[];
}

async function computeClusterDrift(clusterHosts: Host[]): Promise<Omit<ClusterState, "hosts" | "loading">> {
  const perHostRules = await Promise.all(
    clusterHosts.map(async (host) => {
      try {
        return { host, rules: await api.getLiveRules(host.id) };
      } catch {
        return { host, rules: null as LiveRule[] | null };
      }
    })
  );

  const reachable = perHostRules.filter((r): r is { host: Host; rules: LiveRule[] } => r.rules !== null);
  const byFingerprint = new Map<string, { sample: LiveRule; presentOn: Set<string>; uuids: Map<string, string> }>();
  for (const { host, rules } of reachable) {
    for (const rule of rules) {
      const fp = fingerprint(rule);
      const entry = byFingerprint.get(fp) ?? { sample: rule, presentOn: new Set<string>(), uuids: new Map<string, string>() };
      entry.presentOn.add(host.id);
      if (!entry.uuids.has(host.id)) entry.uuids.set(host.id, rule.uuid);
      byFingerprint.set(fp, entry);
    }
  }

  const allRows = Array.from(byFingerprint.entries())
    .map(([fp, entry]) => ({ fingerprint: fp, sample: entry.sample, presentOn: entry.presentOn, uuids: entry.uuids }))
    .sort((a, b) => a.sample.description.localeCompare(b.sample.description));
  const driftedRows = allRows.filter((row) => row.presentOn.size < reachable.length);

  return { allRows, driftedRows };
}

export function LiveRulesDrift() {
  const { t, errorMessage } = useLanguage();
  const [clusterStates, setClusterStates] = useState<Record<string, ClusterState> | null>(null);
  const [ungrouped, setUngrouped] = useState<Host[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [onlyDrift, setOnlyDrift] = useState(false);
  const [historyOf, setHistoryOf] = useState<{ title: string; targets: HistoryTarget[] } | null>(null);

  useEffect(() => {
    api
      .listHosts()
      .then((hosts) => {
        const { clusters, ungrouped: solo } = groupHostsByCluster(hosts);
        setUngrouped(solo);

        const initial: Record<string, ClusterState> = {};
        for (const [clusterName, clusterHosts] of Object.entries(clusters)) {
          initial[clusterName] = { hosts: clusterHosts, loading: true, allRows: [], driftedRows: [] };
        }
        setClusterStates(initial);

        // Each cluster's diff is computed independently — a slow or
        // unreachable host in one cluster never blocks the others from
        // showing up.
        for (const [clusterName, clusterHosts] of Object.entries(clusters)) {
          computeClusterDrift(clusterHosts).then((result) => {
            setClusterStates((prev) => ({
              ...prev,
              [clusterName]: { hosts: clusterHosts, loading: false, ...result },
            }));
          });
        }
      })
      .catch((e) => setError(errorMessage(e, "drift.loadRulesFailed")));
  }, [errorMessage]);

  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (!clusterStates) return <LiveRulesSkeleton />;

  const clusterEntries = Object.entries(clusterStates);

  return (
    <div className="space-y-8">
      {clusterEntries.length === 0 && ungrouped.length === 0 && (
        <EmptyState icon={Network} title={t("drift.noHosts")} />
      )}

      {clusterEntries.length > 0 && (
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input type="checkbox" checked={onlyDrift} onChange={(e) => setOnlyDrift(e.target.checked)} />
          {t("drift.onlyDiffs")}
        </label>
      )}

      {clusterEntries.map(([clusterName, cluster]) => {
        const rows = onlyDrift ? cluster.driftedRows : cluster.allRows;
        const driftedFingerprints = new Set(cluster.driftedRows.map((r) => r.fingerprint));
        return (
        <div key={clusterName}>
          <div className="mb-1 flex items-baseline gap-2">
            <h3 className="text-lg font-semibold text-slate-900">{clusterName}</h3>
            {cluster.loading ? (
              <span className="flex items-center gap-1.5 text-sm text-slate-400">
                <Spinner size={13} />
                {t("drift.comparingRules", { count: cluster.hosts.length })}
              </span>
            ) : (
              <span className="text-sm text-slate-500">
                {t("drift.rulesTotal", { count: cluster.allRows.length })}
                {cluster.driftedRows.length > 0
                  ? t("drift.someDiffer", { count: cluster.driftedRows.length })
                  : t("drift.allIdentical")}
              </span>
            )}
          </div>

          {cluster.loading ? (
            <Card className="p-5">
              <Skeleton className="mb-2 h-3 w-full" />
              <Skeleton className="mb-2 h-3 w-5/6" />
              <Skeleton className="h-3 w-3/4" />
            </Card>
          ) : rows.length === 0 ? (
            <p className="mb-4 text-sm text-emerald-600">
              {onlyDrift ? t("drift.noDiffs") : t("drift.noRules")}
            </p>
          ) : (
            <Card className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium">{t("drift.colDescription")}</th>
                    <th className="px-5 py-3 font-medium">{t("drift.colInterface")}</th>
                    <th className="px-5 py-3 font-medium">{t("drift.colSourceDest")}</th>
                    <th className="px-5 py-3 font-medium">{t("drift.colAction")}</th>
                    {cluster.hosts.map((host) => (
                      <th key={host.id} className="px-5 py-3 font-medium">
                        {host.name}
                      </th>
                    ))}
                    <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr
                      key={row.fingerprint}
                      className={clsx(
                        "border-b border-slate-100 last:border-0",
                        driftedFingerprints.has(row.fingerprint) && "bg-flame-50"
                      )}
                    >
                      <td className="px-5 py-3 text-slate-900">
                        {row.sample.description}
                        {row.sample.is_automatic && <span className="ml-1.5 text-xs text-slate-400">{t("drift.automatic")}</span>}
                      </td>
                      <td className="px-5 py-3 text-slate-500">{row.sample.interface_label ?? "—"}</td>
                      <td className="px-5 py-3 text-slate-500">
                        {row.sample.source_net}
                        {row.sample.source_port ? `:${row.sample.source_port}` : ""} → {row.sample.destination_net}
                        {row.sample.destination_port ? `:${row.sample.destination_port}` : ""}
                      </td>
                      <td className="px-5 py-3 text-slate-500">{row.sample.action_label}</td>
                      {cluster.hosts.map((host) => (
                        <td key={host.id} className="px-5 py-3">
                          {row.presentOn.has(host.id) ? (
                            <Check size={16} className="text-emerald-500" />
                          ) : (
                            <X size={16} className="text-red-400" />
                          )}
                        </td>
                      ))}
                      <td className="px-5 py-3 text-right">
                        {!row.sample.is_automatic && (
                          <button
                            type="button"
                            onClick={() => setHistoryOf({
                              title: row.sample.description,
                              targets: cluster.hosts.filter((h) => row.uuids.has(h.id)).map((h) => ({ hostId: h.id, hostName: h.name, uuid: row.uuids.get(h.id) as string })),
                            })}
                            className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
                            aria-label={t("ruleHistory.open", { rule: row.sample.description })}
                          >
                            <History size={14} aria-hidden="true" />
                            {t("ruleHistory.action")}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </div>
        );
      })}

      {historyOf && <RuleHistoryModal title={historyOf.title} targets={historyOf.targets} onClose={() => setHistoryOf(null)} />}

      {ungrouped.length > 0 && (
        <div>
          <h3 className="mb-2 text-lg font-semibold text-slate-900">{t("drift.noCluster")}</h3>
          <p className="mb-3 text-sm text-slate-500">
            {t("drift.noClusterHint")}
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

function LiveRulesSkeleton() {
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
