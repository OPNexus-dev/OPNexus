"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, Host, UnmanagedRule } from "@/lib/api";
import { groupHostsByCluster } from "@/lib/clusters";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

interface Unit {
  key: string;
  name: string;
  // The firewall that is asked. An HA pair shares its configuration, so one member speaks for the pair.
  host: Host;
  members: number;
}

interface UnitState {
  rules: UnmanagedRule[] | null;
  failed: boolean;
}

// The same rule on two firewalls has two different ids; matched by what it says (interfaces by their label, which
// is what the operator sees). Rules that differ in any of these fields count as different rules.
function fingerprint(rule: UnmanagedRule): string {
  return [rule.description, rule.interface_label, rule.direction, rule.protocol, rule.source_net, rule.source_port,
    rule.destination_net, rule.destination_port, rule.action].join("|");
}

function summary(rule: UnmanagedRule): string {
  const port = (value: string) => (value ? `:${value}` : "");
  return `${rule.protocol} ${rule.source_net}${port(rule.source_port)} → ${rule.destination_net}${port(rule.destination_port)}`;
}

/**
 * The rules that exist on a firewall but are not managed centrally (made by hand on the firewall).
 * Read-only overview below the central rules; "take over" opens the existing adopt dialog on that firewall.
 * Loaded when opened, not with the page: every firewall is asked live.
 */
export function HostLocalRules({ hosts, reloadKey, onAdopt }: { hosts: Host[]; reloadKey: number; onAdopt: (hostId: string) => void }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<Record<string, UnitState>>({});
  const [loading, setLoading] = useState(false);

  const units = useMemo<Unit[]>(() => {
    const enabled = hosts.filter((h) => h.enabled);
    const { clusters, ungrouped } = groupHostsByCluster(enabled);
    return [
      ...Object.entries(clusters).map(([name, members]) => ({ key: `c:${name}`, name, host: members[0], members: members.length })),
      ...ungrouped.map((h) => ({ key: `h:${h.id}`, name: h.name, host: h, members: 1 })),
    ].sort((a, b) => a.name.localeCompare(b.name));
  }, [hosts]);

  const load = useCallback(async () => {
    setLoading(true);
    const results = await Promise.allSettled(units.map((u) => api.listUnmanagedRules(u.host.id)));
    const next: Record<string, UnitState> = {};
    units.forEach((u, i) => {
      const r = results[i];
      next[u.key] = r.status === "fulfilled" ? { rules: r.value, failed: false } : { rules: null, failed: true };
    });
    setState(next);
    setLoading(false);
  }, [units]);

  useEffect(() => {
    if (open) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reloadKey, units.length]);

  const seenOn = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const u of units) for (const rule of state[u.key]?.rules ?? []) {
      const list = map.get(fingerprint(rule)) ?? [];
      list.push(u.name);
      map.set(fingerprint(rule), list);
    }
    return map;
  }, [units, state]);

  const total = units.reduce((sum, u) => sum + (state[u.key]?.rules?.length ?? 0), 0);

  return (
    <Card className="mt-8">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-5 py-4 text-left"
      >
        {open ? <ChevronDown size={16} aria-hidden /> : <ChevronRight size={16} aria-hidden />}
        <span className="font-semibold text-slate-900">{t("rules.local.title")}</span>
        {open && !loading && Object.keys(state).length > 0 && <Badge tone={total ? "warning" : "success"}>{total}</Badge>}
        <InfoTip text={t("rules.local.info")} size={14} />
      </button>
      {open && (
        <div className="space-y-5 border-t border-slate-100 px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-slate-600">{t("rules.local.intro")}</p>
            <Button variant="ghost" onClick={load} disabled={loading} loading={loading}>
              <RefreshCw size={14} />
              {t("rules.local.refresh")}
            </Button>
          </div>
          {units.length === 0 && <p className="text-sm text-slate-500">{t("rules.local.noHosts")}</p>}
          {units.map((u) => {
            const s = state[u.key];
            return (
              <section key={u.key} aria-label={u.name}>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-slate-800">
                    {u.name}
                    {u.members > 1 && <span className="ml-2 text-xs font-normal text-slate-500">{t("rules.local.pair", { count: u.members })}</span>}
                    {s?.rules && <span className="ml-2 text-xs font-normal text-slate-500">({s.rules.length})</span>}
                  </h3>
                  {s?.rules && s.rules.length > 0 && (
                    <Button variant="outline" onClick={() => onAdopt(u.host.id)}>{t("rules.local.adopt")}</Button>
                  )}
                </div>
                {!s && loading && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
                {s?.failed && <p role="alert" className="text-sm text-red-600">{t("rules.local.loadFailed", { host: u.name })}</p>}
                {s?.rules?.length === 0 && <p className="text-sm text-slate-500">{t("rules.local.none")}</p>}
                <ul className="space-y-1.5">
                  {(s?.rules ?? []).map((rule) => {
                    const others = (seenOn.get(fingerprint(rule)) ?? []).filter((n) => n !== u.name);
                    return (
                      <li key={rule.uuid} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 px-3 py-2">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-slate-900">{rule.description}</p>
                          <p className="text-xs text-slate-500">
                            {rule.interface_label} · {rule.direction} · {rule.action} · {summary(rule)}
                            {!rule.enabled ? ` · ${t("common.statusDisabled")}` : ""}
                          </p>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {others.length === 0
                            ? <Badge tone="neutral">{t("rules.local.onlyHere")}</Badge>
                            : <Badge tone="info">{t("rules.local.alsoOn", { hosts: others.join(", ") })}</Badge>}
                          {!rule.adoptable && !rule.linkable && <Badge tone="neutral">{t("rules.adopt.notPossible")}</Badge>}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </Card>
  );
}
