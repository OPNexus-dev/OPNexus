"use client";

import { Badge, BadgeTone, badgeToneClasses } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { FilterBar } from "@/components/ui/FilterBar";
import { PageHeader } from "@/components/ui/PageHeader";
import { Input, Select } from "@/components/ui/Field";
import { Spinner } from "@/components/ui/Spinner";
import {
  api,
  Host,
  LogFilterOptions,
  LogSearchEntry,
  LogSearchResult,
  LogStats,
} from "@/lib/api";
import { groupHostsByCluster } from "@/lib/clusters";
import { shortenMiddle } from "@/lib/format";
import { useLanguage } from "@/components/LanguageProvider";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Loading } from "@/components/ui/Loading";
const LIMIT = 25;

type Preset = "1h" | "24h" | "7d" | "all" | "custom";

const PRESETS: Preset[] = ["1h", "24h", "7d", "all", "custom"];
const DEFAULT_PRESET: Preset = "24h";

function presetSinceIso(preset: Preset): string | undefined {
  const now = Date.now();
  const hour = 60 * 60 * 1000;
  if (preset === "1h") return new Date(now - hour).toISOString();
  if (preset === "24h") return new Date(now - 24 * hour).toISOString();
  if (preset === "7d") return new Date(now - 7 * 24 * hour).toISOString();
  return undefined;
}

// action -> badge tone. Unknown/future action values fall back to a
// neutral slate pill rather than being hidden — see actionTone().
const ACTION_TONE: Record<string, BadgeTone> = {
  pass: "success",
  block: "danger",
  reject: "warning",
};

function actionTone(action: string): BadgeTone {
  return ACTION_TONE[action] ?? "neutral";
}

// A ranked, single-hue magnitude bar list — deliberately not a full chart
// library: one color per panel encodes "this panel's metric", bar length
// encodes magnitude relative to the panel's own max, and the exact count
// is always direct-labeled (never color/length alone). See the `dataviz`
// skill's guidance on when a stat/ranked-list, not a chart-with-axes, is
// the right form for this — the panel just answers "who's biggest".
function RankedBars({
  title,
  items,
  colorClass,
  emptyLabel,
}: {
  title: string;
  items: { value: string; count: number }[];
  colorClass: string;
  emptyLabel: string;
}) {
  const { locale } = useLanguage();
  const max = Math.max(1, ...items.map((i) => i.count));
  return (
    <div>
      <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">{title}</h2>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">{emptyLabel}</p>
      ) : (
        <ul className="space-y-1.5">
          {items.map((item) => (
            <li key={item.value} title={`${item.value}: ${item.count.toLocaleString(locale)}`} className="flex items-center gap-2 text-sm">
              <span className="w-44 shrink-0 whitespace-nowrap font-mono text-xs text-slate-600" title={item.value}>{shortenMiddle(item.value)}</span>
              <span className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                <span
                  className={`block h-full rounded-full ${colorClass}`}
                  style={{ width: `${Math.max(6, (item.count / max) * 100)}%` }}
                />
              </span>
              <span className="w-16 shrink-0 text-right tabular-nums text-slate-500">{item.count.toLocaleString(locale)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function LogsPage() {
  const { t, errorMessage, locale } = useLanguage();
  const [hosts, setHosts] = useState<Host[]>([]);
  const [selection, setSelection] = useState<string>(""); // "" (all), "host:<id>" or "cluster:<name>"
  const [search, setSearch] = useState("");
  const [action, setAction] = useState("");
  const [iface, setIface] = useState("");
  const [protocol, setProtocol] = useState("");
  const [srcIp, setSrcIp] = useState("");
  const [dstIp, setDstIp] = useState("");
  const [port, setPort] = useState("");
  const [preset, setPreset] = useState<Preset>(DEFAULT_PRESET);
  const [customSince, setCustomSince] = useState("");
  const [customUntil, setCustomUntil] = useState("");
  const [filterOptions, setFilterOptions] = useState<LogFilterOptions | null>(null);

  const [result, setResult] = useState<LogSearchResult | null>(null);
  const [stats, setStats] = useState<LogStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    api.listHosts().then(setHosts);
    api.getLogFilterOptions().then(setFilterOptions).catch(() => setFilterOptions(null));
  }, []);

  const { clusters } = groupHostsByCluster(hosts);
  const hostId = selection.startsWith("host:") ? selection.slice("host:".length) : undefined;
  const clusterName = selection.startsWith("cluster:") ? selection.slice("cluster:".length) : undefined;

  // Memoized on [preset, customSince, customUntil] only — presetSinceIso()
  // reads Date.now(), so computing it directly in the render body would
  // produce a new string on every render, which as a dependency below
  // would refetch in an infinite loop (and, worse, keep resetting the
  // search debounce's timer before it ever fires). A fixed "since" for
  // the duration of viewing a given preset is fine for this feature —
  // fast polling already makes it "quasi-live" without needing every
  // render to shift the window by a few milliseconds.
  const since = useMemo(
    () => (preset === "custom" ? (customSince ? new Date(customSince).toISOString() : undefined) : presetSinceIso(preset)),
    [preset, customSince, customUntil]
  );
  const until = useMemo(
    () => (preset === "custom" ? (customUntil ? new Date(customUntil).toISOString() : undefined) : undefined),
    [preset, customUntil]
  );

  const hostsById = useMemo(() => Object.fromEntries(hosts.map((h) => [h.id, h.name])), [hosts]);

  // Reset to page 1 whenever any filter changes (matches the audit page's
  // convention), and debounce the free-text search specifically so every
  // keystroke doesn't fire a request.
  useEffect(() => {
    setOffset(0);
  }, [selection, action, iface, protocol, preset, customSince, customUntil, srcIp, dstIp, port]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    const timer = setTimeout(() => {
      api
        .searchLogs({ host_id: hostId, cluster_name: clusterName, q: search, action, interface: iface, protocol, src: srcIp.trim(), dst: dstIp.trim(), port: /^\d+$/.test(port.trim()) ? Number(port) : "", since, until, offset, limit: LIMIT })
        .then(setResult)
        .catch((e) => setError(errorMessage(e, "logs.loadFailed")))
        .finally(() => setLoading(false));
    }, 200);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId, clusterName, search, action, iface, protocol, srcIp, dstIp, port, since, until, offset]);

  useEffect(() => {
    api
      .getLogStats({ host_id: hostId, cluster_name: clusterName, since, until })
      .then(setStats)
      .catch(() => setStats(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId, clusterName, since, until]);

  const total = result?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / LIMIT));
  const currentPage = Math.floor(offset / LIMIT);
  const entries: LogSearchEntry[] = result?.items ?? [];

  return (
    <div>
      <PageHeader
        title={t("logs.title")}
        actions={
          <div className="w-full sm:w-72">
            <Select aria-label={t("logs.hostOrCluster")} value={selection} onChange={(e) => setSelection(e.target.value)}>
              <option value="">{t("logs.allHosts")}</option>
              {Object.keys(clusters).map((name) => (
                <option key={`cluster:${name}`} value={`cluster:${name}`}>
                  {t("logs.clusterOption", { name })}
                </option>
              ))}
              {hosts.map((h) => (
                <option key={h.id} value={`host:${h.id}`}>
                  {h.name}
                </option>
              ))}
            </Select>
          </div>
        }
      />

      {/* Stats/Dashboard-Panel */}
      <Card className="mb-6 p-5">
        {!stats ? (
          <Loading>{t("logs.statsLoading")}</Loading>
        ) : (
          <div className="grid gap-6 lg:grid-cols-3">
            <div>
              <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">{t("logs.byAction")}</h2>
              <div className="flex flex-wrap gap-2">
                {Object.entries(stats.by_action).length === 0 && <p className="text-sm text-slate-400">{t("logs.noData")}</p>}
                {Object.entries(stats.by_action).map(([a, count]) => (
                  <span key={a} className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium ${badgeToneClasses[actionTone(a)]}`}>
                    {a}
                    <span className="tabular-nums">{count.toLocaleString(locale)}</span>
                  </span>
                ))}
              </div>
              <h2 className="mb-2 mt-4 text-xs font-medium uppercase tracking-wide text-slate-500">{t("logs.byInterface")}</h2>
              <div className="flex flex-wrap gap-2">
                {Object.entries(stats.by_interface).length === 0 && <p className="text-sm text-slate-400">{t("logs.noData")}</p>}
                {Object.entries(stats.by_interface).map(([i, count]) => (
                  <span key={i} className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 px-3 py-1.5 text-sm font-medium text-slate-600">
                    {i}
                    <span className="tabular-nums">{count.toLocaleString(locale)}</span>
                  </span>
                ))}
              </div>
            </div>
            <RankedBars
              title={t("logs.topBlocked")}
              items={stats.top_blocked_sources}
              colorClass="bg-red-500"
              emptyLabel={t("logs.noBlocked")}
            />
            <RankedBars
              title={t("logs.topPorts")}
              items={stats.top_destination_ports}
              colorClass="bg-sky-500"
              emptyLabel={t("logs.noData")}
            />
          </div>
        )}
      </Card>

      {/* Suche & Filter */}
      <FilterBar
        className="mb-4"
        search={{ value: search, onChange: setSearch, placeholder: t("logs.searchPlaceholder"), label: t("logs.searchLabel") }}
        filters={[
          { key: "action", label: t("logs.action"), value: action, onChange: setAction, allLabel: t("logs.allActions"), options: (filterOptions?.actions ?? []).map((a) => ({ value: a, label: a })) },
          { key: "interface", label: t("logs.interface"), value: iface, onChange: setIface, allLabel: t("logs.allInterfaces"), options: (filterOptions?.interfaces ?? []).map((i) => ({ value: i, label: i })) },
          { key: "protocol", label: t("logs.protocol"), value: protocol, onChange: setProtocol, allLabel: t("logs.allProtocols"), options: (filterOptions?.protocols ?? []).map((p) => ({ value: p, label: p })) },
          // The period always has a value; the default (24 h) plays the role of "no filter" so it is not shown as an active chip.
          { key: "period", label: t("logs.period"), value: preset === DEFAULT_PRESET ? "" : preset, onChange: (v) => setPreset((v || DEFAULT_PRESET) as Preset), allLabel: t(`logs.preset.${DEFAULT_PRESET}`), options: PRESETS.filter((p) => p !== DEFAULT_PRESET).map((p) => ({ value: p, label: t(`logs.preset.${p}`) })) },
        ]}
        onReset={() => { setSearch(""); setAction(""); setIface(""); setProtocol(""); setPreset(DEFAULT_PRESET); setSrcIp(""); setDstIp(""); setPort(""); }}
      />
      {preset === "custom" && (
        <div className="mb-4 grid gap-3 sm:grid-cols-2">
          <Input aria-label={t("logs.from")} type="datetime-local" value={customSince} onChange={(e) => setCustomSince(e.target.value)} />
          <Input aria-label={t("logs.to")} type="datetime-local" value={customUntil} onChange={(e) => setCustomUntil(e.target.value)} />
        </div>
      )}
      <details className="mb-4" open={!!(srcIp || dstIp || port)}>
        <summary className="cursor-pointer text-sm font-medium text-slate-600 [@media(pointer:coarse)]:py-2">{t("logs.addressFilters")}</summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Input aria-label={t("logs.srcFilter")} placeholder={t("logs.srcFilter")} value={srcIp} onChange={(e) => setSrcIp(e.target.value)} />
          <Input aria-label={t("logs.dstFilter")} placeholder={t("logs.dstFilter")} value={dstIp} onChange={(e) => setDstIp(e.target.value)} />
          <Input aria-label={t("logs.portFilter")} placeholder={t("logs.portFilter")} inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} />
          {(srcIp || dstIp || port) && <Button variant="ghost" onClick={() => { setSrcIp(""); setDstIp(""); setPort(""); }}>{t("logs.clearFilters")}</Button>}
        </div>
      </details>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      <Card className="overflow-hidden p-0">
        {loading ? (
          <div className="px-5 py-8 text-center text-slate-400">
            <span className="inline-flex items-center gap-2">
              <Spinner size={14} />
              {t("common.loading")}
            </span>
          </div>
        ) : (
          <DataTable
            label={t("logs.title")}
            rows={entries}
            rowKey={(entry) => entry.id}
            mobile="list"
            empty={<p className="px-5 py-8 text-center text-slate-400">{t("logs.noEntries")}</p>}
            columns={[
              { key: "time", header: t("logs.colTime"), priority: "high", tabular: true, className: "whitespace-nowrap text-slate-500", cell: (entry) => new Date(entry.timestamp).toLocaleString(locale) },
              { key: "host", header: t("logs.colHost"), priority: "medium", cell: (entry) => hostsById[entry.host_id] ?? "—" },
              { key: "action", header: t("logs.action"), priority: "primary", cell: (entry) => <Badge tone={actionTone(entry.action ?? "")}>{entry.action ?? "?"}</Badge> },
              { key: "interface", header: t("logs.interface"), priority: "medium", cell: (entry) => entry.interface ?? "—" },
              { key: "protocol", header: t("logs.protocol"), priority: "medium", cell: (entry) => entry.protocol ?? "—" },
              {
                key: "source", header: t("logs.colSource"), priority: "primary", isTitle: true, mono: true,
                cell: (entry) => (
                  <>
                    <FilterLink value={entry.source_net} onPick={setSrcIp} label={t("logs.filterBy", { value: entry.source_net ?? "" })} />
                    {entry.source_port ? <>:<FilterLink value={entry.source_port} onPick={setPort} label={t("logs.filterBy", { value: entry.source_port })} /></> : ""}
                  </>
                ),
              },
              {
                key: "destination", header: t("logs.colDestination"), priority: "high", mono: true,
                cell: (entry) => (
                  <>
                    <FilterLink value={entry.destination_net} onPick={setDstIp} label={t("logs.filterBy", { value: entry.destination_net ?? "" })} />
                    {entry.destination_port ? <>:<FilterLink value={entry.destination_port} onPick={setPort} label={t("logs.filterBy", { value: entry.destination_port })} /></> : ""}
                  </>
                ),
              },
            ]}
          />
        )}

        {!loading && total > 0 && (
          <div className="flex flex-col items-center justify-between gap-3 border-t border-slate-100 px-5 py-3 sm:flex-row">
            <span className="text-xs text-slate-500">
              {t("logs.range", { from: (offset + 1).toLocaleString(locale), to: Math.min(total, offset + LIMIT).toLocaleString(locale), total: total.toLocaleString(locale) })}
            </span>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={() => setOffset((o) => Math.max(0, o - LIMIT))} disabled={currentPage === 0}>
                <ChevronLeft size={14} />
                {t("logs.back")}
              </Button>
              <span className="text-xs text-slate-500">
                {t("logs.page", { page: (currentPage + 1).toLocaleString(locale), pages: pageCount.toLocaleString(locale) })}
              </span>
              <Button
                variant="ghost"
                onClick={() => setOffset((o) => o + LIMIT)}
                disabled={currentPage >= pageCount - 1}
              >
                {t("logs.next")}
                <ChevronRight size={14} />
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

/** An address or port in the table: a click filters the list by it. */
function FilterLink({ value, onPick, label }: { value: string | null; onPick: (value: string) => void; label: string }) {
  if (!value) return <>—</>;
  return (
    <button type="button" data-inline-target className="rounded px-0.5 text-left hover:bg-slate-100 hover:underline focus-visible:outline focus-visible:outline-2" title={label} aria-label={label} onClick={() => onPick(value)}>
      {value}
    </button>
  );
}
