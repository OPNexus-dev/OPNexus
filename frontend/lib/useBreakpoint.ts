"use client";

import { useSyncExternalStore } from "react";

/**
 * Screen class for layout decisions that CSS alone cannot make (render a table OR cards, drawer OR bar).
 * Same limits as Tailwind: phone < 768 px (md), tablet 768-1023 px, desktop >= 1024 px (lg).
 * Touch sizing is separate (pointer: coarse in globals.css), it does not depend on the width.
 */
export type Breakpoint = "phone" | "tablet" | "desktop";

export const TABLET_MIN = 768;
export const DESKTOP_MIN = 1024;

export function breakpointFor(width: number): Breakpoint {
  if (width >= DESKTOP_MIN) return "desktop";
  if (width >= TABLET_MIN) return "tablet";
  return "phone";
}

const QUERIES = [`(min-width: ${TABLET_MIN}px)`, `(min-width: ${DESKTOP_MIN}px)`];

function current(): Breakpoint {
  if (typeof window === "undefined" || !window.matchMedia) return "desktop";
  if (window.matchMedia(QUERIES[1]).matches) return "desktop";
  if (window.matchMedia(QUERIES[0]).matches) return "tablet";
  return "phone";
}

function subscribe(onChange: () => void) {
  const lists = QUERIES.map((q) => window.matchMedia(q));
  lists.forEach((l) => l.addEventListener("change", onChange));
  return () => lists.forEach((l) => l.removeEventListener("change", onChange));
}

/** The app renders only in the browser (AuthGate shows nothing before the sign-in check), so there is no server/client mismatch to guard against. */
export function useBreakpoint(): Breakpoint {
  return useSyncExternalStore(subscribe, current, () => "desktop");
}
