"use client";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { useEffect, useState } from "react";
import { api, Host, NatLive } from "@/lib/api";
import { Select } from "@/components/ui/Field";
import { Button } from "@/components/ui/Button";
import { useLanguage } from "@/components/LanguageProvider";

export function LiveNat({kind}: {kind: "port-forward" | "outbound-nat" | "one-to-one"}) {
  const {t, errorMessage} = useLanguage();
  const [hosts, setHosts] = useState<Host[]>([]);
  const [hostId, setHostId] = useState("");
  const [data, setData] = useState<NatLive | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => { api.listHosts().then(h => {setHosts(h); setHostId(h[0]?.id ?? "");}).catch(e => setError(errorMessage(e))); }, [errorMessage]);
  useEffect(() => {
    let active = true;
    setData(null); setError("");
    if (hostId) (kind === "one-to-one" ? api.getLiveOneToOne(hostId) : kind === "outbound-nat" ? api.getLiveOutboundNat(hostId) : api.getLivePortForward(hostId)).then(d => {if (active) setData(d);}).catch(e => {if (active) setError(errorMessage(e));});
    return () => {active = false;};
  }, [hostId, kind, reload, errorMessage]);
  const show = (value: unknown) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
  function rows(items: Record<string, unknown>[]) {
    return <div className="space-y-2">{items.length === 0 && <p className="text-sm text-slate-500">{t("liveNat.noEntries")}</p>}{items.map((r, i) => <details key={String(r.uuid ?? i)} className="rounded-lg border border-slate-200 bg-white p-3"><summary className="cursor-pointer text-sm">{show(r.description || r.description || r.descr || r.uuid)} · {show(r["%interface"] || r.interface)}</summary><dl className="mt-3 grid grid-cols-2 gap-2 text-xs">{Object.entries(r).filter(([k]) => !k.startsWith("alias_meta")).map(([k,v]) => <div key={k} className="break-all"><dt className="text-slate-500">{k}</dt><dd>{show(v)}</dd></div>)}</dl></details>)}</div>;
  }
  return <div className="space-y-4"><div className="flex gap-2"><Select aria-label={t("liveNat.firewallSelect")} value={hostId} onChange={e => setHostId(e.target.value)}>{hosts.map(h => <option key={h.id} value={h.id}>{h.name}</option>)}</Select><InfoTip text={t("liveNat.description")} className="self-center" /><Button variant="ghost" onClick={() => setReload(n => n + 1)}>{t("common.refresh")}</Button></div>{error && <p role="alert" className="text-red-600">{error}</p>}{!data && hostId && !error && <p>{t("common.loading")}</p>}{data && <><h2 className="font-medium">{t("liveNat.configured", {count: data.rules.length})}</h2>{rows(data.rules)}<h2 className="font-medium">{t("liveNat.automatic", {count: data.automatic_rules.length})}</h2>{rows(data.automatic_rules)}</>}</div>;
}
