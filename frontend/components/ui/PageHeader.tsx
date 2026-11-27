"use client";

import clsx from "clsx";
import { ArrowLeft } from "lucide-react";
import { ReactNode } from "react";
import { IconButton } from "@/components/ui/IconButton";
import { InfoTip } from "@/components/ui/Tooltip";

interface PageHeaderProps {
  title: ReactNode;
  /** Short explanation shown as a tooltip next to the title (instead of a text block below it). */
  info?: string;
  /** Icon button to the left of the title; for detail pages. */
  back?: { href: string; label: string };
  /** Right side; one primary action, further ones as outline. On phones they wrap under the title. */
  actions?: ReactNode;
  /** One row of tabs (components/ui/Tabs) or a similar control directly under the title. At most one level (docs/DESIGN-UI-2026-11.md section 4). */
  tabs?: ReactNode;
  /** Small text line under the title, for a subtitle that is not worth a tooltip (a host's address, a version). */
  meta?: ReactNode;
  className?: string;
}

/**
 * Page head of every page: exactly one <h1>, always at the same place (the page content starts at the edge of <main>,
 * the page adds no outer padding of its own). Replaces the hand-built <h1> variants that differed in size and spacing.
 */
export function PageHeader({ title, info, back, actions, tabs, meta, className }: PageHeaderProps) {
  return (
    <header className={clsx("mb-5 space-y-3", className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="flex min-w-0 items-center gap-2">
          {back && <IconButton href={back.href} icon={ArrowLeft} label={back.label} className="-ml-2" />}
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-xl font-semibold text-slate-900 sm:text-2xl">
              <span className="min-w-0 break-words">{title}</span>
              {info && <InfoTip text={info} size={18} />}
            </h1>
            {meta && <div className="mt-1 text-sm text-slate-500">{meta}</div>}
          </div>
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {tabs}
    </header>
  );
}
