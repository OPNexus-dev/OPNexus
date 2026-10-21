"use client";

import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, DeviceGroup, MaintenanceWindow, RolloutCheck, RolloutPlan, RolloutPlanWrite, RolloutWaveWrite } from "@/lib/api";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

const OPTIONAL_CHECKS: RolloutCheck[] = ["carp", "gateways", "services", "compliance"];
const DEFAULT_CHECKS: Partial<Record<RolloutCheck, boolean>> = { carp: true, gateways: true, services: true, compliance: false };

function newWave(index: number): RolloutWaveWrite {
  return { label: ["canary", "pilot", "prod"][index] ?? `${index + 1}`, group_ids: [], soak_minutes: 30, carp_handover: index > 0, require_approval: false };
}

export function RolloutPlanEditor({ plan, groups, windows, onClose, onSaved }: {
  plan: RolloutPlan | null; groups: DeviceGroup[]; windows: MaintenanceWindow[]; onClose: () => void; onSaved: (p: RolloutPlan) => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [form, setForm] = useState<RolloutPlanWrite>(() => plan ? {
    name: plan.name, description: plan.description, action: plan.action, window_id: plan.window_id, auto_start: plan.auto_start,
    gate_checks: { ...DEFAULT_CHECKS, ...plan.gate_checks }, compliance_mode: plan.compliance_mode,
    waves: plan.waves.map((w) => ({ label: w.label, group_ids: w.group_ids, soak_minutes: w.soak_minutes, carp_handover: w.carp_handover, require_approval: w.require_approval })),
  } : { name: "", description: "", action: "update", window_id: null, auto_start: false, gate_checks: DEFAULT_CHECKS, compliance_mode: "warn", waves: [newWave(0)] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (patch: Partial<RolloutPlanWrite>) => setForm((f) => ({ ...f, ...patch }));
  const setWave = (i: number, patch: Partial<RolloutWaveWrite>) => set({ waves: form.waves.map((w, j) => (j === i ? { ...w, ...patch } : w)) });
  const moveWave = (i: number, dir: -1 | 1) => {
    const waves = [...form.waves];
    [waves[i], waves[i + dir]] = [waves[i + dir], waves[i]];
    set({ waves });
  };
  const autoStartPossible = form.action === "update" && !!form.window_id;

  async function save() {
    setBusy(true);
    setError("");
    const body = { ...form, auto_start: autoStartPossible && form.auto_start };
    try {
      onSaved(plan ? await api.updateRolloutPlan(plan.id, body) : await api.createRolloutPlan(body));
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  const valid = form.name.trim() && form.waves.length > 0 && form.waves.every((w) => w.label.trim() && w.group_ids.length > 0);
  return (
    <Modal title={plan ? t("rollouts.plan.edit") : t("rollouts.plan.new")} onClose={() => !busy && onClose()}>
      <div className="space-y-4 text-sm">
        <div className="grid gap-4 sm:grid-cols-2">
          <FieldGroup label={t("rollouts.plan.name")}>
            <Input value={form.name} maxLength={120} onChange={(e) => set({ name: e.target.value })} />
          </FieldGroup>
          <FieldGroup label={t("rollouts.plan.action")}>
            <Select value={form.action} onChange={(e) => set({ action: e.target.value as RolloutPlanWrite["action"] })}>
              <option value="update">{t("rollouts.action.update")}</option>
              <option value="major_update">{t("rollouts.action.major_update")}</option>
            </Select>
          </FieldGroup>
        </div>
        <FieldGroup label={t("rollouts.plan.description")}>
          <Input value={form.description} maxLength={2000} onChange={(e) => set({ description: e.target.value })} />
        </FieldGroup>
        <FieldGroup label={t("rollouts.plan.window")}>
          <Select value={form.window_id ?? ""} onChange={(e) => set({ window_id: e.target.value || null })}>
            <option value="">{t("rollouts.plan.noWindow")}</option>
            {windows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
          </Select>
        </FieldGroup>
        <label className={`flex items-start gap-2 ${autoStartPossible ? "" : "opacity-60"}`}>
          <input type="checkbox" className="mt-1" disabled={!autoStartPossible} checked={autoStartPossible && form.auto_start}
            onChange={(e) => set({ auto_start: e.target.checked })} />
          <span>{t("rollouts.plan.autoStart")} <InfoTip text={t("rollouts.plan.autoStartHint")} size={13} /></span>
        </label>

        <fieldset className="space-y-3">
          <legend className="mb-1 font-medium text-slate-700">{t("rollouts.plan.waves")}</legend>
          {form.waves.map((wave, i) => (
            <div key={i} className="space-y-3 rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-end gap-2">
                <span className="mb-2 inline-flex h-6 w-6 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold text-slate-700">{i + 1}</span>
                <div className="min-w-[8rem] flex-1">
                  <FieldGroup label={t("rollouts.plan.waveLabel")}>
                    <Input value={wave.label} maxLength={60} onChange={(e) => setWave(i, { label: e.target.value })} />
                  </FieldGroup>
                </div>
                <div className="w-32">
                  <FieldGroup label={t("rollouts.plan.soak")} hint={t("rollouts.plan.soakHint")}>
                    <Input type="number" min={0} max={1440} value={wave.soak_minutes} onChange={(e) => setWave(i, { soak_minutes: Number(e.target.value) })} />
                  </FieldGroup>
                </div>
                <div className="mb-0.5 flex gap-1">
                  <Button variant="ghost" aria-label={t("rollouts.plan.moveUp")} disabled={i === 0} onClick={() => moveWave(i, -1)}><ArrowUp size={14} /></Button>
                  <Button variant="ghost" aria-label={t("rollouts.plan.moveDown")} disabled={i === form.waves.length - 1} onClick={() => moveWave(i, 1)}><ArrowDown size={14} /></Button>
                  <Button variant="dangerSubtle" aria-label={t("rollouts.plan.removeWave")} disabled={form.waves.length === 1}
                    onClick={() => set({ waves: form.waves.filter((_, j) => j !== i) })}><Trash2 size={14} /></Button>
                </div>
              </div>
              <div>
                <p className="mb-1 font-medium text-slate-700">{t("rollouts.plan.groups")}</p>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {groups.map((g) => (
                    <label key={g.id} className="flex items-center gap-1">
                      <input type="checkbox" checked={wave.group_ids.includes(g.id)}
                        onChange={(e) => setWave(i, { group_ids: e.target.checked ? [...wave.group_ids, g.id] : wave.group_ids.filter((x) => x !== g.id) })} />
                      {g.name} <span className="text-xs text-slate-500">({g.host_ids.length})</span>
                    </label>
                  ))}
                </div>
              </div>
              <div className="flex flex-wrap gap-x-6 gap-y-1">
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={wave.carp_handover} onChange={(e) => setWave(i, { carp_handover: e.target.checked })} />
                  {t("rollouts.plan.handover")}
                </label>
                {i > 0 && (
                  <label className="flex items-center gap-2">
                    <input type="checkbox" checked={wave.require_approval} onChange={(e) => setWave(i, { require_approval: e.target.checked })} />
                    {t("rollouts.plan.approval")}
                  </label>
                )}
              </div>
            </div>
          ))}
          <Button variant="outline" disabled={form.waves.length >= 10} onClick={() => set({ waves: [...form.waves, newWave(form.waves.length)] })}>
            <Plus size={14} />{t("rollouts.plan.addWave")}
          </Button>
        </fieldset>

        <fieldset>
          <legend className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("rollouts.plan.checks")}<InfoTip text={t("rollouts.plan.checksHint")} size={13} /></legend>
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            {OPTIONAL_CHECKS.map((c) => (
              <label key={c} className="flex items-center gap-2">
                <input type="checkbox" checked={!!form.gate_checks[c]} onChange={(e) => set({ gate_checks: { ...form.gate_checks, [c]: e.target.checked } })} />
                {t(`rollouts.gate.check.${c}`)}
              </label>
            ))}
          </div>
          {form.gate_checks.compliance && (
            <div className="mt-2 max-w-xs">
              <FieldGroup label={t("rollouts.plan.complianceMode")}>
                <Select value={form.compliance_mode} onChange={(e) => set({ compliance_mode: e.target.value as "warn" | "fail" })}>
                  <option value="warn">{t("rollouts.plan.complianceWarn")}</option>
                  <option value="fail">{t("rollouts.plan.complianceFail")}</option>
                </Select>
              </FieldGroup>
            </div>
          )}
        </fieldset>
        {error && <p role="alert" className="text-red-600">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>{t("common.cancel")}</Button>
          <Button onClick={save} loading={busy} disabled={!valid}>{t("rollouts.plan.save")}</Button>
        </div>
      </div>
    </Modal>
  );
}
