"use client";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle } from "lucide-react";
import { useState } from "react";

// Two-step warning gate for ACME's register/sign actions — mechanically
// the same "armed" pattern as IpsecTunnelReview.tsx/OpenVpnInstanceReview.
// tsx/WireguardTunnelReview.tsx (explicit second click before the API
// call fires), but with DIFFERENT wording: the risk here is contacting a
// REAL external Certificate Authority over the internet and counting
// against its rate limits, not a connectivity-loss/lockout risk the way
// the VPN features' warning is about — see the plan / README.
export function AcmeActionConfirm({ title, actionLabel, busyLabel, onConfirm, onClose }: {
  title: string;
  actionLabel: string;
  busyLabel: string;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onClose();
    } catch (e) {
      setError(errorMessage(e, "acme.actionFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={title} onClose={() => { if (!busy) onClose(); }}>
      <div className="space-y-4">
        <div role="alert" className="flex items-start gap-2 rounded-xl border-2 border-red-300 bg-red-50 p-4 text-sm font-medium text-red-800">
          <AlertTriangle size={20} className="mt-0.5 shrink-0" />
          <span>{t("acme.externalCaWarning")}</span>
        </div>
        {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        {!armed && (
          <Button variant="danger" disabled={busy} onClick={() => setArmed(true)}>
            {t("acme.warningUnderstood")}
          </Button>
        )}
        {armed && (
          <div className="space-y-2 rounded-xl border-2 border-red-300 bg-red-50 p-4">
            <p className="text-sm font-medium text-red-800">{t("acme.areYouSure", { action: actionLabel })}</p>
            <div className="flex gap-2">
              <Button variant="ghost" disabled={busy} onClick={() => setArmed(false)}>{t("common.cancel")}</Button>
              <Button variant="danger" loading={busy} onClick={run}>{busy ? busyLabel : t("acme.yes", { action: actionLabel.toLowerCase() })}</Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
