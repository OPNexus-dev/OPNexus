"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { RuleAnalysisCard } from "@/components/RuleAnalysisCard";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input, Label, Select } from "@/components/ui/Field";
import { Loading } from "@/components/ui/Loading";
import { InfoTip } from "@/components/ui/Tooltip";
import { AnalysisResult, api, DeviceGroup, Host, InterfaceOption } from "@/lib/api";
import { Network, Search } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useEffect, useMemo, useRef, useState } from "react";

type Scope = "all" | "hosts" | "group";
interface FormState {
  scope: Scope; hostIds: string[]; groupId: string; src: string; dst: string; proto: string;
  dport: string; sport: string; icmptype: string; iface: string; egress: boolean; egressIface: string;
}

const PROTOCOLS = ["tcp", "udp", "icmp", "ipv6-icmp", "gre", "esp", "ah", "ospf", "vrrp", "carp"];

function fromParams(params: URLSearchParams): FormState {
  const hosts = (params.get("hosts") ?? "").split(",").filter(Boolean);
  const group = params.get("group") ?? "";
  const egress = params.get("egress") ?? "";
  return {
    scope: group ? "group" : hosts.length ? "hosts" : "all", hostIds: hosts, groupId: group,
    src: params.get("src") ?? "", dst: params.get("dst") ?? "", proto: params.get("proto") ?? "tcp",
    dport: params.get("dport") ?? "", sport: params.get("sport") ?? "", icmptype: params.get("icmptype") ?? "",
    iface: params.get("iface") ?? "auto", egress: egress !== "" && egress !== "none", egressIface: egress && egress !== "none" ? egress : "auto",
  };
}

function toParams(form: FormState): URLSearchParams {
  const q = new URLSearchParams();
  q.set("src", form.src.trim());
  q.set("dst", form.dst.trim());
  q.set("proto", (form.proto || "tcp").trim().toLowerCase());
  const proto = (form.proto || "tcp").trim().toLowerCase();
  const ported = ["tcp", "udp", "6", "17"].includes(proto);
  const icmp = ["icmp", "ipv6-icmp", "1", "58"].includes(proto);
  if (ported && form.dport.trim()) q.set("dport", form.dport.trim());
  if (ported && form.sport.trim()) q.set("sport", form.sport.trim());
  if (icmp && form.icmptype.trim()) q.set("icmptype", form.icmptype.trim());
  if (form.iface && form.iface !== "auto") q.set("iface", form.iface);
  if (form.egress) q.set("egress", form.egressIface || "auto");
  if (form.scope === "group" && form.groupId) q.set("group", form.groupId);
  if (form.scope === "hosts" && form.hostIds.length) q.set("hosts", form.hostIds.join(","));
  return q;
}

export default function AnalysisPage() {
  const { t } = useLanguage();
  return <Suspense fallback={<Loading>{t("common.loading")}</Loading>}><AnalysisContent /></Suspense>;
}

function AnalysisContent() {
  const { t, errorMessage } = useLanguage();
  const router = useRouter();
  const params = useSearchParams();
  const key = params.toString();
  const [form, setForm] = useState<FormState>(() => fromParams(params));
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [interfaces, setInterfaces] = useState<InterfaceOption[]>([]);
  const [results, setResults] = useState<AnalysisResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refreshNext = useRef(false);
  const counter = useRef(0);

  useEffect(() => {
    api.listHosts().then(setHosts).catch((e) => { setHosts([]); setError(errorMessage(e, "analysis.loadFailed")); });
    api.listGroups().then(setGroups).catch(() => setGroups([]));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the form in step with the address bar (back/forward, shared links).
  useEffect(() => { setForm(fromParams(new URLSearchParams(key))); }, [key]);

  const enabledHosts = useMemo(() => (hosts ?? []).filter((h) => h.enabled), [hosts]);
  const scopedHosts = useMemo(() => {
    if (form.scope === "hosts") return enabledHosts.filter((h) => form.hostIds.includes(h.id));
    if (form.scope === "group") { const g = groups.find((x) => x.id === form.groupId); return enabledHosts.filter((h) => g?.host_ids.includes(h.id)); }
    return enabledHosts;
  }, [enabledHosts, form.scope, form.hostIds, form.groupId, groups]);

  // Interface choices: the union over the firewalls in scope (names differ per firewall; an unknown one is rejected per host).
  const scopeKey = scopedHosts.map((h) => h.id).join(",");
  useEffect(() => {
    let active = true;
    Promise.allSettled(scopedHosts.map((h) => api.getHostInterfaces(h.id))).then((all) => {
      if (!active) return;
      const merged = new Map<string, InterfaceOption>();
      for (const item of all) if (item.status === "fulfilled") for (const o of item.value) if (!merged.has(o.name)) merged.set(o.name, o);
      setInterfaces([...merged.values()]);
    });
    return () => { active = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey]);

  // Run whenever the URL carries a query (initial load, submit, shared link).
  useEffect(() => {
    const p = new URLSearchParams(key);
    const src = p.get("src"); const dst = p.get("dst");
    if (!src || !dst) { setResults(null); setError(null); return; }
    const mine = ++counter.current;
    const refresh = refreshNext.current;
    refreshNext.current = false;
    setRunning(true);
    setError(null);
    api.analyzeRule({
      src, dst, proto: p.get("proto") ?? undefined, dport: p.get("dport") ?? undefined, sport: p.get("sport") ?? undefined,
      icmptype: p.get("icmptype") ?? undefined, iface: p.get("iface") ?? undefined, egress: p.get("egress") ?? undefined,
      host_ids: p.get("hosts") ?? undefined, group_id: p.get("group") ?? undefined, refresh,
    })
      .then((list) => { if (mine === counter.current) setResults(list); })
      .catch((e) => { if (mine === counter.current) { setResults(null); setError(errorMessage(e, "analysis.queryFailed")); } })
      .finally(() => { if (mine === counter.current) setRunning(false); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);

  function submit(event?: FormEvent, refresh = false) {
    event?.preventDefault();
    if (!form.src.trim() || !form.dst.trim()) { setFormError(t("analysis.form.missing")); return; }
    if (form.scope === "hosts" && form.hostIds.length === 0) { setFormError(t("analysis.form.noHostsSelected")); return; }
    if (form.scope === "group" && !form.groupId) { setFormError(t("analysis.form.noGroupSelected")); return; }
    setFormError(null);
    const next = toParams(form).toString();
    refreshNext.current = refresh;
    if (next === key) setTick((n) => n + 1);
    else router.push(`/analysis?${next}`);
  }

  const set = <K extends keyof FormState>(field: K, value: FormState[K]) => setForm((f) => ({ ...f, [field]: value }));
  const proto = (form.proto || "tcp").trim().toLowerCase();
  const ported = ["tcp", "udp", "6", "17"].includes(proto);
  const icmp = ["icmp", "ipv6-icmp", "1", "58"].includes(proto);
  const ifaceChoices = (current: string) => (current && current !== "auto" && !interfaces.some((o) => o.name === current) ? [...interfaces, { name: current, label: current }] : interfaces);

  return (
    <div>
      <h1 className="mb-6 flex items-center gap-2 text-2xl font-semibold text-slate-900">
        {t("analysis.title")}
        <InfoTip text={t("analysis.intro")} size={18} />
      </h1>

      <Card className="mb-6 p-4 sm:p-5">
        <form onSubmit={submit} aria-label={t("analysis.form.title")} className="flex flex-col gap-4" noValidate>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <FieldGroup label={t("analysis.form.scope")}>
              <Select value={form.scope} onChange={(e) => set("scope", e.target.value as Scope)}>
                <option value="all">{t("analysis.form.scopeAll")}</option>
                <option value="hosts">{t("analysis.form.scopeHosts")}</option>
                <option value="group">{t("analysis.form.scopeGroup")}</option>
              </Select>
            </FieldGroup>
            {form.scope === "group" && (
              <FieldGroup label={t("analysis.form.group")}>
                <Select value={form.groupId} onChange={(e) => set("groupId", e.target.value)}>
                  <option value="">-</option>
                  {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </Select>
              </FieldGroup>
            )}
            <FieldGroup label={t("analysis.form.src")} hint={t("analysis.form.srcHint")}>
              <Input value={form.src} onChange={(e) => set("src", e.target.value)} inputMode="text" autoComplete="off" spellCheck={false} placeholder="198.51.100.7" className="font-mono" />
            </FieldGroup>
            <FieldGroup label={t("analysis.form.dst")}>
              <Input value={form.dst} onChange={(e) => set("dst", e.target.value)} autoComplete="off" spellCheck={false} placeholder="203.0.113.9" className="font-mono" />
            </FieldGroup>
            <FieldGroup label={t("analysis.form.proto")} hint={t("analysis.form.protoHint")}>
              <Input value={form.proto} onChange={(e) => set("proto", e.target.value)} list="analysis-protocols" autoComplete="off" spellCheck={false} />
              <datalist id="analysis-protocols">{PROTOCOLS.map((p) => <option key={p} value={p} />)}</datalist>
            </FieldGroup>
            {ported && (
              <>
                <FieldGroup label={t("analysis.form.dport")} hint={t("analysis.form.dportHint")}>
                  <Input value={form.dport} onChange={(e) => set("dport", e.target.value)} inputMode="numeric" autoComplete="off" placeholder="443" />
                </FieldGroup>
                <FieldGroup label={t("analysis.form.sport")} hint={t("analysis.form.sportHint")}>
                  <Input value={form.sport} onChange={(e) => set("sport", e.target.value)} inputMode="numeric" autoComplete="off" />
                </FieldGroup>
              </>
            )}
            {icmp && (
              <FieldGroup label={t("analysis.form.icmptype")} hint={t("analysis.form.icmptypeHint")}>
                <Input value={form.icmptype} onChange={(e) => set("icmptype", e.target.value)} autoComplete="off" placeholder="echoreq" />
              </FieldGroup>
            )}
            <FieldGroup label={t("analysis.form.iface")} hint={t("analysis.form.ifaceHint")}>
              <Select value={form.iface} onChange={(e) => set("iface", e.target.value)}>
                <option value="auto">{t("analysis.form.ifaceAuto")}</option>
                {ifaceChoices(form.iface).map((o) => <option key={o.name} value={o.name}>{o.label || o.name}</option>)}
              </Select>
            </FieldGroup>
          </div>

          {form.scope === "hosts" && (
            <fieldset className="min-w-0">
              <legend className="mb-1 text-sm font-medium text-slate-700">{t("analysis.form.hosts")}</legend>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                {enabledHosts.map((h) => (
                  <label key={h.id} className="inline-flex items-center gap-2 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      checked={form.hostIds.includes(h.id)}
                      onChange={(e) => set("hostIds", e.target.checked ? [...form.hostIds, h.id] : form.hostIds.filter((id) => id !== h.id))}
                    />
                    {h.name}
                  </label>
                ))}
              </div>
            </fieldset>
          )}

          <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
            <label className="inline-flex min-h-[2.5rem] items-center gap-2 text-sm font-medium text-slate-700">
              <input type="checkbox" checked={form.egress} onChange={(e) => set("egress", e.target.checked)} />
              {t("analysis.form.egress")}
              <InfoTip text={t("analysis.form.egressHint")} size={13} />
            </label>
            {form.egress && (
              <div className="w-full sm:w-56">
                <Label htmlFor="analysis-egress-iface">{t("analysis.form.egressIface")}</Label>
                <Select id="analysis-egress-iface" aria-label={t("analysis.form.egressIface")} value={form.egressIface} onChange={(e) => set("egressIface", e.target.value)}>
                  <option value="auto">{t("analysis.form.ifaceAuto")}</option>
                  {ifaceChoices(form.egressIface).map((o) => <option key={o.name} value={o.name}>{o.label || o.name}</option>)}
                </Select>
              </div>
            )}
            <div className="ml-auto flex flex-wrap gap-2">
              <Button type="button" variant="outline" onClick={() => submit(undefined, true)} disabled={running} title={t("analysis.form.refreshHint")}>
                {t("analysis.form.refresh")}
              </Button>
              <Button type="submit" loading={running}>
                <Search size={16} aria-hidden="true" />
                {t("analysis.form.run")}
              </Button>
            </div>
          </div>
          {formError && <p role="alert" className="text-sm text-red-600">{formError}</p>}
        </form>
      </Card>

      {error && <p role="alert" className="mb-4 text-sm text-red-600">{error}</p>}
      {running && results === null && <Loading>{t("analysis.form.run")}…</Loading>}
      {results === null && !running && !error && <EmptyState icon={Network} title={t("analysis.empty.title")} description={t("analysis.empty.description")} />}
      {results && results.length === 0 && <EmptyState icon={Network} title={t("analysis.noHosts.title")} description={t("analysis.noHosts.description")} />}
      {results && results.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-2" aria-busy={running} role="region" aria-label={t("analysis.title")}>
          {results.map((result) => <RuleAnalysisCard key={result.host_id} result={result} />)}
        </div>
      )}
    </div>
  );
}
