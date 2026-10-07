"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Spinner } from "@/components/ui/Spinner";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { api, CarpSwitchPreview, Host, MaintenanceState } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import type { TranslationParams } from "@/lib/i18n";
import { useReadOnly } from "@/lib/useReadOnly";
import { ArrowLeftRight, ArrowRight } from "lucide-react";
import { useState } from "react";

export function carpMaintenanceWarning(host: Host, state: MaintenanceState, t: (key: string, params?: TranslationParams) => string): string | null {
  if (!host.cluster_name || state.carp_maintenance_mode) return null;
  const peer = state.peers?.find((peer) => peer.id !== host.id && peer.carp_maintenance_mode);
  return peer ? t("carp.peerInMaintenance", { peer: peer.name }) : null;
}

export function CarpSwitchControl({ host, state, disabled = false, onChanged }: {
  host: Host;
  state: MaintenanceState;
  disabled?: boolean;
  onChanged: () => unknown | Promise<unknown>;
}) {
  const readOnly = useReadOnly();
  const { t, errorMessage, locale } = useLanguage();
  const [plan, setPlan] = useState<CarpSwitchPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [ack, setAck] = useState(false);
  const [autoRevertMinutes, setAutoRevertMinutes] = useState<number | "">(30);
  const maintenanceMode = Boolean(state.carp_maintenance_mode);
  const role = state.carp_status ?? host.carp_status;
  if (!host.cluster_name || (!maintenanceMode && role !== "MASTER")) return null;

  const enter = !maintenanceMode;
  // Entering maintenance on a second node while one is already demoted
  // leaves both stuck (observed live twice) — steer to the right host
  // instead of letting this button open a preview that the backend would
  // reject anyway.
  const peerInMaintenance = enter ? state.peers?.find((peer) => peer.id !== host.id && peer.carp_maintenance_mode) : undefined;
  if (peerInMaintenance) return null;
  async function preview() {
    setBusy(true);setError(null);
    try {
      setPlan(await api.previewCarpSwitch(host.id));setName("");setAck(false);
    } catch (caught) {
      setError(errorMessage(caught, "carp.previewFailed"));
    } finally { setBusy(false); }
  }
  async function execute() {
    if (!plan) return;
    setBusy(true);setError(null);
    try {
      await api.executeCarpSwitch(host.id,plan.preview_id,name,ack,plan.action==="carp_enter"?(autoRevertMinutes||null):null);
      setPlan(null);
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught, "carp.outcomeUnclear"));
      setPlan(null);
      await onChanged();
    } finally { setBusy(false); }
  }

  return <>
    <div>
      <Button variant={enter ? "danger" : "secondary"} disabled={readOnly || disabled || busy} onClick={() => void preview()}>
        {busy ? <Spinner size={13}/> : <ArrowLeftRight size={14}/>} {enter ? t("carp.switchToPartner") : t("carp.restoreNormal")}
      </Button>
      {error && <p role="alert" className="mt-1 max-w-sm text-xs text-red-600">{error}</p>}
    </div>
    {plan && <Modal title={plan.action === "carp_enter" ? t("carp.switchToPartner") : t("carp.restoreNormal")} onClose={() => { if (!busy) setPlan(null); }}>
      <div className="space-y-4 text-sm">
        <p className="flex items-center gap-2 font-medium text-flame-800">
          <ArrowLeftRight size={16} className="shrink-0" />
          {plan.action === "carp_enter" ? t("carp.handoverShort", { host: host.name }) : t("carp.leaveShort")}
          <InfoTip text={plan.action === "carp_enter" ? t("carp.enterDescription", { host: host.name }) : t("carp.leaveDescription", { host: host.name })} />
        </p>
        <div className="divide-y divide-slate-200 rounded-xl border border-slate-200">
          {plan.state.peers.map((peer) => {
            const current = peer.carp_status ?? peer.status;
            const expected = plan.action === "carp_enter" ? (peer.id === host.id ? "BACKUP" : "MASTER") : null;
            return (
              <p key={peer.id} className="flex items-center justify-between gap-3 px-3 py-2">
                <span className="truncate">{peer.name}</span>
                <span className="flex shrink-0 items-center gap-1.5 font-semibold">{current}{expected && <><ArrowRight size={14} className="text-slate-400" aria-label={t("carp.expectedRole")} />{expected}</>}</span>
              </p>
            );
          })}
        </div>
        <p className="flex items-center gap-1.5 text-xs text-slate-500">
          {t("carp.previewValidShort", { time: new Date(plan.expires_at).toLocaleTimeString(locale) })}
          <InfoTip text={t("carp.previewValidTip")} size={13} />
        </p>
        {plan.state.auto_revert_window && (
          <Badge tone="info">{t("carp.autoRevertRunning", { time: new Date(plan.state.auto_revert_window.auto_revert_at).toLocaleString(locale) })}</Badge>
        )}
        {plan.action === "carp_enter" && (
          <div className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-1">{t("carp.autoRevertAfter")}<InfoTip text={t("carp.autoRevertHint")} size={13} /></span>
            <Select className="!w-auto" aria-label={t("carp.autoRevertAfter")} value={String(autoRevertMinutes)} onChange={(event) => setAutoRevertMinutes(event.target.value === "" ? "" : Number(event.target.value))}>
              <option value="15">{t("carp.minutes", { n: 15 })}</option>
              <option value="30">{t("carp.minutes", { n: 30 })}</option>
              <option value="60">{t("carp.minutes", { n: 60 })}</option>
              <option value="">{t("carp.never")}</option>
            </Select>
          </div>
        )}
        <label className="block"><span className="flex items-center gap-1">{t("carp.confirmHostname")}<InfoTip text={t("updates.tipConfirmName")} size={13} /></span><Input className="mt-1" autoFocus autoComplete="off" spellCheck={false} aria-label={t("carp.confirmHostnameAria")} value={name} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !busy && name === host.name && ack) void execute(); }} placeholder={host.name}/></label>
        <label className="flex items-start gap-2"><input className="mt-0.5" type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)}/><span className="flex items-start gap-1">{plan.action === "carp_enter" ? t("carp.ackEnterShort") : t("carp.ackLeaveShort")}<InfoTip text={`${plan.action === "carp_enter" ? t("carp.ackEnter") : t("carp.ackLeave")}${t("carp.ackCommon")}`} size={13} className="mt-0.5" /></span></label>
        {error && <p role="alert" className="text-red-600">{error}</p>}
        <Tooltip focusable={busy || name !== host.name || !ack} content={name !== host.name ? t("updates.confirmRunDisabledName") : !ack ? t("updates.confirmRunDisabledAck") : null}>
          <Button variant="danger" disabled={busy || name !== host.name || !ack} onClick={() => void execute()}>{busy ? <Spinner size={14}/> : <ArrowLeftRight size={14}/>} {plan.action === "carp_enter" ? t("carp.executeEnter") : t("carp.executeLeave")}</Button>
        </Tooltip>
      </div>
    </Modal>}
  </>;
}
