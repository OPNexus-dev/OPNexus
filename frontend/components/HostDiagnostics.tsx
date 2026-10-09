"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Table, TBody, TD, TH, THead, TRow } from "@/components/ui/Table";
import { Tabs } from "@/components/ui/Tabs";
import { api, DnsLookupResult, PingResult, PortprobeResult, TracerouteResult } from "@/lib/api";
import { useState } from "react";

type Tool = "ping" | "traceroute" | "portprobe" | "dns";
type Result = { tool: "ping"; data: PingResult } | { tool: "traceroute"; data: TracerouteResult } | { tool: "portprobe"; data: PortprobeResult } | { tool: "dns"; data: DnsLookupResult };

/**
 * Network probes run FROM the selected firewall. Results are only shown, never stored; every run is
 * recorded in the audit log by the backend (target and tool).
 */
export function HostDiagnostics({ hostId, readOnly }: { hostId: string; readOnly: boolean }) {
  const { t, errorMessage } = useLanguage();
  const [tool, setTool] = useState<Tool>("ping");
  const [target, setTarget] = useState("");
  const [ipproto, setIpproto] = useState("inet");
  const [source, setSource] = useState("");
  const [count, setCount] = useState("4");
  const [packetsize, setPacketsize] = useState("");
  const [protocol, setProtocol] = useState("icmp");
  const [port, setPort] = useState("443");
  const [server, setServer] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const src = source.trim() || undefined;
      if (tool === "ping")
        setResult({ tool, data: await api.runPing(hostId, { target: target.trim(), ipproto, count: Number(count), packetsize: packetsize ? Number(packetsize) : null, source: src }) });
      else if (tool === "traceroute") setResult({ tool, data: await api.runTraceroute(hostId, { target: target.trim(), ipproto, protocol, source: src }) });
      else if (tool === "portprobe") setResult({ tool, data: await api.runPortprobe(hostId, { target: target.trim(), port: Number(port), ipproto, source: src }) });
      else setResult({ tool, data: await api.runDnsLookup(hostId, { name: target.trim(), server: server.trim() || undefined }) });
    } catch (err) {
      setError(errorMessage(err, "hostsystem.diag.failed"));
    } finally {
      setRunning(false);
    }
  }

  const tabs = (["ping", "traceroute", "portprobe", "dns"] as Tool[]).map((k) => ({ key: k, label: t(`hostsystem.diag.tools.${k}`) }));

  return (
    <div>
      <Tabs items={tabs} active={tool} onChange={(k) => { setTool(k as Tool); setResult(null); setError(null); }} className="overflow-x-auto px-3 [&>button]:whitespace-nowrap" />
      <form onSubmit={run} className="space-y-4 p-5">
        <fieldset disabled={readOnly} className="contents">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <FieldGroup label={tool === "dns" ? t("hostsystem.diag.name") : t("hostsystem.diag.target")}>
              <Input required value={target} onChange={(e) => setTarget(e.target.value)} autoComplete="off" placeholder={tool === "dns" ? "example.com" : "192.0.2.1"} />
            </FieldGroup>
            {tool === "portprobe" && (
              <FieldGroup label={t("hostsystem.diag.port")}>
                <Input required type="number" min={1} max={65535} value={port} onChange={(e) => setPort(e.target.value)} />
              </FieldGroup>
            )}
            {tool === "ping" && (
              <FieldGroup label={t("hostsystem.diag.count")}>
                <Input required type="number" min={1} max={10} value={count} onChange={(e) => setCount(e.target.value)} />
              </FieldGroup>
            )}
            {tool === "traceroute" && (
              <FieldGroup label={t("hostsystem.diag.protocol")}>
                <Select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
                  <option value="icmp">ICMP</option>
                  <option value="udp">UDP</option>
                </Select>
              </FieldGroup>
            )}
            {tool === "dns" ? (
              <FieldGroup label={t("hostsystem.diag.server")} hint={t("hostsystem.diag.serverHint")}>
                <Input value={server} onChange={(e) => setServer(e.target.value)} autoComplete="off" />
              </FieldGroup>
            ) : (
              <>
                <FieldGroup label={t("hostsystem.diag.ipproto")}>
                  <Select value={ipproto} onChange={(e) => setIpproto(e.target.value)}>
                    <option value="inet">IPv4</option>
                    <option value="inet6">IPv6</option>
                  </Select>
                </FieldGroup>
                <FieldGroup label={t("hostsystem.diag.source")} hint={t("hostsystem.diag.sourceHint")}>
                  <Input value={source} onChange={(e) => setSource(e.target.value)} autoComplete="off" />
                </FieldGroup>
              </>
            )}
            {tool === "ping" && (
              <FieldGroup label={t("hostsystem.diag.packetsize")}>
                <Input type="number" min={1} max={9000} value={packetsize} onChange={(e) => setPacketsize(e.target.value)} />
              </FieldGroup>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={running || !target.trim()} loading={running}>
              {running ? t("hostsystem.diag.running") : t("hostsystem.diag.run")}
            </Button>
            <span className="text-xs text-slate-500">{t("hostsystem.diag.auditNote")}</span>
          </div>
        </fieldset>
      </form>
      {error && <p role="alert" className="px-5 pb-4 text-sm text-red-600">{error}</p>}
      {result && (
        <div className="border-t border-slate-200 px-5 py-4" aria-live="polite">
          {result.tool === "ping" && <PingView r={result.data} />}
          {result.tool === "traceroute" && <TracerouteView r={result.data} />}
          {result.tool === "portprobe" && <PortView r={result.data} />}
          {result.tool === "dns" && <DnsView r={result.data} />}
        </div>
      )}
    </div>
  );
}

function PingView({ r }: { r: PingResult }) {
  const { t } = useLanguage();
  const ok = r.received > 0;
  const ms = (v: number | null) => (v === null || v === undefined ? "–" : `${v} ms`);
  return (
    <div className="space-y-2 text-sm">
      <Badge tone={ok ? "success" : "danger"}>{ok ? t("hostsystem.diag.reachable") : t("hostsystem.diag.unreachable")}</Badge>
      <p>{t("hostsystem.diag.pingSummary", { sent: r.sent, received: r.received, loss: r.loss })}</p>
      <p className="font-mono text-xs text-slate-600">min {ms(r.min)} · avg {ms(r.avg)} · max {ms(r.max)} · σ {ms(r.stddev)}</p>
      {r.error && <p className="text-amber-700">{r.error}</p>}
    </div>
  );
}

function TracerouteView({ r }: { r: TracerouteResult }) {
  const { t } = useLanguage();
  return (
    <div className="space-y-2">
      <p className="font-mono text-xs text-slate-600">{r.notice}</p>
      {r.hops.length === 0 ? <p className="text-sm text-slate-500">{t("network.empty")}</p> : (
        <Table>
          <THead><TRow><TH>{t("hostsystem.diag.hop")}</TH><TH>{t("network.col.address")}</TH><TH>{t("hostsystem.diag.hostname")}</TH><TH>AS</TH><TH>{t("hostsystem.diag.time")}</TH></TRow></THead>
          <TBody>
            {r.hops.map((h) => (
              <TRow key={h.ttl + h.address}><TD>{h.ttl}</TD><TD className="font-mono text-xs">{h.address || "*"}</TD><TD className="font-mono text-xs">{h.host}</TD><TD>{h.as}</TD><TD className="font-mono text-xs">{h.probes}</TD></TRow>
            ))}
          </TBody>
        </Table>
      )}
    </div>
  );
}

function PortView({ r }: { r: PortprobeResult }) {
  const { t } = useLanguage();
  return (
    <div className="space-y-2 text-sm">
      <Badge tone={r.open ? "success" : "danger"}>{r.open ? t("hostsystem.diag.open", { port: r.port }) : t("hostsystem.diag.closed", { port: r.port })}</Badge>
      <p className="font-mono text-xs text-slate-600">{r.message}</p>
    </div>
  );
}

function DnsView({ r }: { r: DnsLookupResult }) {
  const { t } = useLanguage();
  const types = Object.keys(r.records);
  if (types.length === 0) return <p className="text-sm text-slate-500">{t("hostsystem.diag.noRecords", { name: r.name })}</p>;
  return (
    <div className="space-y-3">
      {types.map((type) => (
        <div key={type}>
          <p className="text-sm font-medium">{type} <span className="font-normal text-slate-500">· {r.records[type].server} · {r.records[type].query_time}</span></p>
          <ul className="font-mono text-xs text-slate-700">
            {r.records[type].answers.map((a, i) => (<li key={i}>{a.replace(/\t/g, "  ")}</li>))}
          </ul>
        </div>
      ))}
    </div>
  );
}
