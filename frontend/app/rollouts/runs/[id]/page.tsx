"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, RolloutGate, RolloutRunDetail } from "@/lib/api";
import { CHECKS, codeText, HOST_TONE, isActiveRun, pauseDetailText, pauseText, RESULT_TONE, RUN_TONE, WAVE_TONE } from "@/lib/rollouts";
import { useReadOnly } from "@/lib/useReadOnly";
import { AlertTriangle, ArrowLeft, CheckCheck, Pause, Play, SkipForward, Square, Waves } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

type HostRow = RolloutRunDetail["hosts"][number];

function GateMatrix({ gate, run, title }: { gate: RolloutGate; run: RolloutRunDetail; title: string }) {
  const { t } = useLanguage();
  const names = Object.fromEntries(run.hosts.filter((h) => h.host_id).map((h) => [h.host_id as string, h.host_name]));
  const checks = CHECKS.filter((c) => Object.values(gate.hosts).some((row) => row[c]));
  return (
    <div className="overflow-x-auto">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-slate-700">
        {title}
        <Badge tone={gate.verdict === "ok" ? "success" : gate.verdict === "warn" ? "warning" : "danger"}>{t(`rollouts.gate.verdict.${gate.verdict}`)}</Badge>
      </div>
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2 font-medium">{t("rollouts.col.host")}</th>
            {checks.map((c) => <th key={c} className="px-3 py-2 font-medium">{t(`rollouts.gate.check.${c}`)}</th>)}
          </tr>
        </thead>
        <tbody>
          {Object.entries(gate.hosts).map(([hid, row]) => (
            <tr key={hid} className="border-b border-slate-100 last:border-0">
              <td className="px-3 py-2 font-medium">{names[hid] ?? hid.slice(0, 8)}</td>
              {checks.map((c) => {
                const r = row[c];
                return (
                  <td key={c} className="px-3 py-2">
                    {r ? (
                      <Tooltip focusable content={codeText(t, "rollouts.gate.code", r)}>
                        <Badge tone={RESULT_TONE[r.result]}>{t(`rollouts.gate.result.${r.result}`)}</Badge>
                      </Tooltip>
                    ) : <span className="text-slate-400">–</span>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function RolloutRunPage() {
  const { t, ts, errorMessage, locale } = useLanguage();
  const { id } = useParams<{ id: string }>();
  const toast = useToast();
  const readOnly = useReadOnly();
  const [run, setRun] = useState<RolloutRunDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<{ kind: "pause" } | { kind: "abort" } | { kind: "skip"; host: HostRow } | null>(null);
  const [reason, setReason] = useState("");
  const [confirmName, setConfirmName] = useState("");

  const load = useCallback(async () => {
    try {
      setRun(await api.getRolloutRun(id));
      setError("");
    } catch (e) {
      setError(errorMessage(e, "rollouts.loadFailed"));
    }
  }, [id, errorMessage]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!run || !isActiveRun(run.status)) return;
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [run?.status, load]); // eslint-disable-line react-hooks/exhaustive-deps

  async function act(fn: () => Promise<RolloutRunDetail>, okKey: string) {
    setBusy(true);
    try {
      setRun(await fn());
      toast.show(t(okKey), "success");
      setDialog(null);
    } catch (e) {
      toast.show(errorMessage(e, "rollouts.actionFailed"), "danger");
    } finally {
      setBusy(false);
    }
  }

  const when = (value: string | null | undefined) => (value ? new Date(value).toLocaleString(locale) : "");
  const time = (value: string | null | undefined) => (value ? new Date(value).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" }) : "");

  if (error && !run) {
    return <EmptyState icon={Waves} title={t("rollouts.detail.notFound")} description={error} action={<Button href="/rollouts" variant="outline">{t("rollouts.detail.back")}</Button>} />;
  }
  if (!run) return <Skeleton className="h-64" />;

  const active = isActiveRun(run.status);
  const current = run.waves.find((w) => w.index === run.current_wave);
  const recheck = run.progress.gate_recheck_at || run.progress.cluster_recheck_at || run.progress.resume_recheck_at;
  return (
    <div>
      <Link href="/rollouts" className="mb-3 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-900"><ArrowLeft size={14} />{t("rollouts.detail.back")}</Link>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="flex flex-wrap items-center gap-2 text-2xl font-semibold text-slate-900">
          {run.plan_name}
          <Badge tone={RUN_TONE[run.status]}>{t(`rollouts.run.status.${run.status}`)}</Badge>
        </h1>
        {!readOnly && active && (
          <div className="flex flex-wrap gap-2">
            {run.status !== "paused" && <Button variant="outline" disabled={busy} onClick={() => { setReason(""); setDialog({ kind: "pause" }); }}><Pause size={14} />{t("rollouts.actions.pause")}</Button>}
            {run.status === "paused" && <Button disabled={busy} onClick={() => act(() => api.resumeRolloutRun(run.id), "rollouts.actions.resumed")}><Play size={14} />{t("rollouts.actions.resume")}</Button>}
            {run.status === "awaiting_approval" && <Button disabled={busy} onClick={() => act(() => api.approveRolloutWave(run.id), "rollouts.actions.approved")}><CheckCheck size={14} />{t("rollouts.actions.approve")}</Button>}
            <Button variant="dangerSubtle" disabled={busy} onClick={() => { setReason(""); setConfirmName(""); setDialog({ kind: "abort" }); }}><Square size={14} />{t("rollouts.actions.abort")}</Button>
          </div>
        )}
      </div>

      <div className="mb-4 grid gap-2 text-sm text-slate-700 sm:grid-cols-2 lg:grid-cols-4">
        <p>{t("rollouts.col.target")}: <strong>{run.target_version ?? "–"}</strong></p>
        <p>{t(`rollouts.action.${run.action}`)} · {t(`rollouts.trigger.${run.trigger}`)}</p>
        <p>{t("rollouts.col.started")}: {when(run.created_at)}</p>
        <p>{t("rollouts.detail.owner")}: {run.started_by}</p>
      </div>

      {run.status === "paused" && (
        <div role="alert" className="mb-4 rounded-xl border border-flame-200 bg-flame-50 p-4 text-sm text-flame-800">
          <p className="flex items-center gap-2 font-semibold"><AlertTriangle size={16} />{pauseText(t, run.pause_reason)}
            <InfoTip text={t("rollouts.pauseHint")} size={14} /></p>
          {run.pause_detail && <p className="mt-1 break-words">{pauseDetailText(t, run.pause_detail)}</p>}
        </div>
      )}
      {run.status === "cancelled" && run.pause_detail && Array.isArray(run.pause_detail.still_running) && run.pause_detail.still_running.length > 0 && (
        <p className="mb-4 text-sm text-slate-600">{t("rollouts.detail.stillRunning", { hosts: (run.pause_detail.still_running as string[]).join(", ") })}</p>
      )}
      {run.status === "waiting_window" && <p className="mb-4 text-sm text-slate-600">{t("rollouts.detail.waitingWindow")}</p>}
      {run.status === "awaiting_approval" && <p className="mb-4 text-sm text-slate-600">{t("rollouts.detail.awaiting")}</p>}
      {(run.status === "soaking" && run.soak_until) && <p className="mb-4 text-sm text-slate-600">{t("rollouts.detail.soakUntil", { time: time(run.soak_until) })}</p>}
      {["gate", "wave_running", "resuming"].includes(run.status) && recheck && <p className="mb-4 text-sm text-slate-600">{t("rollouts.detail.recheckAt", { time: time(recheck) })}</p>}
      {run.status === "wave_running" && run.progress.blocked_since && <p className="mb-4 text-sm text-slate-600">{t("rollouts.detail.blockedSince", { time: time(run.progress.blocked_since) })}</p>}

      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">{t("rollouts.detail.waves")}</h2>
      <ol className="mb-6 flex flex-wrap gap-2">
        {run.waves.map((w) => (
          <li key={w.index} aria-current={w.index === run.current_wave && active ? "step" : undefined}
            className={`min-w-[9rem] flex-1 rounded-xl border p-3 text-sm ${w.index === run.current_wave && active ? "border-sky-300" : "border-slate-200"}`}>
            <div className="flex items-center justify-between gap-2">
              <strong>{w.index + 1}. {w.label}</strong>
              <Badge tone={WAVE_TONE[w.status] ?? "neutral"}>{t(`rollouts.wave.status.${w.status}`)}</Badge>
            </div>
            {w.spec && <p className="mt-1 text-xs text-slate-500">{w.spec.groups.join(", ")} · {t("rollouts.detail.soak", { minutes: w.spec.soak_minutes })}
              {w.spec.carp_handover && <> · {t("rollouts.detail.handover")}</>}{w.spec.require_approval && w.index > 0 && <> · {t("rollouts.detail.approval")}</>}</p>}
            {w.approved_by && <p className="mt-1 text-xs text-slate-500">{t("rollouts.detail.approvedBy", { user: w.approved_by })}</p>}
          </li>
        ))}
      </ol>

      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">{t("rollouts.detail.hosts")}</h2>
      <Card className="mb-6 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3 font-medium">{t("rollouts.col.host")}</th>
              <th className="px-4 py-3 font-medium">{t("rollouts.col.wave")}</th>
              <th className="px-4 py-3 font-medium">{t("rollouts.col.status")}</th>
              <th className="px-4 py-3 font-medium">{t("rollouts.col.version")}</th>
              <th className="px-4 py-3 font-medium">{t("rollouts.col.step")}</th>
              <th className="px-4 py-3 font-medium">{t("rollouts.col.note")}</th>
              <th className="px-4 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
            </tr>
          </thead>
          <tbody>
            {run.hosts.map((h) => (
              <tr key={h.id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-3 font-medium">{h.host_id ? <Link className="text-brand-600 hover:underline" href={`/hosts/${h.host_id}`}>{h.host_name}</Link> : h.host_name}
                  {h.cluster_name && <div className="text-xs font-normal text-slate-500">{h.cluster_name}</div>}</td>
                <td className="px-4 py-3">{run.waves.find((w) => w.index === h.wave_index)?.label}</td>
                <td className="px-4 py-3"><Badge tone={HOST_TONE[h.status]}>{t(`rollouts.host.status.${h.status}`)}</Badge></td>
                <td className="px-4 py-3 whitespace-nowrap">{h.version_before ?? "–"}{h.version_after && <> → <strong>{h.version_after}</strong></>}</td>
                <td className="px-4 py-3 text-xs">{h.schedule?.phase ? t(`maint.phase.${h.schedule.phase}`) : h.schedule ? t(`maint.scheduleStatus.${h.schedule.status === "succeeded" ? "completed" : h.schedule.status}`) : ""}
                  {h.schedule?.carp_handover && <div><Badge tone="info">{t("maint.handoverBadge")}</Badge></div>}</td>
                <td className="max-w-xs px-4 py-3 text-xs text-slate-600 break-words">{h.detail ? ts(codeText(t, "rollouts.hostNote", h.detail)) : ""}</td>
                <td className="px-4 py-3 text-right">
                  {!readOnly && active && h.host_id && ["queued", "blocked", "failed"].includes(h.status) && (
                    <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => { setReason(""); setDialog({ kind: "skip", host: h }); }}><SkipForward size={13} />{t("rollouts.actions.skip")}</Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <h2 className="mb-2 flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-500">{t("rollouts.gate.title")}<InfoTip text={t("rollouts.gate.hint")} size={13} /></h2>
      <div className="space-y-4">
        {run.progress.resume_gate && Object.keys(run.progress.resume_gate.hosts).length > 0 && (
          <Card className="p-4"><GateMatrix gate={run.progress.resume_gate} run={run} title={t("rollouts.gate.resume")} /></Card>
        )}
        {active && run.progress.cluster_gate && (
          <Card className="p-4"><GateMatrix gate={run.progress.cluster_gate} run={run} title={t("rollouts.gate.cluster")} /></Card>
        )}
        {[...run.waves].reverse().filter((w) => w.gate_result).map((w) => (
          <Card key={w.index} className="p-4">
            <GateMatrix gate={w.gate_result as RolloutGate} run={run} title={`${w.index + 1}. ${w.label} · ${t("rollouts.detail.attempt", { n: w.gate_attempts })}`} />
          </Card>
        ))}
        {!run.waves.some((w) => w.gate_result) && !run.progress.resume_gate && <p className="text-sm text-slate-500">{t("rollouts.detail.noGate")}{current ? ` (${current.label})` : ""}</p>}
      </div>

      {dialog?.kind === "pause" && (
        <Modal title={t("rollouts.actions.pause")} onClose={() => !busy && setDialog(null)}>
          <div className="space-y-4 text-sm">
            <label className="block"><span className="mb-1 block">{t("rollouts.actions.pauseReason")}</span>
              <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} /></label>
            <div className="flex justify-end"><Button loading={busy} onClick={() => act(() => api.pauseRolloutRun(run.id, reason), "rollouts.actions.paused")}><Pause size={14} />{t("rollouts.actions.pause")}</Button></div>
          </div>
        </Modal>
      )}
      {dialog?.kind === "skip" && (
        <Modal title={t("rollouts.actions.skipTitle", { host: dialog.host.host_name })} onClose={() => !busy && setDialog(null)}>
          <div className="space-y-4 text-sm">
            <p>{t("rollouts.actions.skipHint")}</p>
            <label className="block"><span className="mb-1 block">{t("rollouts.actions.reason")}</span>
              <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} /></label>
            <div className="flex justify-end"><Button loading={busy} disabled={reason.trim().length < 3}
              onClick={() => act(() => api.skipRolloutHost(run.id, dialog.host.host_id as string, reason), "rollouts.actions.skipped")}>{t("rollouts.actions.skip")}</Button></div>
          </div>
        </Modal>
      )}
      {dialog?.kind === "abort" && (
        <Modal title={t("rollouts.actions.abortTitle")} onClose={() => !busy && setDialog(null)}>
          <div className="space-y-4 text-sm">
            <p className="flex items-start gap-2 text-flame-800"><AlertTriangle size={16} className="mt-0.5 shrink-0" />{t("rollouts.actions.abortHint")}</p>
            <label className="block"><span className="mb-1 block">{t("rollouts.actions.reason")}</span>
              <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} /></label>
            <label className="block"><span className="mb-1 block">{t("rollouts.actions.confirmPlan")}</span>
              <Input autoComplete="off" spellCheck={false} value={confirmName} placeholder={run.plan_name} onChange={(e) => setConfirmName(e.target.value)} /></label>
            <div className="flex justify-end"><Button variant="danger" loading={busy} disabled={reason.trim().length < 3 || confirmName !== run.plan_name}
              onClick={() => act(() => api.abortRolloutRun(run.id, reason, confirmName), "rollouts.actions.aborted")}>{t("rollouts.actions.abort")}</Button></div>
          </div>
        </Modal>
      )}
    </div>
  );
}
