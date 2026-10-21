"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, GuiAccessSettings } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** Switch and limits for opening a firewall's own web interface through OPNexus. Admin only: renders nothing for other roles. */
export function GuiAccessSection() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const [cfg, setCfg] = useState<GuiAccessSettings | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [idle, setIdle] = useState(15);
  const [absolute, setAbsolute] = useState(60);
  const [slotCount, setSlotCount] = useState(4);

  function apply(data: GuiAccessSettings) {
    setCfg(data);
    setEnabled(data.enabled);
    setIdle(data.idle_timeout_minutes);
    setAbsolute(data.absolute_timeout_minutes);
    setSlotCount(data.slot_count);
  }

  useEffect(() => {
    api.getGuiAccess().then(apply).catch(() => setHidden(true));
  }, []);

  if (hidden) return null;

  const idleOk = Number.isInteger(idle) && idle >= 1 && idle <= 240;
  const absoluteOk = Number.isInteger(absolute) && absolute >= 5 && absolute <= 720;
  const orderOk = !idleOk || !absoluteOk || idle <= absolute;
  const slotsOk = Number.isInteger(slotCount) && slotCount >= 1 && slotCount <= 4;
  const valid = idleOk && absoluteOk && orderOk && slotsOk;
  const unavailable = cfg ? !cfg.available : false;
  // Switching on is refused by the server while the setup is incomplete; switching off is always allowed.
  const cannotEnable = unavailable && !cfg?.enabled;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError("");
    try {
      apply(await api.updateGuiAccess({ enabled, idle_timeout_minutes: idle, absolute_timeout_minutes: absolute, slot_count: slotCount }));
      toast.show(t("guiAccess.saved"), "success");
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id="gui-access" className="scroll-mt-4 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("guiAccess.title")}
        <InfoTip text={t("guiAccess.intro")} />
        {cfg && <Badge tone={cfg.enabled ? "success" : "neutral"}>{cfg.enabled ? t("guiAccess.on") : t("guiAccess.off")}</Badge>}
      </h2>
      <p className="text-sm text-slate-600">{t("guiAccess.defaultOff")}</p>
      {cfg && unavailable && (
        <div role="status" className="space-y-1 rounded-lg border border-[rgb(var(--tone-warning-border))] bg-[rgb(var(--tone-warning-bg))] p-3 text-sm text-[rgb(var(--tone-warning-text))]">
          <p className="font-medium">{t("guiAccess.notReady")}</p>
          <ul className="list-disc space-y-0.5 pl-5">
            {cfg.unavailable_reasons.map((r) => <li key={r}>{t(`guiAccess.reason.${r}`)}</li>)}
          </ul>
        </div>
      )}
      <form onSubmit={save} className="space-y-4">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input type="checkbox" checked={enabled} disabled={!cfg || cannotEnable} onChange={(e) => setEnabled(e.target.checked)} />
          {t("guiAccess.enabled")}
          <InfoTip text={t("guiAccess.enabledTip")} />
        </label>
        <fieldset disabled={!cfg} className="grid gap-3 sm:grid-cols-3">
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("guiAccess.idle")}<InfoTip text={t("guiAccess.idleTip")} /></span>
            <Input type="number" min={1} max={240} value={Number.isNaN(idle) ? "" : idle} onChange={(e) => setIdle(Number(e.target.value))} aria-invalid={!idleOk || !orderOk} />
          </label>
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("guiAccess.absolute")}<InfoTip text={t("guiAccess.absoluteTip")} /></span>
            <Input type="number" min={5} max={720} value={Number.isNaN(absolute) ? "" : absolute} onChange={(e) => setAbsolute(Number(e.target.value))} aria-invalid={!absoluteOk || !orderOk} />
          </label>
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("guiAccess.slotCount")}<InfoTip text={t("guiAccess.slotCountTip")} /></span>
            <Input type="number" min={1} max={4} value={Number.isNaN(slotCount) ? "" : slotCount} onChange={(e) => setSlotCount(Number(e.target.value))} aria-invalid={!slotsOk} />
          </label>
        </fieldset>
        {!orderOk && <p role="alert" className="text-sm text-red-600">{t("guiAccess.orderError")}</p>}
        {cfg && (
          <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
            <span className="flex items-center gap-1 font-medium text-slate-700">{t("guiAccess.activeSlots")}<InfoTip text={t("guiAccess.activeSlotsTip")} /></span>
            {cfg.slots.length === 0 ? <span>{t("guiAccess.noSlots")}</span> : cfg.slots.map((s) => <Badge key={s} tone="info">{s}</Badge>)}
          </div>
        )}
        {cfg?.updated_by && cfg.updated_at && (
          <p className="text-xs text-slate-500">{t("guiAccess.lastChange", { who: cfg.updated_by, when: new Date(cfg.updated_at).toLocaleString(locale) })}</p>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <Button type="submit" disabled={busy || !cfg || !valid} loading={busy}>{t("common.save")}</Button>
      </form>
    </section>
  );
}
