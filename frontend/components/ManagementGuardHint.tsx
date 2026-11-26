"use client";

import { api, ManagementRisk, RuleManagementCheck } from "@/lib/api";
import { Loading } from "@/components/ui/Loading";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

export type ManagementGuardState = { risk: ManagementRisk; acknowledged: boolean };

/**
 * Read-only check next to a rule's preview: would the rule block the way OPNexus (or a management source) reaches the
 * firewall? The automatic revert runs in OPNexus and needs that way. A sure finding asks for an explicit acknowledgement
 * (the server refuses the deploy without it); "unknown" is only a note. A firewall that cannot be read shows nothing.
 */
export function ManagementGuardHint({ ruleId, compact = false, onChange }: {
  ruleId: string; compact?: boolean; onChange?: (state: ManagementGuardState) => void;
}) {
  const { t } = useLanguage();
  const [data, setData] = useState<RuleManagementCheck | null | undefined>(undefined);
  const [acknowledged, setAcknowledged] = useState(false);

  useEffect(() => {
    let active = true;
    setData(undefined); setAcknowledged(false);
    api.ruleManagementCheck(ruleId).then((d) => { if (active) setData(d); }).catch(() => { if (active) setData(null); });
    return () => { active = false; };
  }, [ruleId]);

  const risk: ManagementRisk = data?.risk ?? "none";
  useEffect(() => { onChange?.({ risk, acknowledged }); }, [risk, acknowledged, onChange]);

  if (data === undefined) return compact ? null : <Loading className="py-2">{t("rules.management.checking")}</Loading>;
  if (!data || data.risk === "none") return null;

  if (data.risk === "unknown") {
    return <p className="rounded-lg bg-slate-50 p-2 text-xs text-slate-600" aria-live="polite">{t("rules.management.unknown")}</p>;
  }
  const blocking = data.hosts.filter((h) => h.risk === "blocks");
  const who = (h: RuleManagementCheck["hosts"][number]) => {
    const kinds = new Set(h.findings.filter((f) => f.sure).map((f) => f.who));
    return t(kinds.has("opnexus") && kinds.has("admin") ? "rules.management.whoBoth" : kinds.has("admin") ? "rules.management.whoAdmin" : "rules.management.whoOpnexus");
  };
  return (
    <div className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800" role="alert">
      {blocking.map((h) => <p key={h.host_id}>{t("rules.management.blocks", { host: h.host_name, who: who(h) })}</p>)}
      <label className="flex items-start gap-2 font-medium">
        <input type="checkbox" className="mt-1" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
        <span>{t("rules.management.acknowledge")}</span>
      </label>
    </div>
  );
}
