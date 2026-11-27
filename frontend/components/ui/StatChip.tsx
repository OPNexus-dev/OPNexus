"use client";

import clsx from "clsx";
import { badgeToneClasses, BadgeTone } from "@/components/ui/Badge";

/**
 * Count + label as a chip ("0 pending", "3 to review"): replaces running text like "0 pending changes · 0 to review".
 * A count of 0 is shown in the neutral tone (not faded: opacity lowers the contrast below WCAG AA), so the chips that matter stand out by colour.
 * With `onClick` it is a button (usually sets the matching filter).
 */
export function StatChip({
  count, label, tone = "neutral", onClick, active,
}: { count: number; label: string; tone?: BadgeTone; onClick?: () => void; active?: boolean }) {
  const classes = clsx(
    "inline-flex min-h-7 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm",
    count === 0 ? badgeToneClasses.neutral : badgeToneClasses[tone],
    onClick && "cursor-pointer hover:brightness-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 [@media(pointer:coarse)]:min-h-11",
    active && "ring-2 ring-brand-500"
  );
  const content = (<><strong className={clsx("tabular-nums", count === 0 ? "font-normal" : "font-semibold")}>{count}</strong><span>{label}</span></>);
  return onClick
    ? <button type="button" onClick={onClick} aria-pressed={active} className={classes}>{content}</button>
    : <span className={classes}>{content}</span>;
}
