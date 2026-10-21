"use client";

import { MaintenanceWindowEditor } from "@/components/MaintenanceWindowEditor";
import { RolloutPlanEditor } from "@/components/RolloutPlanEditor";
import { RolloutStatusBanner } from "@/components/RolloutStatusBanner";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, DeviceGroup, MaintenanceWindow, Occurrence, RolloutPlan, RolloutPreview, RolloutRunSummary } from "@/lib/api";
import { codeText, RUN_TONE, windowSummary } from "@/lib/rollouts";
import { useReadOnly } from "@/lib/useReadOnly";
import { AlertTriangle, CalendarClock, ListOrdered, Pencil, Play, Plus, Power, PowerOff, Trash2, Waves } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

type Tab = "runs" | "plans" | "windows";

export default function RolloutsPage() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const router = useRouter();
  const readOnly = useReadOnly();
  const [tab, setTab] = useState<Tab>("runs");
  const [runs, setRuns] = useState<RolloutRunSummary[] | null>(null);
  const [plans, setPlans] = useState<RolloutPlan[] | null>(null);
  const [windows, setWindows] = useState<MaintenanceWindow[] | null>(null);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [error, setError] = useState("");
  const [editPlan, setEditPlan] = useState<RolloutPlan | "new" | null>(null);
  const [editWindow, setEditWindow] = useState<MaintenanceWindow | "new" | null>(null);
  const [preview, setPreview] = useState<{ mode: "start" | "activate"; data: RolloutPreview } | null>(null);
  const [move, setMove] = useState<{ window: MaintenanceWindow; occ: Occurrence; to: string } | null>(null);

  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("tab");
    if (wanted === "plans" || wanted === "windows") setTab(wanted);
  }, []);

  async function load() {
    try {
      const [r, p, w, g] = await Promise.all([api.listRolloutRuns(), api.listRolloutPlans(), api.listMaintenanceWindows(), api.listGroups()]);
      setRuns(r);
      setPlans(p);
      setWindows(w);
      setGroups(g);
      setError("");
    } catch (e) {
      setError(errorMessage(e, "rollouts.loadFailed"));
    }
  }
  useEffect(() => {
    load();
    const timer = setInterval(() => { if (document.visibilityState === "visible") api.listRolloutRuns().then(setRuns).catch(() => {}); }, 15000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function changeTab(next: string) {
    setTab(next as Tab);
    window.history.replaceState(null, "", next === "runs" ? "/rollouts" : `/rollouts?tab=${next}`);
  }

  async function act(fn: () => Promise<unknown>, okKey: string) {
    try {
      await fn();
      toast.show(t(okKey), "success");
      await load();
    } catch (e) {
      toast.show(errorMessage(e, "rollouts.actionFailed"), "danger");
    }
  }

  async function openPreview(plan: RolloutPlan, mode: "start" | "activate") {
    try {
      setPreview({ mode, data: await api.previewRolloutPlan(plan.id) });
    } catch (e) {
      toast.show(errorMessage(e, "rollouts.actionFailed"), "danger");
    }
  }

  const when = (value: string | null | undefined) => (value ? new Date(value).toLocaleString(locale) : "");

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h1 className="flex items-center gap-2 text-2xl font-semibold text-slate-900">
          {t("rollouts.title")}
          <InfoTip text={t("rollouts.intro")} size={18} />
        </h1>
        {!readOnly && tab === "plans" && <Button onClick={() => setEditPlan("new")}><Plus size={16} />{t("rollouts.plan.new")}</Button>}
        {!readOnly && tab === "windows" && <Button onClick={() => setEditWindow("new")}><Plus size={16} />{t("mwindows.new")}</Button>}
      </div>
      <RolloutStatusBanner />
      <Tabs className="mb-4" active={tab} onChange={changeTab} items={[
        { key: "runs", label: t("rollouts.tab.runs") },
        { key: "plans", label: t("rollouts.tab.plans") },
        { key: "windows", label: t("rollouts.tab.windows") },
      ]} />
      {error && <p role="alert" className="mb-4 text-sm text-red-600">{error}</p>}

      {tab === "runs" && (runs === null ? <Skeleton className="h-32" /> : runs.length === 0 ? (
        <EmptyState icon={Waves} title={t("rollouts.noRuns")} description={t("rollouts.noRunsHint")} />
      ) : (
        <Card className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-3 font-medium">{t("rollouts.col.plan")}</th>
                <th className="px-4 py-3 font-medium">{t("rollouts.col.status")}</th>
                <th className="px-4 py-3 font-medium">{t("rollouts.col.wave")}</th>
                <th className="px-4 py-3 font-medium">{t("rollouts.col.target")}</th>
                <th className="px-4 py-3 font-medium">{t("rollouts.col.hosts")}</th>
                <th className="px-4 py-3 font-medium">{t("rollouts.col.started")}</th>
                <th className="px-4 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-4 py-3 font-medium text-slate-900">{r.plan_name}<div className="text-xs font-normal text-slate-500">{t(`rollouts.action.${r.action}`)}</div></td>
                  <td className="px-4 py-3"><Badge tone={RUN_TONE[r.status]}>{t(`rollouts.run.status.${r.status}`)}</Badge></td>
                  <td className="px-4 py-3 whitespace-nowrap">{t("rollouts.waveOf", { current: Math.min(r.current_wave + 1, r.wave_count), total: r.wave_count })} {r.current_wave_label && <span className="text-slate-500">({r.current_wave_label})</span>}</td>
                  <td className="px-4 py-3">{r.target_version ?? "–"}</td>
                  <td className="px-4 py-3 whitespace-nowrap text-xs">
                    {Object.entries(r.host_counts).map(([s, n]) => <span key={s} className="mr-2">{t(`rollouts.host.status.${s}`)}: {n}</span>)}
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-600">{when(r.created_at)}<div>{t("rollouts.by", { user: r.started_by })} · {t(`rollouts.trigger.${r.trigger}`)}</div></td>
                  <td className="px-4 py-3 text-right"><Button variant="outline" href={`/rollouts/runs/${r.id}`}>{t("rollouts.details")}</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ))}

      {tab === "plans" && (plans === null ? <Skeleton className="h-32" /> : plans.length === 0 ? (
        <EmptyState icon={ListOrdered} title={t("rollouts.plan.none")} description={t("rollouts.plan.noneHint")}
          action={!readOnly && <Button onClick={() => setEditPlan("new")}><Plus size={16} />{t("rollouts.plan.new")}</Button>} />
      ) : (
        <div className="space-y-3">
          {plans.map((p) => (
            <Card key={p.id} className="p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="flex flex-wrap items-center gap-2 font-semibold text-slate-900">
                    {p.name}
                    <Badge tone={p.action === "major_update" ? "accent" : "info"}>{t(`rollouts.action.${p.action}`)}</Badge>
                    {p.auto_start && (p.active ? (p.fingerprint_ok
                      ? <Badge tone="success">{t("rollouts.plan.active")}</Badge>
                      : <Tooltip focusable content={t("rollouts.plan.changedTip")}><Badge tone="warning">{t("rollouts.plan.changed")}</Badge></Tooltip>)
                      : <Badge tone="neutral">{t("rollouts.plan.inactive")}</Badge>)}
                    {p.errors.length > 0 && <Tooltip focusable content={<span className="block space-y-1">{p.errors.map((e, i) => <span key={i} className="block">{codeText(t, "rollouts.issue", e)}</span>)}</span>}><Badge tone="danger">{t("rollouts.plan.errors", { count: p.errors.length })}</Badge></Tooltip>}
                    {p.warnings.length > 0 && <Tooltip focusable content={<span className="block space-y-1">{p.warnings.map((e, i) => <span key={i} className="block">{codeText(t, "rollouts.issue", e)}</span>)}</span>}><Badge tone="warning">{t("rollouts.plan.warnings", { count: p.warnings.length })}</Badge></Tooltip>}
                  </h2>
                  {p.description && <p className="mt-1 text-sm text-slate-600">{p.description}</p>}
                  <ol className="mt-2 flex flex-wrap items-center gap-1 text-sm">
                    {p.waves.map((w, i) => (
                      <li key={w.id} className="flex items-center gap-1">
                        {i > 0 && <span aria-hidden className="text-slate-400">→</span>}
                        <span className="rounded-lg border border-slate-200 px-2 py-0.5">
                          <strong>{w.label}</strong> <span className="text-slate-500">{w.groups.join(", ")} · {t("rollouts.plan.hostsCount", { count: w.host_count })}</span>
                        </span>
                      </li>
                    ))}
                  </ol>
                  <p className="mt-2 text-xs text-slate-500">
                    {p.window ? <>{t("rollouts.plan.window")}: {p.window.name}{p.window.next[0] && <> · {t("rollouts.plan.nextOccurrence", { time: when(p.window.next[0].start) })}</>}</> : t("rollouts.plan.noWindow")}
                    {p.active && p.activated_by && <> · {t("rollouts.plan.activatedBy", { user: p.activated_by })}</>}
                  </p>
                  {p.auto_start_error && (
                    <p className="mt-1 flex items-center gap-1 text-xs text-flame-700"><AlertTriangle size={13} />
                      {t("rollouts.plan.lastAutoStartError", { time: when(p.auto_start_error.occurrence), reason: codeText(t, "rollouts.issue", { code: p.auto_start_error.code, params: p.auto_start_error.params ?? {} }) })}
                    </p>
                  )}
                </div>
                {!readOnly && (
                  <div className="flex flex-wrap gap-2">
                    <Button onClick={() => openPreview(p, "start")}><Play size={14} />{t("rollouts.plan.previewStart")}</Button>
                    {p.auto_start && (p.active
                      ? <Button variant="outline" onClick={() => act(() => api.deactivateRolloutPlan(p.id), "rollouts.plan.deactivated")}><PowerOff size={14} />{t("rollouts.plan.deactivate")}</Button>
                      : <Button variant="outline" onClick={() => openPreview(p, "activate")}><Power size={14} />{t("rollouts.plan.activate")}</Button>)}
                    {p.auto_start && p.active && !p.fingerprint_ok && <Button variant="outline" onClick={() => openPreview(p, "activate")}><Power size={14} />{t("rollouts.plan.activate")}</Button>}
                    <Button variant="outline" aria-label={t("rollouts.plan.edit")} onClick={() => setEditPlan(p)}><Pencil size={14} /></Button>
                    <Button variant="dangerSubtle" aria-label={t("common.delete")} onClick={() => confirm(t("rollouts.plan.deleteConfirm", { name: p.name })) && act(() => api.deleteRolloutPlan(p.id), "rollouts.plan.deleted")}><Trash2 size={14} /></Button>
                  </div>
                )}
              </div>
            </Card>
          ))}
        </div>
      ))}

      {tab === "windows" && (windows === null ? <Skeleton className="h-32" /> : windows.length === 0 ? (
        <EmptyState icon={CalendarClock} title={t("mwindows.none")} description={t("mwindows.noneHint")}
          action={!readOnly && <Button onClick={() => setEditWindow("new")}><Plus size={16} />{t("mwindows.new")}</Button>} />
      ) : (
        <div className="space-y-3">
          {windows.map((w) => (
            <Card key={w.id} className="p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="flex flex-wrap items-center gap-2 font-semibold text-slate-900">
                    {w.name}
                    {w.open_now && <Badge tone="success">{t("mwindows.openNow")}</Badge>}
                    {!w.enabled && <Badge tone="neutral">{t("mwindows.off")}</Badge>}
                  </h2>
                  <p className="mt-1 text-sm text-slate-600">{windowSummary(t, w)} · {t("mwindows.cutoffSummary", { minutes: w.start_cutoff_minutes })}</p>
                  {w.used_by.length > 0 && <p className="mt-1 text-xs text-slate-500">{t("mwindows.usedBy", { plans: w.used_by.map((p) => p.name).join(", ") })}</p>}
                </div>
                {!readOnly && (
                  <div className="flex gap-2">
                    <Button variant="outline" aria-label={t("mwindows.edit")} onClick={() => setEditWindow(w)}><Pencil size={14} /></Button>
                    <Button variant="dangerSubtle" aria-label={t("common.delete")} onClick={() => confirm(t("mwindows.deleteConfirm", { name: w.name })) && act(() => api.deleteMaintenanceWindow(w.id), "mwindows.deleted")}><Trash2 size={14} /></Button>
                  </div>
                )}
              </div>
              {w.next.length > 0 && (
                <div className="mt-3">
                  <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{t("mwindows.next")}</h3>
                  <ul className="space-y-1 text-sm">
                    {w.next.map((o) => {
                      const exc = w.exceptions.find((e) => e.occurrence_date === o.local_date);
                      return (
                        <li key={`${o.local_date}-${o.start}`} className="flex flex-wrap items-center gap-2">
                          <span className={o.skipped ? "text-slate-400 line-through" : ""}>{when(o.start)} – {new Date(o.end).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}</span>
                          {o.skipped && <Badge tone="neutral">{t("mwindows.skipped")}</Badge>}
                          {o.moved && <Badge tone="info">{t("mwindows.moved")}</Badge>}
                          {exc?.reason && <span className="text-xs text-slate-500">{exc.reason}</span>}
                          {!readOnly && !exc && (
                            <span className="flex gap-1">
                              <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => act(() => api.addWindowException(w.id, { occurrence_date: o.local_date, kind: "skip" }), "mwindows.exceptionSaved")}>{t("mwindows.skip")}</Button>
                              <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setMove({ window: w, occ: o, to: "" })}>{t("mwindows.move")}</Button>
                            </span>
                          )}
                          {!readOnly && exc && <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => act(() => api.deleteWindowException(w.id, exc.id), "mwindows.exceptionSaved")}>{t("mwindows.removeException")}</Button>}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </Card>
          ))}
        </div>
      ))}

      {editPlan && (
        <RolloutPlanEditor plan={editPlan === "new" ? null : editPlan} groups={groups} windows={windows ?? []} onClose={() => setEditPlan(null)}
          onSaved={async () => { setEditPlan(null); toast.show(t("rollouts.plan.saved"), "success"); await load(); }} />
      )}
      {editWindow && (
        <MaintenanceWindowEditor window={editWindow === "new" ? null : editWindow} onClose={() => setEditWindow(null)}
          onSaved={async () => { setEditWindow(null); toast.show(t("mwindows.saved"), "success"); await load(); }} />
      )}
      {move && (
        <Modal title={`${t("mwindows.move")}: ${when(move.occ.start)}`} onClose={() => setMove(null)}>
          <div className="space-y-4 text-sm">
            <label className="block"><span className="mb-1 block font-medium text-slate-700">{t("mwindows.moveTo")}</span>
              <Input type="datetime-local" value={move.to} onChange={(e) => setMove({ ...move, to: e.target.value })} /></label>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setMove(null)}>{t("common.cancel")}</Button>
              <Button disabled={!move.to} onClick={async () => {
                const m = move;
                setMove(null);
                await act(() => api.addWindowException(m.window.id, { occurrence_date: m.occ.local_date, kind: "move", moved_start: new Date(m.to).toISOString() }), "mwindows.exceptionSaved");
              }}>{t("rollouts.plan.save")}</Button>
            </div>
          </div>
        </Modal>
      )}
      {preview && <PreviewModal preview={preview} onClose={() => setPreview(null)} onDone={async (runId) => {
        setPreview(null);
        if (runId) router.push(`/rollouts/runs/${runId}`);
        else await load();
      }} />}
    </div>
  );
}

function PreviewModal({ preview, onClose, onDone }: { preview: { mode: "start" | "activate"; data: RolloutPreview }; onClose: () => void; onDone: (runId?: string) => void }) {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const { data, mode } = preview;
  const [name, setName] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const plan = data.plan;

  async function go() {
    setBusy(true);
    setError("");
    try {
      if (mode === "start") {
        const run = await api.startRolloutPlan(plan.id, data.preview_token, name);
        toast.show(t("rollouts.plan.started"), "success");
        onDone(run.id);
      } else {
        await api.activateRolloutPlan(plan.id, data.preview_token);
        toast.show(t("rollouts.plan.activated"), "success");
        onDone();
      }
    } catch (e) {
      setError(errorMessage(e, "rollouts.actionFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={t("rollouts.plan.previewTitle", { name: plan.name })} onClose={() => !busy && onClose()}>
      <div className="space-y-4 text-sm">
        <p><Badge tone={plan.action === "major_update" ? "accent" : "info"}>{t(`rollouts.action.${plan.action}`)}</Badge>
          {data.window && <span className="ml-2 text-slate-600">{t("rollouts.plan.window")}: {data.window.name}{data.window.next[0] && <> · {t("rollouts.plan.nextOccurrence", { time: new Date(data.window.next[0].start).toLocaleString(locale) })}</>}</span>}</p>
        <ol className="space-y-2">
          {data.waves.map((w) => (
            <li key={w.index} className="rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <strong>{w.index + 1}. {w.label}</strong>
                <span className="text-xs text-slate-500">{t("rollouts.detail.soak", { minutes: w.soak_minutes })}</span>
                {w.carp_handover && <Badge tone="info">{t("rollouts.detail.handover")}</Badge>}
                {w.require_approval && w.index > 0 && <Badge tone="accent">{t("rollouts.detail.approval")}</Badge>}
              </div>
              <p className="mt-1 text-slate-600">
                {w.hosts.length === 0 ? t("rollouts.plan.emptyWave") : w.hosts.map((h) => `${h.name}${h.cluster_name ? ` (${h.cluster_name})` : ""}${h.enabled ? "" : ` – ${t("rollouts.plan.disabledHost")}`}`).join(", ")}
              </p>
            </li>
          ))}
        </ol>
        {data.errors.length > 0 && <ul role="alert" className="space-y-1 rounded-lg bg-red-50 p-3 text-red-700">{data.errors.map((e, i) => <li key={i}>{codeText(t, "rollouts.issue", e)}</li>)}</ul>}
        {data.warnings.length > 0 && <ul className="space-y-1 rounded-lg bg-flame-50 p-3 text-flame-800">{data.warnings.map((e, i) => <li key={i} className="flex gap-1"><AlertTriangle size={14} className="mt-0.5 shrink-0" />{codeText(t, "rollouts.issue", e)}</li>)}</ul>}
        {data.can_start ? (
          <>
            {mode === "start" && (
              <label className="block"><span className="mb-1 block">{t("rollouts.actions.confirmPlan")}</span>
                <Input autoComplete="off" spellCheck={false} aria-label={t("rollouts.actions.confirmPlan")} value={name} placeholder={plan.name} onChange={(e) => setName(e.target.value)} /></label>
            )}
            <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={ack} onChange={(e) => setAck(e.target.checked)} />{t("rollouts.plan.unattended")}</label>
            {error && <p role="alert" className="text-red-600">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={onClose} disabled={busy}>{t("common.cancel")}</Button>
              <Button variant={mode === "start" ? "danger" : "primary"} loading={busy} disabled={!ack || (mode === "start" && name !== plan.name)} onClick={go}>
                {mode === "start" ? t("rollouts.plan.start") : t("rollouts.plan.activate")}
              </Button>
            </div>
          </>
        ) : <p className="text-red-700">{t("rollouts.plan.cannotStart")}</p>}
        {data.can_start || <div className="flex justify-end"><Link href="#" onClick={(e) => { e.preventDefault(); onClose(); }} className="text-brand-600">{t("common.close")}</Link></div>}
      </div>
    </Modal>
  );
}
