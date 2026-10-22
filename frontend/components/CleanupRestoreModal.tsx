"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { InfoTip } from "@/components/ui/Tooltip";
import { api, CleanupItem, CleanupOperation } from "@/lib/api";
import { cleanupMessage } from "@/lib/cleanup";
import { CircleCheck, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";

/**
 * Bring a deleted rule back from the content backup OPNexus took before deleting it. The rule returns switched OFF
 * (a new UUID, at its old position where that still exists); switching it on is a separate step with a confirmation
 * window. Admin only, hostname typed.
 */
export function CleanupRestoreModal({ host, item, onClose, onRestored }: {
  host: { id: string; name: string }; item: CleanupItem; onClose: () => void; onRestored: (operation: CleanupOperation) => void;
}) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [confirmName, setConfirmName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CleanupOperation | null>(null);
  const hintId = useId();
  const nameOk = confirmName === host.name;

  async function restore() {
    if (!nameOk) return;
    setBusy(true);
    setError(null);
    try {
      const operation = await api.restoreCleanupItem(host.id, item.id, confirmName);
      setResult(operation);
      onRestored(operation);
      if (operation.status === "done") toast.show(t("cleanup.restore.done", { rule: item.description ?? item.rule_uuid }), "success");
      else toast.show(cleanupMessage(t, operation.items.find((i) => i.message)?.message ?? operation.message) || t("cleanup.restore.failed"), "danger");
    } catch (e) {
      setError(errorMessage(e, "cleanup.restore.failed"));
      toast.show(errorMessage(e, "cleanup.restore.failed"), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={t("cleanup.restore.title")} onClose={() => { if (!busy) onClose(); }}>
      <div className="space-y-4">
        <p className="break-words font-medium text-slate-800">{item.description || item.rule_uuid}</p>
        {!result && (
          <>
            <p className="text-sm text-slate-600">
              {t("cleanup.restore.intro")}
              <InfoTip text={t("cleanup.restore.introHint")} size={13} className="ml-1 align-middle" />
            </p>
            <label className="block text-sm">
              <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("cleanup.preview.confirmName")}<InfoTip text={t("cleanup.preview.confirmNameHint")} size={13} /></span>
              <Input
                autoComplete="off"
                spellCheck={false}
                aria-label={t("cleanup.preview.confirmNameAria", { host: host.name })}
                aria-describedby={confirmName && !nameOk ? hintId : undefined}
                value={confirmName}
                placeholder={host.name}
                disabled={busy}
                onChange={(e) => setConfirmName(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && nameOk && !busy) void restore(); }}
              />
              {confirmName && !nameOk && <span id={hintId} className="mt-1 block text-xs text-slate-500">{t("cleanup.preview.deployDisabledName")}</span>}
            </label>
          </>
        )}
        {error && <p role="alert" className="break-words rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        {result && (
          <div role="status" className="space-y-2 text-sm">
            {result.status === "done"
              ? <p className="flex items-start gap-1.5 text-emerald-800"><CircleCheck size={16} className="mt-0.5 shrink-0" aria-hidden="true" />{t("cleanup.restore.done", { rule: item.description ?? item.rule_uuid })}</p>
              : <p className="break-words rounded-lg bg-red-50 p-3 text-red-700">{cleanupMessage(t, result.items.find((i) => i.message)?.message ?? result.message) || t("cleanup.restore.failed")}</p>}
            {result.warnings.map((code) => (
              <p key={code} className="flex items-start gap-1.5 rounded-lg bg-flame-50 p-3 text-flame-800"><TriangleAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" />{cleanupMessage(t, code)}</p>
            ))}
            <p className="text-slate-600">{t("cleanup.restore.next")}</p>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          {!result && <Button onClick={() => void restore()} disabled={!nameOk} loading={busy}>{t("cleanup.restore.action")}</Button>}
          <Button variant={result ? "primary" : "ghost"} onClick={onClose} disabled={busy}>{result ? t("common.close") : t("common.cancel")}</Button>
        </div>
      </div>
    </Modal>
  );
}
