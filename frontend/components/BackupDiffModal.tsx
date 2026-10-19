"use client";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { api, BackupDiff } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** What changed between two stored backups of a host. Secrets arrive already replaced by hashes. */
export function BackupDiffModal({ hostId, older, newer, onClose }: { hostId: string; older: string; newer: string; onClose: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [diff, setDiff] = useState<BackupDiff | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getBackupDiff(hostId, older, newer).then(setDiff).catch((e) => setError(errorMessage(e, "backupDiff.failed")));
  }, [hostId, older, newer]);

  return (
    <Modal title={t("backupDiff.title")} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-slate-500">{t("backupDiff.hint")}</p>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {!diff && !error && <Skeleton className="h-24 w-full" />}
        {diff?.identical && <p className="text-sm text-slate-700">{t("backupDiff.identical")}</p>}
        {diff && !diff.identical && (
          <>
            <p className="text-sm text-slate-700">{t("backupDiff.summary", { added: diff.added, removed: diff.removed })}</p>
            <pre className="max-h-[50vh] overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 font-mono text-xs leading-5">
              {diff.lines.map((line, i) => (
                <div
                  key={i}
                  className={
                    line.startsWith("+") && !line.startsWith("+++") ? "bg-emerald-500/15 text-emerald-800"
                    : line.startsWith("-") && !line.startsWith("---") ? "bg-red-500/15 text-red-800"
                    : line.startsWith("@@") ? "text-sky-700" : "text-slate-600"
                  }
                >
                  {line || " "}
                </div>
              ))}
            </pre>
            {diff.truncated && <p className="text-sm text-amber-700">{t("backupDiff.truncated")}</p>}
          </>
        )}
        <div className="flex justify-end pt-1">
          <Button onClick={onClose}>{t("tokens.done")}</Button>
        </div>
      </div>
    </Modal>
  );
}
