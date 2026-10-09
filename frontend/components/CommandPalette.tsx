"use client";

import { navGroups, navLabelKey } from "@/components/Nav";
import { useLanguage } from "@/components/LanguageProvider";
import { api, Host, SearchHit } from "@/lib/api";
import { OPEN_COMMAND_PALETTE_EVENT } from "@/lib/events";
import clsx from "clsx";
import { CornerDownLeft, Loader2, Network, Search, Shield, Tag, Waypoints, KeyRound, GitBranch, Layers } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

type PaletteItem = {
  key: string;
  href: string;
  label: string;
  hint?: string;
  icon: typeof Network;
  keywords?: string[];
  category: string;
};

// Icon per category — used for remote (backend-searched) hits, which don't
// carry their own per-row icon the way nav items do.
const CATEGORY_ICONS: Record<string, typeof Network> = {
  rules: Shield,
  aliases: Tag,
  nat: Waypoints,
  vpn: KeyRound,
  certificates: KeyRound,
  groups: Layers,
  "policy-hierarchy": GitBranch,
};

const SEARCH_MIN_LENGTH = 2;
const SEARCH_DEBOUNCE_MS = 200;

// Cheap relevance heuristic for the two locally-searched categories
// (navigation, hosts) — mirrors the backend's own scoring in
// routers/search.py so a query behaves consistently whether it's matched
// client-side or server-side. Not fuzzy/edit-distance matching, just reward
// tighter matches (exact > prefix > word-start > mid-string) over loose
// substring hits.
function matchScore(text: string | undefined, q: string): number {
  if (!text) return 0;
  const t = text.toLowerCase();
  if (t === q) return 100;
  if (t.startsWith(q)) return 80;
  const idx = t.indexOf(q);
  if (idx === -1) return 0;
  if (idx === 0 || !/[a-z0-9]/i.test(t[idx - 1])) return 60;
  return 40;
}

function itemScore(item: PaletteItem, q: string): number {
  const scores = [matchScore(item.label, q), matchScore(item.hint, q), ...(item.keywords ?? []).map((k) => matchScore(k, q))];
  return Math.max(...scores);
}

function rankAndFilter(items: PaletteItem[], q: string): PaletteItem[] {
  return items
    .map((item) => ({ item, score: itemScore(item, q) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score)
    .map(({ item }) => item);
}

// Bolds the first occurrence of the query within a label — a plain <span>
// rather than <mark> so it doesn't carry the browser's default yellow
// highlight styling, just Tailwind's.
function HighlightedText({ text, query }: { text: string; query: string }) {
  const q = query.trim();
  if (!q) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(q.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <span className="rounded bg-flame-100 px-0.5 text-flame-800">{text.slice(idx, idx + q.length)}</span>
      {text.slice(idx + q.length)}
    </>
  );
}

export function CommandPalette() {
  const router = useRouter();
  const { t } = useLanguage();
  const navItems: PaletteItem[] = useMemo(
    () =>
      navGroups.flatMap((group) =>
        group.items.map((item) => {
          const label = t(navLabelKey(item.href));
          const groupLabel = t(`nav.groups.${group.key}`);
          return {
            key: `nav:${item.href}`,
            href: item.href,
            label,
            hint: label === groupLabel ? undefined : groupLabel,
            icon: item.icon,
            keywords: item.keywords,
            category: "navigation",
          };
        })
      ),
    [t]
  );
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hosts, setHosts] = useState<Host[]>([]);
  const [remoteHits, setRemoteHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function openPalette() {
      setOpen(true);
    }
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    }
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, openPalette);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, openPalette);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    setRemoteHits([]);
    api.listHosts().then(setHosts).catch(() => setHosts([]));
    const id = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [open]);

  // Navigation and hosts stay instant/local (small, already in memory);
  // everything else (rules, aliases, NAT, VPN, ...) is content that only the
  // backend can search, so it's fetched from GET /api/search once the query
  // is long enough to be worth a round trip, debounced so a fast typist
  // doesn't fire one request per keystroke.
  useEffect(() => {
    const q = query.trim();
    if (!open || q.length < SEARCH_MIN_LENGTH) {
      setRemoteHits([]);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timeout = setTimeout(() => {
      api
        .search(q, { signal: controller.signal })
        .then((hits) => setRemoteHits(hits.filter((hit) => hit.category !== "hosts")))
        .catch((err) => {
          if (err?.name !== "AbortError") setRemoteHits([]);
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      controller.abort();
      clearTimeout(timeout);
    };
  }, [query, open]);

  const hostItems: PaletteItem[] = useMemo(
    () =>
      hosts.map((host) => ({
        key: `host:${host.id}`,
        href: `/hosts/${host.id}`,
        label: host.name,
        hint: host.base_url.replace(/^https?:\/\//, ""),
        icon: Network,
        category: "hosts",
      })),
    [hosts]
  );

  const remoteItems: PaletteItem[] = useMemo(
    () =>
      remoteHits.map((hit) => ({
        key: `${hit.category}:${hit.id}`,
        href: hit.href,
        label: hit.title,
        hint: hit.subtitle ?? undefined,
        icon: CATEGORY_ICONS[hit.category] ?? Search,
        category: hit.category,
      })),
    [remoteHits]
  );

  // Grouped by category (navigation, then hosts, then whatever the backend
  // returned) rather than one global relevance ranking — keeps each
  // category's results together as a contiguous, clearly-headed block, with
  // relevance ranking only *within* a category (locally via matchScore,
  // server-side via the backend's own scoring).
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [...navItems, ...hostItems];
    return [...rankAndFilter(navItems, q), ...rankAndFilter(hostItems, q), ...remoteItems];
  }, [query, navItems, hostItems, remoteItems]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  function close() {
    setOpen(false);
  }

  function go(item: PaletteItem) {
    router.push(item.href);
    close();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = results[activeIndex];
      if (item) go(item);
    }
  }

  if (!open) return null;

  let previousCategory: string | null = null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 px-4 pt-[12vh]" onClick={close}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("palette.dialog")}
        className="flex max-h-[70vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-slate-100 px-4 py-3">
          {searching ? <Loader2 size={18} className="shrink-0 animate-spin text-slate-400" /> : <Search size={18} className="shrink-0 text-slate-400" />}
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={t("palette.placeholder")}
            className="w-full border-none bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
          />
          <kbd className="hidden shrink-0 rounded border border-slate-200 px-1.5 py-0.5 text-xs text-slate-400 sm:inline">Esc</kbd>
        </div>
        <div className="overflow-y-auto py-2">
          {results.length === 0 && <p className="px-4 py-6 text-center text-sm text-slate-400">{t("palette.noResults")}</p>}
          {results.map((item, index) => {
            const Icon = item.icon;
            const showHeader = item.category !== previousCategory;
            previousCategory = item.category;
            return (
              <div key={item.key}>
                {showHeader && (
                  <p className="px-4 pb-1 pt-3 text-xs font-medium uppercase tracking-wide text-slate-400 first:pt-1">
                    {t(`palette.category.${item.category}`) === `palette.category.${item.category}` ? item.category : t(`palette.category.${item.category}`)}
                  </p>
                )}
                <button
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => go(item)}
                  className={clsx(
                    "flex w-full items-center gap-3 px-4 py-2 text-left text-sm transition-colors",
                    index === activeIndex ? "bg-sky-50 text-sky-700" : "text-slate-700"
                  )}
                >
                  <Icon size={16} className="shrink-0 text-slate-400" />
                  <span className="flex-1 truncate">
                    <HighlightedText text={item.label} query={query} />
                  </span>
                  {item.hint && <span className="shrink-0 truncate text-xs text-slate-400">{item.hint}</span>}
                  {index === activeIndex && <CornerDownLeft size={14} className="shrink-0 text-sky-400" />}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
