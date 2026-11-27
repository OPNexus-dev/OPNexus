"use client";

import clsx from "clsx";
import { ReactNode, useCallback, useEffect, useRef, useState } from "react";

/**
 * Horizontally scrolling row (sub navigation, tab strips) that never cuts content off silently: a fade at the edge shows that there is
 * more, and the active entry (aria-current or aria-selected="true") is scrolled into view when the row appears or the entry changes.
 * `fadeClass` carries the colour of the background behind the row (the fade has to match it).
 */
export function ScrollRow({
  children,
  className,
  fadeFrom = "from-slate-50",
  label,
  activeKey,
  layout = "row",
}: {
  children: ReactNode;
  className?: string;
  /** Tailwind `from-*` class for the edge fade: the background colour behind the row. */
  fadeFrom?: string;
  /** aria-label of the scrolling region (it is focusable by keyboard when it scrolls, axe: scrollable-region-focusable). */
  label?: string;
  /** Changes whenever the active entry changes (usually the pathname), so the row scrolls to it. */
  activeKey?: string;
  /** "row": children laid out in a flex row (navigation). "block": normal block flow (a wide table inside). */
  layout?: "row" | "block";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    update();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    observer?.observe(el);
    window.addEventListener("resize", update);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [update]);

  useEffect(() => {
    const el = ref.current;
    const active = el?.querySelector<HTMLElement>('[aria-current], [aria-selected="true"]');
    if (el && active && typeof active.scrollIntoView === "function") {
      // Scroll only this row, never the page.
      const target = active.offsetLeft - (el.clientWidth - active.offsetWidth) / 2;
      el.scrollTo?.({ left: Math.max(0, target) });
    }
    update();
  }, [activeKey, update]);

  const scrolls = edges.left || edges.right;
  const fade = `pointer-events-none absolute inset-y-0 w-8 from-0% to-transparent ${fadeFrom}`;
  return (
    <div className="relative">
      <div
        ref={ref}
        onScroll={update}
        // A scrolling region must be reachable by keyboard (WCAG 2.1.1, axe scrollable-region-focusable); the links inside are tabbable already,
        // so tabIndex is only set when there are none.
        tabIndex={scrolls && (layout === "block" || !ref.current?.querySelector("a, button")) ? 0 : undefined}
        role={scrolls && label ? "region" : undefined}
        aria-label={label}
        className={clsx(layout === "row" ? "flex items-center" : "block", "overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden", className)}
      >
        {children}
      </div>
      {edges.left && <span aria-hidden="true" data-fade="left" className={clsx(fade, "left-0 bg-gradient-to-r")} />}
      {edges.right && <span aria-hidden="true" data-fade="right" className={clsx(fade, "right-0 bg-gradient-to-l")} />}
    </div>
  );
}
