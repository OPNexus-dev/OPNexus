"use client";

import clsx from "clsx";
import { Info } from "lucide-react";
import { ReactNode, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

type Placement = "top" | "bottom";

const GAP = 8;
const MARGIN = 8;

/**
 * Accessible tooltip. Shows on hover, keyboard focus and tap; Esc closes it.
 * The wrapper (not the child) receives the pointer/focus events, so it also
 * works on disabled buttons — which swallow their own events — and is exactly
 * where "why is this disabled?" explanations belong. The bubble is portalled
 * to <body> with fixed positioning so it is never clipped by overflow-hidden
 * cards or scrolling modals, and flips/clamps to stay inside the viewport.
 */
export function Tooltip({
  content,
  children,
  placement = "top",
  className,
  disabled = false,
  focusable = false,
}: {
  content: ReactNode;
  children: ReactNode;
  placement?: Placement;
  className?: string;
  disabled?: boolean;
  /** Make the wrapper itself tabbable — needed when the child is a disabled button. */
  focusable?: boolean;
}) {
  const id = useId();
  const wrapRef = useRef<HTMLSpanElement>(null);
  const bubbleRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const hasContent = Boolean(content) && !disabled;

  const reposition = useCallback(() => {
    const wrap = wrapRef.current;
    const bubble = bubbleRef.current;
    if (!wrap || !bubble) return;
    const anchor = wrap.getBoundingClientRect();
    const size = bubble.getBoundingClientRect();
    let top = placement === "top" ? anchor.top - size.height - GAP : anchor.bottom + GAP;
    if (top < MARGIN) top = anchor.bottom + GAP;
    else if (top + size.height > window.innerHeight - MARGIN) top = Math.max(MARGIN, anchor.top - size.height - GAP);
    const centered = anchor.left + anchor.width / 2 - size.width / 2;
    const left = Math.min(Math.max(MARGIN, centered), Math.max(MARGIN, window.innerWidth - size.width - MARGIN));
    setPos({ left, top });
  }, [placement]);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    reposition();
  }, [open, content, reposition]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onOutside = (event: Event) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    document.addEventListener("pointerdown", onOutside);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      document.removeEventListener("pointerdown", onOutside);
    };
  }, [open, reposition]);

  if (!hasContent) return <span className={clsx("inline-flex", className)}>{children}</span>;

  return (
    <span
      ref={wrapRef}
      tabIndex={focusable ? 0 : undefined}
      className={clsx("inline-flex", className)}
      aria-describedby={open ? id : undefined}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {children}
      {open &&
        createPortal(
          <span
            ref={bubbleRef}
            id={id}
            role="tooltip"
            style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? "visible" : "hidden" }}
            className="pointer-events-none fixed z-[60] max-w-xs [overflow-wrap:anywhere] rounded-lg border border-[#3f3f46] bg-[#18181b] px-3 py-2 text-xs font-normal normal-case leading-snug tracking-normal text-[#f8fafc] shadow-lg"
          >
            {content}
          </span>,
          document.body
        )}
    </span>
  );
}

/**
 * Small "i" icon that carries explanatory text in a tooltip — the replacement
 * for permanent intro paragraphs. Focusable, so keyboard and touch users get
 * the text too.
 */
export function InfoTip({ text, label, className, size = 15 }: { text: ReactNode; label?: string; className?: string; size?: number }) {
  return (
    <Tooltip content={text} className={className}>
      <button
        type="button"
        aria-label={label ?? (typeof text === "string" ? text : undefined)}
        className="inline-flex items-center rounded-full text-slate-400 transition-colors hover:text-brand-600 focus-visible:text-brand-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500"
        onClick={(event) => event.preventDefault()}
      >
        <Info size={size} aria-hidden="true" />
      </button>
    </Tooltip>
  );
}
