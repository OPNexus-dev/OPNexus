import clsx from "clsx";
import { Loader2 } from "lucide-react";
import { ReactNode } from "react";

/** Inline "something is loading" line: animated spinner plus text, announced politely. */
export function Loading({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p role="status" className={clsx("inline-flex items-center gap-2 text-sm text-slate-500", className)}>
      <Loader2 size={16} aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
      {children}
    </p>
  );
}
