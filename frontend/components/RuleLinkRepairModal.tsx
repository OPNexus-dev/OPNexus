"use client";

import { api, Host, RuleLinkProposals } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Loading } from "@/components/ui/Loading";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { useLanguage } from "@/components/LanguageProvider";
import { labelFor } from "@/lib/i18n";
import { useEffect, useState } from "react";

type Loaded = { host: Host; found: RuleLinkProposals | null };

/**
 * Rules whose recorded OPNsense ID on a firewall is stale although the rule is there (typically after an OPNsense config
 * sync): shows what would change and repairs it on request. Only OPNexus's own records change, never a firewall.
 */
export function RuleLinkRepairModal({ hosts, onClose, onRepaired }: { hosts: Host[]; onClose: () => void; onRepaired: () => void }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [loaded, setLoaded] = useState<Loaded[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all(hosts.map(async (host): Promise<Loaded> => {
      try { return { host, found: await api.ruleLinks(host.id) }; } catch { return { host, found: null }; }
    })).then((list) => { if (active) setLoaded(list); });
    return () => { active = false; };
    // The hosts are fixed while the dialog is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const total = (loaded ?? []).reduce((n, l) => n + (l.found?.items.length ?? 0), 0);

  async function repair() {
    if (!loaded) return;
    setBusy(true); setError(null);
    try {
      let done = 0;
      for (const l of loaded) {
        if (l.found && l.found.items.length > 0) done += (await api.repairRuleLinks(l.host.id, l.found.token)).repaired;
      }
      toast.show(t("rules.order.linkRepair.done", { count: done }), "success");
      onRepaired();
      onClose();
    } catch (e) { setError(errorMessage(e, "common.requestFailed")); }
    finally { setBusy(false); }
  }

  return (
    <Modal wide title={t("rules.order.linkRepair.title")} onClose={() => { if (!busy) onClose(); }}>
      <p className="mb-4 text-sm text-slate-500">{t("rules.order.linkRepair.intro")}</p>
      {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {loaded === null && <Loading className="py-4">{t("rules.order.linkRepair.loading")}</Loading>}
      {loaded !== null && (
        <div className="space-y-4">
          {loaded.map(({ host, found }) => (
            <div key={host.id} className="rounded-xl border border-slate-200 p-3">
              <p className="mb-2 font-medium text-slate-800">{host.name}</p>
              {found === null && <p className="text-sm text-red-700">{t("rules.order.linkRepair.failed", { host: host.name })}</p>}
              {found !== null && found.items.length === 0 && <p className="text-sm text-slate-500">{t("rules.order.linkRepair.none", { host: host.name })}</p>}
              {found !== null && found.items.length > 0 && (
                <ul className="space-y-2">
                  {found.items.map((i) => (
                    <li key={i.deployment_id} className="text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-medium text-slate-900">{i.rule_description}</span>
                        <Badge tone={i.strength === "partner_uuid" ? "success" : "warning"}>{t(`rules.order.linkRepair.${i.strength === "partner_uuid" ? "strong" : i.strength === "content" ? "content" : "drift"}`)}</Badge>
                      </div>
                      <p className="font-mono text-xs text-slate-500">{t("rules.order.linkRepair.from")} {i.stored_uuid} → {t("rules.order.linkRepair.to")} {i.live_uuid}</p>
                      {i.differences.length > 0 && (
                        <div className="mt-1 rounded-lg bg-flame-50 p-2 text-xs text-flame-800">
                          <p className="font-medium">{t("rules.order.linkRepair.driftNote")}</p>
                          <ul className="mt-1 space-y-0.5">
                            {i.differences.map((d) => (
                              <li key={d.field}>{labelFor(t, "fields", d.field)}: <span className="font-mono">{d.recorded || "—"}</span> → <span className="font-mono">{d.live || "—"}</span></li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {found !== null && found.ambiguous > 0 && <p className="mt-2 text-xs text-slate-500">{t("rules.order.linkRepair.ambiguous", { count: found.ambiguous })}</p>}
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <Button disabled={busy || total === 0} loading={busy} onClick={repair}>{t("rules.order.linkRepair.run", { count: total })}</Button>
            <Button variant="ghost" disabled={busy} onClick={onClose}>{t("rules.bulk.close")}</Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
