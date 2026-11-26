"use client";

import { AuditEvent } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";

type Payload = Record<string, unknown>;
const asPayload = (value: unknown): Payload | null => (value && typeof value === "object" && !Array.isArray(value) ? (value as Payload) : null);
const show = (value: unknown, empty: string) => {
  if (value === null || value === undefined || value === "") return empty;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
};

/** What an audit entry changed, field by field (deploy entries carry the payload before and after), plus the raw record. */
export function AuditDetail({ event }: { event: AuditEvent }) {
  const { t } = useLanguage();
  const details = event.details as Payload;
  const before = asPayload(details.before_payload);
  const after = asPayload(details.after_payload) ?? asPayload(details.requested_payload);
  const fieldLabel = (field: string) => (`fields.${field}` !== t(`fields.${field}`) ? t(`fields.${field}`) : field);
  const empty = t("audit.detail.empty");
  const keys = Array.from(new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})]));
  const changed = after ? keys.filter((k) => JSON.stringify(before?.[k] ?? null) !== JSON.stringify(after[k] ?? null)) : [];
  // Stop clicks inside the detail from folding the entry (the whole card is the toggle).
  const comment = typeof details.comment === "string" ? details.comment : null;
  return (
    <div className="mt-3 space-y-3" onClick={(e) => e.stopPropagation()}>
      {comment && <p className="text-sm text-slate-700"><span className="font-medium">{t("audit.detail.comment")}:</span> {comment}</p>}
      {after && (
        <div>
          <p className="mb-1 text-xs font-medium text-slate-600">{t("audit.detail.changes")}</p>
          {changed.length === 0 ? <p className="text-xs text-slate-500">{t("audit.detail.noChange")}</p> : (
            <table className="w-full table-fixed text-left text-xs">
              <thead><tr className="text-slate-400"><th className="w-1/4">{t("audit.detail.field")}</th><th>{t("audit.detail.before")}</th><th>{t("audit.detail.after")}</th></tr></thead>
              <tbody>
                {changed.map((k) => (
                  <tr key={k} className="border-t border-slate-100">
                    <td className="break-words py-1.5 pr-2">{fieldLabel(k)}</td>
                    <td className="break-words pr-2 text-slate-500">{show(before?.[k], empty)}</td>
                    <td className="break-words">{show(after[k], empty)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <details>
        <summary className="cursor-pointer text-xs text-slate-500">{t("audit.detail.technical")}</summary>
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-50 p-3 text-xs">{JSON.stringify({ id: event.id, correlation_id: event.correlation_id, entity_id: event.entity_id, host_id: event.host_id, ...event.details }, null, 2)}</pre>
      </details>
    </div>
  );
}
