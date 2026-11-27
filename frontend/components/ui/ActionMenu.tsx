"use client";

import clsx from "clsx";
import { MoreHorizontal, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLanguage } from "@/components/LanguageProvider";
import { IconButton } from "@/components/ui/IconButton";

export interface MenuAction {
  key: string;
  label: string;
  icon?: LucideIcon;
  onSelect?: () => void;
  href?: string;
  /** Open `href` in a new tab (links to the firewall's own interface). */
  external?: boolean;
  /** "danger" actions always sit at the end of the menu, below a separator. */
  tone?: "default" | "danger";
  disabled?: boolean;
}

const MARGIN = 8;

/** Order of the menu: normal entries first, destructive ones last (docs/DESIGN-UI-2026-11.md section 6). */
export function orderActions(actions: MenuAction[]): MenuAction[] {
  return [...actions.filter((a) => a.tone !== "danger"), ...actions.filter((a) => a.tone === "danger")];
}

/**
 * "..." button with a menu (WAI-ARIA menu button pattern): arrow keys, Home/End, Esc, focus returns to the button.
 * The list is portalled to <body> with fixed positioning so a table or card with overflow can never clip it.
 */
export function ActionMenu({ actions, label, align = "end" }: { actions: MenuAction[]; label?: string; align?: "start" | "end" }) {
  const { t } = useLanguage();
  const menuId = useId();
  const triggerRef = useRef<HTMLButtonElement | HTMLAnchorElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const ordered = orderActions(actions);
  const firstDanger = ordered.findIndex((a) => a.tone === "danger");

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) (triggerRef.current as HTMLElement | null)?.focus();
  }, []);

  useLayoutEffect(() => {
    if (!open) { setPos(null); return; }
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const anchor = trigger.getBoundingClientRect();
    const size = menu.getBoundingClientRect();
    let left = align === "end" ? anchor.right - size.width : anchor.left;
    left = Math.min(Math.max(MARGIN, left), Math.max(MARGIN, window.innerWidth - size.width - MARGIN));
    let top = anchor.bottom + 4;
    if (top + size.height > window.innerHeight - MARGIN) top = Math.max(MARGIN, anchor.top - size.height - 4);
    setPos({ left, top });
  }, [open, align, ordered.length]);

  useEffect(() => {
    if (!open) return;
    const items = () => Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? []);
    items()[0]?.focus();
    function onKey(event: KeyboardEvent) {
      const list = items();
      const index = list.indexOf(document.activeElement as HTMLElement);
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); }
      else if (event.key === "ArrowDown") { event.preventDefault(); list[(index + 1) % list.length]?.focus(); }
      else if (event.key === "ArrowUp") { event.preventDefault(); list[(index - 1 + list.length) % list.length]?.focus(); }
      else if (event.key === "Home") { event.preventDefault(); list[0]?.focus(); }
      else if (event.key === "End") { event.preventDefault(); list[list.length - 1]?.focus(); }
      else if (event.key === "Tab") close(false);
    }
    function onOutside(event: Event) {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !(triggerRef.current as HTMLElement | null)?.contains(target)) close(false);
    }
    // The menu sits at a fixed position: when the page scrolls or resizes it would float away from its button, so it closes.
    const onMove = () => close(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onOutside);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onOutside);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, close]);

  if (ordered.length === 0) return null;
  const name = label ?? t("common.moreActions");

  return (
    <>
      <IconButton
        ref={triggerRef}
        icon={MoreHorizontal}
        label={name}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((v) => !v)}
      />
      {open && typeof document !== "undefined" && createPortal(
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={name}
          style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? "visible" : "hidden" }}
          className="fixed z-[70] min-w-[12rem] rounded-xl border border-slate-200 bg-white p-1 text-sm shadow-lg"
        >
          {ordered.map((action, index) => {
            const Icon = action.icon;
            const classes = clsx(
              "flex min-h-9 w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left [@media(pointer:coarse)]:min-h-11",
              action.tone === "danger" ? "text-red-600 hover:bg-red-50 focus:bg-red-50" : "text-slate-700 hover:bg-slate-100 focus:bg-slate-100",
              "focus:outline-none aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
            );
            const content = <>{Icon && <Icon size={15} aria-hidden="true" />}{action.label}</>;
            return (
              // No wrapper element: the menu must own its items directly (ARIA required owned elements).
              <Fragment key={action.key}>
                {index === firstDanger && index > 0 && <div role="separator" className="my-1 border-t border-slate-100" />}
                {action.href && !action.disabled ? (
                  action.external
                    ? <a role="menuitem" tabIndex={-1} href={action.href} target="_blank" rel="noopener noreferrer" onClick={() => close(false)} className={classes}>{content}</a>
                    : <Link role="menuitem" tabIndex={-1} href={action.href} onClick={() => close(false)} className={classes}>{content}</Link>
                ) : (
                  <button
                    type="button"
                    role="menuitem"
                    aria-disabled={action.disabled || undefined}
                    tabIndex={-1}
                    className={classes}
                    onClick={() => { if (action.disabled) return; close(true); action.onSelect?.(); }}
                  >
                    {content}
                  </button>
                )}
              </Fragment>
            );
          })}
        </div>,
        document.body
      )}
    </>
  );
}
