"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Field";
import { Loading } from "@/components/ui/Loading";
import { Modal } from "@/components/ui/Modal";
import { Tooltip } from "@/components/ui/Tooltip";
import { api, RuleHistory, RuleHistoryChange, RuleHistoryEvent } from "@/lib/api";
import { History } from "lucide-react";
import { useEffect, useState } from "react";

/** One firewall on which the rule exists, with the rule's id there (ids differ between firewalls). */
export interface HistoryTarget { hostId: string; hostName: string; uuid: string; }

const KIND_TONE: Record<string, BadgeTone> = {
  added: "success", changed: "info", moved: "neutral", removed: "danger", enabled: "success", disabled: "warning",
};

function shorten(value: unknown): string {
  const text = value === null || value === undefined || value === "" ? "–" : typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 60 ? `${text.slice(0, 57)}…` : text;
}

function useOptionalText() {
  const { t } = useLanguage();
  // Unknown codes (a newer backend, an audit action we have no wording for) show raw instead of breaking the list.
  return (key: string, fallback: string) => {
    const text = t(key);
    return text === key ? fallback : text;
  };
}

function ChangeList({ changes }: { changes: RuleHistoryChange[] }) {
  const optional = useOptionalText();
  if (changes.length === 0) return null;
  return (
    <ul className="mt-1 space-y-0.5 text-xs text-slate-600">
      {changes.map((change) => (
        <li key={change.field} className="break-words">
          <span className="font-medium text-slate-700">{optional(`ruleHistory.field.${change.field}`, change.field)}</span>
          {": "}
          <span className="font-mono text-slate-500 line-through decoration-slate-300">{shorten(change.before)}</span>
          {" → "}
          <span className="font-mono text-slate-800">{shorten(change.after)}</span>
        </li>
      ))}
    </ul>
  );
}

function EventRow({ event }: { event: RuleHistoryEvent }) {
  const { t, locale } = useLanguage();
  const optional = useOptionalText();
  const label = event.source === "observed"
    ? optional(`ruleHistory.kind.${event.kind}`, event.kind)
    : event.source === "revision"
      ? t("ruleHistory.kind.revision")
      : optional(`ruleHistory.audit.${event.kind}`, event.kind);
  const tone: BadgeTone = event.source === "observed" ? KIND_TONE[event.kind] ?? "neutral" : "neutral";
  const showChanges = event.source === "revision" || event.kind === "changed" || event.kind === "moved";
  return (
    <li className="border-b border-slate-100 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Badge tone={tone}>{label}</Badge>
        {event.source === "audit" && event.status && <span className="text-xs text-slate-500">{event.status}</span>}
        <Tooltip content={t(`ruleHistory.origin.${event.origin}_hint`)} focusable>
          <Badge tone={event.origin === "opnexus" ? "accent" : "warning"}>{t(`ruleHistory.origin.${event.origin}`)}</Badge>
        </Tooltip>
        <time dateTime={event.at} className="text-xs text-slate-500">{new Date(event.at).toLocaleString(locale)}</time>
        {event.actor && <span className="text-xs text-slate-500">{t("ruleHistory.by", { actor: event.actor })}</span>}
      </div>
      {event.note && <p className="mt-1 text-sm text-slate-600">{event.note}</p>}
      {event.message && <p className="mt-1 break-words text-xs text-slate-500">{event.message}</p>}
      {showChanges && <ChangeList changes={event.changes} />}
    </li>
  );
}

export function RuleHistoryModal({ title, targets, onClose }: { title: string; targets: HistoryTarget[]; onClose: () => void }) {
  const { t, errorMessage, locale } = useLanguage();
  const [index, setIndex] = useState(0);
  const [history, setHistory] = useState<RuleHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const target = targets[Math.min(index, targets.length - 1)];

  useEffect(() => {
    if (!target) return;
    let active = true;
    setHistory(null);
    setError(null);
    api.getRuleHistory(target.hostId, target.uuid)
      .then((h) => active && setHistory(h))
      .catch((e) => active && setError(errorMessage(e, "ruleHistory.loadFailed")));
    return () => { active = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.hostId, target?.uuid]);

  return (
    <Modal title={t("ruleHistory.title", { rule: title })} onClose={onClose} wide>
      {targets.length > 1 && (
        <div className="mb-3">
          <label htmlFor="rule-history-host" className="mb-1 block text-sm font-medium text-slate-700">{t("ruleHistory.host")}</label>
          <Select id="rule-history-host" value={String(index)} onChange={(e) => setIndex(Number(e.target.value))}>
            {targets.map((item, i) => <option key={item.hostId} value={i}>{item.hostName}</option>)}
          </Select>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {!error && history === null && <Loading>{t("common.loading")}</Loading>}
      {history && history.events.length === 0 && (
        <EmptyState
          icon={History}
          title={t("ruleHistory.empty.title")}
          description={history.watching_since
            ? t("ruleHistory.empty.since", { when: new Date(history.watching_since).toLocaleString(locale) })
            : t("ruleHistory.empty.notYet")}
        />
      )}
      {history && history.events.length > 0 && (
        <>
          {history.watching_since && (
            <p className="mb-1 text-xs text-slate-500">{t("ruleHistory.since", { when: new Date(history.watching_since).toLocaleString(locale) })}</p>
          )}
          <ul aria-label={t("ruleHistory.list")}>
            {history.events.map((event, i) => <EventRow key={`${event.source}-${event.at}-${i}`} event={event} />)}
          </ul>
        </>
      )}
    </Modal>
  );
}
