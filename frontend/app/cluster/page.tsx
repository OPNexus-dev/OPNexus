"use client";

import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { StatusDot } from "@/components/ui/StatusDot";
import { api, CarpEvent, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { groupHostsByCluster } from "@/lib/clusters";
import { AlertTriangle, CheckCircle2, History, Network } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

interface RuleCountState {
  count: number | null;
  error: string | null;
  loading: boolean;
}

export default function ClusterPage() {
  const { t, errorMessage } = useLanguage();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [ruleCounts, setRuleCounts] = useState<Record<string, RuleCountState>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listHosts()
      .then((list) => {
        setHosts(list);
        // Rendered immediately with hosts above; each host's rule count
        // loads independently afterwards so one slow/unreachable host
        // never blocks the rest of the page from showing up.
        setRuleCounts(Object.fromEntries(list.map((h) => [h.id, { count: null, error: null, loading: true }])));
        for (const host of list) {
          api
            .getLiveRules(host.id)
            .then((rules) => {
              setRuleCounts((prev) => ({ ...prev, [host.id]: { count: rules.length, error: null, loading: false } }));
            })
            .catch((e) => {
              setRuleCounts((prev) => ({
                ...prev,
                [host.id]: { count: null, error: errorMessage(e, "cluster.error"), loading: false },
              }));
            });
        }
      })
      .catch((e) => setError(errorMessage(e, "cluster.loadFailed")));
  }, [errorMessage]);

  if (error) return <p className="text-sm text-red-600">{error}</p>;

  if (!hosts) {
    return (
      <div>
        <h1 className="mb-6 text-2xl font-semibold text-slate-900">{t("nav.items.cluster")}</h1>
        <ClusterSkeleton />
      </div>
    );
  }

  const { clusters, ungrouped } = groupHostsByCluster(hosts);

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold text-slate-900">{t("nav.items.cluster")}</h1>

      {Object.keys(clusters).length === 0 && ungrouped.length === 0 && (
        <EmptyState icon={Network} title={t("drift.noHosts")} />
      )}

      {Object.entries(clusters).map(([clusterName, clusterHosts]) => {
        const states = clusterHosts.map((h) => ruleCounts[h.id]);
        const stillLoading = states.some((s) => s?.loading);
        const counts = states.map((s) => s?.count).filter((c): c is number => c !== null && c !== undefined);
        const drift = !stillLoading && counts.length > 1 && new Set(counts).size > 1;
        const synced = !stillLoading && counts.length > 1 && new Set(counts).size === 1;
        return (
          <div key={clusterName} className="mb-8">
            <div className="mb-3 flex items-center gap-2">
              <h2 className="text-lg font-semibold text-slate-900">{clusterName}</h2>
              {stillLoading ? (
                <span className="flex items-center gap-1.5 rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-500">
                  <Spinner size={12} />
                  {t("cluster.comparing")}
                </span>
              ) : drift ? (
                <Link
                  href="/rules?tab=live"
                  className="flex items-center gap-1 rounded-full bg-flame-100 px-2.5 py-0.5 text-xs font-medium text-flame-700 hover:bg-flame-200"
                >
                  <AlertTriangle size={12} />
                  {t("cluster.countDiffers")}
                </Link>
              ) : synced ? (
                <span className="flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                  <CheckCircle2 size={12} />
                  {t("cluster.synced")}
                </span>
              ) : null}
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {clusterHosts.map((host) => (
                <HostCard key={host.id} host={host} ruleCount={ruleCounts[host.id]} />
              ))}
            </div>
            <CarpEventTimeline clusterName={clusterName} />
          </div>
        );
      })}

      {ungrouped.length > 0 && (
        <div>
          <h2 className="mb-3 text-lg font-semibold text-slate-900">{t("cluster.ungrouped")}</h2>
          <p className="mb-3 text-sm text-slate-500">
            {t("cluster.ungroupedHint")}
          </p>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {ungrouped.map((host) => (
              <HostCard key={host.id} host={host} ruleCount={ruleCounts[host.id]} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function HostCard({ host, ruleCount }: { host: Host; ruleCount: RuleCountState | undefined }) {
  const { t } = useLanguage();
  return (
    <Card className="p-5">
      <div className="mb-2 flex items-center justify-between">
        <Link href={`/hosts/${host.id}`} className="font-medium text-sky-700 hover:underline">
          {host.name}
        </Link>
        {host.carp_status && (
          <span
            className={
              host.carp_status === "MASTER"
                ? "rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700"
                : "rounded-full bg-flame-100 px-2.5 py-0.5 text-xs font-medium text-flame-700"
            }
          >
            {host.carp_status}
          </span>
        )}
      </div>
      <p className="mb-3 text-sm text-slate-500">{host.base_url}</p>
      <div className="flex items-center justify-between text-sm">
        <StatusDot status={host.status} />
        {ruleCount?.loading ? (
          <span className="flex items-center gap-1.5 text-slate-400">
            <Spinner size={13} />
            {t("common.loading")}
          </span>
        ) : ruleCount?.count !== null && ruleCount?.count !== undefined ? (
          <span className="text-slate-600">{t("cluster.rulesCount", { count: ruleCount.count })}</span>
        ) : (
          <span className="text-red-500" title={ruleCount?.error ?? undefined}>
            {t("cluster.rulesUnavailable")}
          </span>
        )}
      </div>
    </Card>
  );
}

function CarpEventTimeline({ clusterName }: { clusterName: string }) {
  const { t, errorMessage, locale } = useLanguage();
  const [events, setEvents] = useState<CarpEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.carpEvents(clusterName).then((data) => { if (active) setEvents(data); })
      .catch((e) => { if (active) setError(errorMessage(e, "cluster.carpHistoryFailed")); });
    return () => { active = false; };
  }, [clusterName]);

  if (error) return null;
  if (events === null) return null;

  return (
    <details className="mt-3 rounded-xl border border-slate-200 bg-white p-3 text-sm">
      <summary className="flex cursor-pointer items-center gap-1.5 font-medium text-slate-700">
        <History size={14} /> {t("cluster.carpHistory", { count: events.length })}
      </summary>
      {events.length === 0 ? (
        <p className="mt-2 text-slate-500">{t("cluster.noRoleChanges")}</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {events.map((event) => (
            <li key={event.id} className="flex flex-wrap items-center gap-2 text-slate-600">
              <span className="text-xs text-slate-400">{new Date(event.detected_at).toLocaleString(locale)}</span>
              <strong className="text-slate-900">{event.host_name}</strong>
              <span>{event.from_status ?? t("cluster.unknown")} → {event.to_status ?? t("cluster.unknown")}</span>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

function ClusterSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      {[0, 1].map((i) => (
        <Card key={i} className="p-5">
          <div className="mb-3 flex items-center justify-between">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-4 w-16 rounded-full" />
          </div>
          <Skeleton className="mb-4 h-3 w-36" />
          <div className="flex items-center justify-between">
            <Skeleton className="h-3 w-14" />
            <Skeleton className="h-3 w-16" />
          </div>
        </Card>
      ))}
    </div>
  );
}
