"use client";

import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CarpSwitchControl, carpMaintenanceWarning } from "@/components/CarpSwitchControl";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Spinner } from "@/components/ui/Spinner";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import {
  api,
  Host,
  HostOperation,
  MaintenancePreview,
  MaintenanceRead,
  MaintenanceState,
} from "@/lib/api";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";
import type { TranslationParams } from "@/lib/i18n";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleOff,
  Clock3,
  FileClock,
  Lock,
  RefreshCw,
  Server,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type HostUpdateData = { read: MaintenanceRead | null; loading: boolean; checking: boolean; error: string | null };

const activeStatuses = new Set(["starting", "submitted", "unknown"]);

type Translate = (key: string, params?: TranslationParams) => string;

function formatDate(value: string | null | undefined, t: Translate, locale: string) {
  if (!value) return t("updates.notChecked");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale);
}

function firmwareLabel(state: MaintenanceState, t: Translate): { text: string; tone: BadgeTone } {
  if (state.running !== "ready") return { text: t("updates.fw.running"), tone: "info" };
  if (state.firmware_status === "update") return { text: t("updates.fw.update"), tone: "warning" };
  if (state.firmware_status === "upgrade") return { text: t("updates.fw.upgrade"), tone: "accent" };
  if (state.firmware_status === "error") return { text: t("updates.fw.error"), tone: "danger" };
  if (state.firmware_status === "none") return { text: t("updates.fw.none"), tone: "neutral" };
  return { text: t("updates.fw.current"), tone: "success" };
}

function blockerExplanation(blocker: string, host: Host, t: Translate, ts: (text: string) => string) {
  if (blocker.includes("Cluster-Wartung nur auf dem CARP-BACKUP")) {
    return {
      title: t("updates.blocker.haOrder.title"),
      text: host.carp_status === "MASTER"
        ? t("updates.blocker.haOrder.master")
        : t("updates.blocker.haOrder.backup"),
    };
  }
  if (blocker.includes("Kein erreichbarer aktiver Cluster-Partner")) {
    return { title: t("updates.blocker.partner.title"), text: t("updates.blocker.partner.text") };
  }
  if (blocker.includes("lokales Konfigurationsbackup")) {
    return { title: t("updates.blocker.backup.title"), text: t("updates.blocker.backup.text") };
  }
  if (blocker.includes("Kein stabiles Update")) {
    return { title: t("updates.blocker.noUpdate.title"), text: t("updates.blocker.noUpdate.text") };
  }
  if (blocker.includes("Kein Major-Upgrade")) {
    return { title: t("updates.blocker.noMajor.title"), text: t("updates.blocker.noMajor.text") };
  }
  if (blocker.includes("frühere Wartungsaktion")) {
    return { title: t("updates.blocker.pending.title"), text: t("updates.blocker.pending.text") };
  }
  if (blocker.includes("Offene Bestätigung") || blocker.includes("Rollback")) {
    return { title: t("updates.blocker.change.title"), text: t("updates.blocker.change.text") };
  }
  return { title: t("updates.blocker.locked"), text: ts(blocker) };
}

function packageReason(reason: string, t: Translate) {
  return ["upgrade", "new", "reinstall", "downgrade", "obsolete"].includes(reason) ? t(`updates.pkg.${reason}`) : reason;
}

function SummaryCard({ label, value, icon: Icon, tone }: { label: string; value: number; icon: typeof Server; tone: string }) {
  return (
    <Card className="flex items-center gap-3 p-4">
      <span className={`rounded-xl p-2.5 ${tone}`}><Icon size={20} /></span>
      <span><strong className="block text-xl text-slate-900">{value}</strong><span className="text-xs text-slate-500">{label}</span></span>
    </Card>
  );
}

function BlockedTip({ reasons, children }: { reasons: React.ReactNode[]; children: React.ReactNode }) {
  if (reasons.length === 0) return <>{children}</>;
  return <Tooltip focusable placement="bottom" content={<span className="block space-y-1">{reasons.map((reason, index) => <span key={index} className="block">{reason}</span>)}</span>}>{children}</Tooltip>;
}

export default function UpdatesPage() {
  const { t, ts, errorMessage, locale } = useLanguage();
  const tRef = useRef(t);
  tRef.current = t;
  const errRef = useRef(errorMessage);
  errRef.current = errorMessage;
  const readOnly = useReadOnly();
  const toast = useToast();
  const mounted = useRef(true);
  const automaticCheckStarted = useRef(false);
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [details, setDetails] = useState<Record<string, HostUpdateData>>({});
  const [pageError, setPageError] = useState<string | null>(null);
  const setNotice = useCallback((message: string | null) => { if (message) toast.show(message, "success"); }, [toast]);
  const noticeRef = useRef(setNotice);
  noticeRef.current = setNotice;
  const [refreshing, setRefreshing] = useState(false);
  const [bulkChecking, setBulkChecking] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<{ host: Host; plan: MaintenancePreview } | null>(null);
  const [finish, setFinish] = useState<{ host: Host; operation: HostOperation } | null>(null);
  const [cancelTarget, setCancelTarget] = useState<{ host: Host; operation: HostOperation } | null>(null);
  const [confirmName, setConfirmName] = useState("");
  const [ack, setAck] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [log, setLog] = useState<{ host: Host; text: string } | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const loadOne = useCallback(async (host: Host) => {
    setDetails((current) => ({
      ...current,
      [host.id]: { read: current[host.id]?.read ?? null, checking: current[host.id]?.checking ?? false, loading: true, error: null },
    }));
    try {
      const read = await api.hostMaintenance(host.id);
      if (mounted.current) setDetails((current) => ({ ...current, [host.id]: { ...current[host.id], read, loading: false, error: null } }));
      return read;
    } catch (error) {
      if (mounted.current) setDetails((current) => ({
        ...current,
        [host.id]: { ...current[host.id], loading: false, error: errRef.current(error, "updates.statusFailed") },
      }));
      return null;
    }
  }, []);

  const loadFleet = useCallback(async () => {
    setRefreshing(true);
    setPageError(null);
    try {
      const list = await api.listHosts();
      if (!mounted.current) return;
      setHosts(list);
      setDetails((current) => Object.fromEntries(list.map((host) => [host.id, current[host.id] ?? { read: null, loading: true, checking: false, error: null }])));
      await Promise.all(list.map(loadOne));
    } catch (error) {
      if (mounted.current) setPageError(errRef.current(error, "updates.hostsFailed"));
    } finally {
      if (mounted.current) setRefreshing(false);
    }
  }, [loadOne]);

  useEffect(() => { void loadFleet(); }, [loadFleet]);

  const waitForCheck = useCallback(async (host: Host) => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
      if (!mounted.current) return;
      try {
        const progress = await api.hostUpdateProgress(host.id);
        if (["done", "reboot", "error"].includes(progress.status)) break;
      } catch {
        break;
      }
    }
    await loadOne(host);
  }, [loadOne]);

  const checkHost = useCallback(async (host: Host, quiet = false) => {
    setDetails((current) => ({ ...current, [host.id]: { ...current[host.id], checking: true, error: null } }));
    try {
      await api.checkHostUpdates(host.id);
      if (!quiet) noticeRef.current(tRef.current("updates.checkStarted", { host: host.name }));
      await waitForCheck(host);
      return true;
    } catch (error) {
      setDetails((current) => ({ ...current, [host.id]: { ...current[host.id], error: errRef.current(error, "updates.checkFailed") } }));
      return false;
    } finally {
      if (mounted.current) setDetails((current) => ({ ...current, [host.id]: { ...current[host.id], checking: false } }));
    }
  }, [waitForCheck]);

  const checkAll = useCallback(async (automatic = false) => {
    if (!hosts) return;
    const eligible = hosts.filter((host) => host.enabled && details[host.id]?.read?.state?.running === "ready" && !details[host.id]?.read?.operations.some((op) => activeStatuses.has(op.status)));
    setBulkChecking(true);
    setPageError(null);
    if (automatic) toast.show(tRef.current("updates.autoRunning", { count: eligible.length }), "info");
    const results = await Promise.all(eligible.map((host) => checkHost(host, true)));
    const succeeded = results.filter(Boolean).length;
    setBulkChecking(false);
    noticeRef.current(tRef.current("updates.checkedSummary", { prefix: automatic ? tRef.current("updates.autoDone") : "", done: succeeded, total: eligible.length }));
  }, [hosts, details, checkHost, toast]);

  useEffect(() => {
    if (automaticCheckStarted.current || hosts === null || refreshing || hosts.some((host) => details[host.id]?.loading)) return;
    automaticCheckStarted.current = true;
    void api.me().then((session) => {
      if (session.role !== "read_only" && mounted.current) void checkAll(true);
    });
  }, [hosts, refreshing, details, checkAll]);

  async function createBackup(host: Host) {
    setSubmitting(true);
    setPageError(null);
    try {
      await api.triggerHostBackup(host.id);
      setNotice(t("updates.backupSaved", { host: host.name }));
      await loadOne(host);
    } catch (error) {
      setPageError(errorMessage(error, "updates.backupFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  async function openPreview(host: Host, action: "update" | "major_update") {
    setSubmitting(true);
    setPageError(null);
    try {
      const plan = await api.previewMaintenance(host.id, action);
      setPreview({ host, plan });
      setConfirmName("");
      setAck(false);
    } catch (error) {
      setPageError(errorMessage(error, "updates.previewFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  async function executeUpdate() {
    if (!preview) return;
    setSubmitting(true);
    const label = preview.plan.action === "major_update" ? t("updates.labelMajor") : t("updates.labelUpdate");
    try {
      const operation = await api.executeMaintenance(preview.host.id, preview.plan.preview_id, confirmName, ack);
      setNotice(operation.message ? ts(operation.message) : t("updates.handedOver", { label, host: preview.host.name }));
      const host = preview.host;
      setPreview(null);
      await loadOne(host);
    } catch (error) {
      setPageError(errorMessage(error, "updates.outcomeUnclear"));
      const host = preview.host;
      setPreview(null);
      await loadOne(host);
    } finally {
      setSubmitting(false);
    }
  }

  async function acknowledgeOperation() {
    if (!finish) return;
    setSubmitting(true);
    try {
      await api.acknowledgeMaintenance(finish.host.id, finish.operation.id, confirmName);
      const host = finish.host;
      setFinish(null);
      setNotice(t("updates.finishDocumented", { host: host.name }));
      await loadOne(host);
    } catch (error) {
      setPageError(errorMessage(error, "updates.finishFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelOperation() {
    if (!cancelTarget) return;
    setSubmitting(true);
    try {
      await api.cancelMaintenance(cancelTarget.host.id, cancelTarget.operation.id, confirmName, cancelReason);
      const host = cancelTarget.host;
      setCancelTarget(null);
      setNotice(t("updates.cancelledNote", { host: host.name }));
      await loadOne(host);
    } catch (error) {
      setPageError(errorMessage(error, "updates.cancelFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  async function showLog(host: Host) {
    setSubmitting(true);
    setPageError(null);
    try {
      const progress = await api.hostUpdateProgress(host.id);
      setLog({ host, text: progress.log || progress.status });
    } catch (error) {
      setPageError(errorMessage(error, "updates.logFailed"));
    } finally {
      setSubmitting(false);
    }
  }

  const summary = useMemo(() => {
    const rows = hosts ?? [];
    return {
      total: rows.length,
      available: rows.filter((host) => details[host.id]?.read?.state?.firmware_status === "update").length,
      major: rows.filter((host) => details[host.id]?.read?.state?.firmware_status === "upgrade").length,
      unavailable: rows.filter((host) => details[host.id]?.error || details[host.id]?.read?.error).length,
    };
  }, [hosts, details]);

  const eligibleCount = (hosts ?? []).filter((host) => host.enabled && details[host.id]?.read?.state?.running === "ready" && !details[host.id]?.read?.operations.some((op) => activeStatuses.has(op.status))).length;

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold text-slate-900">{t("updates.title")}<InfoTip text={t("updates.intro")} size={18} />{readOnly && <Tooltip content={t("updates.readOnlyNote")} focusable><Badge tone="warning">{t("updates.readOnlyBadge")}</Badge></Tooltip>}</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Tooltip content={t("updates.tipCheckAll")} focusable={readOnly || eligibleCount === 0}><Button disabled={readOnly || refreshing || bulkChecking || eligibleCount === 0} onClick={() => void checkAll(false)}>{bulkChecking ? <Spinner size={14} /> : <RefreshCw size={16} />}{t("updates.checkAgain")}</Button></Tooltip>
          <Tooltip content={t("updates.tipRefreshView")}><Button variant="ghost" disabled={refreshing || bulkChecking} onClick={() => void loadFleet()}><RefreshCw size={16} className={refreshing ? "animate-spin" : ""} />{t("updates.refreshView")}</Button></Tooltip>
        </div>
      </div>

      {pageError && <p role="alert" className="mb-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{pageError}</p>}

      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard label={t("updates.sumHosts")} value={summary.total} icon={Server} tone="bg-slate-100 text-slate-600" />
        <SummaryCard label={t("updates.sumStable")} value={summary.available} icon={AlertTriangle} tone="bg-flame-50 text-flame-700" />
        <SummaryCard label={t("updates.sumMajor")} value={summary.major} icon={FileClock} tone="bg-violet-50 text-violet-700" />
        <SummaryCard label={t("updates.sumUnavailable")} value={summary.unavailable} icon={CircleOff} tone="bg-red-50 text-red-700" />
      </div>

      <div className="space-y-3">
        {hosts === null && <Card className="flex items-center justify-center gap-2 p-10 text-sm text-slate-400"><Spinner size={16} />{t("updates.fleetLoading")}</Card>}
        {hosts?.length === 0 && <EmptyState icon={Server} title={t("drift.noHosts")} />}
        {hosts?.map((host) => {
          const detail = details[host.id];
          const read = detail?.read;
          const state = read?.state;
          const problem = detail?.error ?? read?.error;
          const label = state ? firmwareLabel(state, t) : null;
          const packages = Object.values(state?.packages ?? {});
          const upgradeSets = Object.values(state?.upgrade_sets ?? {});
          const operations = read?.operations ?? [];
          const activeOperation = operations.find((operation) => activeStatuses.has(operation.status));
          const blockers = state?.actions.update ?? [];
          const majorBlockers = state?.actions.major_update ?? [];
          const hasCarpRoleBlocker = blockers.some((blocker) => blocker.includes("Cluster-Wartung nur auf dem CARP-BACKUP"));
          const visibleBlockers = blockers.filter((blocker) => !(hasCarpRoleBlocker && blocker.includes("Kein erreichbarer aktiver Cluster-Partner")));
          const hasMajorCarpRoleBlocker = majorBlockers.some((blocker) => blocker.includes("Cluster-Wartung nur auf dem CARP-BACKUP"));
          const visibleMajorBlockers = majorBlockers.filter((blocker) => !(hasMajorCarpRoleBlocker && blocker.includes("Kein erreichbarer aktiver Cluster-Partner")));
          const blockerReasons = (list: string[]) => list.map((blocker) => { const explanation = blockerExplanation(blocker, host, t, ts); return <span key={blocker}><strong>{explanation.title}:</strong> {explanation.text}</span>; });
          const carpWarning = state ? carpMaintenanceWarning(host, state, t) : null;
          const canCheck = host.enabled && state?.running === "ready" && !activeOperation;
          const isExpanded = expanded.has(host.id);
          return (
            <Card key={host.id} className="overflow-hidden">
              <div className="p-4 sm:p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link href={`/hosts/${host.id}`} className="font-semibold text-slate-900 hover:text-sky-700 hover:underline">{host.name}</Link>
                      {label && <Badge tone={label.tone}>{label.text}</Badge>}
                      {!host.enabled && <Badge tone="neutral">{t("updates.disabled")}</Badge>}
                      {(state?.carp_status ?? host.carp_status) && <Badge tone="neutral">CARP {state?.carp_status ?? host.carp_status}</Badge>}
                      {carpWarning && <Tooltip content={carpWarning} focusable><Badge tone="warning">{t("updates.carpMode")}</Badge></Tooltip>}
                      {state?.reboot_required && <Tooltip content={t("updates.tipRebootRequired")} focusable><Badge tone="warning"><AlertTriangle size={12} />{t("updates.rebootRequired")}</Badge></Tooltip>}
                    </div>
                    <p className="mt-1 truncate text-xs text-slate-400">{host.base_url}{host.cluster_name ? t("updates.clusterSuffix", { name: host.cluster_name }) : ""}</p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Tooltip content={t("updates.tipCheck")} focusable={!canCheck}><Button variant="secondary" disabled={readOnly || !canCheck || detail?.checking || submitting || bulkChecking} onClick={() => void checkHost(host)}>{detail?.checking ? <Spinner size={13} /> : <RefreshCw size={14} />}{t("updates.check")}</Button></Tooltip>
                    {state && <CarpSwitchControl host={host} state={state} disabled={readOnly || Boolean(activeOperation) || submitting || detail?.checking} onChanged={() => loadOne(host)} />}
                    <BlockedTip reasons={readOnly ? [t("updates.readOnlyNote")] : blockerReasons(visibleBlockers)}>
                      <Button disabled={readOnly || blockers.length > 0 || submitting || detail?.checking} onClick={() => void openPreview(host, "update")}>{blockers.length > 0 && !readOnly && <Lock size={13} aria-label={t("updates.lockedPrefix")} />}{t("updates.prepareUpdate")}</Button>
                    </BlockedTip>
                    <BlockedTip reasons={readOnly ? [t("updates.readOnlyNote")] : blockerReasons(visibleMajorBlockers)}>
                      <Button variant="danger" disabled={readOnly || majorBlockers.length > 0 || submitting || detail?.checking} onClick={() => void openPreview(host, "major_update")}>{majorBlockers.length > 0 && !readOnly && <Lock size={13} aria-label={t("updates.lockedPrefix")} />}{t("updates.prepareMajor")}</Button>
                    </BlockedTip>
                    {/* Not nested under `state &&`: a host can be offline/unreachable
                        precisely because a maintenance action is stuck — the operator
                        must still be able to confirm or cancel it from here. */}
                    {activeOperation && <>
                      <Tooltip content={t("updates.tipFinish")}><Button variant="secondary" disabled={readOnly || submitting} onClick={() => { setFinish({ host, operation: activeOperation }); setConfirmName(""); setAck(false); }}><Clock3 size={14} />{t("updates.reviewFinish")}</Button></Tooltip>
                      <Tooltip content={t("updates.tipCancel")}><Button variant="danger" disabled={readOnly || submitting} onClick={() => { setCancelTarget({ host, operation: activeOperation }); setConfirmName(""); setCancelReason(""); }}><CircleOff size={14} />{t("updates.cancelOperation")}</Button></Tooltip>
                    </>}
                    <Tooltip content={t("updates.tipView")} className="ml-auto"><Button variant="ghost" disabled={detail?.loading || detail?.checking || submitting} onClick={() => void loadOne(host)} aria-label={t("updates.view")}><RefreshCw size={14} className={detail?.loading ? "animate-spin" : ""} />{t("updates.view")}</Button></Tooltip>
                  </div>
                </div>

                {detail?.loading && !state && <p className="mt-4 inline-flex items-center gap-2 text-sm text-slate-400"><Spinner size={14} />{t("updates.readingStatus")}</p>}
                {problem && <p role="alert" className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{ts(problem)}</p>}
                {state && (
                  <>
                    <div className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                      <div><span className="flex items-center gap-1 text-xs text-slate-400">{t("updates.installed")}<InfoTip text={t("updates.tipInstalled")} size={12} /></span><strong>{state.version}</strong></div>
                      <div><span className="flex items-center gap-1 text-xs text-slate-400">{t("updates.available")}<InfoTip text={t("updates.tipAvailable")} size={12} /></span><strong>{upgradeSets[0]?.new || state.product_latest || (state.firmware_status === "update" ? t("updates.packageUpdates") : "—")}</strong></div>
                      <div><span className="block text-xs text-slate-400">{t("updates.lastCheck")}</span><span>{formatDate(state.last_check, t, locale)}</span></div>
                      <div><span className="flex items-center gap-1 text-xs text-slate-400">{t("updates.localBackup")}<InfoTip text={t("updates.tipBackup")} size={12} /></span><span className={state.backup_fresh ? "text-emerald-700" : "text-flame-700"}>{state.backup ? `${formatDate(state.backup.created_at, t, locale)}${state.backup_fresh ? t("updates.backupFresh") : t("updates.backupOld")}` : t("updates.backupMissing")}</span>{!state.backup_fresh && <button className="mt-1 block text-xs font-medium text-sky-700 hover:underline disabled:opacity-50" disabled={readOnly || submitting} onClick={() => void createBackup(host)}>{t("updates.backupNow")}</button>}</div>
                    </div>
                    {state.firmware_message && <p className="mt-3 text-sm text-slate-600">{ts(state.firmware_message)}</p>}
                    <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
                      <Tooltip content={t("updates.tipPackages")}><Button variant="ghost" className="px-2" aria-expanded={isExpanded} onClick={() => setExpanded((current) => {
                        const next = new Set(current);
                        if (next.has(host.id)) next.delete(host.id);
                        else next.add(host.id);
                        return next;
                      })}>{isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}{t("updates.packageChanges", { count: packages.length })}{upgradeSets.length > 0 ? t("updates.majorTargets", { count: upgradeSets.length }) : ""}</Button></Tooltip>
                      <Tooltip content={t("updates.tipLog")}><Button variant="ghost" className="px-2" disabled={submitting} onClick={() => void showLog(host)}><FileClock size={15} />{t("updates.updateLog")}</Button></Tooltip>
                    </div>
                  </>
                )}
              </div>

              {isExpanded && state && (
                <div className="space-y-4 border-t border-slate-200 bg-slate-50 px-4 py-4 sm:px-5">
                  {upgradeSets.length > 0 && (
                    <div>
                      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-violet-700">{t("updates.majorTargetsTitle")}</p>
                      <div className="overflow-x-auto rounded-xl border border-violet-200 bg-white">
                        <table className="w-full min-w-[540px] text-left text-sm">
                          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400"><tr><th className="px-4 py-2 font-medium">{t("updates.colTarget")}</th><th className="px-4 py-2 font-medium">{t("updates.colInstalled")}</th><th className="px-4 py-2 font-medium">{t("updates.colNew")}</th><th className="px-4 py-2 font-medium">{t("updates.colRepository")}</th></tr></thead>
                          <tbody>{upgradeSets.map((set) => <tr key={set.name} className="border-b border-slate-100 last:border-0"><td className="px-4 py-2 font-medium">{set.name}</td><td className="px-4 py-2 text-slate-500">{set.old}</td><td className="px-4 py-2 text-slate-700">{set.new}</td><td className="px-4 py-2 text-slate-500">{set.repository ?? "—"}</td></tr>)}</tbody>
                        </table>
                      </div>
                    </div>
                  )}
                  <div>
                    {upgradeSets.length > 0 && <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{t("updates.stablePackageChanges")}</p>}
                    {packages.length === 0 ? <p className="text-sm text-slate-400">{t("updates.noPackageChanges")}</p> : (
                      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
                        <table className="w-full min-w-[540px] text-left text-sm">
                          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400"><tr><th className="px-4 py-2 font-medium">{t("updates.colPackage")}</th><th className="px-4 py-2 font-medium">{t("updates.colAction")}</th><th className="px-4 py-2 font-medium">{t("updates.colInstalled")}</th><th className="px-4 py-2 font-medium">{t("updates.colNew")}</th></tr></thead>
                          <tbody>{packages.map((pkg) => <tr key={pkg.name} className="border-b border-slate-100 last:border-0"><td className="px-4 py-2 font-medium">{pkg.name}</td><td className="px-4 py-2 text-slate-500">{packageReason(pkg.reason, t)}</td><td className="px-4 py-2 text-slate-500">{pkg.old}</td><td className="px-4 py-2 text-slate-700">{pkg.new}</td></tr>)}</tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </Card>
          );
        })}
      </div>

      {preview && (
        <Modal title={preview.plan.action === "major_update" ? t("updates.previewTitleMajor", { host: preview.host.name }) : t("updates.previewTitleUpdate", { host: preview.host.name })} onClose={() => { if (!submitting) setPreview(null); }}>
          <div className="space-y-4 text-sm">
            {preview.plan.action === "major_update" && <p className="flex items-center gap-2 font-medium text-red-700"><AlertTriangle size={16} />{t("updates.majorWarnShort")}<InfoTip text={t("updates.majorWarnTip")} /></p>}
            <p className="flex items-center gap-2 font-medium text-flame-800"><Clock3 size={16} />{t("updates.downtimeShort")}<InfoTip text={t("updates.downtimeTip")} /></p>
            <div className="grid grid-cols-2 gap-2 rounded-xl border border-slate-200 p-3"><span className="text-slate-500">{t("updates.installed")}</span><strong>{preview.plan.state.version}</strong><span className="text-slate-500">{t("updates.carpRole")}</span><strong>{preview.host.carp_status || t("updates.standalone")}</strong><span className="text-slate-500">{t("updates.previewValidUntil")}</span><strong>{new Date(preview.plan.expires_at).toLocaleTimeString(locale)}</strong></div>
            <label className="block"><span className="flex items-center gap-1">{t("maint.confirmHostname")}<InfoTip text={t("updates.tipConfirmName")} size={13} /></span><Input className="mt-1" autoFocus autoComplete="off" spellCheck={false} aria-label={t("maint.confirmHostnameAria")} value={confirmName} onChange={(event) => setConfirmName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !submitting && confirmName === preview.host.name && ack) void executeUpdate(); }} placeholder={preview.host.name} /></label>
            <label className="flex items-start gap-2"><input className="mt-0.5" type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} /><span>{t("maint.windowReady")}</span></label>
            <Tooltip focusable={confirmName !== preview.host.name || !ack} content={confirmName !== preview.host.name ? t("updates.confirmRunDisabledName") : !ack ? t("updates.confirmRunDisabledAck") : null}><Button variant="danger" disabled={submitting || confirmName !== preview.host.name || !ack} onClick={() => void executeUpdate()}>{submitting ? <Spinner size={14} /> : null}{preview.plan.action === "major_update" ? t("updates.runMajorNow") : t("updates.runUpdateNow")}</Button></Tooltip>
          </div>
        </Modal>
      )}

      {finish && (
        <Modal title={finish.operation.action.startsWith("carp_") ? t("updates.finishTitleCarp", { host: finish.host.name }) : t("updates.finishTitle", { host: finish.host.name })} onClose={() => { if (!submitting) setFinish(null); }}>
          <div className="space-y-4 text-sm">
            <p className="flex items-start gap-1">{finish.operation.action.startsWith("carp_") ? t("updates.finishCheckCarp") : t("updates.finishCheck")}<InfoTip text={t("updates.finishNoteTip")} size={13} className="mt-0.5" /></p>
            <Input autoFocus autoComplete="off" spellCheck={false} aria-label={t("maint.finishHostnameAria")} value={confirmName} onChange={(event) => setConfirmName(event.target.value)} placeholder={finish.host.name} />
            <label className="flex items-start gap-2"><input className="mt-0.5" type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} /><span>{t("maint.finishChecked")}</span></label>
            <Button disabled={submitting || confirmName !== finish.host.name || !ack} onClick={() => void acknowledgeOperation()}>{submitting ? <Spinner size={14} /> : <CheckCircle2 size={14} />}{t("maint.saveFinish")}</Button>
          </div>
        </Modal>
      )}

      {cancelTarget && (
        <Modal title={t("updates.cancelTitle", { host: cancelTarget.host.name })} onClose={() => { if (!submitting) setCancelTarget(null); }}>
          <div className="space-y-4 text-sm">
            <p className="flex items-center gap-2 font-medium text-flame-800"><AlertTriangle size={16} />{t("updates.cancelWarnShort")}<InfoTip text={t("updates.cancelWarnTip")} /></p>
            <Input autoFocus autoComplete="off" spellCheck={false} aria-label={t("maint.cancelHostnameAria")} value={confirmName} onChange={(event) => setConfirmName(event.target.value)} placeholder={cancelTarget.host.name} />
            <label className="block">{t("maint.reasonLabel")}<textarea className="mt-1 w-full rounded-lg border border-slate-300 p-2 text-sm" rows={2} maxLength={500} value={cancelReason} onChange={(event) => setCancelReason(event.target.value)} placeholder={t("maint.reasonPlaceholder")} /></label>
            <Button variant="danger" disabled={submitting || confirmName !== cancelTarget.host.name || cancelReason.trim().length < 3} onClick={() => void cancelOperation()}>{submitting ? <Spinner size={14} /> : <CircleOff size={14} />}{t("maint.closeWithoutConfirm")}</Button>
          </div>
        </Modal>
      )}

      {log && (
        <Modal title={t("updates.logTitle", { host: log.host.name })} onClose={() => setLog(null)}>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-xl bg-slate-950 p-4 text-xs text-slate-100">{log.text}</pre>
        </Modal>
      )}
    </div>
  );
}
