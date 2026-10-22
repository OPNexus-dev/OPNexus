"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Tooltip } from "@/components/ui/Tooltip";
import { CleanupItem, CleanupItemState, CleanupOperation, CleanupOperationStatus } from "@/lib/api";
import { cleanupMessage, COUNTDOWN_STATUSES, currentUuid, formatClock, latestItemIds, secondsUntil } from "@/lib/cleanup";
import { CircleCheck, Clock, RotateCcw, Undo2 } from "lucide-react";
import { forwardRef, ReactNode } from "react";

const CONFIRMABLE: CleanupOperationStatus[] = ["awaiting_confirm", "partial", "apply_failed"];

const STATUS_TONE: Record<CleanupOperationStatus, BadgeTone> = {
  preview: "neutral", running: "info", awaiting_confirm: "warning", confirmed: "success", partial: "danger",
  apply_failed: "danger", reverting: "info", reverted: "neutral", revert_failed: "danger", done: "success",
  failed: "danger", cancelled: "neutral", expired: "neutral",
};

const ITEM_TONE: Record<CleanupItemState, BadgeTone> = {
  queued: "neutral", disabling: "info", disabled: "warning", confirmed: "success", reverting: "info", reverted: "neutral",
  deleting: "info", deleted: "neutral", restoring: "info", restored: "success", failed: "danger", unknown: "danger",
  revert_failed: "danger", skipped: "neutral",
};

function useStateLabel() {
  const { t } = useLanguage();
  return (kind: string, state: CleanupItemState) => {
    // For "enable" the item states mean switching on, not off.
    if (kind === "enable" && (state === "disabling" || state === "disabled")) return t(`cleanup.stateEnable.${state}`);
    return t(`cleanup.state.${state}`);
  };
}

/** The operation's own message, unless a rule below already says the same (a restore warning sits on both). */
function ownMessage(t: (key: string) => string, operation: CleanupOperation): string {
  const message = cleanupMessage(t, operation.message);
  return message && !operation.items.some((i) => cleanupMessage(t, i.message) === message) ? message : "";
}

function ItemRows({ operation, actions }: { operation: CleanupOperation; actions?: (item: CleanupItem) => ReactNode }) {
  const { t } = useLanguage();
  const label = useStateLabel();
  const clustered = new Set(operation.items.map((i) => i.host_id)).size > 1;
  return (
    <ul className="divide-y divide-slate-100 text-sm">
      {operation.items.map((item) => {
        const message = cleanupMessage(t, item.message);
        return (
          <li key={item.id} className="flex flex-wrap items-start gap-x-3 gap-y-1.5 py-2">
            <div className="min-w-0 flex-1">
              <p className="break-words font-medium text-slate-900">{item.description || item.rule_uuid}</p>
              {clustered && item.host_name && (
                <p className="text-xs text-slate-500">{item.host_name}{item.carp_role ? ` · ${item.carp_role}` : ""}</p>
              )}
              {message && <p className="mt-0.5 break-words text-xs text-slate-600">{message}</p>}
            </div>
            <Badge tone={ITEM_TONE[item.state]}>{label(operation.kind, item.state)}</Badge>
            {item.restored_uuid && item.state === "deleted" && <Badge tone="success">{t("cleanup.hist.restoredBadge")}</Badge>}
            {actions && <div className="flex flex-wrap gap-1.5">{actions(item)}</div>}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The one operation that is open on the host/cluster: what happened, how long the automatic revert still waits and the
 * buttons for the decision (confirm = "I still have access", revert now, try again). Focusable so a roll-out can put
 * the keyboard focus here.
 */
export const OperationBanner = forwardRef<HTMLElement, {
  operation: CleanupOperation; now: number; busy: string | null; readOnly: boolean; notAdmin: boolean; operatorsMayDeploy: boolean;
  onConfirm: () => void; onRevert: () => void; onRetry: () => void;
}>(function OperationBanner({ operation, now, busy, readOnly, notAdmin, operatorsMayDeploy, onConfirm, onRevert, onRetry }, ref) {
  const { t, locale } = useLanguage();
  const left = COUNTDOWN_STATUSES.includes(operation.status) ? secondsUntil(operation.revert_deadline, now) : null;
  const message = ownMessage(t, operation);
  const revertBlocked = notAdmin && !operatorsMayDeploy;
  const confirmHint = operation.status === "partial" ? t("cleanup.op.confirmPartial") : operation.status === "apply_failed" ? t("cleanup.op.confirmApplyFailed") : null;
  return (
    <section
      ref={ref}
      tabIndex={-1}
      aria-labelledby={`cleanup-op-${operation.id}`}
      className="mb-6 rounded-2xl border border-flame-300 bg-[rgb(var(--tone-warning-bg))] p-4 outline-none focus-visible:ring-2 focus-visible:ring-brand-500 sm:p-5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 id={`cleanup-op-${operation.id}`} className="text-base font-semibold text-slate-900">{t(`cleanup.op.title.${operation.kind}`)}</h3>
        <Tooltip content={t(`cleanup.status.${operation.status}_hint`)} focusable>
          <Badge tone={STATUS_TONE[operation.status]}>{t(`cleanup.status.${operation.status}`)}</Badge>
        </Tooltip>
        <span className="ml-auto text-xs text-slate-500">
          {operation.actor}{operation.created_at ? ` · ${new Date(operation.created_at).toLocaleString(locale)}` : ""}
        </span>
      </div>
      <p className="mt-2 text-sm text-slate-700">{t(`cleanup.op.state.${operation.status}`)}</p>
      {left !== null && (
        <p className="mt-2 flex items-center gap-1.5 text-sm font-medium text-flame-700">
          <Clock size={15} aria-hidden="true" />
          <span aria-hidden="true">{t("cleanup.op.countdown", { time: formatClock(left) })}</span>
          <span className="sr-only" role="status" aria-live="polite">{t("cleanup.op.countdownMinutes", { n: Math.ceil(left / 60) })}</span>
        </p>
      )}
      {message && <p role="status" className="mt-2 break-words rounded-lg bg-red-50 p-2.5 text-sm text-red-700">{message}</p>}
      <div className="mt-3 rounded-xl bg-white/70 px-3"><ItemRows operation={operation} /></div>
      {operation.warnings.map((code) => <p key={code} className="mt-2 text-sm text-flame-800">{cleanupMessage(t, code)}</p>)}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {CONFIRMABLE.includes(operation.status) && operation.kind !== "delete" && operation.kind !== "restore" && !readOnly && (
          <Tooltip content={!operation.can_confirm ? (confirmHint ?? t("cleanup.op.confirmNotYet")) : null} focusable={!operation.can_confirm}>
            <Button onClick={onConfirm} disabled={!operation.can_confirm || busy !== null} loading={busy === "confirm"}>
              <CircleCheck size={15} aria-hidden="true" />{t("cleanup.op.confirm")}
            </Button>
          </Tooltip>
        )}
        {operation.can_revert && !readOnly && (
          <Tooltip content={revertBlocked ? t("cleanup.op.revertNeedsAdmin") : null} focusable={revertBlocked}>
            <Button variant="outline" onClick={onRevert} disabled={revertBlocked || busy !== null} loading={busy === "revert"}>
              <Undo2 size={15} aria-hidden="true" />{t("cleanup.op.revert")}
            </Button>
          </Tooltip>
        )}
        {operation.can_retry && !readOnly && (
          <Tooltip content={notAdmin ? t("cleanup.op.retryNeedsAdmin") : null} focusable={notAdmin}>
            <Button variant="secondary" onClick={onRetry} disabled={notAdmin || busy !== null} loading={busy === "retry"}>
              <RotateCcw size={15} aria-hidden="true" />{t(operation.kind === "disable" || operation.kind === "enable" ? "cleanup.op.retryRevert" : "cleanup.op.retryCheck")}
            </Button>
          </Tooltip>
        )}
      </div>
      {operation.can_confirm && <p className="mt-3 text-xs text-slate-600">{t("cleanup.op.autoRevertNote")}</p>}
    </section>
  );
});

/** Finished operations of this host (and its cluster peers), newest first, with the follow-up action per rule. */
export function OperationHistory({ operations, hostId, now, minDays, readOnly, notAdmin, busy, onRestore, onFollowUp }: {
  operations: CleanupOperation[]; hostId: string; now: number; minDays: number; readOnly: boolean; notAdmin: boolean; busy: boolean;
  onRestore: (item: CleanupItem) => void;
  onFollowUp: (kind: "enable" | "delete", item: CleanupItem, uuid: string) => void;
}) {
  const { t, locale } = useLanguage();
  const latest = latestItemIds(operations, hostId);
  if (operations.length === 0) return <p className="py-2 text-sm text-slate-500">{t("cleanup.hist.empty")}</p>;
  return (
    <div className="space-y-3">
      {operations.map((operation) => (
        <Card key={operation.id} className="p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold text-slate-900">{t(`cleanup.op.title.${operation.kind}`)}</h4>
            <Tooltip content={t(`cleanup.status.${operation.status}_hint`)} focusable>
              <Badge tone={STATUS_TONE[operation.status]}>{t(`cleanup.status.${operation.status}`)}</Badge>
            </Tooltip>
            <span className="ml-auto text-xs text-slate-500">
              {operation.actor}{operation.created_at ? ` · ${new Date(operation.created_at).toLocaleString(locale)}` : ""}
            </span>
          </div>
          {ownMessage(t, operation) && <p className="mt-1.5 break-words text-xs text-slate-600">{ownMessage(t, operation)}</p>}
          <ItemRows
            operation={operation}
            actions={readOnly ? undefined : (item) => {
              const own = item.host_id === hostId && latest.has(item.id);
              const buttons: ReactNode[] = [];
              if (item.restorable && item.host_id === hostId) {
                buttons.push(
                  <Tooltip key="restore" content={notAdmin ? t("cleanup.hist.adminOnly") : t("cleanup.hist.restoreHint")} focusable>
                    <Button variant="outline" className="!px-2.5 !py-1 text-xs" disabled={notAdmin || busy} onClick={() => onRestore(item)}>{t("cleanup.hist.restore")}</Button>
                  </Tooltip>,
                );
              }
              const reenable = own && ((operation.kind === "disable" && item.state === "confirmed") || (operation.kind === "restore" && item.state === "restored"));
              if (own && operation.kind === "disable" && item.state === "confirmed") {
                const due = operation.confirmed_at ? new Date(operation.confirmed_at).getTime() + minDays * 86400000 : 0;
                const waiting = due > now;
                buttons.push(
                  <Tooltip key="delete" content={waiting ? t("cleanup.hist.deleteWait", { when: new Date(due).toLocaleDateString(locale) }) : t("cleanup.hist.deleteHint")} focusable>
                    <Button variant="outline" className="!px-2.5 !py-1 text-xs" disabled={waiting || busy} onClick={() => onFollowUp("delete", item, currentUuid(item, operation.kind))}>{t("cleanup.hist.delete")}</Button>
                  </Tooltip>,
                );
              }
              if (reenable) {
                buttons.push(
                  <Tooltip key="enable" content={t("cleanup.hist.enableHint")} focusable>
                    <Button variant="outline" className="!px-2.5 !py-1 text-xs" disabled={busy} onClick={() => onFollowUp("enable", item, currentUuid(item, operation.kind))}>{t("cleanup.hist.enable")}</Button>
                  </Tooltip>,
                );
              }
              return buttons;
            }}
          />
        </Card>
      ))}
    </div>
  );
}
