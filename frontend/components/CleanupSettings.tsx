"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, GovernanceSettings } from "@/lib/api";
import { parsePorts, parseSources } from "@/lib/cleanup";
import { useNotAdmin } from "@/lib/useReadOnly";
import clsx from "clsx";
import { useEffect, useId, useState } from "react";

/**
 * Settings of the rule cleanup: the management sources (addresses the firewalls are managed from; without them the
 * cleanup is locked, because a rule that could cut them off is never switched off), the rule limit per run and the
 * waiting time before a rule may be deleted. Admin only: renders nothing for other roles. `compact` is the inline
 * variant of the cleanup tab, which only asks for the sources.
 */
export function CleanupSettings({ compact = false, onSaved }: { compact?: boolean; onSaved?: (settings: GovernanceSettings) => void }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const notAdmin = useNotAdmin();
  const [settings, setSettings] = useState<GovernanceSettings | null>(null);
  const [sources, setSources] = useState("");
  const [maxRules, setMaxRules] = useState("10");
  const [minDays, setMinDays] = useState("7");
  const [ports, setPorts] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sourcesId = useId();
  const portsId = useId();
  const portsErrorId = useId();
  const errorId = useId();

  function apply(data: GovernanceSettings) {
    setSettings(data);
    setSources(data.cleanup_management_sources.join("\n"));
    setMaxRules(String(data.cleanup_max_rules));
    setMinDays(String(data.cleanup_delete_min_days));
    setPorts((data.cleanup_management_ports ?? []).join(", "));
    setTouched(false);
  }

  useEffect(() => {
    api.getGovernance().then(apply).catch(() => {});
  }, []);

  // Reached from the cleanup tab via /settings#cleanup: the section only exists once the page has loaded.
  useEffect(() => {
    if (!compact && settings && !notAdmin && window.location.hash === "#cleanup") document.getElementById("cleanup")?.scrollIntoView({ block: "start" });
  }, [compact, settings, notAdmin]);

  const parsed = parseSources(sources);
  const sourceError = parsed.error === "invalid" ? t("cleanup.settings.sourcesInvalid", { value: parsed.bad ?? "" })
    : parsed.error === "everything" ? t("cleanup.settings.sourcesEverything", { value: parsed.bad ?? "" })
    : parsed.error === "tooMany" ? t("cleanup.settings.sourcesTooMany") : "";
  const maxValue = Number(maxRules);
  const daysValue = Number(minDays);
  const maxError = !Number.isInteger(maxValue) || maxValue < 1 || maxValue > 25 ? t("cleanup.settings.maxRulesInvalid") : "";
  const daysError = !Number.isInteger(daysValue) || daysValue < 1 || daysValue > 365 ? t("cleanup.settings.minDaysInvalid") : "";
  const parsedPorts = parsePorts(ports);
  const portsError = parsedPorts.error === "invalid" ? t("cleanup.settings.portsInvalid", { value: parsedPorts.bad ?? "" })
    : parsedPorts.error === "tooMany" ? t("cleanup.settings.portsTooMany") : "";
  const invalid = Boolean(sourceError || (!compact && (maxError || daysError || portsError)));
  const dirty = settings !== null && (parsed.entries.join("\n") !== settings.cleanup_management_sources.join("\n")
    || (!compact && (maxValue !== settings.cleanup_max_rules || daysValue !== settings.cleanup_delete_min_days
      || parsedPorts.ports.join(",") !== (settings.cleanup_management_ports ?? []).join(","))));

  if (notAdmin) return null;

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (invalid) return;
    setBusy(true);
    setError("");
    try {
      const saved = await api.updateCleanupSettings({
        cleanup_management_sources: parsed.entries,
        ...(compact ? {} : { cleanup_max_rules: maxValue, cleanup_delete_min_days: daysValue, cleanup_management_ports: parsedPorts.ports }),
      });
      apply(saved);
      toast.show(t("cleanup.settings.saved"), "success");
      onSaved?.(saved);
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  const form = (
    <form onSubmit={save} className="space-y-4" noValidate>
      <div>
        <label htmlFor={sourcesId} className="mb-1 flex items-center gap-1 text-sm font-medium text-slate-700">
          {t("cleanup.settings.sources")}
          <InfoTip text={t("cleanup.settings.sourcesHint")} size={13} />
        </label>
        <textarea
          id={sourcesId}
          rows={compact ? 2 : 3}
          value={sources}
          onChange={(e) => setSources(e.target.value)}
          onBlur={() => setTouched(true)}
          placeholder={t("cleanup.settings.sourcesPlaceholder")}
          spellCheck={false}
          autoComplete="off"
          aria-invalid={touched && Boolean(sourceError)}
          aria-describedby={touched && sourceError ? errorId : undefined}
          className={clsx(
            "w-full rounded-lg border px-3 py-2 font-mono text-sm text-slate-900 shadow-sm focus:outline-none focus:ring-1",
            touched && sourceError ? "border-red-400 focus:border-red-500 focus:ring-red-500" : "border-slate-300 focus:border-brand-500 focus:ring-brand-500",
          )}
        />
        {touched && sourceError && <p id={errorId} role="alert" className="mt-1 text-xs text-red-600">{sourceError}</p>}
      </div>
      {!compact && (
        <div>
          <label htmlFor={portsId} className="mb-1 flex items-center gap-1 text-sm font-medium text-slate-700">
            {t("cleanup.settings.ports")}
            <InfoTip text={t("cleanup.settings.portsHint")} size={13} />
          </label>
          <Input
            id={portsId}
            inputMode="numeric"
            value={ports}
            onChange={(e) => setPorts(e.target.value)}
            placeholder={t("cleanup.settings.portsPlaceholder")}
            autoComplete="off"
            aria-invalid={Boolean(portsError)}
            aria-describedby={portsError ? portsErrorId : undefined}
          />
          {portsError && <p id={portsErrorId} role="alert" className="mt-1 text-xs text-red-600">{portsError}</p>}
        </div>
      )}
      {!compact && (
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("cleanup.settings.maxRules")}<InfoTip text={t("cleanup.settings.maxRulesHint")} size={13} /></span>
            <Input type="number" inputMode="numeric" min={1} max={25} value={maxRules} onChange={(e) => setMaxRules(e.target.value)} aria-invalid={Boolean(maxError)} aria-label={t("cleanup.settings.maxRules")} />
            {maxError && <span role="alert" className="mt-1 block text-xs text-red-600">{maxError}</span>}
          </label>
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("cleanup.settings.minDays")}<InfoTip text={t("cleanup.settings.minDaysHint")} size={13} /></span>
            <Input type="number" inputMode="numeric" min={1} max={365} value={minDays} onChange={(e) => setMinDays(e.target.value)} aria-invalid={Boolean(daysError)} aria-label={t("cleanup.settings.minDays")} />
            {daysError && <span role="alert" className="mt-1 block text-xs text-red-600">{daysError}</span>}
          </label>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <Button type="submit" loading={busy} disabled={!dirty || (touched && invalid)}>{t("common.save")}</Button>
    </form>
  );

  if (compact) return form;
  return (
    <section id="cleanup" className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">{t("cleanup.settings.title")}<InfoTip text={t("cleanup.settings.intro")} /></h2>
      {settings === null ? <p className="text-sm text-slate-500">{t("common.loading")}</p> : form}
    </section>
  );
}
