"use client";

import { api, BulkDeployResult, Rule, RulePreview } from "@/lib/api";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Loading } from "@/components/ui/Loading";
import { Modal } from "@/components/ui/Modal";
import { OverlapHint } from "@/components/OverlapHint";
import { ManagementGuardHint, ManagementGuardState } from "@/components/ManagementGuardHint";
import { DeployCommentField, useDeployCommentRequired } from "@/components/DeployCommentField";
import { useToast } from "@/components/ui/Toast";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

type Loaded = { rule: Rule; plan: RulePreview | null; error: string | null };

const STATUS_TONE: Record<BulkDeployResult["results"][number]["status"], BadgeTone> = {
  deployed: "success", attention: "warning", failed: "danger", skipped: "neutral",
};

/**
 * Deploys several rules in one go. Nothing new happens on the server: every rule is deployed through the same single-rule
 * path with the preview approval shown here, one after the other (POST /rules/bulk/deploy). This dialog loads one
 * preview per rule, lets the user look at every change, and shows the outcome per rule afterwards.
 */
export function BulkDeployModal({ rules: initialRules, onClose, onDone }: { rules: Rule[]; onClose: () => void; onDone: () => void }) {
  // The selection at the moment of opening: the page clears its own selection (and reloads) as soon as the deploy is done,
  // which must not change the title or the rule names of the result shown here.
  const [rules] = useState(initialRules);
  const { t, ts, errorMessage } = useLanguage();
  const toast = useToast();
  const [loaded, setLoaded] = useState<Loaded[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BulkDeployResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const commentRequired = useDeployCommentRequired();
  const [guards, setGuards] = useState<Record<string, ManagementGuardState>>({});
  const setGuard = (id: string) => (state: ManagementGuardState) => setGuards((g) => (g[id]?.risk === state.risk && g[id]?.acknowledged === state.acknowledged ? g : { ...g, [id]: state }));
  const fieldLabel = (field: string) => (`fields.${field}` !== t(`fields.${field}`) ? t(`fields.${field}`) : field);

  useEffect(() => {
    let active = true;
    Promise.all(rules.map(async (rule): Promise<Loaded> => {
      try { return { rule, plan: await api.previewRule(rule.id), error: null }; }
      catch (e) { return { rule, plan: null, error: errorMessage(e, "review.loadFailed") }; }
    })).then((list) => { if (active) setLoaded(list); });
    return () => { active = false; };
    // The selection is fixed while the dialog is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const deployable = (loaded ?? []).filter((l) => l.plan?.can_deploy);
  const unacknowledged = deployable.filter((l) => guards[l.rule.id]?.risk === "blocks" && !guards[l.rule.id]?.acknowledged).length;
  const byId = new Map(rules.map((r) => [r.id, r]));

  async function deploy() {
    setBusy(true); setError(null);
    try {
      const outcome = await api.bulkDeployRules(deployable.map((l) => ({ rule_id: l.rule.id, preview_token: l.plan!.token, acknowledge_management_risk: guards[l.rule.id]?.risk === "blocks" && guards[l.rule.id]?.acknowledged })), comment);
      setResult(outcome);
      onDone();
    } catch (e) { setError(errorMessage(e, "review.pushFailed")); }
    finally { setBusy(false); }
  }

  async function confirmDeployed() {
    if (!result) return;
    setConfirming(true); setError(null);
    try {
      const ids = result.results.filter((r) => r.status === "deployed").map((r) => r.rule_id);
      const done = await api.bulkConfirmRules(ids);
      toast.show(t("rules.bulk.confirmedAll", { count: done.confirmed }), "success");
      onDone();
      onClose();
    } catch (e) { setError(errorMessage(e, "rules.confirmFailed")); }
    finally { setConfirming(false); }
  }

  const waitingForConfirm = result?.results.some((r) => r.status === "deployed" && r.rule?.deployments.some((d) => d.status === "pending"));

  return (
    <Modal wide title={t("rules.bulk.title", { count: rules.length })} onClose={() => { if (!busy && !confirming) onClose(); }}>
      {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      {!result && <>
        <p className="mb-4 text-sm text-slate-500">{t("rules.bulk.intro")}</p>
        {loaded === null && <Loading className="py-4">{t("common.loading")}</Loading>}
        {loaded !== null && <div className="space-y-3">
          {loaded.map(({ rule, plan, error: loadError }) => {
            const eligible = !!plan?.can_deploy;
            return (
              <div key={rule.id} className={`rounded-xl border p-3 ${eligible ? "border-slate-200" : "border-slate-100 opacity-80"}`}>
                <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                  <strong className="break-words">{rule.description}</strong>
                  {!eligible && <Badge tone="neutral">{t("rules.bulk.notDeployable")}</Badge>}
                </div>
                {loadError && <p className="mt-2 text-sm text-red-700">{t("rules.bulk.previewFailed", { error: loadError })}</p>}
                {plan && !eligible && plan.warnings.length === 0 && <p className="mt-2 text-sm text-slate-500">{t("rules.bulk.noChange")}</p>}
                {plan && plan.warnings.map((w) => <p key={w} className="mt-2 rounded-lg bg-flame-50 p-2 text-sm text-flame-800">{ts(w)}</p>)}
                {plan && eligible && <>
                  <ul className="mt-2 space-y-1 text-sm">
                    {plan.hosts.map((h) => <li key={h.host_id} className="flex justify-between gap-2"><span>{h.host_name}</span><span className="text-sky-700">{t(`review.actions.${h.action}`)}</span></li>)}
                  </ul>
                  <div className="mt-2"><OverlapHint ruleId={rule.id} compact /></div>
                  <div className="mt-2"><ManagementGuardHint ruleId={rule.id} compact onChange={setGuard(rule.id)} /></div>
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-slate-500">{t("rules.bulk.details")}</summary>
                    {plan.hosts.filter((h) => h.changes.length > 0 && h.action !== "disabled").map((h) => (
                      <table key={h.host_id} className="mt-2 w-full table-fixed text-left text-xs">
                        <caption className="mb-1 text-left font-medium text-slate-600">{h.host_name}</caption>
                        <thead><tr className="text-slate-400"><th className="w-1/3">{t("review.colField")}</th><th>{t("review.colBefore")}</th><th>{t("review.colDraft")}</th></tr></thead>
                        <tbody>{h.changes.map((c) => <tr key={c.field} className="border-t border-slate-100"><td className="break-words py-1.5 pr-2">{fieldLabel(c.field)}</td><td className="break-words pr-2 text-slate-500">{c.before ?? "—"}</td><td className="break-words text-blue-800">{c.after || "—"}</td></tr>)}</tbody>
                      </table>
                    ))}
                  </details>
                </>}
              </div>
            );
          })}
        </div>}
        {loaded !== null && <div className="mt-4 space-y-2">
          {deployable.length > 0 && <DeployCommentField value={comment} onChange={setComment} required={commentRequired} disabled={busy} />}
          <p className="text-sm text-slate-600">{deployable.length === 0 ? t("rules.bulk.nothingToDeploy") : t("rules.bulk.willDeploy", { count: deployable.length, total: loaded.length })}</p>
          <div className="flex flex-wrap gap-2">
            <Button disabled={busy || deployable.length === 0 || unacknowledged > 0 || (commentRequired && !comment.trim())} loading={busy} onClick={deploy}>{busy ? t("rules.bulk.deploying") : t("rules.bulk.deploy", { count: deployable.length })}</Button>
            <Button variant="ghost" disabled={busy} onClick={onClose}>{t("rules.bulk.close")}</Button>
          </div>
          {unacknowledged > 0 && <p className="text-xs text-red-700">{t("rules.management.needsAck")}</p>}
          <p className="text-xs text-slate-500">{t("review.pushShort")}</p>
        </div>}
      </>}

      {result && <div className="space-y-4">
        <p className="text-sm text-slate-700">{t("rules.bulk.summary", { deployed: result.deployed, attention: result.attention, failed: result.failed, skipped: result.skipped })}</p>
        <ul className="space-y-2">
          {result.results.map((r) => (
            <li key={r.rule_id} className="rounded-xl border border-slate-200 p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <strong className="break-words">{byId.get(r.rule_id)?.description ?? r.rule_id}</strong>
                <Badge tone={STATUS_TONE[r.status]}>{t(`rules.bulk.status.${r.status}`)}</Badge>
              </div>
              {r.error && <p className="mt-2 break-words text-red-700">{errorMessage(r.error)}</p>}
              {r.rule?.deployments.filter((d) => d.message).map((d) => <p key={d.host_id} className="mt-2 break-words text-red-700">{ts(d.message ?? "")}</p>)}
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-2">
          {waitingForConfirm && <Button disabled={confirming} loading={confirming} onClick={confirmDeployed}>{t("rules.bulk.confirmDeployed")}</Button>}
          <Button variant="ghost" disabled={confirming} onClick={onClose}>{t("rules.bulk.close")}</Button>
        </div>
        {waitingForConfirm && <p className="text-xs text-slate-500">{t("review.pushShort")}</p>}
      </div>}
    </Modal>
  );
}
