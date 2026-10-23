"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Modal } from "@/components/ui/Modal";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, ApiError, ConfigAction, ConfigFileError, ConfigPlan, ConfigPlanItem } from "@/lib/api";
import { useNotAdmin } from "@/lib/useReadOnly";
import { CheckCircle2, Download, FileCode2, TriangleAlert, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";

const MAX_BYTES = 1024 * 1024;
const ROW_LIMIT = 200;
const ACTION_TONE: Record<ConfigAction, "success" | "info" | "danger" | "neutral" | "warning"> = {
  create: "success", update: "info", delete: "danger", unchanged: "neutral", blocked: "warning",
};

/** A change value for display: strings as they are, lists and numbers as JSON, shortened. */
function show(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 160 ? `${text.slice(0, 159)}…` : text;
}

export default function ConfigCodePage() {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const notAdmin = useNotAdmin();
  const fileInput = useRef<HTMLInputElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const [yaml, setYaml] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [prune, setPrune] = useState(false);
  const [plan, setPlan] = useState<ConfigPlan | null>(null);
  const [fileErrors, setFileErrors] = useState<ConfigFileError[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"export" | "plan" | "apply" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [applied, setApplied] = useState<ConfigPlan["summary"] | null>(null);
  const [tab, setTab] = useState("all");
  const [showAll, setShowAll] = useState(false);

  // A new file or another prune setting invalidates the plan: what is applied must be what was shown.
  function reset() {
    setPlan(null); setFileErrors([]); setError(null); setApplied(null); setTab("all"); setShowAll(false);
  }
  useEffect(() => {
    if (plan || fileErrors.length || error) resultRef.current?.focus();
  }, [plan, fileErrors, error]);

  async function download() {
    setBusy("export");
    try {
      const { blob, filename } = await api.exportConfig();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || "opnexus-config.yaml";
      link.click();
      URL.revokeObjectURL(url);
      toast.show(t("configCode.exported"), "success");
    } catch (e) {
      toast.show(errorMessage(e, "configCode.exportFailed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  async function pickFile(file: File | undefined) {
    if (!file) return;
    reset();
    if (file.size > MAX_BYTES) {
      setError(t("configCode.tooLarge"));
      return;
    }
    setYaml(await file.text());
    setFileName(file.name);
  }

  function failed(e: unknown, fallbackKey: string) {
    const errors = e instanceof ApiError ? ((e.params as unknown as { errors?: ConfigFileError[] } | undefined)?.errors ?? []) : [];
    setFileErrors(errors);
    setError(errorMessage(e, fallbackKey));
  }

  async function makePlan() {
    reset();
    setBusy("plan");
    try {
      setPlan(await api.planConfig(yaml, prune));
    } catch (e) {
      failed(e, "configCode.planFailed");
    } finally {
      setBusy(null);
    }
  }

  async function apply() {
    if (!plan) return;
    setBusy("apply");
    try {
      const result = await api.applyConfig(yaml, prune, plan.token);
      setConfirming(false);
      setApplied(result.summary);
      setPlan(null);
      toast.show(t("configCode.applied", { create: result.summary.create, update: result.summary.update, delete: result.summary.delete }), "success");
    } catch (e) {
      setConfirming(false);
      failed(e, "configCode.applyFailed");
      setPlan(null);
    } finally {
      setBusy(null);
    }
  }

  const kinds = plan ? Array.from(new Set(plan.items.map((item) => item.kind))) : [];
  const visible = plan ? plan.items.filter((item) => tab === "all" || item.kind === tab) : [];
  const rows = showAll ? visible : visible.slice(0, ROW_LIMIT);
  const differences = plan ? plan.summary.create + plan.summary.update + plan.summary.delete : 0;
  const reasonText = (code: string) => t(`configCode.reason.${code}`);
  const actionLabel = (item: ConfigPlanItem) => t(`configCode.action.${item.action === "blocked" ? "blocked" : item.planned_action}`);

  return (
    <div>
      <div className="mb-6">
        <h1 className="flex items-center gap-2 text-2xl font-semibold text-slate-900">
          {t("configCode.title")}
          <InfoTip text={t("configCode.intro")} size={18} />
        </h1>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card className="self-start p-5">
          <h2 className="mb-1 flex items-center gap-2 text-lg font-semibold text-slate-900">
            {t("configCode.export.title")}
            <InfoTip text={t("configCode.export.tip")} />
          </h2>
          <p className="mb-4 text-sm text-slate-600">{t("configCode.export.text")}</p>
          <Button onClick={download} loading={busy === "export"}>
            <Download size={16} aria-hidden="true" />
            {t("configCode.export.button")}
          </Button>
        </Card>

        <Card className="p-5">
          <h2 className="mb-1 text-lg font-semibold text-slate-900">{t("configCode.import.title")}</h2>
          <p className="mb-4 text-sm text-slate-600">{t("configCode.import.text")}</p>

          <div className="mb-3 flex flex-wrap items-center gap-3">
            <input
              ref={fileInput}
              type="file"
              accept=".yaml,.yml,text/yaml,text/plain"
              className="sr-only"
              aria-label={t("configCode.import.chooseFile")}
              onChange={(e) => { void pickFile(e.target.files?.[0]); e.target.value = ""; }}
            />
            <Button variant="outline" onClick={() => fileInput.current?.click()}>
              <Upload size={16} aria-hidden="true" />
              {t("configCode.import.chooseFile")}
            </Button>
            {fileName && <span className="text-sm text-slate-500">{fileName}</span>}
          </div>

          <label htmlFor="config-yaml" className="mb-1 block text-sm font-medium text-slate-700">{t("configCode.import.paste")}</label>
          <textarea
            id="config-yaml"
            value={yaml}
            onChange={(e) => { setYaml(e.target.value); setFileName(null); reset(); }}
            spellCheck={false}
            rows={10}
            className="mb-3 w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs text-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
            placeholder="opnexus_config: 1"
          />

          <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2">
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input type="checkbox" checked={prune} onChange={(e) => { setPrune(e.target.checked); reset(); }} />
              {t("configCode.import.prune")}
              <InfoTip text={t("configCode.import.pruneTip")} />
            </label>
            <Button onClick={makePlan} loading={busy === "plan"} disabled={!yaml.trim()}>
              <FileCode2 size={16} aria-hidden="true" />
              {t("configCode.import.plan")}
            </Button>
          </div>
        </Card>
      </div>

      <div ref={resultRef} tabIndex={-1} className="mt-6 outline-none" aria-live="polite">
        {error && (
          <Card className="mb-4 border-red-200 p-5">
            <p className="flex items-center gap-2 text-sm font-medium text-red-700"><TriangleAlert size={16} aria-hidden="true" />{error}</p>
            {fileErrors.length > 0 && (
              <ul className="mt-3 space-y-1 text-sm text-slate-700">
                {fileErrors.slice(0, 50).map((fe, i) => (
                  <li key={i} className="font-mono text-xs">
                    {fe.line ? `${t("configCode.line", { n: fe.line })}: ` : ""}
                    {fe.path ? `${fe.path}: ` : ""}
                    <span className="font-sans text-sm">{t(`configCode.err.${fe.code}`)}</span>
                    {fe.params?.field != null && <span className="text-slate-500"> ({String(fe.params.field)})</span>}
                    {fe.params?.value != null && <span className="text-slate-500"> ({String(fe.params.value)})</span>}
                  </li>
                ))}
                {fileErrors.length > 50 && <li className="text-slate-500">{t("configCode.moreErrors", { n: fileErrors.length - 50 })}</li>}
              </ul>
            )}
          </Card>
        )}

        {applied && (
          <Card className="p-5">
            <p className="flex items-center gap-2 font-medium text-slate-900"><CheckCircle2 size={18} className="text-green-600" aria-hidden="true" />
              {t("configCode.applied", { create: applied.create, update: applied.update, delete: applied.delete })}</p>
            <p className="mt-2 text-sm text-slate-600">{t("configCode.appliedNext")}</p>
            <div className="mt-3 flex gap-3">
              <Button href="/rules" variant="outline">{t("configCode.viewDrafts")}</Button>
            </div>
          </Card>
        )}

        {plan && (
          <Card className="p-5">
            <div className="mb-4 flex flex-wrap items-center gap-2" role="group" aria-label={t("configCode.summary")}>
              {(["create", "update", "delete", "unchanged", "blocked"] as const).map((key) => (
                <Badge key={key} tone={plan.summary[key] ? ACTION_TONE[key] : "neutral"}>
                  {plan.summary[key]} {t(`configCode.action.${key}`)}
                </Badge>
              ))}
              <span className="ml-auto text-xs text-slate-400" title={plan.sha256}>SHA-256 {plan.sha256.slice(0, 12)}…</span>
            </div>

            {differences === 0 && plan.summary.blocked === 0 ? (
              <EmptyState icon={CheckCircle2} title={t("configCode.noDifferences")} description={t("configCode.noDifferencesText")} />
            ) : (
              <>
                {kinds.length > 1 && (
                  <Tabs
                    className="mb-3"
                    active={tab}
                    onChange={(key) => { setTab(key); setShowAll(false); }}
                    items={[{ key: "all", label: `${t("configCode.all")} (${plan.items.length})` },
                      ...kinds.map((kind) => ({ key: kind, label: `${t(`configCode.kind.${kind}`)} (${plan.items.filter((i) => i.kind === kind).length})` }))]}
                  />
                )}
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                      <tr>
                        <th className="hidden px-4 py-2 font-medium sm:table-cell">{t("configCode.colKind")}</th>
                        <th className="px-4 py-2 font-medium">{t("configCode.colKey")}</th>
                        <th className="px-4 py-2 font-medium">{t("configCode.colAction")}</th>
                        <th className="hidden px-4 py-2 font-medium md:table-cell">{t("configCode.colChange")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((item) => (
                        <tr key={`${item.kind}:${item.key}`} className="border-b border-slate-100 align-top last:border-0">
                          <td className="hidden px-4 py-2 text-slate-600 sm:table-cell">{t(`configCode.kind.${item.kind}`)}</td>
                          <td className="px-4 py-2 font-mono text-xs text-slate-900">
                            <div className="mb-0.5 font-sans text-xs text-slate-500 sm:hidden">{t(`configCode.kind.${item.kind}`)}</div>
                            {item.key}
                            <div className="mt-1 font-sans text-sm text-slate-700 md:hidden">
                              {item.reasons.length > 0 && (
                                <p className="mb-1 flex items-center gap-1 text-amber-700"><TriangleAlert size={14} aria-hidden="true" />{item.reasons.map(reasonText).join("; ")}</p>
                              )}
                              {item.deployed_hosts > 0 && item.action !== "blocked" && (
                                <p className="mb-1 text-xs text-slate-500">{t("configCode.deployedOn", { n: item.deployed_hosts })}</p>
                              )}
                              {item.implicit && <p className="mb-1 text-xs text-slate-500">{t("configCode.implicit")}</p>}
                              {item.planned_action === "update" && item.changes.map((change) => (
                                <p key={change.field} className="break-words font-mono text-xs">
                                  <span className="text-slate-500">{change.field}:</span> <span className="text-red-700">{show(change.before)}</span> → <span className="text-green-700">{show(change.after)}</span>
                                </p>
                              ))}
                            </div>
                          </td>
                          <td className="px-4 py-2"><Badge tone={ACTION_TONE[item.action]}>{actionLabel(item)}</Badge></td>
                          <td className="hidden px-4 py-2 text-slate-700 md:table-cell">
                            {item.reasons.length > 0 && (
                              <p className="mb-1 flex items-center gap-1 text-amber-700"><TriangleAlert size={14} aria-hidden="true" />{item.reasons.map(reasonText).join("; ")}</p>
                            )}
                            {item.deployed_hosts > 0 && item.action !== "blocked" && (
                              <p className="mb-1 text-xs text-slate-500">{t("configCode.deployedOn", { n: item.deployed_hosts })}</p>
                            )}
                            {item.implicit && <p className="mb-1 text-xs text-slate-500">{t("configCode.implicit")}</p>}
                            {item.planned_action === "update" && item.changes.map((change) => (
                              <p key={change.field} className="break-words font-mono text-xs">
                                <span className="text-slate-500">{change.field}:</span> <span className="text-red-700">{show(change.before)}</span> → <span className="text-green-700">{show(change.after)}</span>
                              </p>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {visible.length > rows.length && (
                  <div className="mt-3 text-center">
                    <Button variant="ghost" onClick={() => setShowAll(true)}>{t("configCode.showAll", { n: visible.length - rows.length })}</Button>
                  </div>
                )}
              </>
            )}

            {plan.warnings.length > 0 && (
              <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/60 p-3">
                <p className="mb-1 text-sm font-medium text-amber-800">{t("configCode.warnings")}</p>
                <ul className="space-y-1 text-sm text-amber-900">
                  {plan.warnings.slice(0, 30).map((w, i) => (
                    <li key={i}>{w.path && <span className="font-mono text-xs">{w.path}: </span>}{t(`configCode.warn.${w.code}`, w.params as Record<string, string | number>)}</li>
                  ))}
                  {plan.warnings.length > 30 && <li>{t("configCode.moreErrors", { n: plan.warnings.length - 30 })}</li>}
                </ul>
              </div>
            )}

            {!plan.applicable && <p className="mt-4 text-sm text-amber-700">{t("configCode.blockedHint")}</p>}
            {differences > 0 && (
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <Button onClick={() => setConfirming(true)} disabled={notAdmin || !plan.applicable}>
                  {t("configCode.import.apply")}
                </Button>
                {notAdmin && <span className="text-sm text-slate-500">{t("configCode.import.adminOnly")}</span>}
              </div>
            )}
          </Card>
        )}
      </div>

      {confirming && plan && (
        <Modal title={t("configCode.confirm.title")} onClose={() => setConfirming(false)}>
          <p className="mb-2 text-sm text-slate-700">{t("configCode.confirm.text", { create: plan.summary.create, update: plan.summary.update, delete: plan.summary.delete })}</p>
          <p className="mb-2 text-sm text-slate-600">{t("configCode.confirm.nothingOnFirewalls")}</p>
          {plan.prune && plan.summary.delete > 0 && (
            <p className="mb-2 text-sm font-medium text-red-700">{t("configCode.confirm.prune", { n: plan.summary.delete })}</p>
          )}
          <div className="mt-4 flex justify-end gap-3">
            <Button variant="ghost" onClick={() => setConfirming(false)}>{t("common.cancel")}</Button>
            <Button variant={plan.prune && plan.summary.delete > 0 ? "danger" : "primary"} onClick={apply} loading={busy === "apply"}>
              {t("configCode.import.apply")}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
