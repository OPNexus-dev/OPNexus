import clsx from "clsx";
import { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";

/**
 * Thin styled wrappers around the plain <table> markup already used
 * consistently across hosts/rules/nat/aliases/etc (see e.g. app/hosts/page.tsx)
 * — same classes, just named so new tables don't have to re-copy them by hand.
 */
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return <table className={clsx("w-full text-left text-sm", className)} {...props} />;
}

export function THead({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead
      className={clsx(
        "border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500",
        className
      )}
      {...props}
    />
  );
}

export function TBody(props: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody {...props} />;
}

export function TRow({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={clsx("border-b border-slate-100 last:border-0", className)} {...props} />;
}

export function TH({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return <th className={clsx("px-5 py-3 font-medium", className)} {...props} />;
}

export function TD({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={clsx("px-5 py-3", className)} {...props} />;
}
