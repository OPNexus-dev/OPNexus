"use client";

import { Host, Rule, RuleUsage } from "@/lib/api";
import { ActionBadge, DeploymentIcon } from "@/components/RuleBits";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Table, TBody, TD, TH, THead, TRow } from "@/components/ui/Table";
import { Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { ArrowDown, ArrowUp, ArrowUpDown, Copy, History, Pencil, Rocket, ShieldCheck, Trash2 } from "lucide-react";

export type SortKey = "description" | "action" | "source" | "destination" | "state" | "lastHit";
export interface SortState { key: SortKey; dir: "asc" | "desc" }

const endpoint = (net: string, port: string | null) => (port ? `${net}:${port}` : net);

/** The compact list of the rules page: one line per rule, with when it last matched something. */
export function RulesTable({ rules, hostsById, usage, selected, onToggle, onToggleAll, sort, onSort, secondsLeft, confirmingId, readOnly,
  onEdit, onPreview, onHistory, onDuplicate, onDelete, onConfirm }: {
  rules: Rule[]; hostsById: Map<string, Host>; usage: Map<string, RuleUsage>; selected: Set<string>;
  onToggle: (id: string) => void; onToggleAll: (checked: boolean) => void; sort: SortState; onSort: (key: SortKey) => void;
  secondsLeft: (deadline: string) => number; confirmingId: string | null; readOnly: boolean;
  onEdit: (rule: Rule) => void; onPreview: (rule: Rule) => void; onHistory: (rule: Rule) => void; onDuplicate: (rule: Rule) => void;
  onDelete: (id: string) => void; onConfirm: (id: string) => void;
}) {
  const { t, locale } = useLanguage();
  const all = rules.length > 0 && rules.every((r) => selected.has(r.id));

  function ago(iso: string): string {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 1) return t("rules.usage.justNow");
    if (minutes < 60) return t("rules.usage.minutesAgo", { n: minutes });
    if (minutes < 60 * 48) return t("rules.usage.hoursAgo", { n: Math.round(minutes / 60) });
    return t("rules.usage.daysAgo", { n: Math.round(minutes / 1440) });
  }
  const date = (iso: string) => new Date(iso).toLocaleString(locale);

  function lastHit(rule: Rule) {
    const u = usage.get(rule.id);
    if (!u) return <span className="text-slate-400">—</span>;
    const partial = u.hosts_total > 1 && u.hosts_tracked < u.hosts_total ? ` ${t("rules.usage.partialTip", { tracked: u.hosts_tracked, total: u.hosts_total })}` : "";
    switch (u.state) {
      case "used":
        return <Tooltip content={t("rules.usage.usedTip", { date: date(u.last_hit_at!) }) + partial}><span>{ago(u.last_hit_at!)}</span></Tooltip>;
      case "unused":
        return <Tooltip content={t("rules.usage.unusedTip", { date: date(u.watching_since!), days: u.days }) + partial}><span><Badge tone="warning">{t("rules.usage.unusedBadge", { days: u.days })}</Badge></span></Tooltip>;
      case "collecting":
        return <Tooltip content={t("rules.usage.collectingTip", { date: date(u.watching_since!), days: u.days }) + partial}><span><Badge tone="neutral">{t("rules.usage.collectingBadge")}</Badge></span></Tooltip>;
      case "disabled":
        return <Tooltip content={t("rules.usage.disabledTip")}><span className="text-slate-400">—</span></Tooltip>;
      default:
        return <Tooltip content={t("rules.usage.unknownTip")}><span className="text-slate-400">—</span></Tooltip>;
    }
  }

  function header(key: SortKey, labelKey: string) {
    const column = t(labelKey);
    const active = sort.key === key;
    const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
    return (
      <TH className="whitespace-nowrap px-2 py-2" aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
        <button type="button" className="inline-flex items-center gap-1 uppercase tracking-wide hover:text-slate-800" aria-label={t("rules.table.sortBy", { column })} onClick={() => onSort(key)}>
          {column}<Icon size={12} className={active ? "text-sky-600" : "text-slate-300"} aria-hidden="true" />
        </button>
      </TH>
    );
  }

  return (
    <Card className="overflow-x-auto p-0">
      <Table>
        <THead>
          <tr>
            <TH className="w-8 px-2 py-2">
              {!readOnly && <input type="checkbox" checked={all} aria-label={t("rules.bulk.selectAll")} onChange={(e) => onToggleAll(e.target.checked)} />}
            </TH>
            {header("action", "rules.table.col.action")}
            {header("description", "rules.table.col.name")}
            {header("source", "rules.table.col.source")}
            {header("destination", "rules.table.col.destination")}
            {header("state", "rules.table.col.state")}
            {header("lastHit", "rules.table.col.lastHit")}
            <TH className="px-1 py-2 text-right"><span className="sr-only">{t("rules.table.col.actions")}</span></TH>
          </tr>
        </THead>
        <TBody>
          {rules.map((rule) => {
            const pending = rule.deployments.some((d) => d.status === "pending");
            const locked = rule.deployments.some((d) => ["pending", "revert_failed"].includes(d.status));
            const hostNames = rule.effective_host_ids.map((id) => hostsById.get(id)?.name ?? id.slice(0, 8)).join(", ");
            return (
              <TRow key={rule.id} className={selected.has(rule.id) ? "bg-sky-50/40" : undefined}>
                <TD className="px-2 py-2">
                  {!readOnly && <input type="checkbox" checked={selected.has(rule.id)} aria-label={t("rules.bulk.selectRule", { name: rule.description })} onChange={() => onToggle(rule.id)} />}
                </TD>
                <TD className="px-2 py-2"><ActionBadge action={rule.action} /></TD>
                <TD className="max-w-[16rem] px-2 py-2">
                  <span className="block truncate font-medium text-slate-900" title={rule.description}>{rule.description}</span>
                  <span className="block truncate text-xs text-slate-500" title={hostNames}>
                    {rule.interface} · {rule.direction === "in" ? t("rules.dirIn") : t("rules.dirOut")} · {rule.protocol} · {t("rules.table.col.targets")} {rule.workflow_counts.active_targets}/{rule.workflow_counts.targets}
                    {!rule.enabled ? t("rules.disabledSuffix") : ""}
                  </span>
                </TD>
                <TD className="max-w-[9rem] px-2 py-2 font-mono text-xs text-slate-600"><span className="block truncate" title={endpoint(rule.source_net, rule.source_port)}>{endpoint(rule.source_net, rule.source_port)}</span></TD>
                <TD className="max-w-[10rem] px-2 py-2 font-mono text-xs text-slate-600"><span className="block truncate" title={endpoint(rule.destination_net, rule.destination_port)}>{endpoint(rule.destination_net, rule.destination_port)}</span></TD>
                <TD className="px-2 py-2">
                  <div className="flex flex-wrap items-center gap-1.5 [&_span]:whitespace-nowrap">
                    <WorkflowBadge state={rule.workflow_state} />
                    {rule.deployments.map((d) => (
                      <Tooltip key={d.host_id} content={`${d.host_name}${d.status === "pending" && d.revert_deadline ? ` ${t("rules.expiresIn", { seconds: secondsLeft(d.revert_deadline) })}` : ""}`}>
                        <span><DeploymentIcon status={d.status} /></span>
                      </Tooltip>
                    ))}
                    {pending && !readOnly && (
                      <Button variant="secondary" disabled={confirmingId === rule.id} onClick={() => onConfirm(rule.id)}>
                        <ShieldCheck size={13} />{t("rules.table.confirmShort")}
                      </Button>
                    )}
                  </div>
                </TD>
                <TD className="whitespace-nowrap px-2 py-2 text-slate-600">{lastHit(rule)}</TD>
                <TD className="px-1 py-2">
                  <div className="flex justify-end">
                    <Tooltip content={t("rules.edit")}><Button variant="ghost" className="!px-1.5" disabled={readOnly || locked} aria-label={t("rules.edit")} onClick={() => onEdit(rule)}><Pencil size={14} /></Button></Tooltip>
                    <Tooltip content={t("rules.reviewPush")}><Button variant="ghost" className="!px-1.5" disabled={readOnly} aria-label={t("rules.reviewPush")} onClick={() => onPreview(rule)}><Rocket size={14} /></Button></Tooltip>
                    <Tooltip content={t("rules.historyTitle")}><Button variant="ghost" className="!px-1.5" aria-label={t("rules.historyTitle")} onClick={() => onHistory(rule)}><History size={14} /></Button></Tooltip>
                    <Tooltip content={t("rules.duplicateTitle")}><Button variant="ghost" className="!px-1.5" disabled={readOnly} aria-label={t("rules.duplicateTitle")} onClick={() => onDuplicate(rule)}><Copy size={14} /></Button></Tooltip>
                    <Tooltip content={t("rules.delete")}><Button variant="dangerSubtle" className="!px-1.5" disabled={readOnly} aria-label={t("rules.delete")} onClick={() => onDelete(rule.id)}><Trash2 size={14} /></Button></Tooltip>
                  </div>
                </TD>
              </TRow>
            );
          })}
        </TBody>
      </Table>
    </Card>
  );
}
