"use client";

import clsx from "clsx";
import { useId } from "react";

/** Two to four mutually exclusive views of the same data (table/cards, central/live). Native radio inputs: keyboard and screen readers for free. */
export function SegmentedControl<V extends string>({
  label, value, options, onChange,
}: { label: string; value: V; options: { value: V; label: string }[]; onChange: (value: V) => void }) {
  const name = useId();
  return (
    <fieldset className="inline-flex rounded-lg border border-slate-300 bg-white p-0.5">
      <legend className="sr-only">{label}</legend>
      {options.map((option) => (
        <label
          key={option.value}
          className={clsx(
            "relative inline-flex min-h-8 cursor-pointer items-center rounded-md px-3 text-sm font-medium transition-colors [@media(pointer:coarse)]:min-h-10",
            value === option.value ? "bg-brand-500 text-white" : "text-slate-600 hover:bg-slate-100",
            "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-brand-500"
          )}
        >
          <input type="radio" name={name} value={option.value} checked={value === option.value} onChange={() => onChange(option.value)} className="sr-only" />
          {option.label}
        </label>
      ))}
    </fieldset>
  );
}
