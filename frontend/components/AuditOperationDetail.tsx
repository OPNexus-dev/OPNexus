"use client";

import { useEffect, useState } from "react";
import { api, AuditEvent, AuditOperation } from "@/lib/api";
import { AuditDetail } from "@/components/AuditDetail";
import { Badge } from "@/components/ui/Badge";
import { useLanguage } from "@/components/LanguageProvider";
import { auditTone } from "@/lib/audit";

/** The steps of one operation in the order they happened, loaded when the row is opened. A step that carries data shows what it changed. */
export function AuditOperationDetail({ operation }: { operation: AuditOperation }) {
  const { t, ts, locale, errorMessage } = useLanguage();
  const [steps, setSteps] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    api.audit({ correlation_id: operation.correlation_id, limit: 200 })
      .then((page) => { if (active) setSteps([...page.items].reverse()); })
      .catch((e) => { if (active) setError(errorMessage(e)); });
    return () => { active = false; };
  }, [operation.correlation_id, errorMessage]);

  const status = (value: string) => { const key = `audit.status.${value}`; const text = t(key); return text === key ? value : text; };
  if (error) return <p role="alert" className="text-sm text-red-600">{error}</p>;
  if (!steps) return <p className="text-sm text-slate-500">{t("audit.loadingSteps")}</p>;

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{t("audit.stepsHeading")}</h3>
      <ol className="space-y-2">
        {steps.map((step) => {
          const hasData = Object.keys(step.details ?? {}).length > 0;
          return (
            <li key={step.id} className="rounded-lg border border-slate-100 bg-white px-3 py-2 text-sm">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="tabular-nums text-slate-500">{new Date(step.created_at).toLocaleTimeString(locale)}</span>
                <Badge tone={auditTone(step.status)}>{status(step.status)}</Badge>
                {step.host_name && <span className="text-slate-700">{step.host_name}</span>}
                {step.message && !/^HTTP \d+$/.test(step.message) && <span className="min-w-0 break-words text-slate-600">{ts(step.message)}</span>}
              </div>
              {hasData && <AuditDetail event={step} />}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
