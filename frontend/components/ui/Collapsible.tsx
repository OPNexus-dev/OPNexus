"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { ReactNode, useId, useState } from "react";
import { Badge } from "@/components/ui/Badge";

/**
 * Section that starts closed so long lists (hundreds of live firewall rules)
 * don't push everything else off the page. The count shows what is inside
 * without opening it.
 */
export function Collapsible({
  title,
  count,
  defaultOpen = false,
  className,
  children,
}: {
  title: string;
  count?: number | null;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const panelId = useId();
  return (
    <section className={className}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        className="mb-3 flex w-full items-center gap-2 rounded-lg text-left text-lg font-semibold text-slate-900 hover:text-brand-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
      >
        {open ? <ChevronDown size={18} aria-hidden="true" /> : <ChevronRight size={18} aria-hidden="true" />}
        {title}
        {count != null && <Badge tone="neutral">{count.toLocaleString()}</Badge>}
      </button>
      {open && <div id={panelId}>{children}</div>}
    </section>
  );
}
