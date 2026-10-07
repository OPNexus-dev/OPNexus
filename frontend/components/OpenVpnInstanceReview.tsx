"use client";

import { api, OpenVpnPreview, OpenVpnInstance, OpenVpnInstanceBase, OpenVpnInstanceRevision } from "@/lib/api";
import { InfoTip } from "@/components/ui/Tooltip";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";

import { Loading } from "@/components/ui/Loading";
// Hard-to-miss, non-breezy warning shown before every apply action —
// wording matches backend/app/services/openvpn_service.py's
// NO_ROLLBACK_WARNING constant byte-for-byte, the same technique already
// used for IpsecTunnelReview.tsx. Deliberately NOT a toast: the user must
// actively arm the push (see `armed` below) before the API call that
// writes config + reconfigures OpenVPN can fire at all.
const NO_ROLLBACK_WARNING =
  "Kein automatischer Rollback: falls dieser Host nur über den OpenVPN-Tunnel erreichbar ist, den du gerade änderst, " +
  "kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen.";

export function OpenVpnInstanceReview({ instance, mode, onClose, onDeployed, onRestore, onChanged }: {
  instance: OpenVpnInstance; mode: "preview" | "history"; onClose: () => void;
  onDeployed: () => void; onChanged: () => void; onRestore: (snapshot: OpenVpnInstanceBase) => void;
}) {
  const [plan, setPlan] = useState<OpenVpnPreview | null>(null);
  const [revisions, setRevisions] = useState<OpenVpnInstanceRevision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The explicit "are you sure" gate: armed only after the user has
  // actively acknowledged the no-rollback warning for THIS push attempt.
  // Reset whenever the plan reloads so a stale confirmation can never
  // carry over to a changed plan.
  const [armed, setArmed] = useState(false);
  const toast = useToast();
  const { t, ts, errorMessage, locale } = useLanguage();

  useEffect(() => {
    let active = true;
    setLoading(true);
    setArmed(false);
    async function load() {
      try {
        if (mode === "preview") { const data = await api.previewOpenVpnInstance(instance.id); if (active) setPlan(data); }
        else { const data = await api.openVpnInstanceRevisions(instance.id); if (active) setRevisions(data); }
      } catch (e) { if (active) setError(errorMessage(e, "review.loadFailed")); }
      finally { if (active) setLoading(false); }
    }
    load();
    return () => { active = false; };
  }, [instance.id, mode, reload, errorMessage]);

  async function push() {
    if (!plan || !armed) return;
    setBusy(true); setError(null);
    try { await api.deployOpenVpnInstance(instance.id, plan.token); toast.show(t("review.instance.deployed", { name: instance.description }), "success"); onDeployed(); }
    catch (e) { setError(errorMessage(e, "review.pushFailed")); setPlan(null); setArmed(false); }
    finally { setBusy(false); }
  }

  async function removeTarget(host: OpenVpnPreview["hosts"][number]) {
    if (!plan) return;
    if (!confirm(t("review.instance.removeConfirm", { name: instance.description, host: host.host_name }))) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      await api.removeOpenVpnInstancePreviousTarget(instance.id, host.host_id, plan.token);
      setNotice(t("review.instance.removed", { host: host.host_name }));
    } catch (e) { setError(errorMessage(e, "review.actionFailed")); }
    finally { setBusy(false); setPlan(null); setReload(n => n + 1); onChanged(); }
  }

  return <Modal title={mode === "preview" ? t("review.previewTitle") : t("review.instance.historyTitle")} onClose={() => { if (!busy) onClose(); }}>
    <p className="mb-4 font-medium text-slate-700">{instance.description}</p>
    {loading && <Loading className="py-4">{t("common.loading")}</Loading>}
    {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
    {notice && <p role="status" className="mb-4 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}
    {!loading && <Button variant="ghost" disabled={busy} onClick={() => { setError(null); setPlan(null); setReload(n => n + 1); }}>{t("review.reload")}</Button>}
    {mode === "preview" && plan && <div className="space-y-4">
      <div role="alert" className="flex items-start gap-2 rounded-xl border-2 border-red-300 bg-red-50 p-4 text-sm font-medium text-red-800">
        <AlertTriangle size={20} className="mt-0.5 shrink-0" />
        <span>{ts(NO_ROLLBACK_WARNING)}</span>
      </div>
      <p className="text-sm text-slate-500">{ts(plan.basis)}</p>
      {plan.warnings.filter(w => w !== NO_ROLLBACK_WARNING).map(w => <p key={w} className="rounded-lg bg-flame-50 p-3 text-sm text-flame-800">{ts(w)}</p>)}
      {plan.hosts.length === 0 && <div className="space-y-2"><p className="text-sm text-slate-500">{t("review.instance.noTargets")}</p><Button onClick={() => onRestore(instance)}>{t("review.assignTargets")}</Button></div>}
      {plan.hosts.map(h => <div key={h.host_id} className="rounded-xl border border-slate-200 p-3">
        <div className="flex flex-wrap justify-between gap-2 text-sm"><strong>{h.host_name}</strong><span className="text-sky-700">{t(`review.actions.${h.action}`)}</span></div>
        {h.message && <p className="mt-2 break-words text-sm text-red-700">{ts(h.message)}</p>}
        {h.can_remove && <Button className="mt-3" variant="danger" disabled={busy || loading} onClick={() => removeTarget(h)}>{t("review.removeFromHost")}</Button>}
        {h.action === "disabled" && <p className="mt-2 text-sm text-slate-500">Host ist deaktiviert.</p>}
        {h.changes.length > 0 && h.action !== "disabled" && <table className="mt-3 w-full table-fixed text-left text-xs"><thead><tr className="text-slate-400"><th className="w-1/3">{t("review.colField")}</th><th>{t("review.colBefore")}</th><th>{t("review.colDraft")}</th></tr></thead><tbody>
          {h.changes.map(c => <tr key={c.field} className="border-t border-slate-100"><td className="break-words py-2 pr-2">{ts(c.field)}</td><td className="break-words pr-2 text-slate-500">{String(c.before ?? "—")}</td><td className="break-words text-blue-800">{String(c.after ?? "—")}</td></tr>)}
        </tbody></table>}
      </div>)}
      {!plan.can_deploy && plan.hosts.length > 0 && plan.hosts.every(h => h.action === "unchanged") && <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{t("review.allInSync")}</p>}
      {plan.can_deploy && !armed && <Button variant="danger" disabled={busy || loading} onClick={() => setArmed(true)}>{t("review.armWarning")}</Button>}
      {plan.can_deploy && armed && <div className="space-y-2 rounded-xl border-2 border-red-300 bg-red-50 p-4">
        <p className="text-sm font-medium text-red-800">{t("review.armSure")}</p>
        <div className="flex gap-2">
          <Button variant="ghost" disabled={busy} onClick={() => setArmed(false)}>{t("common.cancel")}</Button>
          <Button variant="danger" disabled={busy} onClick={push} loading={busy}>{busy ? t("review.pushing") : t("review.yesDeploy")}</Button>
        </div>
      </div>}
    </div>}
    {mode === "history" && !loading && <div className="space-y-3">
      <p className="flex items-center gap-1.5 text-sm font-medium text-slate-700">{t("review.historyTitle")}<InfoTip text={t("review.historyHintStaticKey")} /></p>
      {revisions.length === 0 && <p className="py-3 text-sm text-slate-500">{t("review.instance.noHistory")}</p>}
      {revisions.map((r, i) => <div key={r.id} className="rounded-xl border border-slate-200 p-3 text-sm">
        <div className="flex justify-between gap-2"><strong>{t("review.version", { n: revisions.length - i })}</strong><span className="text-slate-500">{new Date(r.created_at).toLocaleString(locale)}</span></div>
        <p className="mt-1 text-slate-500">{ts(r.note)}</p>
        <p className="my-2 break-words">{r.snapshot.description} · {r.snapshot.role} · {r.snapshot.role === "server" ? r.snapshot.server_network : r.snapshot.remote}</p>
        <Button variant="ghost" onClick={() => onRestore(r.snapshot)}>{t("review.adoptAsDraft")}</Button>
      </div>)}
    </div>}
  </Modal>;
}
