"use client";

import clsx from "clsx";
import type { LucideIcon } from "lucide-react";
import Link from "next/link";
import { ButtonHTMLAttributes, forwardRef } from "react";
import { Tooltip } from "@/components/ui/Tooltip";

type Tone = "default" | "danger";

interface Common {
  icon: LucideIcon;
  /** Accessible name and tooltip text. Required: an icon alone says nothing to a screen reader. */
  label: string;
  tone?: Tone;
  size?: number;
  className?: string;
}

const toneClasses: Record<Tone, string> = {
  default: "text-slate-500 hover:bg-slate-100 hover:text-slate-800",
  danger: "text-red-600 hover:bg-red-50",
};

// 32 px with a mouse, 44 px on touch screens (docs/DESIGN-UI-2026-11.md section 7). The pointer rule is in the class, so pages do nothing.
const base =
  "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:cursor-not-allowed disabled:opacity-50 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11";

type ButtonProps = Common & { href?: undefined } & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "children">;
type LinkProps = Common & { href: string; onClick?: () => void; target?: string; rel?: string };

/** Icon-only action with a tooltip and an accessible name (row actions, toolbars). Renders a link when `href` is set. */
export const IconButton = forwardRef<HTMLButtonElement | HTMLAnchorElement, ButtonProps | LinkProps>(function IconButton(props, ref) {
  const { icon: Icon, label, tone = "default", size = 16 } = props;
  const classes = clsx(base, toneClasses[tone], props.className);
  if (props.href !== undefined) {
    const { href, onClick, target, rel } = props as LinkProps;
    const external = /^https?:\/\//.test(href);
    return (
      <Tooltip content={label}>
        {external
          ? <a ref={ref as React.Ref<HTMLAnchorElement>} href={href} onClick={onClick} target={target} rel={rel ?? (target === "_blank" ? "noopener noreferrer" : undefined)} aria-label={label} className={classes}><Icon size={size} aria-hidden="true" /></a>
          : <Link ref={ref as React.Ref<HTMLAnchorElement>} href={href} onClick={onClick} aria-label={label} className={classes}><Icon size={size} aria-hidden="true" /></Link>}
      </Tooltip>
    );
  }
  const { icon: _i, label: _l, tone: _t, size: _s, className: _c, type = "button", ...rest } = props as ButtonProps;
  void _i; void _l; void _t; void _s; void _c;
  return (
    <Tooltip content={label}>
      <button ref={ref as React.Ref<HTMLButtonElement>} type={type} aria-label={label} className={classes} {...rest}><Icon size={size} aria-hidden="true" /></button>
    </Tooltip>
  );
});
