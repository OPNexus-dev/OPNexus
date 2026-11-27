"use client";

import clsx from "clsx";
import { LogOut, Search, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef } from "react";
import { useLanguage } from "@/components/LanguageProvider";
import { IconButton } from "@/components/ui/IconButton";
import { Logo } from "@/components/Logo";
import { matchesNav, navGroups, navLabelKey } from "@/components/Nav";
import { OPEN_COMMAND_PALETTE_EVENT } from "@/lib/events";
import { APP_CODENAME, APP_VERSION } from "@/lib/version";

/**
 * Side menu for screens below 1024 px (docs/DESIGN-UI-2026-11.md section 3): all groups and pages with labels,
 * instead of two rows of unlabelled icons. Focus stays inside while it is open, Esc and a tap next to it close it,
 * and the focus returns to the button that opened it.
 */
export function NavDrawer({
  open,
  onClose,
  pathname,
  username,
  roleLabel,
  onLogout,
  id,
}: {
  open: boolean;
  onClose: () => void;
  pathname: string | null;
  username?: string;
  roleLabel?: string;
  onLogout: () => void;
  id: string;
}) {
  const { t } = useLanguage();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    (panel?.querySelector<HTMLElement>("[aria-current='page']") ?? panel?.querySelector<HTMLElement>("a, button"))?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 print:hidden">
      <button type="button" aria-label={t("nav.closeMenu")} tabIndex={-1} onClick={onClose} className="absolute inset-0 bg-slate-900/50" />
      <div
        id={id}
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("nav.menu")}
        className="absolute inset-y-0 left-0 flex w-[min(20rem,85vw)] flex-col border-r border-slate-200 bg-white shadow-xl"
      >
        <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-3 py-2.5">
          <Logo className="h-6 w-auto" />
          <IconButton icon={X} label={t("nav.closeMenu")} onClick={onClose} />
        </div>
        <button
          type="button"
          onClick={() => { onClose(); window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT)); }}
          className="mx-3 mt-3 flex min-h-11 items-center gap-2 rounded-lg border border-slate-200 px-3 text-left text-sm text-slate-500 hover:bg-slate-100"
        >
          <Search size={16} aria-hidden="true" />
          <span className="flex-1">{t("nav.searchShort")}</span>
        </button>
        <nav aria-label={t("nav.mainNavigation")} className="flex-1 overflow-y-auto px-3 py-3">
          {navGroups.map((group) => {
            const items = group.items.filter((item) => !item.hideInNav);
            return (
              <div key={group.key} className="mb-3">
                <h2 className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">{t(`nav.groups.${group.key}`)}</h2>
                <ul>
                  {items.map(({ href, icon: Icon }) => {
                    const active = matchesNav(pathname, href);
                    return (
                      <li key={href}>
                        <Link
                          href={href}
                          onClick={onClose}
                          aria-current={active ? "page" : undefined}
                          className={clsx(
                            "flex min-h-11 items-center gap-3 rounded-lg px-2 text-sm",
                            active ? "bg-brand-50 font-medium text-brand-600" : "text-slate-700 hover:bg-slate-100"
                          )}
                        >
                          <Icon size={16} aria-hidden="true" />
                          {t(navLabelKey(href))}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </nav>
        <div className="flex items-center justify-between gap-2 border-t border-slate-200 px-3 py-3 text-xs text-slate-500">
          <div className="min-w-0">
            {username && <p className="truncate font-medium text-slate-700">{username}{roleLabel ? ` · ${roleLabel}` : ""}</p>}
            <p>{t("common.versionLabel", { version: APP_VERSION, codename: APP_CODENAME })}</p>
          </div>
          <button type="button" onClick={onLogout} className="flex min-h-11 shrink-0 items-center gap-1.5 rounded-lg px-3 text-sm font-medium text-slate-600 hover:bg-slate-100">
            <LogOut size={16} aria-hidden="true" />
            {t("nav.logout")}
          </button>
        </div>
      </div>
    </div>
  );
}
