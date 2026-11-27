"use client";

import clsx from "clsx";
import { ChevronDown, Search, SlidersHorizontal, X } from "lucide-react";
import { ReactNode, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";
import { Modal } from "@/components/ui/Modal";
import { useBreakpoint } from "@/lib/useBreakpoint";

export interface FilterSelectDef {
  key: string;
  /** Accessible name of the select ("Status"). */
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  /** Text of the "no filter" entry (value ""), e.g. "All states". */
  allLabel: string;
}

export interface FilterBarProps {
  search?: { value: string; onChange: (value: string) => void; placeholder: string; label: string };
  filters?: FilterSelectDef[];
  /** View switch (table/cards, central/live) that belongs to the toolbar. */
  segment?: ReactNode;
  /** Right side of the toolbar: counters (StatChip) or small actions. */
  trailing?: ReactNode;
  onReset?: () => void;
  className?: string;
}

function FilterSelect({ def, stacked }: { def: FilterSelectDef; stacked?: boolean }) {
  return (
    <div className={clsx("relative", stacked ? "w-full" : "w-auto min-w-[9rem] max-w-[16rem]")}>
      <select
        aria-label={def.label}
        value={def.value}
        onChange={(e) => def.onChange(e.target.value)}
        className="min-h-[2.5rem] w-full appearance-none truncate rounded-lg border border-slate-300 bg-white py-2 pl-3 pr-9 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 [@media(pointer:coarse)]:min-h-11"
      >
        <option value="">{def.allLabel}</option>
        {def.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      <ChevronDown size={16} aria-hidden="true" className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400" />
    </div>
  );
}

/**
 * Toolbar above lists (docs/DESIGN-UI-2026-11.md section 8): search, up to three filters, an optional view switch and counters on one line.
 * On a phone only the search and one "Filters (n)" button are shown, the filters open in a sheet. Active filters show as chips with a reset.
 */
export function FilterBar({ search, filters = [], segment, trailing, onReset, className }: FilterBarProps) {
  const { t } = useLanguage();
  const phone = useBreakpoint() === "phone";
  const [sheet, setSheet] = useState(false);
  const active = filters.filter((f) => f.value !== "");
  const anything = active.length > 0 || !!search?.value;
  const optionLabel = (f: FilterSelectDef) => f.options.find((o) => o.value === f.value)?.label ?? f.value;

  const searchField = search && (
    <div className="relative min-w-0 flex-1 basis-60">
      <Search size={16} aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
      <input
        type="search"
        aria-label={search.label}
        placeholder={search.placeholder}
        value={search.value}
        onChange={(e) => search.onChange(e.target.value)}
        className="min-h-[2.5rem] w-full rounded-lg border border-slate-300 py-2 pl-9 pr-3 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 [@media(pointer:coarse)]:min-h-11"
      />
    </div>
  );

  return (
    <div className={clsx("space-y-2", className)}>
      <div className="flex flex-wrap items-center gap-2">
        {searchField}
        {phone ? (
          filters.length > 0 && (
            <button
              type="button"
              onClick={() => setSheet(true)}
              aria-haspopup="dialog"
              className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 text-sm font-medium text-slate-700"
            >
              <SlidersHorizontal size={16} aria-hidden="true" />
              {active.length > 0 ? t("common.filterCount", { count: active.length }) : t("common.filter")}
            </button>
          )
        ) : (
          filters.map((f) => <FilterSelect key={f.key} def={f} />)
        )}
        {segment}
        {trailing && <div className="flex flex-wrap items-center gap-2 sm:ml-auto">{trailing}</div>}
      </div>

      {anything && (
        <div className="flex flex-wrap items-center gap-2" aria-label={t("common.filter")}>
          {active.map((f) => (
            <span key={f.key} className="inline-flex items-center gap-1 rounded-full border border-brand-200 bg-brand-50 py-0.5 pl-2.5 pr-1 text-sm text-brand-700">
              <span>{f.label}: {optionLabel(f)}</span>
              <button
                type="button"
                aria-label={t("common.removeFilter", { name: `${f.label}: ${optionLabel(f)}` })}
                onClick={() => f.onChange("")}
                className="inline-flex h-6 w-6 items-center justify-center rounded-full hover:bg-brand-100 [@media(pointer:coarse)]:h-8 [@media(pointer:coarse)]:w-8"
              >
                <X size={12} aria-hidden="true" />
              </button>
            </span>
          ))}
          {onReset && (
            <button type="button" onClick={onReset} className="min-h-8 rounded-lg px-2 text-sm font-medium text-slate-600 hover:bg-slate-100 [@media(pointer:coarse)]:min-h-11">
              {t("common.resetFilters")}
            </button>
          )}
        </div>
      )}

      {sheet && (
        <Modal title={t("common.filter")} onClose={() => setSheet(false)} sheet>
          <div className="space-y-3">
            {filters.map((f) => (
              <label key={f.key} className="block text-sm font-medium text-slate-700">
                <span className="mb-1 block">{f.label}</span>
                <FilterSelect def={f} stacked />
              </label>
            ))}
            <div className="flex justify-between gap-2 pt-2">
              {onReset ? <button type="button" onClick={onReset} className="min-h-11 rounded-lg px-3 text-sm font-medium text-slate-600 hover:bg-slate-100">{t("common.resetFilters")}</button> : <span />}
              <button type="button" onClick={() => setSheet(false)} className="min-h-11 rounded-lg bg-brand-500 px-4 text-sm font-medium text-white hover:bg-brand-600">{t("common.done")}</button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
