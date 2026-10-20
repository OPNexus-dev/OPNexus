"use client";

import { InfoTip } from "@/components/ui/Tooltip";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { api, ComplianceFinding, ComplianceHost, RuleHit, Severity } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useReadOnly } from "@/lib/useReadOnly";
import { CheckCircle2, CircleHelp, FileText, Info, RefreshCw, ShieldAlert, XCircle } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

const SEVERITY_TONE: Record<Severity, "danger" | "warning" | "info" | "neutral"> = {
  critical: "danger", high: "danger", medium: "warning", low: "info", info: "neutral",
};
const SEVERITY_RANK: Record<Severity, number> = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
const GRADE_TONE = { good: "success", fair: "warning", poor: "danger" } as const;

function ScoreBadge({ host }: { host: ComplianceHost }) {
  const { t } = useLanguage();
  if (host.score === null || host.grade === null) return <Badge tone="neutral">{host.error ? t(`security.error.${host.error}`) : t("security.notChecked")}</Badge>;
  return <Badge tone={GRADE_TONE[host.grade]}>{host.score} / 100</Badge>;
}

export default function SecurityPage() {
  const { t, errorMessage, locale } = useLanguage();
  const readOnly = useReadOnly();
  const toast = useToast();
  const [hosts, setHosts] = useState<ComplianceHost[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ComplianceHost | null>(null);
  const [hits, setHits] = useState<RuleHit[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const list = await api.listCompliance();
      setHosts(list);
      setSelected((current) => current ?? [...list].filter((h) => h.score !== null).sort((a, b) => (a.score ?? 0) - (b.score ?? 0))[0]?.host_id ?? list[0]?.host_id ?? null);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "security.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    let active = true;
    api.getRuleHits(selected).then((h) => active && setHits(h)).catch(() => active && setHits([]));
    api.getHostCompliance(selected).then((d) => active && setDetail(d)).catch((e) => active && setError(errorMessage(e, "security.loadFailed")));
    return () => { active = false; };
  }, [selected, hosts]);

  async function runCheck(all: boolean) {
    setBusy(true);
    try {
      if (all || !selected) await api.checkFleetCompliance();
      else await api.checkHostCompliance(selected);
      await load();
      toast.show(t("security.checked"), "success");
    } catch (e) {
      setError(errorMessage(e, "security.checkFailed"));
    } finally {
      setBusy(false);
    }
  }

  const findings = [...(detail?.findings ?? [])].sort(
    (a, b) => Number(a.passed === true) - Number(b.passed === true) || SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity],
  );

  return (
    <div>
      <div className="mb-6 flex items-center justify-between gap-3">
        <h1 className="flex items-center gap-2 text-2xl font-semibold text-slate-900">
          {t("security.title")}
          <InfoTip text={t("security.intro")} size={18} />
        </h1>
        <div className="flex gap-2">
          <Link href="/security/report" className="inline-flex items-center gap-2 rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
            <FileText size={16} />
            {t("report.open_report")}
          </Link>
          <fieldset disabled={readOnly} className="contents">
            <Button onClick={() => runCheck(true)} disabled={busy} loading={busy}>
              <RefreshCw size={16} />
              {t("security.checkAll")}
            </Button>
          </fieldset>
        </div>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {hosts === null && <Skeleton className="h-24 w-full" />}
      {hosts?.length === 0 && <EmptyState icon={ShieldAlert} title={t("security.noHosts")} description={t("security.noHostsHint")} />}

      {hosts && hosts.length > 0 && (
        <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {hosts.map((host) => (
            <button
              key={host.host_id}
              type="button"
              onClick={() => setSelected(host.host_id)}
              aria-pressed={selected === host.host_id}
              className={`rounded-2xl border bg-white p-4 text-left transition-colors hover:border-slate-400 ${selected === host.host_id ? "border-slate-900 ring-1 ring-slate-900" : "border-slate-200"}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-slate-900">{host.host_name}</span>
                <ScoreBadge host={host} />
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
                {host.counts && (["critical", "high", "medium", "low"] as Severity[]).filter((s) => host.counts![s] > 0).map((s) => (
                  <Badge key={s} tone={SEVERITY_TONE[s]}>{host.counts![s]} {t(`security.severity.${s}`)}</Badge>
                ))}
                {host.counts && Object.entries(host.counts).every(([k, v]) => k === "info" || v === 0) && host.score !== null && (
                  <span className="text-slate-500">{t("security.allGood")}</span>
                )}
              </div>
              <p className="mt-2 text-xs text-slate-400">
                {host.checked_at ? t("security.checkedAt", { when: new Date(host.checked_at).toLocaleString(locale) }) : t("security.neverChecked")}
              </p>
            </button>
          ))}
        </div>
      )}

      {detail && (
        <Card className="p-0">
          <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-5 py-3">
            <h2 className="font-medium text-slate-900">{detail.host_name}</h2>
            <fieldset disabled={readOnly} className="contents">
              <Button variant="outline" onClick={() => runCheck(false)} disabled={busy}>
                <RefreshCw size={14} />
                {t("security.checkHost")}
              </Button>
            </fieldset>
          </div>
          {detail.error && <p className="px-5 py-4 text-sm text-red-600">{t(`security.errorHint.${detail.error}`)}</p>}
          {!detail.error && findings.length === 0 && <p className="px-5 py-6 text-sm text-slate-500">{t("security.neverChecked")}</p>}
          <ul className="divide-y divide-slate-100">
            {findings.map((finding) => (
              <FindingRow key={finding.id} finding={finding} />
            ))}
          </ul>
        </Card>
      )}

      {detail && hits.length > 0 && (
        <Card className="mt-6 p-0">
          <div className="border-b border-slate-100 px-5 py-3">
            <h2 className="flex items-center gap-2 font-medium text-slate-900">
              {t("security.hits.title")}
              <InfoTip text={t("security.hits.intro")} />
            </h2>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-5 py-3 font-medium">{t("security.hits.rule")}</th>
                  <th className="px-5 py-3 font-medium">{t("security.hits.state")}</th>
                  <th className="px-5 py-3 font-medium">{t("security.hits.lastHit")}</th>
                  <th className="px-5 py-3 text-right font-medium">{t("security.hits.packets")}</th>
                </tr>
              </thead>
              <tbody>
                {hits.map((hit) => (
                  <tr key={hit.rule_uuid} className="border-b border-slate-100 last:border-0">
                    <td className="px-5 py-2 text-slate-900">{hit.description || hit.rule_uuid}</td>
                    <td className="px-5 py-2">
                      <Badge tone={hit.state === "unused" ? "warning" : hit.state === "used" ? "success" : "neutral"}>{t(`security.hits.states.${hit.state}`)}</Badge>
                    </td>
                    <td className="px-5 py-2 text-slate-500">{hit.last_hit_at ? new Date(hit.last_hit_at).toLocaleString(locale) : <span className="text-slate-300">—</span>}</td>
                    <td className="px-5 py-2 text-right tabular-nums text-slate-500">{hit.packets.toLocaleString(locale)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

function FindingRow({ finding }: { finding: ComplianceFinding }) {
  const { t } = useLanguage();
  const failed = finding.passed === false;
  // A note (severity "info") is a suggestion, not an error: neutral icon instead of the red cross.
  const note = failed && finding.severity === "info";
  const Icon = finding.passed === null ? CircleHelp : note ? Info : failed ? XCircle : CheckCircle2;
  const color = finding.passed === null || note ? "text-slate-400" : failed ? "text-red-600" : "text-emerald-600";
  const { examples = [], count = 0, reason, protocol, age_days } = finding.params;
  const vars = { count, reason: reason ? t(`security.reason.${reason}`) : "", protocol: protocol ?? "", days: age_days ?? "" };
  return (
    <li className="flex gap-3 px-5 py-3">
      <Icon size={18} className={`mt-0.5 shrink-0 ${color}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-slate-900">{t(`security.checks.${finding.id}.title`)}</span>
          {failed && <Badge tone={SEVERITY_TONE[finding.severity]}>{t(`security.severity.${finding.severity}`)}</Badge>}
          <span className="sr-only">{finding.passed === null ? t("security.unknown") : failed ? t("security.failed") : t("security.passed")}</span>
        </div>
        <p className="text-sm text-slate-600">{t(`security.checks.${finding.id}.${failed ? "fail" : finding.passed === null ? "unknown" : "ok"}`, vars)}</p>
        {failed && examples.length > 0 && (
          <ul className="mt-1 list-disc pl-5 text-sm text-slate-700">
            {examples.map((example, i) => <li key={i} className="break-words">{example}</li>)}
            {count > examples.length && <li className="list-none text-slate-400">{t("security.andMore", { n: count - examples.length })}</li>}
          </ul>
        )}
        {failed && <p className="mt-1 text-sm text-slate-500">{t(`security.checks.${finding.id}.fix`)}</p>}
      </div>
    </li>
  );
}
