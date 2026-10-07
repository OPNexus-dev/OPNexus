"use client";

import clsx from "clsx";
import { InfoTip } from "@/components/ui/Tooltip";
import { createContext, useContext, useId, InputHTMLAttributes, LabelHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

const FieldLabel = createContext<string | undefined>(undefined);

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={`mb-1 block text-sm font-medium text-slate-700 ${className ?? ""}`} {...props} />;
}

const controlClass =
  "min-h-[2.5rem] w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  const labelId = useContext(FieldLabel);
  return <input aria-labelledby={props["aria-label"] ? undefined : labelId} {...props} className={clsx(controlClass, className)} />;
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement> & { children: ReactNode }) {
  const labelId = useContext(FieldLabel);
  return <select aria-labelledby={props["aria-label"] ? undefined : labelId} {...props} className={clsx(controlClass, className)} />;
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
