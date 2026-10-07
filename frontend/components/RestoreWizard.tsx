"use client";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Tooltip } from "@/components/ui/Tooltip";
import { api, RestoreOperation } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";

// Deliberately not a toast, same convention as IpsecTunnelReview.tsx's
// NO_ROLLBACK_WARNING — this is the point where OPNexus stops being able to
// help at all: the actual restore happens by hand in the OPNsense GUI.
export function RestoreWizard({
  hostId, hostName, backupId, onClose, onChanged,
}: { hostId: string; hostName: string; backupId: string; onClose: () => void; onChanged: () => void }) {
  const { t, ts, errorMessage } = useLanguage();
  const [op, setOp] = useState<RestoreOperation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmName, setConfirmName] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    api.previewRestore(hostId, backupId)
      .then((data) => { if (active) setOp(data); })
      .catch((e) => { if (active) setError(errorMessage(e, "restore.previewFailed")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [hostId, backupId, errorMessage]);

  async function handoff() {
    if (!op) return;
    setBusy(true); setError(null);
    try { setOp(await api.handoffRestore(hostId, op.id, confirmName)); setConfirmName(""); }
    catch (e) { setError(errorMessage(e, "restore.handoffFailed")); }
    finally { setBusy(false); }
  }

  async function confirm() {
    if (!op) return;
    setBusy(true); setError(null);
    try { setOp(await api.confirmRestore(hostId, op.id, confirmName, message || undefined)); onChanged(); }
    catch (e) { setError(errorMessage(e, "restore.confirmFailed")); }
    finally { setBusy(false); }
  }

  async function cancel() {
    if (!op) return;
    setBusy(true); setError(null);
    try { setOp(await api.cancelRestore(hostId, op.id)); onChanged(); }
    catch (e) { setError(errorMessage(e, "restore.cancelFailed")); }
    finally { setBusy(false); }
  }

  const canClose = !op || op.status !== "handed_off";

  return (
    <Modal title={t("restore.title")} onClose={() => { if (!busy && canClose) onClose(); }}>
      <p className="mb-4 font-medium text-slate-700">{t("restore.hostBackup", { host: hostName, backup: backupId })}</p>
      {loading && <p className="py-4 text-sm text-slate-500">{t("restore.checking")}</p>}
      {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      {op && op.status === "preview" && (
        <div className="space-y-4">
          {op.warnings.map((w) => <p key={w} className="rounded-lg bg-flame-50 p-3 text-sm text-flame-800">{ts(w)}</p>)}
          <div className="rounded-xl border border-slate-200 p-3 text-sm">
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5">
              <dt className="text-slate-500">{t("restore.hostname")}</dt><dd>{op.manifest.hostname}</dd>
              <dt className="text-slate-500">{t("restore.domain")}</dt><dd>{op.manifest.domain}</dd>
              <dt className="text-slate-500">WebGUI</dt><dd>{op.manifest.webgui_protocol}:{op.manifest.webgui_port}</dd>
              <dt className="text-slate-500">Interfaces</dt>
              <dd>{op.manifest.interfaces.map((i) => `${i.name} (${i.if})`).join(", ") || t("restore.unknown")}</dd>
              <dt className="text-slate-500">{t("restore.haSync")}</dt>
              <dd>{op.manifest.hasync_enabled ? `${t("restore.active")}${op.manifest.synchronize_to_ip ? ` → ${op.manifest.synchronize_to_ip}` : ""}` : t("restore.inactive")}</dd>
              <dt className="text-slate-500">{t("restore.secretFields")}</dt>
              <dd>{t("restore.secretsFound", { count: op.manifest.secret_fields_redacted })}</dd>
            </dl>
          </div>
          <div role="alert" className="flex items-start gap-2 rounded-xl bg-flame-50 p-3 text-sm font-medium text-flame-800">
            <AlertTriangle size={18} className="mt-0.5 shrink-0" />
            <span>{t("restore.handoffWarning")}</span>
          </div>
          <label className="block text-sm">
            {t("restore.typeHostname", { host: hostName })}
            <Input
              className="mt-1" autoFocus autoComplete="off" spellCheck={false} aria-label={t("restore.typeHostnameAria")}
              value={confirmName} placeholder={hostName} onChange={(e) => setConfirmName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !busy && confirmName === hostName) void handoff(); }}
            />
          </label>
          <div className="flex gap-2">
            <Button variant="ghost" disabled={busy} onClick={onClose}>{t("common.cancel")}</Button>
            <Tooltip focusable={busy || confirmName !== hostName} content={confirmName !== hostName ? t("updates.confirmRunDisabledName") : null}>
              <Button variant="danger" disabled={busy || confirmName !== hostName} onClick={handoff} loading={busy}>
                {busy ? t("restore.handingOff") : t("restore.yesHandoff")}
              </Button>
            </Tooltip>
          </div>
        </div>
      )}

      {op && op.status === "handed_off" && (
        <div className="space-y-4">
          <p className="rounded-lg bg-sky-50 p-3 text-sm text-sky-800">{ts(op.message ?? "")}</p>
          <label className="block text-sm text-slate-700">
            {t("restore.typeHostname", { host: hostName })}
            <Input
              className="mt-1" autoFocus autoComplete="off" spellCheck={false} aria-label={t("restore.typeHostnameAria")}
              value={confirmName} placeholder={hostName} onChange={(e) => setConfirmName(e.target.value)}
            />
          </label>
          <label className="block text-sm text-slate-700">
            {t("restore.noteOptional")}
            <textarea
              className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
              rows={2} value={message} onChange={(e) => setMessage(e.target.value)}
            />
          </label>
          <div className="flex gap-2">
            <Button variant="ghost" disabled={busy} onClick={cancel}>{t("restore.cancel")}</Button>
            <Button variant="danger" disabled={busy || confirmName !== hostName} onClick={confirm} loading={busy}>
              {busy ? t("restore.confirming") : t("restore.confirm")}
            </Button>
          </div>
        </div>
      )}

      {op && op.status === "verified" && (
        <div className="space-y-4">
          <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{ts(op.message ?? "")}</p>
          <Button onClick={onClose}>{t("common.close")}</Button>
        </div>
      )}

      {op && op.status === "cancelled" && (
        <div className="space-y-4">
          <p className="rounded-lg bg-slate-100 p-3 text-sm text-slate-700">{t("restore.cancelled")}</p>
          <Button onClick={onClose}>{t("common.close")}</Button>
        </div>
      )}
    </Modal>
  );
}
