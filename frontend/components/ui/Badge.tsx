import clsx from "clsx";
import { HTMLAttributes } from "react";

export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";

// Exported so call sites that need the tone colors at a non-badge size (e.g.
// a larger stat pill) can reuse the exact same palette instead of hardcoding
// bg-x-50/text-x-700 literals again — see app/logs/page.tsx's action stat row.
export const badgeToneClasses: Record<BadgeTone, string> = {
  neutral: "bg-[rgb(var(--tone-neutral-bg))] text-[rgb(var(--tone-neutral-text))] border-[rgb(var(--tone-neutral-border))]",
  info: "bg-[rgb(var(--tone-info-bg))] text-[rgb(var(--tone-info-text))] border-[rgb(var(--tone-info-border))]",
  success: "bg-[rgb(var(--tone-success-bg))] text-[rgb(var(--tone-success-text))] border-[rgb(var(--tone-success-border))]",
  warning: "bg-[rgb(var(--tone-warning-bg))] text-[rgb(var(--tone-warning-text))] border-[rgb(var(--tone-warning-border))]",
  danger: "bg-[rgb(var(--tone-danger-bg))] text-[rgb(var(--tone-danger-text))] border-[rgb(var(--tone-danger-border))]",
  accent: "bg-[rgb(var(--tone-accent-bg))] text-[rgb(var(--tone-accent-text))] border-[rgb(var(--tone-accent-border))]",
};

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

/**
 * Small status pill. Colors come from the --tone-* CSS variables (globals.css),
 * which are defined per-theme directly — not derived from Tailwind's slate/sky/...
 * scales — so a page using this component needs no entry in the legacy
 * dark-mode override table to look right in both themes.
 */
export function Badge({ tone = "neutral", className, ...props }: BadgeProps) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        badgeToneClasses[tone],
        className
      )}
      {...props}
    />
  );
}
