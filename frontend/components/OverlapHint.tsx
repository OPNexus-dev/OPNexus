"use client";

import { api, RuleOverlap } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Loading } from "@/components/ui/Loading";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/**
 * Read-only hint next to a rule's preview: would the rule be shadowed by (or duplicate) an earlier rule on its targets,
 * which later rules would it make pointless? The preview itself does not wait for it and its token does not depend on it;
 * a firewall that cannot be read only shows a short note, and nothing here ever blocks the deployment.
 */
export function OverlapHint({ ruleId, compact = false }: { ruleId: string; compact?: boolean }) {
  const { t } = useLanguage();
  const [data, setData] = useState<RuleOverlap | null | undefined>(undefined);

  useEffect(() => {
    let active = true;
    setData(undefined);
    api.ruleOverlap(ruleId).then((d) => { if (active) setData(d); }).catch(() => { if (active) setData(null); });
    return () => { active = false; };
  }, [ruleId]);

  if (data === undefined) return compact ? null : <Loading className="py-2">{t("rules.overlap.checking")}</Loading>;
  if (!data) return null;
  const confidence = (c: "sure" | "probable") => <Badge tone={c === "sure" ? "danger" : "warning"}>{t(`rules.overlap.${c}`)}</Badge>;
  const name = (r: { description: string; managed: unknown }) => `${r.description}${r.managed ? ` (${t("rules.overlap.managed")})` : ""}`;

  return (
    <div className="space-y-2" aria-live="polite">
      {data.hosts.map((h) => (
        <div key={h.host_id} className="space-y-1">
          {h.status === "unavailable" && <p className="text-xs text-slate-500">{t("rules.overlap.unavailable", { host: h.host_name })}</p>}
          {h.overlap && (
            <p className="flex flex-wrap items-center gap-2 rounded-lg bg-flame-50 p-2 text-sm text-flame-800">
              {confidence(h.overlap.confidence)}
              <span>{t(`rules.overlap.${h.overlap.kind}`, { host: h.host_name, by: name(h.overlap.by) })}</span>
            </p>
          )}
          {h.makes_pointless_total > 0 && (
            <p className="rounded-lg bg-flame-50 p-2 text-sm text-flame-800">
              {t("rules.overlap.makesPointless", {
                host: h.host_name, count: h.makes_pointless_total,
                list: h.makes_pointless.map((r) => `“${name(r)}”`).join(", ") + (h.makes_pointless_total > h.makes_pointless.length ? ` ${t("rules.overlap.more", { count: h.makes_pointless_total - h.makes_pointless.length })}` : ""),
              })}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
