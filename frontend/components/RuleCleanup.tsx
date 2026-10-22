"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { RuleHistoryModal } from "@/components/RuleHistoryModal";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Select } from "@/components/ui/Field";
import { Skeleton } from "@/components/ui/Skeleton";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { AnalysisConfidence, api, CleanupCandidate, CleanupCategory, CleanupKind, Host, RuleCleanupResult } from "@/lib/api";
import clsx from "clsx";
import { Check, CircleHelp, History, ListChecks, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

const THRESHOLDS = [7, 30, 90, 180];
const KIND_TONE: Record<CleanupKind, BadgeTone> = { unused: "warning", shadowed: "accent", duplicate: "accent", disabled_long: "neutral" };
const CONFIDENCE_TONE: Record<AnalysisConfidence, BadgeTone> = { sure: "info", probable: "warning", unknown: "neutral" };
const KINDS: CleanupKind[] = ["unused", "shadowed", "duplicate", "disabled_long"];

function ConfidenceBadge({ confidence }: { confidence: AnalysisConfidence }) {
  const { t } = useLanguage();
  return (
    <Tooltip content={t(`cleanup.confidence.tip.${confidence}`)} focusable>
      <Badge
        tone={CONFIDENCE_TONE[confidence]}
        className={clsx(confidence === "unknown" && "border-dashed")}
        aria-label={`${t("cleanup.confidence.label")}: ${t(`cleanup.confidence.${confidence}`)}`}
      >
        {confidence === "unknown" ? <CircleHelp size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />}
        {t(`cleanup.confidence.${confidence}`)}
      </Badge>
    </Tooltip>
  );
}

function CategoryLine({ category, days }: { category: CleanupCategory; days: number }) {
  const { t, locale } = useLanguage();
  let detail: string | null = null;
  if (category.kind === "unused") detail = t("cleanup.detail.unused", { days });
  else if (category.kind === "disabled_long") {
    detail = category.since ? t("cleanup.detail.disabledSince", { when: new Date(category.since).toLocaleDateString(locale) }) : t("cleanup.detail.disabled", { days });
  } else if (category.by) {
    detail = t(category.by.automatic ? "cleanup.detail.byAutomatic" : "cleanup.detail.by", { rule: category.by.description || category.by.uuid || "?" });
  }
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <Tooltip content={t(`cleanup.category.${category.kind}_hint`)} focusable>
        <Badge tone={KIND_TONE[category.kind]}>{t(`cleanup.category.${category.kind}`)}</Badge>
      </Tooltip>
      <ConfidenceBadge confidence={category.confidence} />
      {detail && <span className="min-w-0 break-words text-xs text-slate-500">{detail}</span>}
    </div>
  );
}

function RemovableCell({ candidate }: { candidate: CleanupCandidate }) {
  const { t } = useLanguage();
  if (candidate.removable) return <span className="text-sm text-slate-700">{t("cleanup.removable.yes")}</span>;
  const reason = candidate.not_removable_reason ?? "automatic";
  return (
    <div className="flex flex-col items-start gap-1">
      <Tooltip content={t(`cleanup.notRemovable.${reason}_hint`)} focusable>
        <Badge tone="neutral">{t(`cleanup.notRemovable.${reason}`)}</Badge>
      </Tooltip>
      {reason === "managed" && (
        <Link href="/rules" className="text-xs text-brand-600 hover:underline">
          {t("cleanup.notRemovable.managedLink", { rule: candidate.managed_by?.name ?? "" })}
        </Link>
      )}
    </div>
  );
}

export function RuleCleanup() {
  const { t, errorMessage, locale } = useLanguage();
  const router = useRouter();
  const params = useSearchParams();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [result, setResult] = useState<RuleCleanupResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [refreshNext, setRefreshNext] = useState(false);
  const [history, setHistory] = useState<CleanupCandidate | null>(null);

  const enabledHosts = useMemo(() => (hosts ?? []).filter((h) => h.enabled), [hosts]);
  const days = THRESHOLDS.includes(Number(params.get("days"))) ? Number(params.get("days")) : 30;
  const hostId = params.get("host") && enabledHosts.some((h) => h.id === params.get("host")) ? (params.get("host") as string) : enabledHosts[0]?.id ?? "";

  useEffect(() => {
    api.listHosts().then(setHosts).catch((e) => { setHosts([]); setError(errorMessage(e, "cleanup.loadFailed")); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!hostId) { setResult(null); return; }
    let active = true;
    setLoading(true);
    setError(null);
    api.getRuleCleanup(hostId, days, refreshNext)
      .then((r) => active && setResult(r))
      .catch((e) => { if (active) { setResult(null); setError(errorMessage(e, "cleanup.loadFailed")); } })
      .finally(() => { if (active) { setLoading(false); setRefreshNext(false); } });
    return () => { active = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId, days, tick]);

  function update(next: { host?: string; days?: number }) {
    const q = new URLSearchParams();
    q.set("tab", "cleanup");
    q.set("host", next.host ?? hostId);
    q.set("days", String(next.days ?? days));
    router.replace(`/analysis?${q.toString()}`);
  }

  const total = result ? KINDS.reduce((sum, kind) => sum + result.counts[kind], 0) : 0;

  return (
    <div>
      <Card className="mb-6 p-4 sm:p-5">
        <form onSubmit={(e) => e.preventDefault()} aria-label={t("cleanup.form.title")} className="flex flex-wrap items-end gap-4">
          <div className="w-full sm:w-64">
            <FieldGroup label={t("cleanup.form.host")}>
              <Select value={hostId} onChange={(e) => update({ host: e.target.value })} disabled={enabledHosts.length === 0}>
                {enabledHosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
              </Select>
            </FieldGroup>
          </div>
          <div className="w-full sm:w-48">
            <FieldGroup label={t("cleanup.form.threshold")} hint={t("cleanup.form.thresholdHint")}>
              <Select value={String(days)} onChange={(e) => update({ days: Number(e.target.value) })}>
                {THRESHOLDS.map((n) => <option key={n} value={n}>{t("cleanup.form.days", { n })}</option>)}
              </Select>
            </FieldGroup>
          </div>
          <Button type="button" variant="outline" onClick={() => { setRefreshNext(true); setTick((n) => n + 1); }} disabled={loading || !hostId} title={t("cleanup.form.refreshHint")}>
            <RefreshCw size={14} aria-hidden="true" />
            {t("common.refresh")}
          </Button>
        </form>
      </Card>

      {error && <p role="alert" className="mb-4 text-sm text-red-600">{error}</p>}
      {hosts !== null && enabledHosts.length === 0 && <EmptyState icon={ListChecks} title={t("cleanup.noHosts")} />}
      {loading && !result && <Skeleton className="h-40 w-full" />}

      {result && (
        <div aria-busy={loading}>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {KINDS.map((kind) => (
              <Tooltip key={kind} content={t(`cleanup.category.${kind}_hint`)} focusable>
                <Badge tone={result.counts[kind] > 0 ? KIND_TONE[kind] : "neutral"}>{t(`cleanup.category.${kind}`)}: {result.counts[kind]}</Badge>
              </Tooltip>
            ))}
            <span className="text-xs text-slate-500">{t("cleanup.rulesChecked", { count: result.rules_total })}</span>
            <InfoTip
              text={result.usage.watching_since
                ? t("cleanup.watching", { when: new Date(result.usage.watching_since).toLocaleDateString(locale), collecting: result.usage.collecting })
                : t("cleanup.notWatching")}
              size={14}
            />
            <span className="ml-auto text-xs text-slate-400">{t("cleanup.removalLater")}</span>
          </div>
          {result.notes.length > 0 && (
            <div className="mb-3 flex flex-wrap gap-1.5">
              {result.notes.map((code) => {
                const label = t(`cleanup.notes.${code}`);
                return (
                  <Tooltip key={code} content={label === `cleanup.notes.${code}` ? code : t(`cleanup.notes.${code}_hint`)} focusable>
                    <Badge tone="warning">{label === `cleanup.notes.${code}` ? code : label}</Badge>
                  </Tooltip>
                );
              })}
            </div>
          )}

          {total === 0 ? (
            <EmptyState icon={ListChecks} title={t("cleanup.empty.title")} description={t("cleanup.empty.description", { days })} />
          ) : (
            <Card className="overflow-x-auto p-0">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">{t("cleanup.tableCaption", { host: result.host_name })}</caption>
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.rule")}</th>
                    <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.finding")}</th>
                    <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.lastHit")}</th>
                    <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.removable")}</th>
                  </tr>
                </thead>
                <tbody>
                  {result.candidates.map((candidate) => (
                    <tr key={candidate.uuid ?? candidate.label ?? candidate.description} className="border-b border-slate-100 align-top last:border-0">
                      <td className="max-w-[18rem] px-4 py-3">
                        <div className="flex items-start gap-1.5">
                          <span className="min-w-0 break-words font-medium text-slate-900">{candidate.description || candidate.uuid}</span>
                          <button
                            type="button"
                            onClick={() => setHistory(candidate)}
                            className="shrink-0 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
                            aria-label={t("cleanup.history", { rule: candidate.description || candidate.uuid || "" })}
                            title={t("cleanup.historyTitle")}
                          >
                            <History size={15} aria-hidden="true" />
                          </button>
                        </div>
                        <p className="mt-0.5 break-words text-xs text-slate-500">
                          {candidate.action ? t(`analysis.verdict.${candidate.action}`) : "?"} · {candidate.interfaces.join(", ") || t("cleanup.allInterfaces")} · {candidate.source} → {candidate.destination}
                          {candidate.destination_port ? `:${candidate.destination_port}` : ""}
                          {!candidate.enabled && <span className="ml-1">({t("cleanup.disabledRule")})</span>}
                        </p>
                      </td>
                      <td className="min-w-[16rem] px-4 py-3">
                        <div className="space-y-1.5">
                          {candidate.categories.map((category) => <CategoryLine key={category.kind} category={category} days={result.unused_days} />)}
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                        {candidate.last_hit_at
                          ? new Date(candidate.last_hit_at).toLocaleString(locale)
                          : candidate.watched_since
                            ? <Tooltip content={t("cleanup.noHitSince", { when: new Date(candidate.watched_since).toLocaleDateString(locale) })} focusable><span>{t("cleanup.noHit")}</span></Tooltip>
                            : <span className="text-slate-300">–</span>}
                      </td>
                      <td className="min-w-[9rem] px-4 py-3"><RemovableCell candidate={candidate} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </div>
      )}

      {history && result && (
        <RuleHistoryModal
          title={history.description || history.uuid || ""}
          targets={[{ hostId: result.host_id, hostName: result.host_name, uuid: history.uuid ?? "" }]}
          onClose={() => setHistory(null)}
        />
      )}
    </div>
  );
}
