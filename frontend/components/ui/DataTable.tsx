"use client";

import clsx from "clsx";
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";
import { MenuAction } from "@/components/ui/ActionMenu";
import { RowActions } from "@/components/ui/RowActions";
import { ScrollRow } from "@/components/ui/ScrollRow";
import { useBreakpoint } from "@/lib/useBreakpoint";

/** How much a column matters when space runs out (docs/DESIGN-UI-2026-11.md section 5). */
export type Priority = "primary" | "high" | "medium" | "low";

export interface Column<T> {
  key: string;
  header: string;
  priority: Priority;
  cell: (row: T) => ReactNode;
  sortable?: boolean;
  /** Value used for client-side sorting; strings are compared with the locale, numbers numerically, null/undefined last. */
  sortValue?: (row: T) => string | number | null | undefined;
  mono?: boolean;
  /** The column that names the row (card title on a phone, row header for screen readers). Default: the first primary column. */
  isTitle?: boolean;
  /** Digits of equal width (counts, ports, sizes). */
  tabular?: boolean;
  align?: "right";
  className?: string;
}

export interface Sort { key: string; dir: "asc" | "desc" }

export interface DataTableProps<T> {
  rows: T[];
  rowKey: (row: T) => string;
  columns: Column<T>[];
  /** Accessible name of the table (screen readers; also the label of the scroll region). */
  label: string;
  /** Row actions (see RowActions): at most `visibleActions` icon buttons, the rest in a menu, destructive ones last. */
  actions?: (row: T) => MenuAction[];
  visibleActions?: number;
  selection?: { selected: ReadonlySet<string>; onChange: (next: Set<string>) => void; rowLabel: (row: T) => string; selectAllLabel?: string };
  /** What a phone shows: "cards" (object lists, default), "list" (compact two-line list for masses of data), "scroll" (the table itself, for comparisons). */
  mobile?: "cards" | "list" | "scroll";
  empty?: ReactNode;
  sort?: Sort | null;
  onSortChange?: (sort: Sort | null) => void;
  /** "client": the table sorts by `sortValue`. "server": the parent delivers sorted rows, the table only shows the state. */
  sortMode?: "client" | "server";
  rowClassName?: (row: T) => string | undefined;
  /** Expandable detail below a row (all screen sizes): a toggle per row, `render` is only called while the row is open. */
  detail?: { render: (row: T) => ReactNode };
  /** No outer border, rounding or shadow: for a table inside a card that already has them. */
  bare?: boolean;
  /** Marks the cell that names the row (first primary column) as the row header for screen readers. Default true. */
  rowHeader?: boolean;
}

const isEmpty = (v: string | number | null | undefined) => v === null || v === undefined;

function compare(a: string | number, b: string | number, locale: string): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), locale, { numeric: true, sensitivity: "base" });
}

export function sortRows<T>(rows: T[], columns: Column<T>[], sort: Sort | null | undefined, locale: string): T[] {
  if (!sort) return rows;
  const column = columns.find((c) => c.key === sort.key);
  if (!column?.sortValue) return rows;
  const factor = sort.dir === "asc" ? 1 : -1;
  // Empty values go last in both directions; Array.prototype.sort is stable, so equal rows keep the order they came in.
  return [...rows].sort((x, y) => {
    const a = column.sortValue!(x);
    const b = column.sortValue!(y);
    if (isEmpty(a) || isEmpty(b)) return isEmpty(a) && isEmpty(b) ? 0 : isEmpty(a) ? 1 : -1;
    return factor * compare(a!, b!, locale);
  });
}

export function DataTable<T>({
  rows, rowKey, columns, label, actions, visibleActions = 2, selection, mobile = "cards", empty, sort, onSortChange, sortMode = "client", rowClassName, rowHeader = true, detail, bare = false,
}: DataTableProps<T>) {
  const { t, locale } = useLanguage();
  const breakpoint = useBreakpoint();
  const [innerSort, setInnerSort] = useState<Sort | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const current = sort !== undefined ? sort : innerSort;
  const sorted = useMemo(() => (sortMode === "client" ? sortRows(rows, columns, current, locale) : rows), [rows, columns, current, locale, sortMode]);

  function changeSort(key: string) {
    const next: Sort | null = current?.key === key ? (current.dir === "asc" ? { key, dir: "desc" } : null) : { key, dir: "asc" };
    if (onSortChange) onSortChange(next);
    else setInnerSort(next);
  }
  const toggleOpen = (id: string) => setOpen((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });

  if (rows.length === 0) return <>{empty ?? null}</>;

  const phone = breakpoint === "phone";
  const showCards = phone && mobile === "cards";
  const showList = phone && mobile === "list";
  const tablet = breakpoint === "tablet";

  const primary = columns.filter((c) => c.priority === "primary");
  const titleColumn = primary.find((c) => c.isTitle) ?? primary[0];
  const high = columns.filter((c) => c.priority === "high");
  const hiddenAtTablet = columns.filter((c) => c.priority === "medium" || c.priority === "low");
  // Columns of the table itself: all on desktop and in "scroll" mode, primary + high on a tablet.
  const tableColumns = tablet ? [...columns.filter((c) => c.priority === "primary" || c.priority === "high")] : columns;
  const expandable = (tablet && hiddenAtTablet.length > 0) || !!detail;
  const hiddenInDetail = tablet ? hiddenAtTablet : [];

  const allSelected = !!selection && sorted.length > 0 && sorted.every((r) => selection.selected.has(rowKey(r)));
  const someSelected = !!selection && !allSelected && sorted.some((r) => selection.selected.has(rowKey(r)));
  function toggleAll(checked: boolean) {
    if (!selection) return;
    const next = new Set(selection.selected);
    for (const r of sorted) { if (checked) next.add(rowKey(r)); else next.delete(rowKey(r)); }
    selection.onChange(next);
  }
  function toggleRow(id: string) {
    if (!selection) return;
    const next = new Set(selection.selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    selection.onChange(next);
  }

  const sortOptions = columns.filter((c) => c.sortable);
  // A card shows its high columns; the compact list only the first two non-primary columns (high first), the rest goes to "Details".
  const shownInItem = showCards ? high : [...high, ...hiddenAtTablet].slice(0, 2);

  if (showCards || showList) {
    return (
      <div>
        {(selection || (showCards && sortOptions.length > 0)) && (
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-600">
            {selection && (
              <SelectAll checked={allSelected} indeterminate={someSelected} label={selection.selectAllLabel ?? t("common.selectAll")} onChange={toggleAll} />
            )}
            {showCards && sortOptions.length > 0 && (
              <label className="flex items-center gap-2">
                <span>{t("common.sortBy")}</span>
                <select
                  className="min-h-9 rounded-lg border border-slate-300 bg-white px-2 text-sm [@media(pointer:coarse)]:min-h-11"
                  value={current ? `${current.key}:${current.dir}` : ""}
                  onChange={(e) => {
                    const [key, dir] = e.target.value.split(":");
                    const next = key ? ({ key, dir } as Sort) : null;
                    if (onSortChange) onSortChange(next); else setInnerSort(next);
                  }}
                >
                  <option value="" />
                  {sortOptions.flatMap((c) => [
                    <option key={`${c.key}:asc`} value={`${c.key}:asc`}>{c.header} ({t("common.sortAscending")})</option>,
                    <option key={`${c.key}:desc`} value={`${c.key}:desc`}>{c.header} ({t("common.sortDescending")})</option>,
                  ])}
                </select>
              </label>
            )}
          </div>
        )}
        <ul aria-label={label} className={clsx(showList ? "divide-y divide-slate-100 rounded-2xl border border-slate-200 bg-white" : "space-y-3")}>
          {sorted.map((row) => {
            const id = rowKey(row);
            const title = titleColumn;
            const badges = primary.filter((c) => c !== titleColumn);
            const isOpen = open.has(id);
            // Everything that is not on the card/list item itself is behind "Details": nothing is dropped on a phone.
            const detailColumns = columns.filter((c) => c.priority !== "primary" && !shownInItem.includes(c));
            const hasMore = detailColumns.length > 0 || !!detail;
            const rowActionList = actions?.(row) ?? [];
            return (
              <li key={id} className={clsx(showList ? "px-4 py-3" : "rounded-2xl border border-slate-200 bg-white p-4 shadow-sm", rowClassName?.(row))}>
                <div className="flex items-start gap-3">
                  {selection && (
                    <input
                      type="checkbox"
                      className="mt-1 h-5 w-5 shrink-0"
                      checked={selection.selected.has(id)}
                      aria-label={selection.rowLabel(row)}
                      onChange={() => toggleRow(id)}
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <div className={clsx("min-w-0 break-words font-medium text-slate-900", title?.mono && "font-mono text-xs")}>{title?.cell(row)}</div>
                      {badges.length > 0 && <div className="flex shrink-0 flex-wrap justify-end gap-1">{badges.map((c) => <Fragment key={c.key}>{c.cell(row)}</Fragment>)}</div>}
                    </div>
                    {shownInItem.length > 0 && (
                      <dl className="mt-2 grid grid-cols-[auto,minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
                        {shownInItem.map((c) => (
                          <Fragment key={c.key}>
                            <dt className="text-slate-500">{c.header}</dt>
                            <dd className={clsx("min-w-0 break-words text-slate-700", c.mono && "font-mono text-xs")}>{c.cell(row)}</dd>
                          </Fragment>
                        ))}
                      </dl>
                    )}
                    {hasMore && (
                      <>
                        <button
                          type="button"
                          aria-expanded={isOpen}
                          onClick={() => toggleOpen(id)}
                          className="mt-2 inline-flex min-h-9 items-center gap-1 text-sm font-medium text-brand-600 [@media(pointer:coarse)]:min-h-11"
                        >
                          {isOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                          {isOpen ? t("common.hideDetails") : t("common.showDetails")}
                        </button>
                        {isOpen && (
                          <dl className="mt-1 grid grid-cols-[auto,minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
                            {detailColumns.map((c) => (
                              <Fragment key={c.key}>
                                <dt className="text-slate-500">{c.header}</dt>
                                <dd className={clsx("min-w-0 break-words text-slate-700", c.mono && "font-mono text-xs")}>{c.cell(row)}</dd>
                              </Fragment>
                            ))}
                          </dl>
                        )}
                        {isOpen && detail && <div className="mt-2">{detail.render(row)}</div>}
                      </>
                    )}
                  </div>
                </div>
                {rowActionList.length > 0 && <div className={clsx("pt-2", showCards && "mt-3 border-t border-slate-100")}><RowActions actions={rowActionList} visible={visibleActions} /></div>}
              </li>
            );
          })}
        </ul>
      </div>
    );
  }

  const rowHeaderKey = rowHeader ? titleColumn?.key : undefined;
  const colCount = tableColumns.length + (selection ? 1 : 0) + (actions ? 1 : 0) + (expandable ? 1 : 0);

  return (
    <div className={clsx("bg-white", !bare && "overflow-hidden rounded-2xl border border-slate-200 shadow-sm")}>
      <ScrollRow layout="block" label={label} fadeFrom="from-[rgb(var(--surface))]" className={bare ? undefined : "rounded-2xl"}>
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{label}</caption>
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              {expandable && <th className="w-8 px-2 py-3"><span className="sr-only">{t("common.showDetails")}</span></th>}
              {selection && (
                <th className="w-10 px-3 py-3">
                  <SelectAll checked={allSelected} indeterminate={someSelected} label={selection.selectAllLabel ?? t("common.selectAll")} onChange={toggleAll} hideLabel />
                </th>
              )}
              {tableColumns.map((c) => {
                const active = current?.key === c.key;
                const Icon = !active ? ArrowUpDown : current!.dir === "asc" ? ArrowUp : ArrowDown;
                return (
                  <th
                    key={c.key}
                    scope="col"
                    aria-sort={c.sortable ? (active ? (current!.dir === "asc" ? "ascending" : "descending") : "none") : undefined}
                    className={clsx("whitespace-nowrap px-4 py-3 font-medium", c.align === "right" && "text-right")}
                  >
                    {c.sortable ? (
                      <button type="button" onClick={() => changeSort(c.key)} className="inline-flex min-h-8 items-center gap-1 uppercase tracking-wide hover:text-slate-800 [@media(pointer:coarse)]:min-h-11">
                        {c.header}
                        <Icon size={12} aria-hidden="true" className={active ? "text-brand-500" : "text-slate-300"} />
                      </button>
                    ) : c.header}
                  </th>
                );
              })}
              {actions && <th scope="col" className="sticky right-0 z-[1] bg-slate-50 px-3 py-3"><span className="sr-only">{t("common.actions")}</span></th>}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const id = rowKey(row);
              const isOpen = open.has(id);
              return (
                <Fragment key={id}>
                  <tr className={clsx("border-b border-slate-100 last:border-0", selection?.selected.has(id) && "bg-brand-50/40", rowClassName?.(row))}>
                    {expandable && (
                      <td className="px-2 py-3">
                        <button type="button" aria-expanded={isOpen} aria-label={isOpen ? t("common.hideDetails") : t("common.showDetails")} onClick={() => toggleOpen(id)} className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11">
                          {isOpen ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
                        </button>
                      </td>
                    )}
                    {selection && (
                      <td className="px-3 py-3">
                        <input type="checkbox" className="h-4 w-4 [@media(pointer:coarse)]:h-5 [@media(pointer:coarse)]:w-5" checked={selection.selected.has(id)} aria-label={selection.rowLabel(row)} onChange={() => toggleRow(id)} />
                      </td>
                    )}
                    {tableColumns.map((c, index) => {
                      const classes = clsx("px-4 py-3", c.mono && "font-mono text-xs text-slate-600", c.tabular && "tabular-nums", c.align === "right" && "text-right", phone && index === 0 && !selection && !expandable && "sticky left-0 z-[1] bg-white", c.className);
                      return c.key === rowHeaderKey
                        ? <th key={c.key} scope="row" className={clsx(classes, "font-normal")}>{c.cell(row)}</th>
                        : <td key={c.key} className={classes}>{c.cell(row)}</td>;
                    })}
                    {actions && <td className="sticky right-0 z-[1] bg-white px-3 py-3 shadow-[-8px_0_8px_-8px_rgba(0,0,0,0.15)]"><RowActions actions={actions(row)} visible={visibleActions} /></td>}
                  </tr>
                  {expandable && isOpen && (
                    <tr className="border-b border-slate-100 bg-slate-50/60">
                      <td colSpan={colCount} className="px-6 py-3">
                        {hiddenInDetail.length > 0 && (
                          <dl className="grid grid-cols-[auto,minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
                            {hiddenInDetail.map((c) => (
                              <Fragment key={c.key}>
                                <dt className="text-slate-500">{c.header}</dt>
                                <dd className={clsx("min-w-0 break-words text-slate-700", c.mono && "font-mono text-xs")}>{c.cell(row)}</dd>
                              </Fragment>
                            ))}
                          </dl>
                        )}
                        {detail && <div className={clsx(hiddenInDetail.length > 0 && "mt-3")}>{detail.render(row)}</div>}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </ScrollRow>
    </div>
  );
}

function SelectAll({ checked, indeterminate, label, onChange, hideLabel }: { checked: boolean; indeterminate: boolean; label: string; onChange: (checked: boolean) => void; hideLabel?: boolean }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = indeterminate; }, [indeterminate]);
  return (
    <label className="inline-flex min-h-8 items-center gap-2 [@media(pointer:coarse)]:min-h-11">
      <input ref={ref} type="checkbox" className="h-4 w-4 [@media(pointer:coarse)]:h-5 [@media(pointer:coarse)]:w-5" checked={checked} onChange={(e) => onChange(e.target.checked)} aria-label={hideLabel ? label : undefined} />
      {!hideLabel && <span>{label}</span>}
    </label>
  );
}
