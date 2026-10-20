"use client";

import { Button } from "@/components/ui/Button";
import { api, ComplianceFinding, ComplianceHost, Severity } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { APP_VERSION } from "@/lib/version";
import { Printer } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

const RANK: Record<Severity, number> = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };

/** Printable security report of the whole fleet ("Print / save as PDF" in the browser). */
export default function SecurityReportPage() {
  const { t, locale } = useLanguage();
  const [hosts, setHosts] = useState<ComplianceHost[] | null>(null);
  const [error, setError] = useState(false);

  // A report is printed on white paper whatever theme the screen uses.
  useEffect(() => {
    const root = document.documentElement;
    let previous = root.dataset.theme;
    const before = () => { previous = root.dataset.theme; root.dataset.theme = "light"; };
    const after = () => { if (previous === undefined) delete root.dataset.theme; else root.dataset.theme = previous; };
    window.addEventListener("beforeprint", before);
    window.addEventListener("afterprint", after);
    return () => { window.removeEventListener("beforeprint", before); window.removeEventListener("afterprint", after); after(); };
  }, []);

  useEffect(() => {
    api.listCompliance()
      .then((list) => Promise.all(list.filter((h) => h.checked_at).map((h) => api.getHostCompliance(h.host_id))))
      .then((details) => setHosts(details.sort((a, b) => (a.score ?? 101) - (b.score ?? 101))))
      .catch(() => setError(true));
  }, []);

  const failed = (h: ComplianceHost): ComplianceFinding[] =>
    (h.findings ?? []).filter((f) => f.passed === false).sort((a, b) => RANK[a.severity] - RANK[b.severity]);

  return (
    <div className="mx-auto max-w-3xl bg-white p-6 text-slate-900 print:max-w-none print:p-0">
      <div className="mb-6 flex items-start justify-between print:mb-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("report.title")}</h1>
          <p className="text-sm text-slate-500">
            {t("report.generated", { when: new Date().toLocaleString(locale), version: APP_VERSION })}
          </p>
        </div>
        <div className="flex gap-2 print:hidden">
          <Link href="/security" className="inline-flex items-center rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">{t("report.back")}</Link>
          <Button onClick={() => window.print()}><Printer size={16} />{t("report.print")}</Button>
        </div>
      </div>
      {error && <p className="text-sm text-red-600">{t("security.loadFailed")}</p>}
      {hosts === null && !error && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
      {hosts?.length === 0 && <p className="text-sm text-slate-500">{t("security.neverChecked")}</p>}
      {hosts && hosts.length > 0 && (
        <table className="mb-8 w-full text-left text-sm">
          <thead className="border-b border-slate-300 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-2 font-medium">{t("overlay.colName")}</th>
              <th className="py-2 font-medium">{t("report.score")}</th>
              <th className="py-2 font-medium">{t("report.open")}</th>
              <th className="py-2 font-medium">{t("report.checked")}</th>
            </tr>
          </thead>
          <tbody>
            {hosts.map((h) => (
              <tr key={h.host_id} className="border-b border-slate-100">
                <td className="py-1.5 font-medium">{h.host_name}</td>
                <td className="py-1.5">{h.score === null ? "—" : `${h.score} / 100`}</td>
                <td className="py-1.5">{h.counts ? (["critical", "high", "medium", "low"] as Severity[]).map((s) => `${h.counts![s]} ${t(`security.severity.${s}`)}`).join(" · ") : "—"}</td>
                <td className="py-1.5 text-slate-500">{h.checked_at ? new Date(h.checked_at).toLocaleDateString(locale) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {hosts?.map((h) => (
        <section key={h.host_id} className="mb-6 break-inside-avoid">
          <h2 className="mb-1 flex items-baseline justify-between border-b border-slate-300 pb-1 text-lg font-semibold">
            <span>{h.host_name}</span>
            <span className="text-sm font-normal text-slate-600">{h.score === null ? t("security.notChecked") : `${h.score} / 100`}</span>
          </h2>
          {failed(h).length === 0 ? (
            <p className="text-sm text-slate-600">{t("security.allGood")}</p>
          ) : (
            <ul className="space-y-2 text-sm">
              {failed(h).map((f) => (
                <li key={f.id}>
                  <strong>{t(`security.checks.${f.id}.title`)}</strong> <span className="text-slate-500">({t(`security.severity.${f.severity}`)})</span>
                  <div className="text-slate-700">{t(`security.checks.${f.id}.fail`, { count: f.params.count ?? 0, reason: f.params.reason ? t(`security.reason.${f.params.reason}`) : "", protocol: f.params.protocol ?? "", days: f.params.age_days ?? "" })}</div>
                  {(f.params.examples ?? []).length > 0 && <div className="text-slate-600">{(f.params.examples ?? []).join(" · ")}</div>}
                  <div className="text-slate-500">{t(`security.checks.${f.id}.fix`)}</div>
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  );
}
