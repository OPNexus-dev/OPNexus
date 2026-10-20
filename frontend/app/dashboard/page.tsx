"use client";

import { InfoTip } from "@/components/ui/Tooltip";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { StatusDot } from "@/components/ui/StatusDot";
import { api, ComplianceHost, DashboardSummary, NotificationProblem } from "@/lib/api";
import {
  Activity,
  CheckCircle2,
  ClipboardList,
  Globe,
  Layers,
  Network,
  RefreshCw,
  ScrollText,
  ShieldAlert,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";
import { GatewayStateBadge, gatewayNumbers } from "@/components/GatewayStatus";
import { labelFor, type Language } from "@/lib/i18n";

const PENDING_KIND_LABELS: Record<string, string> = {
  rule: "rule",
  port_forward: "port_forward",
  one_to_one: "one_to_one",
  outbound_nat: "outbound_nat",
  policy_hierarchy: "policyHierarchy",
  alias: "alias",
  ipsec: "ipsec",
  openvpn: "openvpn",
  wireguard: "wireguard",
  host_operation: "hostMaintenance",
  carp_auto_revert: "carpAutoRevert",
};

const PENDING_STATUS_LABELS: Record<string, string> = {
  pending: "pending",
  error: "error",
  revert_failed: "revertFailed",
  starting: "starting",
  submitted: "submitted",
  unknown: "unknown",
  active: "active",
};

const PENDING_STATUS_TONE: Record<string, BadgeTone> = {
  pending: "warning",
  error: "danger",
  revert_failed: "danger",
  starting: "warning",
  submitted: "warning",
  unknown: "danger",
  active: "info",
};

interface FirmwareWarning {
  hostId: string;
  hostName: string;
  kind: "update" | "major_update";
}

function SummaryTile({ label, value, icon: Icon, tone }: { label: string; value: number; icon: typeof Network; tone: string }) {
  return (
    <Card className="flex items-center gap-3 p-4">
      <span className={`rounded-xl p-2.5 ${tone}`}>
        <Icon size={20} />
      </span>
      <span>
        <strong className="block text-xl text-slate-900">{value}</strong>
        <span className="text-xs text-slate-500">{label}</span>
      </span>
    </Card>
  );
}

function SectionCard({
  title, icon: Icon, children,
}: { title: string; icon: typeof Network; children: React.ReactNode }) {
  return (
    <Card className="min-w-0 p-5">
      <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
        <Icon size={16} />
        {title}
      </h2>
      {children}
    </Card>
  );
}

function timeAgo(iso: string, language: Language, t: (key: string, params?: Record<string, string | number>) => string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return t("dashboard.justNow");
  if (minutes < 60) return t("dashboard.minutesAgo", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return t("dashboard.hoursAgo", { count: hours });
  return new Date(iso).toLocaleDateString(language === "en" ? "en-GB" : "de-DE");
}

function certProblemLabel(context: Record<string, unknown>, t: (key: string, params?: Record<string, string | number>) => string): string {
  const name = (context.common_name as string) || (context.cert_descr as string) || t("dashboard.certificate");
  const days = context.days_until_expiry as number | undefined;
  if (context.status === "expired") return t("dashboard.certExpired", { name });
  if (typeof days === "number") return t("dashboard.certExpiresIn", { name, count: days });
  return t("dashboard.certExpiresSoon", { name });
}

export default function DashboardPage() {
  const { language, t, ts, errorMessage } = useLanguage();
  const router = useRouter();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [problems, setProblems] = useState<NotificationProblem[] | null>(null);
  const [firmwareWarnings, setFirmwareWarnings] = useState<FirmwareWarning[] | null>(null);
  const [posture, setPosture] = useState<ComplianceHost[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Pending confirmations expire after a few minutes, so keep the summary
  // (hosts, pending actions, activity) fresh; errors here are ignored on
  // purpose — the initial load below already reports a failing backend.
  useEffect(() => {
    const timer = setInterval(() => {
      api.dashboard().then(setSummary).catch(() => {});
    }, 15000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let active = true;

    api
      .dashboard()
      .then(async (data) => {
        if (!active) return;
        setSummary(data);
        // Fresh install: nothing to manage yet and the setup assistant was
        // never finished or skipped, so open it instead of an empty dashboard.
        if (data.hosts.length === 0) {
          Promise.all([api.me(), api.settings()])
            .then(([me, prefs]) => {
              if (active && me.role !== "read_only" && !prefs.setup_done) router.replace("/setup");
            })
            .catch(() => {});
        }
        const reachableHosts = data.hosts.filter((h) => h.status !== "offline" && h.status !== "disabled");
        const results = await Promise.all(
          reachableHosts.map(async (host) => {
            try {
              const maintenance = await api.hostMaintenance(host.id);
              const status = maintenance.state?.firmware_status;
              if (status === "update" || status === "upgrade") {
                return { hostId: host.id, hostName: host.name, kind: status === "upgrade" ? "major_update" : "update" } as FirmwareWarning;
              }
            } catch {
              // Unreachable right now — already surfaced as a host_offline problem below, no need to double-report.
            }
            return null;
          })
        );
        if (active) setFirmwareWarnings(results.filter((r): r is FirmwareWarning => r !== null));
      })
      .catch((e) => setError(errorMessage(e, "dashboard.loadFailed")));

    api.listCompliance().then((d) => active && setPosture(d)).catch(() => active && setPosture([]));

    api
      .notificationProblems(false)
      .then((data) => {
        if (active) setProblems(data);
      })
      .catch(() => {
        if (active) setProblems([]);
      });

    return () => {
      active = false;
    };
  }, [errorMessage, router]);

  const healthProblems = (problems ?? []).filter((p) => ["host_offline", "cert_expiry", "rule_drift", "gateway_down", "resource_threshold", "service_down"].includes(p.problem_type));
  const gatewayOrder: Record<string, number> = { offline: 0, degraded: 1, unknown: 2, forced_down: 3, online: 4 };
  const gatewayRows = (summary?.hosts ?? [])
    .flatMap((host) => (host.gateways ?? []).map((gateway) => ({ host, gateway })))
    .sort((a, b) => gatewayOrder[a.gateway.state] - gatewayOrder[b.gateway.state]);
  const securityWarningsLoading = problems === null || firmwareWarnings === null;
  const securityWarningCount = healthProblems.length + (firmwareWarnings?.length ?? 0);

  // Rule drift fires once per drifted rule, which can mean dozens of near-identical
  // rows for the same cluster (seen with real fleet data: 28 rows for one HA pair) —
  // collapse those into one summary row per cluster instead of flooding the panel.
  const nonDriftProblems = healthProblems.filter((p) => p.problem_type !== "rule_drift");
  const driftCountByCluster = new Map<string, number>();
  for (const p of healthProblems) {
    if (p.problem_type !== "rule_drift") continue;
    const cluster = String(p.context.cluster_name);
    driftCountByCluster.set(cluster, (driftCountByCluster.get(cluster) ?? 0) + 1);
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">Dashboard<InfoTip text={t("dashboard.subtitle")} size={18} /></h1>
      </div>

      {error && <p role="alert" className="mb-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>}

      {summary === null ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[68px]" />)}
        </div>
      ) : (
        <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SummaryTile label={t("dashboard.managedHosts")} value={summary.hosts.length} icon={Network} tone="bg-slate-100 text-slate-600" />
          <SummaryTile
            label={t("dashboard.online")}
            value={summary.hosts.filter((h) => h.status === "online").length}
            icon={CheckCircle2}
            tone="bg-emerald-50 text-emerald-700"
          />
          <SummaryTile label={t("dashboard.pendingActions")} value={summary.pending_actions.length} icon={ClipboardList} tone="bg-flame-50 text-flame-700" />
          <SummaryTile label={t("dashboard.securityWarnings")} value={securityWarningsLoading ? 0 : securityWarningCount} icon={ShieldAlert} tone="bg-red-50 text-red-700" />
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <SectionCard title={t("dashboard.fleetHealth")} icon={Layers}>
          {summary === null ? (
            <div className="space-y-2">{[0, 1].map((i) => <Skeleton key={i} className="h-10" />)}</div>
          ) : summary.hosts.length === 0 ? (
            <EmptyState icon={Network} title={t("dashboard.noHosts")} action={<span className="flex flex-wrap justify-center gap-x-4 gap-y-1"><Link href="/setup" className="text-sm font-medium text-sky-700 hover:underline">{t("dashboard.startSetup")}</Link><Link href="/hosts" className="text-sm text-sky-700 hover:underline">{t("dashboard.toHosts")}</Link></span>} />
          ) : (
            <ul className="divide-y divide-slate-100">
              {summary.hosts.map((host) => (
                <li key={host.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                  <Link href={`/hosts/${host.id}`} className="min-w-0 truncate font-medium text-sky-700 hover:underline">
                    {host.name}
                  </Link>
                  <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
                    {host.cluster_name && <Badge tone="neutral">{host.cluster_name}</Badge>}
                    {host.carp_status && <Badge tone={host.carp_status === "MASTER" ? "success" : "info"}>{host.carp_status}</Badge>}
                    <StatusDot status={host.status} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <SectionCard title={t("dashboard.gateways")} icon={Globe}>
          {summary === null ? (
            <div className="space-y-2">{[0, 1].map((i) => <Skeleton key={i} className="h-10" />)}</div>
          ) : gatewayRows.length === 0 ? (
            <EmptyState icon={Globe} title={t("dashboard.noGateways")} description={t("dashboard.noGatewaysDescription")} />
          ) : (
            <ul className="divide-y divide-slate-100">
              {gatewayRows.slice(0, 8).map(({ host, gateway }) => (
                <li key={`${host.id}-${gateway.name}`} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <Link href={`/hosts/${host.id}`} className="min-w-0 truncate font-medium text-sky-700 hover:underline">
                    {host.name} · {gateway.name}
                  </Link>
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-slate-500">{gatewayNumbers(gateway, t)}</span>
                    <GatewayStateBadge state={gateway.state} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <SectionCard title={t("dashboard.securityPosture")} icon={ShieldAlert}>
          {posture === null ? (
            <Skeleton className="h-10" />
          ) : posture.filter((h) => h.score !== null).length === 0 ? (
            <p className="text-sm text-slate-500">{t("dashboard.postureNone")} <Link href="/security" className="text-sky-700 hover:underline">{t("dashboard.postureOpen")}</Link></p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {[...posture].filter((h) => h.score !== null).sort((a, b) => (a.score ?? 0) - (b.score ?? 0)).slice(0, 6).map((h) => (
                <li key={h.host_id} className="flex items-center justify-between gap-3 py-2 text-sm">
                  <Link href="/security" className="min-w-0 truncate font-medium text-sky-700 hover:underline">{h.host_name}</Link>
                  <div className="flex items-center gap-2">
                    {h.counts && h.counts.critical > 0 && <Badge tone="danger">{h.counts.critical} {t("security.severity.critical")}</Badge>}
                    <Badge tone={h.grade === "good" ? "success" : h.grade === "fair" ? "warning" : "danger"}>{h.score} / 100</Badge>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <SectionCard title={t("dashboard.pendingActions")} icon={ClipboardList}>
          {summary === null ? (
            <div className="space-y-2">{[0, 1].map((i) => <Skeleton key={i} className="h-10" />)}</div>
          ) : summary.pending_actions.length === 0 ? (
            <EmptyState icon={CheckCircle2} title={t("dashboard.nothingPending")} description={t("dashboard.nothingPendingDescription")} />
          ) : (
            <ul className="divide-y divide-slate-100">
              {summary.pending_actions.map((item, index) => (
                <li key={index} className="py-2 text-sm">
                  <Link href={item.link} className="flex items-center justify-between gap-3 hover:underline">
                    <span className="min-w-0 truncate">
                      <span className="font-medium text-slate-900">{item.entity_name}</span>
                      <span className="text-slate-500"> · {PENDING_KIND_LABELS[item.kind] ? t(`dashboard.kinds.${PENDING_KIND_LABELS[item.kind]}`) : item.kind} · {item.host_name}</span>
                    </span>
                    <Badge tone={PENDING_STATUS_TONE[item.status] ?? "neutral"} className="shrink-0">
                      {PENDING_STATUS_LABELS[item.status] ? t(`dashboard.statuses.${PENDING_STATUS_LABELS[item.status]}`) : item.status}
                    </Badge>
                  </Link>
                  {item.message && <p className="mt-0.5 truncate text-xs text-slate-400">{item.message}</p>}
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <SectionCard title={t("dashboard.securityAndExpiryWarnings")} icon={ShieldAlert}>
          {securityWarningsLoading ? (
            <div className="space-y-2">{[0, 1].map((i) => <Skeleton key={i} className="h-10" />)}</div>
          ) : securityWarningCount === 0 ? (
            <EmptyState icon={CheckCircle2} title={t("dashboard.noWarnings")} description={t("dashboard.noWarningsDescription")} />
          ) : (
            <ul className="divide-y divide-slate-100">
              {nonDriftProblems.map((problem) => (
                <li key={problem.id} className="py-2 text-sm">
                  {problem.problem_type === "host_offline" && (
                    <Link href="/hosts" className="flex items-center justify-between gap-3 hover:underline">
                      <span className="min-w-0 truncate font-medium text-slate-900">{t("dashboard.hostIsOffline", { name: String(problem.context.host_name) })}</span>
                      <Badge tone="danger" className="shrink-0">{t("dashboard.hostOffline")}</Badge>
                    </Link>
                  )}
                  {(problem.problem_type === "gateway_down" || problem.problem_type === "resource_threshold" || problem.problem_type === "service_down") && (
                    <Link href={`/hosts/${String(problem.context.host_id)}`} className="flex items-center justify-between gap-3 hover:underline">
                      <span className="min-w-0 truncate font-medium text-slate-900">
                        {String(problem.context.host_name)}: {ts(String(problem.context.gateway ?? problem.context.ressource ?? problem.context.dienst))}
                      </span>
                      <Badge tone="danger" className="shrink-0">{t(`dashboard.alert.${problem.problem_type}`)}</Badge>
                    </Link>
                  )}
                  {problem.problem_type === "cert_expiry" && (
                    <Link href="/certificates" className="flex items-center justify-between gap-3 hover:underline">
                      <span className="min-w-0 truncate font-medium text-slate-900">
                        {String(problem.context.host_name)}: {certProblemLabel(problem.context, t)}
                      </span>
                      <Badge tone={problem.context.status === "expired" ? "danger" : "warning"} className="shrink-0">
                        {problem.context.status === "expired" ? t("dashboard.expired") : t("dashboard.expires")}
                      </Badge>
                    </Link>
                  )}
                </li>
              ))}
              {[...driftCountByCluster.entries()].map(([cluster, count]) => (
                <li key={`drift-${cluster}`} className="py-2 text-sm">
                  <Link href="/cluster" className="flex items-center justify-between gap-3 hover:underline">
                    <span className="min-w-0 truncate font-medium text-slate-900">
                      {t("dashboard.driftLine", { cluster, count })}
                    </span>
                    <Badge tone="warning" className="shrink-0">{t("dashboard.driftBadge")}</Badge>
                  </Link>
                </li>
              ))}
              {firmwareWarnings?.map((warning) => (
                <li key={`${warning.hostId}-${warning.kind}`} className="py-2 text-sm">
                  <Link href="/updates" className="flex items-center justify-between gap-3 hover:underline">
                    <span className="min-w-0 truncate font-medium text-slate-900">
                      {warning.hostName}: {warning.kind === "major_update" ? t("dashboard.majorAvailable") : t("dashboard.stableAvailable")}
                    </span>
                    <Badge tone="info" className="shrink-0">
                      <RefreshCw size={12} /> {t("dashboard.updateBadge")}
                    </Badge>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <SectionCard title={t("dashboard.activity")} icon={Activity}>
          {summary === null ? (
            <div className="space-y-2">{[0, 1].map((i) => <Skeleton key={i} className="h-10" />)}</div>
          ) : summary.recent_activity.length === 0 ? (
            <EmptyState icon={ScrollText} title={t("dashboard.noActivity")} />
          ) : (
            <>
              <ul className="divide-y divide-slate-100">
                {summary.recent_activity.slice(0, 8).map((item) => (
                  <li key={item.id} className="py-2 text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate">
                        <span className="font-medium text-slate-900">{item.entity_name ?? labelFor(t, "audit.kind", item.kind)}</span>
                        <span className="text-slate-500"> · {labelFor(t, "audit.action", item.action)}{item.host_name ? ` · ${item.host_name}` : ""}</span>
                      </span>
                      <Badge tone={item.status === "success" ? "success" : item.status === "error" ? "danger" : "neutral"} className="shrink-0">
                        {labelFor(t, "audit.status", item.status)}
                      </Badge>
                    </div>
                    <p className="mt-0.5 text-xs text-slate-400">{item.actor} · {timeAgo(item.created_at, language, t)}</p>
                  </li>
                ))}
              </ul>
              <Link href="/audit" className="mt-3 inline-block text-sm text-sky-700 hover:underline">
                {t("dashboard.viewAudit")}
              </Link>
            </>
          )}
        </SectionCard>
      </div>
    </div>
  );
}
