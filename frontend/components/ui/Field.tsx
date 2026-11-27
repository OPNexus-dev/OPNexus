"use client";

import clsx from "clsx";
import { InfoTip } from "@/components/ui/Tooltip";
import { ComponentProps, createContext, useContext, useId, LabelHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

const FieldLabel = createContext<string | undefined>(undefined);

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={`mb-1 block text-sm font-medium text-slate-700 ${className ?? ""}`} {...props} />;
}

const controlClass =
  "min-h-[2.5rem] [@media(pointer:coarse)]:min-h-11 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";

// ComponentProps (not InputHTMLAttributes) so that `ref` is accepted: React 19 passes it down as an ordinary prop.
export function Input({ className, ...props }: ComponentProps<"input">) {
  const labelId = useContext(FieldLabel);
  return <input aria-labelledby={props["aria-label"] ? undefined : labelId} {...props} className={clsx(controlClass, className)} />;
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement> & { children: ReactNode }) {
  const labelId = useContext(FieldLabel);
  return <select aria-labelledby={props["aria-label"] ? undefined : labelId} {...props} className={clsx(controlClass, "select-control", className)} />;
}

export function FieldGroup({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  const id = useId();
  return (
    <FieldLabel.Provider value={id}><div>
      <Label id={id}>{label}{hint && <InfoTip text={hint} size={13} className="ml-1 align-middle" />}</Label>
      {children}
    </div></FieldLabel.Provider>
  );
}
