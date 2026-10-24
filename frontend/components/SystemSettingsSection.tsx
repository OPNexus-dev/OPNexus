"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { useToast } from "@/components/ui/Toast";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, SystemSetting } from "@/lib/api";
import { useNotAdmin } from "@/lib/useReadOnly";
import { RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

const GROUPS = ["security", "operation", "monitoring", "integrations"] as const;

type Draft = Record<string, string>;

function draftOf(items: SystemSetting[]): Draft {
  const draft: Draft = {};
  for (const item of items) {
    draft[item.key] = item.kind === "secret_url" ? "" : item.value === null ? "" : String(item.value);
  }
  return draft;
}

/** Settings that used to need the server's .env and are safe to change while OPNexus runs (Settings › several areas).
 *  `groups` picks which groups this instance shows, each as its own card. */
export function SystemSettingsSection({ groups = GROUPS }: { groups?: readonly (typeof GROUPS)[number][] }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const readOnly = useNotAdmin();
  const [items, setItems] = useState<SystemSetting[] | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState("");

  function apply(next: SystemSetting[]) {
    setItems(next);
    setDraft(draftOf(next));
  }

  useEffect(() => {
    api.systemSettings().then((body) => apply(body.settings)).catch((e) => setError(errorMessage(e)));
  }, [errorMessage]);

  const byGroup = useMemo(() => {
    const out: Record<string, SystemSetting[]> = {};
    for (const item of items ?? []) (out[item.group] ??= []).push(item);
    return out;
  }, [items]);

  function changed(item: SystemSetting): boolean {
    if (item.source === "server") return false;
    const value = draft[item.key] ?? "";
    if (item.kind === "secret_url") return value.trim() !== "";
    return value !== (item.value === null ? "" : String(item.value));
  }

  async function save(values: Record<string, number | string | null>, label: string) {
    setSaving(label);
    setError("");
    try {
      apply((await api.saveSystemSettings(values)).settings);
      toast.show(t("settings.system.saved"), "success");
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setSaving(null);
    }
  }

  function saveGroup(group: string) {
    const values: Record<string, number | string | null> = {};
    for (const item of byGroup[group] ?? []) {
      if (!changed(item)) continue;
      const raw = (draft[item.key] ?? "").trim();
      values[item.key] = item.kind === "int" || item.kind === "float" ? (raw === "" ? null : Number(raw)) : raw;
    }
    if (Object.keys(values).length) void save(values, group);
  }

  function sourceBadge(item: SystemSetting) {
    if (item.source === "server") {
      return <Badge tone="warning">{t("settings.system.fromServer", { env: item.env ?? "" })}</Badge>;
    }
    if (item.source === "ui") return <Badge tone="info">{t("settings.system.changed")}</Badge>;
    return null;
  }

  function field(item: SystemSetting) {
    const label = t(`settings.system.field.${item.key}.label`);
    const hint = t(`settings.system.field.${item.key}.hint`);
    const locked = readOnly || item.source === "server";
    const value = draft[item.key] ?? "";
    const set = (v: string) => setDraft((d) => ({ ...d, [item.key]: v }));
    const reset = item.source === "ui" && !readOnly && (
      <Button
        variant="ghost"
        type="button"
        title={t("settings.system.reset")}
        aria-label={`${t("settings.system.reset")}: ${label}`}
        disabled={saving !== null}
        onClick={() => void save({ [item.key]: null }, item.key)}
      >
        <RotateCcw size={14} />
      </Button>
    );
    const head = (
      <span className="mb-1 flex flex-wrap items-center gap-1.5 font-medium text-slate-700">
        {label}
        <InfoTip text={hint} size={13} />
        {sourceBadge(item)}
      </span>
    );

    if (item.kind === "pem") {
      return (
        <div key={item.key} className="block text-sm sm:col-span-2">
          {head}
          {item.source === "server" ? (
            <p className="text-slate-600">{t("settings.system.pemFromServer")}</p>
          ) : (
            <textarea
              aria-label={label}
              className="h-28 w-full rounded-lg border border-slate-300 bg-white p-2 font-mono text-xs"
              placeholder="-----BEGIN CERTIFICATE-----"
              spellCheck={false}
              disabled={locked}
              value={value}
              onChange={(e) => set(e.target.value)}
            />
          )}
          {reset && <div className="mt-1 flex justify-end">{reset}</div>}
        </div>
      );
    }

    if (item.kind === "secret_url") {
      return (
        <div key={item.key} className="block text-sm sm:col-span-2">
          {head}
          <div className="flex gap-2">
            <Input
              aria-label={label}
              type="url"
              inputMode="url"
              spellCheck={false}
              autoComplete="off"
              disabled={locked}
              placeholder={item.is_set ? t("settings.system.secretSet") : "https://"}
              value={value}
              onChange={(e) => set(e.target.value)}
            />
            {item.is_set && item.source === "ui" && !readOnly && (
              <Button variant="secondary" type="button" disabled={saving !== null} onClick={() => void save({ [item.key]: null }, item.key)}>
                {t("settings.system.remove")}
              </Button>
            )}
          </div>
        </div>
      );
    }

    const bounds = item.min !== null && item.max !== null ? t("settings.system.bounds", { min: item.min, max: item.max }) : "";
    return (
      <label key={item.key} className="block text-sm">
        {head}
        <div className="flex items-center gap-2">
          <Input
            type="number"
            min={item.min ?? undefined}
            max={item.max ?? undefined}
            step={item.kind === "float" ? 0.5 : 1}
            disabled={locked}
            value={value}
            onChange={(e) => set(e.target.value)}
          />
          <span className="shrink-0 text-xs text-slate-500">{item.unit ? t(`settings.system.unit.${item.unit}`) : ""}</span>
          {reset}
        </div>
        <span className="mt-1 block text-xs text-slate-500">
          {item.default !== null && t("settings.system.defaultIs", { value: item.default })}
          {bounds && ` · ${bounds}`}
        </span>
      </label>
    );
  }

  return (
    <>
      {!items && !error && (
        <section className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-500">{t("common.loading")}</section>
      )}
      {items &&
        groups.filter((g) => byGroup[g]?.length).map((group) => {
          const dirty = (byGroup[group] ?? []).some(changed);
          return (
            <section key={group} id={`system-${group}`} className="scroll-mt-4 rounded-xl border border-slate-200 bg-white p-5">
              <form
                className="space-y-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  saveGroup(group);
                }}
              >
                <h2 className="flex items-center gap-2 text-lg font-medium">
                  {t(`settings.system.group.${group}`)}
                  <InfoTip text={`${t(`settings.system.groupHint.${group}`)} ${t("settings.system.intro")}`} />
                </h2>
                <div className="grid gap-4 sm:grid-cols-2">{(byGroup[group] ?? []).map(field)}</div>
                {!readOnly && (
                  <div className="flex justify-end border-t border-slate-100 pt-4">
                    <Button type="submit" disabled={!dirty || saving !== null}>
                      {t("common.save")}
                    </Button>
                  </div>
                )}
              </form>
            </section>
          );
        })}
      {error && (
        <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
          {error}
        </p>
      )}
    </>
  );
}
