"use client";

import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, MaintenanceWindow, MaintenanceWindowWrite } from "@/lib/api";
import { useMemo, useState } from "react";

const EMPTY: MaintenanceWindowWrite = {
  name: "", timezone: "Europe/Berlin", recurrence: "weekly", weekdays: [6], month_mode: null, month_nth: null,
  month_weekday: null, month_day: null, start_time: "02:00", duration_minutes: 180, start_cutoff_minutes: 60, enabled: true,
};

function timeZones(): string[] {
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
    const zones = intl.supportedValuesOf?.("timeZone") ?? [];
    return zones.includes("UTC") ? zones : ["UTC", ...zones];
  } catch {
    return ["UTC", "Europe/Berlin"];
  }
}

export function MaintenanceWindowEditor({ window, onClose, onSaved }: {
  window: MaintenanceWindow | null; onClose: () => void; onSaved: (w: MaintenanceWindow) => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [form, setForm] = useState<MaintenanceWindowWrite>(() => (window ? {
    name: window.name, timezone: window.timezone, recurrence: window.recurrence, weekdays: window.weekdays,
    month_mode: window.month_mode, month_nth: window.month_nth, month_weekday: window.month_weekday, month_day: window.month_day,
    start_time: window.start_time, duration_minutes: window.duration_minutes, start_cutoff_minutes: window.start_cutoff_minutes,
    enabled: window.enabled,
  } : EMPTY));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const zones = useMemo(timeZones, []);
  const set = (patch: Partial<MaintenanceWindowWrite>) => setForm((f) => ({ ...f, ...patch }));

  async function save() {
    setBusy(true);
    setError("");
    const body: MaintenanceWindowWrite = form.recurrence === "weekly"
      ? { ...form, month_mode: null, month_nth: null, month_weekday: null, month_day: null }
      : form.month_mode === "day"
        ? { ...form, weekdays: [], month_nth: null, month_weekday: null }
        : { ...form, weekdays: [], month_mode: "nth_weekday", month_nth: form.month_nth ?? 1, month_weekday: form.month_weekday ?? 6, month_day: null };
    try {
      const saved = window ? await api.updateMaintenanceWindow(window.id, body) : await api.createMaintenanceWindow(body);
      onSaved(saved);
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={window ? t("mwindows.edit") : t("mwindows.new")} onClose={() => !busy && onClose()}>
      <div className="space-y-4 text-sm">
        <FieldGroup label={t("mwindows.name")}>
          <Input value={form.name} maxLength={120} onChange={(e) => set({ name: e.target.value })} />
        </FieldGroup>
        <div className="grid gap-4 sm:grid-cols-2">
          <FieldGroup label={t("mwindows.timezone")}>
            <Select value={form.timezone} onChange={(e) => set({ timezone: e.target.value })}>
              {zones.map((z) => <option key={z} value={z}>{z}</option>)}
            </Select>
          </FieldGroup>
          <FieldGroup label={t("mwindows.recurrence")}>
            <Select value={form.recurrence} onChange={(e) => set({ recurrence: e.target.value as "weekly" | "monthly",
              month_mode: e.target.value === "monthly" ? (form.month_mode ?? "nth_weekday") : null })}>
              <option value="weekly">{t("mwindows.weekly")}</option>
              <option value="monthly">{t("mwindows.monthly")}</option>
            </Select>
          </FieldGroup>
        </div>
        {form.recurrence === "weekly" ? (
          <fieldset>
            <legend className="mb-1 font-medium text-slate-700">{t("mwindows.weekdays")}</legend>
            <div className="flex flex-wrap gap-3">
              {[0, 1, 2, 3, 4, 5, 6].map((d) => (
                <label key={d} className="flex items-center gap-1">
                  <input type="checkbox" checked={form.weekdays.includes(d)}
                    onChange={(e) => set({ weekdays: e.target.checked ? [...form.weekdays, d].sort() : form.weekdays.filter((x) => x !== d) })} />
                  {t(`mwindows.dayName.${d}`)}
                </label>
              ))}
            </div>
          </fieldset>
        ) : (
          <div className="grid gap-4 sm:grid-cols-3">
            <FieldGroup label={t("mwindows.monthMode")}>
              <Select value={form.month_mode ?? "nth_weekday"} onChange={(e) => set({ month_mode: e.target.value as "nth_weekday" | "day" })}>
                <option value="nth_weekday">{t("mwindows.nthWeekday")}</option>
                <option value="day">{t("mwindows.dayOfMonth")}</option>
              </Select>
            </FieldGroup>
            {form.month_mode === "day" ? (
              <FieldGroup label={t("mwindows.day")}>
                <Input type="number" min={1} max={28} value={form.month_day ?? 1} onChange={(e) => set({ month_day: Number(e.target.value) })} />
              </FieldGroup>
            ) : (
              <>
                <FieldGroup label={t("mwindows.nthWeekday")}>
                  <Select value={String(form.month_nth ?? 1)} onChange={(e) => set({ month_nth: Number(e.target.value) })}>
                    {[1, 2, 3, 4, -1].map((n) => <option key={n} value={n}>{t(`mwindows.nth.${n}`)}</option>)}
                  </Select>
                </FieldGroup>
                <FieldGroup label={t("mwindows.weekdays")}>
                  <Select value={String(form.month_weekday ?? 6)} onChange={(e) => set({ month_weekday: Number(e.target.value) })}>
                    {[0, 1, 2, 3, 4, 5, 6].map((d) => <option key={d} value={d}>{t(`mwindows.dayLong.${d}`)}</option>)}
                  </Select>
                </FieldGroup>
              </>
            )}
          </div>
        )}
        <div className="grid gap-4 sm:grid-cols-3">
          <FieldGroup label={t("mwindows.startTime")}>
            <Input type="time" value={form.start_time} onChange={(e) => set({ start_time: e.target.value })} />
          </FieldGroup>
          <FieldGroup label={t("mwindows.duration")}>
            <Input type="number" min={15} max={1440} value={form.duration_minutes} onChange={(e) => set({ duration_minutes: Number(e.target.value) })} />
          </FieldGroup>
          <label className="block">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("mwindows.cutoff")}<InfoTip text={t("mwindows.cutoffHint")} size={13} /></span>
            <Input type="number" min={5} max={1440} value={form.start_cutoff_minutes} onChange={(e) => set({ start_cutoff_minutes: Number(e.target.value) })} />
          </label>
        </div>
        <p className="flex items-center gap-1 text-xs text-slate-500">{t("mwindows.dstHint")}</p>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          {t("mwindows.enabled")}
        </label>
        {error && <p role="alert" className="text-red-600">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>{t("common.cancel")}</Button>
          <Button onClick={save} loading={busy} disabled={!form.name.trim()}>{t("rollouts.plan.save")}</Button>
        </div>
      </div>
    </Modal>
  );
}
