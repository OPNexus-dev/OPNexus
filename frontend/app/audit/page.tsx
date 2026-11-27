"use client";
import { useEffect, useState } from "react";
import { ScrollText } from "lucide-react";
import { api, AuditEvent, AuditOperation, AuditOperationPage } from "@/lib/api";
import { auditTone, sinceFor } from "@/lib/audit";
import { AuditOperationDetail } from "@/components/AuditOperationDetail";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Column, DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { FilterBar } from "@/components/ui/FilterBar";
import { PageHeader } from "@/components/ui/PageHeader";
import { useLanguage } from "@/components/LanguageProvider";

const kindKeys = ["rule","rule_cleanup","alias","port_forward","one_to_one","outbound_nat","host","user","auth","backup","ipsec_tunnel","openvpn_instance","wireguard_tunnel","certificate","acme","notification","gui_access","system"];
// "requested" and "accepted" are the HTTP steps every operation has; as a filter they would match everything.
const statusKeys = ["success","pending","error","reverted","revert_failed","removed","rejected","denied","interrupted","submitted","acknowledged","starting","unknown"];
const PAGE = 50;

export default function AuditHistory() {
  const { t, ts, errorMessage, locale } = useLanguage();
  const lbl = (ns: string, key: string) => { const k = `audit.${ns}.${key}`; const v = t(k); return v === k ? key : v; };
  const [data, setData] = useState<AuditOperationPage | null>(null);
  const [kind, setKind] = useState(""); const [status, setStatus] = useState(""); const [range, setRange] = useState(""); const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0); const [error, setError] = useState(""); const [reload, setReload] = useState(0); const [exporting, setExporting] = useState(false);
  const [before, setBefore] = useState(() => new Date().toISOString());

  useEffect(() => {
    let active = true; setData(null); setError("");
    const timer = setTimeout(() => {
      api.auditOperations({ kind, status, q: search, since: sinceFor(range), offset, limit: PAGE, before })
        .then((d) => { if (active) setData(d); })
        .catch((e) => { if (active) setError(errorMessage(e)); });
    }, 200);
    return () => { active = false; clearTimeout(timer); };
  }, [kind, status, range, search, offset, reload, before, errorMessage]);

  // The export keeps every step (the raw events), with the same filters; the list above is the summary.
  async function download() {
    setExporting(true); setError("");
    try {
      const all: AuditEvent[] = []; let cursor = 0;
      while (true) {
        const d = await api.audit({ kind, status, q: search, since: sinceFor(range), offset: cursor, limit: 200, before });
        all.push(...d.items); cursor += d.items.length;
        if (cursor >= d.total || !d.items.length) break;
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify({ exported_at: new Date().toISOString(), through: before, events: all }, null, 2)], { type: "application/json" }));
      const a = document.createElement("a"); a.href = url; a.download = "opnexus-audit.json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(errorMessage(e, "audit.exportFailed")); } finally { setExporting(false); }
  }

  const filterChange = (set: (v: string) => void) => (v: string) => { set(v); setOffset(0); };
  const resetFilters = () => { setKind(""); setStatus(""); setRange(""); setSearch(""); setOffset(0); };
  const hostsText = (op: AuditOperation) => {
    const names = op.hosts.map((h) => h.name).filter(Boolean) as string[];
    return names.length === 0 ? "—" : names.length === 1 ? names[0] : `${names[0]} ${t("audit.moreHosts", { count: names.length - 1 })}`;
  };

  const columns: Column<AuditOperation>[] = [
    {
      key: "operation", header: t("audit.col.operation"), priority: "primary",
      cell: (op) => (
        <div className="min-w-0">
          <span className="font-medium text-slate-900">{lbl("action", op.action)} · {op.entity_name || lbl("kind", op.kind)}{op.entities > 1 && <span className="font-normal text-slate-500"> {t("audit.moreEntities", { count: op.entities - 1 })}</span>}</span>
          {(op.comment || op.message) && <span className="block break-words text-xs font-normal text-slate-500">{op.comment ? `“${op.comment}”` : ts(op.message!)}</span>}
        </div>
      ),
    },
    { key: "result", header: t("audit.col.result"), priority: "primary", cell: (op) => <Badge tone={auditTone(op.result)}>{lbl("status", op.result)}</Badge> },
    { key: "time", header: t("audit.col.time"), priority: "high", tabular: true, className: "whitespace-nowrap", cell: (op) => new Date(op.started_at).toLocaleString(locale) },
    { key: "host", header: t("audit.col.host"), priority: "high", cell: (op) => hostsText(op) },
    { key: "actor", header: t("audit.col.user"), priority: "medium", cell: (op) => op.actor },
    { key: "steps", header: t("audit.col.steps"), priority: "low", tabular: true, align: "right", cell: (op) => op.steps },
  ];

  return (
    <div>
      <PageHeader
        title={t("audit.title")}
        info={t("audit.intro")}
        actions={<>
          <Button variant="outline" onClick={() => { setBefore(new Date().toISOString()); setOffset(0); setReload((n) => n + 1); }}>{t("audit.refresh")}</Button>
          <Button disabled={exporting} onClick={download}>{exporting ? t("audit.exporting") : t("audit.exportJson")}</Button>
        </>}
      />
      <FilterBar
        className="mb-4"
        search={{ value: search, onChange: filterChange(setSearch), placeholder: t("audit.searchPlaceholder"), label: t("audit.searchLabel") }}
        filters={[
          { key: "kind", label: t("audit.typeLabel"), value: kind, onChange: filterChange(setKind), allLabel: t("audit.allTypes"), options: kindKeys.map((k) => ({ value: k, label: lbl("kind", k) })) },
          { key: "status", label: t("audit.resultLabel"), value: status, onChange: filterChange(setStatus), allLabel: t("audit.allResults"), options: statusKeys.map((k) => ({ value: k, label: lbl("status", k) })) },
          { key: "range", label: t("audit.rangeLabel"), value: range, onChange: filterChange(setRange), allLabel: t("audit.allTime"), options: [{ value: "24h", label: t("audit.range24h") }, { value: "7d", label: t("audit.range7d") }, { value: "30d", label: t("audit.range30d") }] },
        ]}
        onReset={resetFilters}
      />
      {error && <p role="alert" className="mb-3 text-red-600">{error}</p>}
      {!data && !error && <p className="text-slate-500">{t("common.loading")}</p>}
      {data && (
        <>
          <p className="mb-2 text-sm text-slate-500">{t("audit.events", { total: data.total, from: data.total ? offset + 1 : 0, to: Math.min(offset + PAGE, data.total) })}</p>
          <DataTable
            label={t("audit.title")}
            rows={data.items}
            rowKey={(op) => op.correlation_id}
            columns={columns}
            mobile="list"
            detail={{ render: (op) => <AuditOperationDetail operation={op} /> }}
            empty={<EmptyState icon={ScrollText} title={t("audit.noEvents")} />}
          />
          <div className="mt-4 flex gap-2">
            <Button variant="ghost" disabled={!offset} onClick={() => setOffset((n) => Math.max(0, n - PAGE))}>{t("logs.back")}</Button>
            <Button variant="ghost" disabled={offset + PAGE >= data.total} onClick={() => setOffset((n) => n + PAGE)}>{t("logs.next")}</Button>
          </div>
        </>
      )}
    </div>
  );
}
