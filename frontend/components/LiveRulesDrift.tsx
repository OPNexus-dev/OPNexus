"use client";

import { HistoryTarget, RuleHistoryModal } from "@/components/RuleHistoryModal";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { useToast } from "@/components/ui/Toast";
import { api, Host, HostOrder, OrderChange, OrderRow } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { groupHostsByCluster } from "@/lib/clusters";
import { useReadOnly } from "@/lib/useReadOnly";
import clsx from "clsx";
import { Check, ChevronDown, ChevronUp, ChevronsDown, ChevronsUp, GripVertical, History, Network, Pencil, RefreshCw, X } from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

// All rules of a firewall (or an HA pair) in the order the firewall evaluates them: the rules OPNexus manages, the ones
// made by hand on a firewall, the hierarchy's. Rules can be edited and moved here. A move is live at once but only on
// probation (see services/rule_order_service.py): it has to be confirmed or it is taken back.
//
// Rules have no id that is the same on every firewall of a pair (each OPNsense mints its own), so they are matched by a
// content fingerprint. Keep it identical to backend/app/services/rule_drift_service.py or the UI and the notifications
// disagree about what counts as a difference.
function fingerprint(rule: OrderRow): string {
  return [
    rule.description,
    rule.interface ?? "",
    rule.direction,
    rule.protocol ?? "",
    rule.source_net,
    rule.source_port ?? "",
    rule.destination_net,
    rule.destination_port ?? "",
    rule.action,
  ].join("|");
}

interface Row {
  key: string;
  sample: OrderRow;
  group: string;
  presentOn: Set<string>;
  uuids: Map<string, string>;
  origins: Map<string, OrderRow["origin"]>;
  origin: OrderRow["origin"];
  mixedOrigin: boolean;
  centralRuleId: string | null;
  movable: boolean;
}

interface Unit {
  key: string;
  name: string;
  solo: boolean;
  hosts: Host[];
  loading: boolean;
  orders: Record<string, HostOrder | null>;
  rows: Row[];
}

const ORIGIN_PRIORITY: OrderRow["origin"][] = ["central", "hierarchy", "local", "system"];

function buildRows(hosts: Host[], orders: Record<string, HostOrder | null>): Row[] {
  const reachable = hosts.filter((h) => orders[h.id]);
  const rows = new Map<string, Row>();
  const order: string[] = [];
  for (const host of reachable) {
    const seen = new Map<string, number>();
    let previous: string | null = null;
    for (const rule of orders[host.id]!.rows) {
      const fp = fingerprint(rule);
      const n = seen.get(fp) ?? 0;
      seen.set(fp, n + 1);
      const key = `${fp}#${n}`;  // two identical rules on one firewall stay two rows
      let row = rows.get(key);
      if (!row) {
        row = { key, sample: rule, group: rule.prio_group, presentOn: new Set(), uuids: new Map(), origins: new Map(), origin: rule.origin, mixedOrigin: false, centralRuleId: null, movable: true };
        rows.set(key, row);
        const at = previous ? order.indexOf(previous) + 1 : order.length;
        order.splice(at, 0, key);
      }
      row.presentOn.add(host.id);
      row.uuids.set(host.id, rule.uuid);
      row.origins.set(host.id, rule.origin);
      row.movable = row.movable && rule.movable;
      if (rule.central_rule_id && !row.centralRuleId) row.centralRuleId = rule.central_rule_id;
      previous = key;
    }
  }
  for (const row of rows.values()) {
    const all = new Set(row.origins.values());
    row.origin = ORIGIN_PRIORITY.find((o) => all.has(o)) ?? row.sample.origin;
    row.mixedOrigin = all.size > 1;
  }
  return order.map((k) => rows.get(k)!);
}

function groupLabelKey(prio: string): string {
  if (prio.startsWith("2")) return "floating";
  if (prio.startsWith("3")) return "ifgroup";
  if (prio.startsWith("4")) return "interface";
  if (prio.startsWith("0")) return "system";
  return "other";
}

function formatRemaining(deadline: string | null, now: number): string {
  if (!deadline) return "";
  const seconds = Math.max(0, Math.ceil((new Date(deadline).getTime() - now) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

interface Props {
  /** Open the editor of a central rule (the page owns the editor). */
  onEditRule?: (ruleId: string) => void;
  /** Reload the central rules after one was taken over from a firewall. */
  onRulesChanged?: () => Promise<void> | void;
}

export function LiveRulesDrift({ onEditRule, onRulesChanged }: Props = {}) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const readOnly = useReadOnly();
  const [units, setUnits] = useState<Unit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onlyDrift, setOnlyDrift] = useState(false);
  const [showSystem, setShowSystem] = useState(false);
  const [historyOf, setHistoryOf] = useState<{ title: string; targets: HistoryTarget[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [drag, setDrag] = useState<{ unit: string; row: string } | null>(null);

  const loadUnit = useCallback(async (hosts: Host[]): Promise<Pick<Unit, "orders" | "rows">> => {
    const results = await Promise.allSettled(hosts.map((h) => api.getRuleOrder(h.id)));
    const orders: Record<string, HostOrder | null> = {};
    hosts.forEach((h, i) => { const r = results[i]; orders[h.id] = r.status === "fulfilled" ? r.value : null; });
    return { orders, rows: buildRows(hosts, orders) };
  }, []);

  const refreshUnit = useCallback(async (unit: Unit) => {
    const data = await loadUnit(unit.hosts);
    setUnits((prev) => prev && prev.map((u) => (u.key === unit.key ? { ...u, ...data, loading: false } : u)));
  }, [loadUnit]);

  const loadAll = useCallback(() => {
    api
      .listHosts()
      .then((all) => {
        const hosts = all.filter((h) => h.enabled);
        const { clusters, ungrouped } = groupHostsByCluster(hosts);
        const initial: Unit[] = [
          ...Object.entries(clusters).map(([name, members]) => ({ key: `c:${name}`, name, solo: false, hosts: members, loading: true, orders: {}, rows: [] as Row[] })),
          ...ungrouped.map((h) => ({ key: `h:${h.id}`, name: h.name, solo: true, hosts: [h], loading: true, orders: {}, rows: [] as Row[] })),
        ].sort((a, b) => a.name.localeCompare(b.name));
        setUnits(initial);
        // Each unit loads on its own: a slow or unreachable firewall never holds the others back.
        for (const unit of initial) {
          loadUnit(unit.hosts).then((data) => setUnits((prev) => prev && prev.map((u) => (u.key === unit.key ? { ...u, ...data, loading: false } : u))));
        }
      })
      .catch((e) => setError(errorMessage(e, "drift.loadRulesFailed")));
  }, [errorMessage, loadUnit]);

  useEffect(() => { loadAll(); }, [loadAll]);

  const anyOpen = (units ?? []).some((u) => Object.values(u.orders).some((o) => o?.open.length));
  useEffect(() => {
    if (!anyOpen) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [anyOpen]);

  // -- moving ---------------------------------------------------------------------------------------------------------

  async function moveRow(unit: Unit, row: Row, target: Row | null) {
    setBusy(row.key);
    const errors: string[] = [];
    let moved = false;
    for (const host of unit.hosts) {
      const uuid = row.uuids.get(host.id);
      if (!uuid) continue;
      // The rule that must follow it on THIS firewall: the target, or the next row after it that this firewall has.
      let before: string | null = null;
      if (target) {
        const start = unit.rows.findIndex((r) => r.key === target.key);
        for (const r of unit.rows.slice(start)) {
          if (r.key !== row.key && r.group === row.group && r.movable && r.uuids.has(host.id)) { before = r.uuids.get(host.id)!; break; }
        }
      }
      try {
        if (await api.moveRule(host.id, uuid, before)) moved = true;
      } catch (e) {
        errors.push(errorMessage(e, "rules.order.moveFailed"));
      }
    }
    if (moved && errors.length === 0) toast.show(t("rules.order.moved", { rule: row.sample.description }), "success");
    errors.forEach((message) => toast.show(message, "danger"));
    await refreshUnit(unit);
    setBusy(null);
  }

  function neighbours(unit: Unit, row: Row) {
    const group = unit.rows.filter((r) => r.group === row.group && r.movable);
    const i = group.findIndex((r) => r.key === row.key);
    return {
      first: i > 0 ? group[0] : undefined,
      up: i > 0 ? group[i - 1] : undefined,
      down: i >= 0 && i < group.length - 1 ? (group[i + 2] ?? null) : undefined,   // null = to the end of the group
      end: i >= 0 && i < group.length - 1 ? null : undefined,
    };
  }

  async function settle(unit: Unit, action: "confirm" | "revert") {
    setBusy(`${unit.key}:${action}`);
    try {
      for (const host of unit.hosts) {
        if (!unit.orders[host.id]?.open.length) continue;
        await (action === "confirm" ? api.confirmOrder(host.id) : api.revertOrder(host.id));
      }
      toast.show(t(action === "confirm" ? "rules.order.confirmed" : "rules.order.reverted"), "success");
    } catch (e) {
      toast.show(errorMessage(e, action === "confirm" ? "rules.order.confirmFailed" : "rules.order.revertFailed"), "danger");
    }
    await refreshUnit(unit);
    setBusy(null);
  }

  // -- editing --------------------------------------------------------------------------------------------------------

  async function edit(unit: Unit, row: Row) {
    if (row.centralRuleId) { onEditRule?.(row.centralRuleId); return; }
    // A rule made by hand is taken over first (that only records it, nothing is written to a firewall); then it is edited
    // like any central rule, with preview, confirmation and automatic rollback.
    setBusy(row.key);
    try {
      const holders = unit.hosts.filter((h) => row.uuids.has(h.id));
      const rule = await api.adoptRule(holders[0].id, row.uuids.get(holders[0].id)!);
      for (const other of holders.slice(1)) {
        try { await api.adoptRule(other.id, row.uuids.get(other.id)!, rule.id); } catch { /* stays a local rule on that firewall; shown as such */ }
      }
      toast.show(t("rules.order.adopted", { rule: row.sample.description }), "success");
      await onRulesChanged?.();
      onEditRule?.(rule.id);
      await refreshUnit(unit);
    } catch (e) {
      toast.show(errorMessage(e, "rules.adopt.failed"), "danger");
    }
    setBusy(null);
  }

  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (!units) return <LiveRulesSkeleton />;

  return (
    <div className="space-y-8">
      {units.length === 0 && <EmptyState icon={Network} title={t("drift.noHosts")} />}

      {units.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-4 text-sm text-slate-600">
            {units.some((u) => u.hosts.length > 1) && (
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={onlyDrift} onChange={(e) => setOnlyDrift(e.target.checked)} />
                {t("drift.onlyDiffs")}
              </label>
            )}
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={showSystem} onChange={(e) => setShowSystem(e.target.checked)} />
              {t("rules.order.showSystem")}
            </label>
          </div>
          <Button variant="ghost" onClick={loadAll}><RefreshCw size={14} />{t("rules.local.refresh")}</Button>
        </div>
      )}

      {units.map((unit) => {
        const reachable = unit.hosts.filter((h) => unit.orders[h.id]);
        const drifted = new Set(unit.rows.filter((r) => r.presentOn.size < reachable.length).map((r) => r.key));
        const visible = unit.rows.filter((r) => (showSystem || r.origin !== "system") && (!onlyDrift || drifted.has(r.key)));
        const open: OrderChange[] = unit.hosts.flatMap((h) => unit.orders[h.id]?.open ?? []);
        const pending = open.filter((c) => c.status === "pending");
        const failed = open.filter((c) => c.status === "revert_failed");
        const deadline = pending.map((c) => c.revert_deadline).filter(Boolean).sort()[0] ?? null;
        const unreachable = unit.hosts.filter((h) => !unit.loading && !unit.orders[h.id]);
        let lastGroup = "";
        return (
          <div key={unit.key}>
            <div className="mb-2 flex flex-wrap items-baseline gap-2">
              <h3 className="text-lg font-semibold text-slate-900">{unit.name}</h3>
              {unit.loading ? (
                <span className="flex items-center gap-1.5 text-sm text-slate-400"><Spinner size={13} />{t("drift.comparingRules", { count: unit.hosts.length })}</span>
              ) : (
                <span className="text-sm text-slate-500">
                  {t("drift.rulesTotal", { count: unit.rows.filter((r) => r.origin !== "system").length })}
                  {unit.hosts.length > 1 && (drifted.size > 0 ? t("drift.someDiffer", { count: drifted.size }) : t("drift.allIdentical"))}
                </span>
              )}
            </div>

            {unreachable.map((h) => <p key={h.id} role="alert" className="mb-2 text-sm text-red-600">{t("rules.order.hostFailed", { host: h.name })}</p>)}

            {pending.length > 0 && (
              <div role="status" className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                <div>
                  <p className="font-medium">{t("rules.order.pendingTitle")}</p>
                  <p>{t("rules.order.pendingBody", { count: pending.length, time: formatRemaining(deadline, now) })}</p>
                </div>
                <div className="flex gap-2">
                  <Button onClick={() => settle(unit, "confirm")} loading={busy === `${unit.key}:confirm`} disabled={busy !== null || readOnly}>{t("rules.order.confirm")}</Button>
                  <Button variant="outline" onClick={() => settle(unit, "revert")} loading={busy === `${unit.key}:revert`} disabled={busy !== null || readOnly}>{t("rules.order.revert")}</Button>
                </div>
              </div>
            )}
            {failed.length > 0 && (
              <div role="alert" className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800">
                <p>{t("rules.order.revertFailedBanner", { rule: failed[0].description, host: failed[0].host_name ?? "" })}</p>
                <Button variant="outline" onClick={() => settle(unit, "revert")} loading={busy === `${unit.key}:revert`} disabled={busy !== null || readOnly}>{t("rules.order.retry")}</Button>
              </div>
            )}

            {unit.loading ? (
              <Card className="p-5"><Skeleton className="mb-2 h-3 w-full" /><Skeleton className="mb-2 h-3 w-5/6" /><Skeleton className="h-3 w-3/4" /></Card>
            ) : visible.length === 0 ? (
              <p className="mb-4 text-sm text-emerald-600">{onlyDrift ? t("drift.noDiffs") : t("drift.noRules")}</p>
            ) : (
              <Card className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="w-8 px-2 py-3"><span className="sr-only">{t("rules.order.colMove")}</span></th>
                      <th className="px-3 py-3 font-medium">{t("drift.colDescription")}</th>
                      <th className="px-3 py-3 font-medium">{t("drift.colInterface")}</th>
                      <th className="px-3 py-3 font-medium">{t("drift.colSourceDest")}</th>
                      <th className="px-3 py-3 font-medium">{t("drift.colAction")}</th>
                      {unit.hosts.length > 1 && unit.hosts.map((h) => <th key={h.id} className="px-3 py-3 font-medium">{h.name}</th>)}
                      <th className="px-3 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((row) => {
                      const header = row.group !== lastGroup;
                      lastGroup = row.group;
                      const nb = neighbours(unit, row);
                      const canMove = row.movable && !readOnly && busy === null;
                      const dropOk = drag?.unit === unit.key && drag.row !== row.key && row.movable && unit.rows.find((r) => r.key === drag.row)?.group === row.group;
                      const colSpan = 6 + (unit.hosts.length > 1 ? unit.hosts.length : 0);
                      return (
                        <Fragment key={row.key}>
                          {header && (
                            <tr className="bg-slate-50/60"><td colSpan={colSpan} className="px-3 py-1.5 text-xs font-medium uppercase tracking-wide text-slate-400">{t(`rules.order.group.${groupLabelKey(row.group)}`)}</td></tr>
                          )}
                          <tr
                            className={clsx(
                              "border-b border-slate-100 last:border-0",
                              drifted.has(row.key) && "bg-flame-50",
                              row.origin === "system" && "text-slate-400",
                              busy === row.key && "opacity-60",
                              dropOk && "outline outline-2 -outline-offset-2 outline-brand-300",
                            )}
                            draggable={canMove}
                            onDragStart={() => setDrag({ unit: unit.key, row: row.key })}
                            onDragEnd={() => setDrag(null)}
                            onDragOver={(e) => { if (dropOk) e.preventDefault(); }}
                            onDrop={(e) => { e.preventDefault(); const from = unit.rows.find((r) => r.key === drag?.row); setDrag(null); if (dropOk && from) moveRow(unit, from, row); }}
                          >
                            <td className="px-2 py-2 text-slate-400">
                              {row.movable && !readOnly && <GripVertical size={14} aria-hidden="true" className="cursor-grab" />}
                            </td>
                            <td className="px-3 py-2 text-slate-900">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className={clsx(row.origin === "system" && "text-slate-400", !row.sample.enabled && "line-through")}>{row.sample.description}</span>
                                <Badge tone={row.origin === "central" ? "info" : row.origin === "local" ? "warning" : "neutral"}>{t(`rules.order.origin.${row.origin}`)}</Badge>
                                {row.mixedOrigin && <Badge tone="warning">{t("rules.order.mixed")}</Badge>}
                                {!row.sample.enabled && <span className="text-xs text-slate-400">{t("common.statusDisabled")}</span>}
                              </div>
                            </td>
                            <td className="px-3 py-2 text-slate-500">{row.sample.interface_label ?? "—"}</td>
                            <td className="px-3 py-2 text-slate-500">
                              {row.sample.protocol_label ? `${row.sample.protocol_label} ` : ""}{row.sample.source_net}{row.sample.source_port ? `:${row.sample.source_port}` : ""} → {row.sample.destination_net}{row.sample.destination_port ? `:${row.sample.destination_port}` : ""}
                            </td>
                            <td className="px-3 py-2 text-slate-500">{row.sample.action_label}</td>
                            {unit.hosts.length > 1 && unit.hosts.map((h) => (
                              <td key={h.id} className="px-3 py-2">
                                {row.presentOn.has(h.id) ? <Check size={16} className="text-emerald-500" aria-label={t("rules.order.present")} /> : <X size={16} className="text-red-400" aria-label={t("rules.order.absent")} />}
                              </td>
                            ))}
                            <td className="whitespace-nowrap px-3 py-2 text-right">
                              <div className="inline-flex items-center gap-0.5">
                                {row.movable && !readOnly && (
                                  <>
                                    <MoveButton label={t("rules.order.moveTop", { rule: row.sample.description })} icon={ChevronsUp} disabled={!canMove || nb.first === undefined} onClick={() => moveRow(unit, row, nb.first!)} />
                                    <MoveButton label={t("rules.order.moveUp", { rule: row.sample.description })} icon={ChevronUp} disabled={!canMove || nb.up === undefined} onClick={() => moveRow(unit, row, nb.up!)} />
                                    <MoveButton label={t("rules.order.moveDown", { rule: row.sample.description })} icon={ChevronDown} disabled={!canMove || nb.down === undefined} onClick={() => moveRow(unit, row, nb.down ?? null)} />
                                    <MoveButton label={t("rules.order.moveEnd", { rule: row.sample.description })} icon={ChevronsDown} disabled={!canMove || nb.end === undefined} onClick={() => moveRow(unit, row, null)} />
                                  </>
                                )}
                                {(row.origin === "central" || row.origin === "local") && !readOnly && onEditRule && (
                                  <button
                                    type="button"
                                    onClick={() => edit(unit, row)}
                                    disabled={busy !== null}
                                    title={row.origin === "local" ? t("rules.order.editLocalHint") : undefined}
                                    aria-label={t("rules.order.edit", { rule: row.sample.description })}
                                    className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500 disabled:opacity-40"
                                  >
                                    <Pencil size={14} aria-hidden="true" />{t("rules.order.editShort")}
                                  </button>
                                )}
                                {row.origin !== "system" && (
                                  <button
                                    type="button"
                                    onClick={() => setHistoryOf({ title: row.sample.description, targets: unit.hosts.filter((h) => row.uuids.has(h.id)).map((h) => ({ hostId: h.id, hostName: h.name, uuid: row.uuids.get(h.id) as string })) })}
                                    className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
                                    aria-label={t("ruleHistory.open", { rule: row.sample.description })}
                                  >
                                    <History size={14} aria-hidden="true" />{t("ruleHistory.action")}
                                  </button>
                                )}
                              </div>
                            </td>
                          </tr>
                        </Fragment>
                      );
                    })}
                    {drag?.unit === unit.key && (
                      <tr
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={(e) => { e.preventDefault(); const from = unit.rows.find((r) => r.key === drag.row); setDrag(null); if (from) moveRow(unit, from, null); }}
                      >
                        <td colSpan={6 + (unit.hosts.length > 1 ? unit.hosts.length : 0)} className="px-3 py-3 text-center text-xs text-slate-500">{t("rules.order.dropEnd")}</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </Card>
            )}
          </div>
        );
      })}

      {historyOf && <RuleHistoryModal title={historyOf.title} targets={historyOf.targets} onClose={() => setHistoryOf(null)} />}
    </div>
  );
}

function MoveButton({ label, icon: Icon, disabled, onClick }: { label: string; icon: typeof ChevronUp; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500 disabled:opacity-30 disabled:hover:bg-transparent"
    >
      <Icon size={15} aria-hidden="true" />
    </button>
  );
}

function LiveRulesSkeleton() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-5 w-32" />
      <Card className="p-5">
        <Skeleton className="mb-2 h-3 w-full" />
        <Skeleton className="mb-2 h-3 w-5/6" />
        <Skeleton className="h-3 w-3/4" />
      </Card>
    </div>
  );
}
