"use client";

import { api, PolicyHierarchyRule, PolicyHierarchyRuleCreate, PolicyHierarchyRuleRevision, RulePreview } from "@/lib/api";
import { InfoTip } from "@/components/ui/Tooltip";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

import { Loading } from "@/components/ui/Loading";
// Copy-adapted from RuleReview.tsx rather than generalized — same pattern
// already used for the other four review components (Alias/PortForward/
// OutboundNat/OneToOne) in this codebase, so a fifth one stays consistent
// with how the others are structured instead of introducing a one-off
// shared abstraction.

export function PolicyHierarchyRuleReview({ rule, mode, onClose, onDeployed, onRestore, onChanged }: {
  rule: PolicyHierarchyRule; mode: "preview" | "history"; onClose: () => void;
  onDeployed: () => void; onChanged: () => void; onRestore: (snapshot: PolicyHierarchyRuleCreate) => void;
}) {
  const [plan, setPlan] = useState<RulePreview | null>(null);
  const [revisions, setRevisions] = useState<PolicyHierarchyRuleRevision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const { t, ts, errorMessage, locale } = useLanguage();
  const fieldLabel = (field: string) => (`fields.${field}` !== t(`fields.${field}`) ? t(`fields.${field}`) : field);
  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        if (mode === "preview") { const data = await api.previewPolicyHierarchyRule(rule.id); if (active) setPlan(data); }
        else { const data = await api.policyHierarchyRuleRevisions(rule.id); if (active) setRevisions(data); }
      } catch (e) { if (active) setError(errorMessage(e, "review.loadFailed")); }
      finally { if (active) setLoading(false); }
    }
    load();
    return () => { active = false; };
  }, [rule.id, mode, reload, errorMessage]);
  async function push() {
    if (!plan) return;
    setBusy(true); setError(null);
    try { await api.deployPolicyHierarchyRule(rule.id, plan.token); toast.show(t("review.rule.deployed", { name: rule.description }), "success"); onDeployed(); }
    catch (e) { setError(errorMessage(e, "review.pushFailed")); setPlan(null); }
    finally { setBusy(false); }
  }
  async function act(host: RulePreview["hosts"][number], action: "remove" | "rollback") {
    if (!plan) return;
    const question = action === "remove"
      ? t("review.rule.removeConfirm", { name: rule.description, host: host.host_name })
      : t("review.rule.rollbackConfirm", { host: host.host_name });
    if (!confirm(question)) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      if (action === "remove") await api.removePolicyHierarchyPreviousTarget(rule.id, host.host_id, plan.token);
      else await api.retryPolicyHierarchyRollback(rule.id, host.host_id, plan.token);
      setNotice(action === "remove" ? t("review.rule.removed", { host: host.host_name }) : t("review.rollbackDone", { host: host.host_name }));
    } catch (e) { setError(errorMessage(e, "review.actionFailed")); }
    finally { setBusy(false); setPlan(null); setReload(n => n + 1); onChanged(); }
  }
  return <Modal title={mode === "preview" ? t("review.previewTitle") : t("review.rule.historyTitle")} onClose={() => { if (!busy) onClose(); }}>
    <p className="mb-4 font-medium text-slate-700">{rule.description}</p>
    {loading && <Loading className="py-4">{t("common.loading")}</Loading>}
    {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
    {notice && <p role="status" className="mb-4 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}
    {!loading && <Button variant="ghost" disabled={busy} onClick={() => { setError(null); setPlan(null); setReload(n => n + 1); }}>{t("review.reload")}</Button>}
    {plan && <div className="space-y-4">
      <p className="text-sm text-slate-500">{ts(plan.basis)}</p>
      {plan.warnings.map(w => <p key={w} className="rounded-lg bg-flame-50 p-3 text-sm text-flame-800">{ts(w)}</p>)}
      {plan.hosts.length === 0 && <div className="space-y-2"><p className="text-sm text-slate-500">{t("review.hierarchy.noTargets")}</p><Button onClick={() => onRestore(rule)}>{t("review.hierarchy.editRule")}</Button></div>}
      {plan.hosts.map(h => <div key={h.host_id} className="rounded-xl border border-slate-200 p-3">
        <div className="flex flex-wrap justify-between gap-2 text-sm"><strong>{h.host_name}</strong><span className="text-sky-700">{t(`review.actions.${h.action}`)}</span></div>
        {h.message && <p className="mt-2 break-words text-sm text-red-700">{ts(h.message)}</p>}
        {h.can_remove && <Button className="mt-3" variant="danger" disabled={busy || loading} onClick={() => act(h, "remove")}>{t("review.removeFromHost")}</Button>}
        {h.can_retry_rollback && <Button className="mt-3" variant="secondary" disabled={busy || loading} onClick={() => act(h, "rollback")}>{t("review.retryRollback")}</Button>}
        {(h.action === "disabled" || (h.action === "retained" && !h.can_remove && !h.can_retry_rollback)) && <p className="mt-2 text-sm text-slate-500">{t("review.actionsRequireHost")}</p>}
        {h.changes.length > 0 && h.action !== "disabled" && <table className="mt-3 w-full table-fixed text-left text-xs"><thead><tr className="text-slate-400"><th className="w-1/3">{t("review.colField")}</th><th>{t("review.colBefore")}</th><th>{t("review.colDraft")}</th></tr></thead><tbody>
          {h.changes.map(c => <tr key={c.field} className="border-t border-slate-100"><td className="break-words py-2 pr-2">{fieldLabel(c.field)}</td><td className="break-words pr-2 text-slate-500">{c.before ?? "—"}</td><td className="break-words text-blue-800">{c.after || "—"}</td></tr>)}
        </tbody></table>}
      </div>)}
      {!plan.can_deploy && plan.hosts.length > 0 && plan.hosts.every(h => h.action === "unchanged") && <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{t("review.allInSync")}</p>}
      {plan.can_deploy && <div className="space-y-2"><Button disabled={busy || loading} onClick={push} loading={busy}>{busy ? t("review.pushing") : t("review.deployNow")}</Button><p className="flex items-center gap-1.5 text-xs text-slate-500">{t("review.pushShort")}<InfoTip text={t("review.hierarchy.pushInfo")} size={13} /></p></div>}
    </div>}
    {mode === "history" && !loading && <div className="space-y-3">
      <p className="flex items-center gap-1.5 text-sm font-medium text-slate-700">{t("review.historyTitle")}<InfoTip text={t("review.historyHint")} /></p>
      {revisions.length === 0 && <p className="py-3 text-sm text-slate-500">{t("review.rule.noHistory")}</p>}
      {revisions.map((r, i) => <div key={r.id} className="rounded-xl border border-slate-200 p-3 text-sm">
        <div className="flex justify-between gap-2"><strong>{t("review.version", { n: revisions.length - i })}</strong><span className="text-slate-500">{new Date(r.created_at).toLocaleString(locale)}</span></div>
        <p className="mt-1 text-slate-500">{ts(r.note)}</p>
        <p className="my-2 break-words">{r.snapshot.description} · {r.snapshot.action} · {r.snapshot.source_net} → {r.snapshot.destination_net}{r.snapshot.destination_port ? `:${r.snapshot.destination_port}` : ""}</p>
        <Button variant="ghost" disabled={rule.deployments.some(d => ["pending", "revert_failed"].includes(d.status))} onClick={() => onRestore(r.snapshot)}>{t("review.adoptAsDraft")}</Button>
      </div>)}
    </div>}
  </Modal>;
}
