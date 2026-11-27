"use client";

import { Host, Rule, RuleUsage } from "@/lib/api";
import { ActionBadge, DeploymentIcon } from "@/components/RuleBits";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Column, DataTable, Sort } from "@/components/ui/DataTable";
import { Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { Copy, History, Pencil, Rocket, ShieldCheck, Trash2 } from "lucide-react";

export type SortKey = "description" | "action" | "source" | "destination" | "state" | "lastHit";
export interface SortState { key: SortKey; dir: "asc" | "desc" }

const endpoint = (net: string, port: string | null) => (port ? `${net}:${port}` : net);

/**
 * The compact list of the rules page: one row per rule on a desktop and tablet, one card per rule on a phone (DataTable), with when it last matched
 * something. Sorting is done by the page (`sort` is controlled), so the order also applies to the cards view and the bulk selection.
 */
export function RulesTable({ rules, hostsById, usage, selected, onSelectionChange, sort, onSortChange, secondsLeft, confirmingId, readOnly,
  onEdit, onPreview, onHistory, onDuplicate, onDelete, onConfirm }: {
  rules: Rule[]; hostsById: Map<string, Host>; usage: Map<string, RuleUsage>; selected: Set<string>;
  onSelectionChange: (next: Set<string>) => void; sort: SortState; onSortChange: (sort: SortState | null) => void;
  secondsLeft: (deadline: string) => number; confirmingId: string | null; readOnly: boolean;
  onEdit: (rule: Rule) => void; onPreview: (rule: Rule) => void; onHistory: (rule: Rule) => void; onDuplicate: (rule: Rule) => void;
  onDelete: (id: string) => void; onConfirm: (id: string) => void;
}) {
  const { t, locale } = useLanguage();

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


  const columns: Column<Rule>[] = [
    { key: "action", header: t("rules.table.col.action"), priority: "primary", cell: (rule) => <ActionBadge action={rule.action} /> },
    {
      key: "description", header: t("rules.table.col.name"), priority: "primary", isTitle: true, className: "max-w-[16rem]",
      cell: (rule) => {
        const hostNames = rule.effective_host_ids.map((id) => hostsById.get(id)?.name ?? id.slice(0, 8)).join(", ");
        return (
          <>
            <span className="block break-words font-medium text-slate-900 sm:truncate" title={rule.description}>{rule.description}</span>
            <span className="block break-words text-xs font-normal text-slate-500 sm:truncate" title={hostNames}>
              {rule.interface} · {rule.direction === "in" ? t("rules.dirIn") : t("rules.dirOut")} · {rule.protocol} · {t("rules.table.col.targets")} {rule.workflow_counts.active_targets}/{rule.workflow_counts.targets}
              {!rule.enabled ? t("rules.disabledSuffix") : ""}
            </span>
          </>
        );
      },
    },
    { key: "source", header: t("rules.table.col.source"), priority: "high", mono: true, className: "max-w-[9rem]", cell: (rule) => <span className="block break-all sm:truncate" title={endpoint(rule.source_net, rule.source_port)}>{endpoint(rule.source_net, rule.source_port)}</span> },
    { key: "destination", header: t("rules.table.col.destination"), priority: "high", mono: true, className: "max-w-[10rem]", cell: (rule) => <span className="block break-all sm:truncate" title={endpoint(rule.destination_net, rule.destination_port)}>{endpoint(rule.destination_net, rule.destination_port)}</span> },
    {
      key: "state", header: t("rules.table.col.state"), priority: "high",
      cell: (rule) => {
        const pending = rule.deployments.some((d) => d.status === "pending");
        return (
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
        );
      },
    },
    { key: "lastHit", header: t("rules.table.col.lastHit"), priority: "medium", className: "whitespace-nowrap text-slate-600", cell: lastHit },
  ];
  // Every column is sortable through the page (the page sorts, the table shows the state).
  const sortable = columns.map((c) => ({ ...c, sortable: true }));

  return (
    <DataTable
      label={t("rules.title")}
      rows={rules}
      rowKey={(rule) => rule.id}
      columns={sortable}
      sortMode="server"
      sort={sort as Sort}
      onSortChange={(next) => onSortChange(next as SortState | null)}
      selection={readOnly ? undefined : {
        selected,
        onChange: onSelectionChange,
        rowLabel: (rule) => t("rules.bulk.selectRule", { name: rule.description }),
        selectAllLabel: t("rules.bulk.selectAll"),
      }}
      rowClassName={(rule) => (selected.has(rule.id) ? "bg-brand-50/40" : undefined)}
      actions={(rule) => {
        const locked = rule.deployments.some((d) => ["pending", "revert_failed"].includes(d.status));
        return [
          { key: "edit", label: t("rules.edit"), icon: Pencil, onSelect: () => onEdit(rule), disabled: readOnly || locked },
          { key: "deploy", label: t("rules.reviewPush"), icon: Rocket, onSelect: () => onPreview(rule), disabled: readOnly },
          { key: "history", label: t("rules.historyTitle"), icon: History, onSelect: () => onHistory(rule) },
          { key: "duplicate", label: t("rules.duplicateTitle"), icon: Copy, onSelect: () => onDuplicate(rule), disabled: readOnly },
          { key: "delete", label: t("rules.delete"), icon: Trash2, tone: "danger", onSelect: () => onDelete(rule.id), disabled: readOnly },
        ];
      }}
    />
  );
}
