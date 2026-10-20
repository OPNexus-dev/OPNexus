"use client";

import { Tooltip, InfoTip } from "@/components/ui/Tooltip";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { WorkflowBadge, workflowStates } from "@/components/WorkflowBadge";
import { useLanguage } from "@/components/LanguageProvider";
import { OneToOneReview } from "@/components/OneToOneReview";
import { AdoptObjectsModal } from "@/components/AdoptObjectsModal";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { LiveNat } from "@/components/LiveNat";
import {
  api,
  AliasOption,
  DeploymentResult,
  DeviceGroup,
  Host,
  InterfaceOption,
  OneToOne,
  OneToOneCreate,
  PolicyHierarchyGroup,
} from "@/lib/api";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Plus,
  Pencil,
  History,
  Copy,
  Rocket,
  Network,
  RotateCcw,
  ShieldCheck,
  Trash2,
  XCircle,
  Download,
} from "lucide-react";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useReadOnly } from "@/lib/useReadOnly";

import { Loading } from "@/components/ui/Loading";
interface ComboOption {
  value: string;
  label: string;
}

// OPNsense's port fields only ever accept a raw number/range or an alias
// name — there's no live API exposing well-known service names the way
// interfaces/protocols/aliases are exposed, so suggesting these by name
// while always inserting the numeric port keeps every suggestion valid
// regardless of what the target firewall's own /etc/services contains.
const WELL_KNOWN_PORTS: ComboOption[] = [
  { value: "80", label: "http" },
  { value: "443", label: "https" },
  { value: "22", label: "ssh" },
  { value: "21", label: "ftp" },
  { value: "20", label: "ftp-data" },
  { value: "23", label: "telnet" },
  { value: "25", label: "smtp" },
  { value: "465", label: "smtps" },
  { value: "587", label: "submission (smtp)" },
  { value: "110", label: "pop3" },
  { value: "995", label: "pop3s" },
  { value: "143", label: "imap" },
  { value: "993", label: "imaps" },
  { value: "53", label: "dns" },
  { value: "123", label: "ntp" },
  { value: "161", label: "snmp" },
  { value: "162", label: "snmptrap" },
  { value: "389", label: "ldap" },
  { value: "636", label: "ldaps" },
  { value: "3306", label: "mysql" },
  { value: "5432", label: "postgresql" },
  { value: "3389", label: "rdp" },
  { value: "445", label: "smb / microsoft-ds" },
  { value: "139", label: "netbios-ssn" },
  { value: "5060", label: "sip" },
  { value: "514", label: "syslog" },
  { value: "69", label: "tftp" },
  { value: "8080", label: "http-alt" },
  { value: "8443", label: "https-alt" },
  { value: "2049", label: "nfs" },
  { value: "6379", label: "redis" },
  { value: "27017", label: "mongodb" },
  { value: "9200", label: "elasticsearch" },
];

const emptyForm: OneToOneCreate = {
  description: "",
  interface: "wan",
  external: "",
  type: "binat",
  log: false,
  natreflection: "",
  source_net: "any",
  destination_net: "any",
  enabled: true,
  source_not: false,
  destination_not: false,
  target_host_ids: [],
  target_group_ids: [],
  policy_hierarchy_group_id: null,
};

export default function OneToOnePanel() {
  const { t } = useLanguage();
  return <Suspense fallback={<Loading>{t("rules.loading")}</Loading>}><OneToOneContent /></Suspense>;
}

function OneToOneContent() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const [showAdopt, setShowAdopt] = useState(false);
  const toast = useToast();
  const router = useRouter();
  const params = useSearchParams();
  const groupFilter = params.get("group") ?? "";
  const ruleFilter = params.get("rule") ?? "";
  const [stateFilter, setStateFilter] = useState("");
  const autoOpened = useRef("");
  const [rules, setRules] = useState<OneToOne[] | null>(null);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<OneToOneCreate>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState<OneToOne | null>(null);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [hierarchyGroups, setHierarchyGroups] = useState<PolicyHierarchyGroup[]>([]);
  const [review, setReview] = useState<{ rule: OneToOne; mode: "preview" | "history" } | null>(null);
  const [search, setSearch] = useState("");
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [activeTab, setActiveTab] = useState<"managed" | "live">("managed");
  const [interfaceOptions, setInterfaceOptions] = useState<InterfaceOption[]>([]);
  const [interfacesError, setInterfacesError] = useState<string | null>(null);
  const [manualInterface, setManualInterface] = useState(false);
  const [aliasOptions, setAliasOptions] = useState<AliasOption[]>([]);
  const [protocolOptions, setProtocolOptions] = useState<InterfaceOption[]>([]);
  const protocolFallback: InterfaceOption[] = [
    { name: "any", label: "any" },
    { name: "tcp", label: "TCP" },
    { name: "udp", label: "UDP" },
    { name: "icmp", label: "ICMP" },
  ];

  useEffect(() => {
    setActiveTab(params.get("tab") === "live" ? "live" : "managed");
  }, [params]);

  function setGroupFilter(id: string) {
    setSearch(""); setStateFilter("");
    const query = new URLSearchParams();
    if (id) query.set("group", id);
    router.push(`/nat${query.size ? `?${query.toString()}` : ""}`);
  }

  useEffect(() => {
    const key = params.toString();
    if (rules !== null && params.get("new") === "1" && key !== autoOpened.current && (!groupFilter || groups.some(g => g.id === groupFilter))) {
      autoOpened.current = key;
      openEditor(null);
      const query = new URLSearchParams(params.toString());
      query.delete("new");
      router.replace(`/nat${query.size ? `?${query.toString()}` : ""}`, { scroll: false });
    }
  }, [params, rules, groups, groupFilter]);

  const hostsById = useMemo(() => new Map(hosts.map((h) => [h.id, h])), [hosts]);

  async function load() {
    try {
      const [ruleList, hostList, groupList, hierarchyGroupList] = await Promise.all([api.listOneToOnes().then(rows => { setRules(rows); return rows; }), api.listHosts(), api.listGroups(), api.listPolicyHierarchyGroups()]);
      setGroups(groupList);
      setHierarchyGroups(hierarchyGroupList);
      setRules(ruleList);
      setHosts(hostList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "rules.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  // Interface dropdown options, merged live across every enabled host so
  // rules that target more than one host still get a sensible list (in an
  // HA pair the interface names are normally identical anyway). Keyed off
  // the host-id set rather than re-run on every load(), since load() also
  // polls every 5s while a deployment is pending and this hits each
  // firewall's own API.
  const enabledHostIdsKey = useMemo(
    () => hosts.filter((h) => h.enabled).map((h) => h.id).sort().join(","),
    [hosts]
  );
  useEffect(() => {
    if (!enabledHostIdsKey) {
      setInterfaceOptions([]);
      setInterfacesError(null);
      setAliasOptions([]);
      setProtocolOptions([]);
      return;
    }
    let cancelled = false;
    (async () => {
      const ids = enabledHostIdsKey.split(",");
      const [interfaceResults, aliasResults, protocolResults] = await Promise.all([
        Promise.allSettled(ids.map((id) => api.getHostInterfaces(id))),
        Promise.allSettled(ids.map((id) => api.getHostAliases(id))),
        Promise.allSettled(ids.map((id) => api.getHostProtocols(id))),
      ]);
      if (cancelled) return;

      const merged = new Map<string, string>();
      let failures = 0;
      for (const r of interfaceResults) {
        if (r.status === "fulfilled") {
          for (const opt of r.value) {
            const existing = merged.get(opt.name);
            if (!existing) merged.set(opt.name, opt.label);
            else if (!existing.split(" / ").includes(opt.label)) merged.set(opt.name, `${existing} / ${opt.label}`);
          }
        } else {
          failures++;
        }
      }
      setInterfaceOptions(
        Array.from(merged, ([name, label]) => ({ name, label })).sort((a, b) => a.label.localeCompare(b.label))
      );
      setInterfacesError(
        failures > 0 ? t("rules.interfacesUnavailable", { failed: failures, total: ids.length }) : null
      );

      // Aliases are config-synced across the fleet in practice, so unlike
      // interfaces this doesn't try to detect/merge conflicting content —
      // just offers every distinct name once, first host wins. The deploy
      // endpoint still checks live content-consistency before any push.
      const aliasMerged = new Map<string, AliasOption>();
      for (const r of aliasResults) {
        if (r.status === "fulfilled") {
          for (const a of r.value) {
            if (!aliasMerged.has(a.name)) aliasMerged.set(a.name, a);
          }
        }
      }
      setAliasOptions(Array.from(aliasMerged.values()).sort((a, b) => a.name.localeCompare(b.name)));

      // Protocol names are IANA constants baked into OPNsense itself, not
      // host config, so — unlike interfaces — no cross-host conflict is
      // possible here; first host to answer wins.
      const protocolMerged = new Map<string, string>();
      for (const r of protocolResults) {
        if (r.status === "fulfilled") {
          for (const p of r.value) if (!protocolMerged.has(p.name)) protocolMerged.set(p.name, p.label);
        }
      }
      setProtocolOptions(
        Array.from(protocolMerged, ([name, label]) => ({ name, label })).sort((a, b) =>
          a.name === "any" ? -1 : b.name === "any" ? 1 : a.label.localeCompare(b.label)
        )
      );
    })();
    return () => {
      cancelled = true;
    };
  }, [enabledHostIdsKey, t]);

  // Live countdown display, ticking independently of the network poll below.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // One stable interval for the page's lifetime; only actually re-fetches
  // while some deployment is still pending confirmation, via a ref so this
  // doesn't need to be torn down/recreated as `rules` changes.
  const rulesRef = useRef(rules);
  rulesRef.current = rules;
  useEffect(() => {
    const t = setInterval(() => {
      const hasPending = rulesRef.current?.some((r) => r.deployments.some((d) => d.status === "pending"));
      if (hasPending) load();
    }, 5000);
    return () => clearInterval(t);
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      if (editing) await api.updateOneToOne(editing.id, form, editing.updated_at);
      else await api.createOneToOne(form);
      setShowModal(false);
      setForm(emptyForm);
      await load();
    } catch (err) {
      setFormError(errorMessage(err, "rules.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  function openEditor(rule: OneToOne | null, copy = false, restored?: OneToOneCreate) {
    setEditing(copy ? null : rule);
    const candidate = restored ?? (rule ? { ...rule, description: copy ? `${rule.description.slice(0, 247)}${t("rules.copySuffix")}` : rule.description } : { ...emptyForm, target_group_ids: groupFilter && groups.some(g => g.id === groupFilter) ? [groupFilter] : [] });
    const available = restored ? {
      ...candidate,
      target_host_ids: candidate.target_host_ids.filter(id => hosts.some(h => h.id === id)),
      target_group_ids: candidate.target_group_ids.filter(id => groups.some(g => g.id === id)),
      policy_hierarchy_group_id: hierarchyGroups.some(g => g.id === candidate.policy_hierarchy_group_id) ? candidate.policy_hierarchy_group_id : null,
    } : candidate;
    setForm(available);
    setFormError(available.target_host_ids.length !== candidate.target_host_ids.length || available.target_group_ids.length !== candidate.target_group_ids.length || available.policy_hierarchy_group_id !== candidate.policy_hierarchy_group_id
      ? t("rules.staleTargets")
      : null);
    setManualInterface(false);
    setReview(null);
    setShowModal(true);
  }

  async function handleConfirm(id: string) {
    setConfirmingId(id);
    try {
      await api.confirmOneToOne(id);
      await load();
      toast.show(t("rules.confirmed"), "success");
    } catch (err) {
      setError(errorMessage(err, "rules.confirmFailed"));
    } finally {
      setConfirmingId(null);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm(t("rules.deleteConfirm"))) return;
    const description = rules?.find((r) => r.id === id)?.description;
    try { await api.deleteOneToOne(id); await load(); toast.show(description ? t("rules.deletedNamed", { name: description }) : t("rules.deleted"), "success"); }
    catch (e) { setError(errorMessage(e, "common.deleteFailed")); }
  }

  function toggleTarget(id: string) {
    setForm((f) => ({
      ...f,
      target_host_ids: f.target_host_ids.includes(id)
        ? f.target_host_ids.filter((x) => x !== id)
        : [...f.target_host_ids, id],
    }));
  }

  function secondsLeft(deadline: string): number {
    return Math.max(0, Math.round((new Date(deadline).getTime() - now) / 1000));
  }

  const interfaceSelectOptions = interfaceOptions.some((opt) => opt.name === form.interface)
    ? interfaceOptions
    : form.interface
      ? [{ name: form.interface, label: form.interface }, ...interfaceOptions]
      : interfaceOptions;
  // A "/" in the merged label means hosts disagreed on what this identifier
  // means — e.g. "opt6" is "Guest" on one firewall and "Dummy01" on
  // another. The backend rejects the push in that case; this just
  // surfaces it before the user gets that far.
  const selectedInterfaceLabel = interfaceOptions.find((opt) => opt.name === form.interface)?.label;
  const interfaceMismatch = selectedInterfaceLabel?.includes(" / ") ?? false;

  const portAliasOptions = aliasOptions.filter((a) => a.type === "port");
  const netComboOptions: ComboOption[] = aliasOptions.map((a) => ({
    value: a.name,
    label: a.description ? `${a.description} (${a.type})` : a.type,
  }));
  const portComboOptions: ComboOption[] = [
    ...WELL_KNOWN_PORTS,
    ...portAliasOptions.map((a) => ({ value: a.name, label: a.description || a.type })),
  ];

  const selectedGroup = groups.find(g => g.id === groupFilter);
  const groupRules = (rules ?? []).filter(rule => !groupFilter || rule.target_group_ids.includes(groupFilter));
  const visibleRules = groupRules.filter(rule => (!ruleFilter || rule.id === ruleFilter) && (!stateFilter || rule.workflow_state === stateFilter) &&
    [rule.description, rule.source_net, rule.destination_net,  rule.interface, rule.external].join(" ").toLowerCase().includes(search.toLowerCase()));

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{selectedGroup ? t("rules.groupTitle", { name: selectedGroup.name }) : '1:1 NAT'}<InfoTip text={t("rules.subtitle")} size={18} /></h1>
        {activeTab === "managed" && (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setShowAdopt(true)} disabled={readOnly}>
              <Download size={16} />
              {t("adopt.button")}
            </Button>
            <Button disabled={readOnly} onClick={() => openEditor(null)}>
              <Plus size={16} />
              {t("rules.new")}
            </Button>
          </div>
        )}
      </div>

      {showAdopt && <AdoptObjectsModal kind="one-to-one" hosts={hosts} onClose={() => setShowAdopt(false)} onAdopted={load} />}

      <Tabs
        className="mb-6"
        items={[
          { key: "managed", label: t("rules.managed") },
          { key: "live", label: t("nat.liveTab") },
        ]}
        active={activeTab}
        onChange={(key) => setActiveTab(key as "managed" | "live")}
      />

      {activeTab === "live" ? (
        <LiveNat kind="one-to-one" />
      ) : (
        <>
      {error && <div className="mb-4"><p role="alert" className="text-sm text-red-600">{error}</p><Button variant="ghost" onClick={load}>{t("rules.retry")}</Button></div>}

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Input aria-label={t("rules.searchLabel")} placeholder={t("rules.searchPlaceholder")} value={search} onChange={e => setSearch(e.target.value)} />
        <Select aria-label={t("rules.groupFilter")} value={groupFilter} onChange={e => setGroupFilter(e.target.value)}><option value="">{t("rules.allGroups")}</option>{groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</Select>
        <Select aria-label={t("rules.stateFilter")} value={stateFilter} onChange={e => setStateFilter(e.target.value)}><option value="">{t("rules.allStates")}</option>{workflowStates.map((value) => <option key={value} value={value}>{t(`workflow.${value}`)}</option>)}</Select>
      </div>
      {selectedGroup && <p className="mb-3 text-sm text-slate-500">{t("rules.groupSummary", { members: selectedGroup.host_ids.length, rules: groupRules.length })}<Link href="/groups" className="text-sky-700 underline">{t("rules.manageMembers")}</Link></p>}
      {ruleFilter && <Button variant="ghost" onClick={() => setGroupFilter(groupFilter)}>{selectedGroup ? t("rules.showAllInGroup") : t("rules.showAll")}</Button>}
      {rules && <div className="mb-4 flex flex-wrap gap-4 text-sm text-slate-600"><span>{t("rules.countDraft", { count: groupRules.filter(r => r.workflow_state === "draft").length })}</span><span>{t("rules.countReview", { count: groupRules.filter(r => ["attention", "cleanup", "pending"].includes(r.workflow_state)).length })}</span><span>{t("rules.countSynced", { count: groupRules.filter(r => r.workflow_state === "synced").length })}</span></div>}
      <div className="flex flex-col gap-4">
        {rules === null && !error && <Card className="p-6"><Loading>{t("rules.loading")}</Loading></Card>}
        {rules !== null && visibleRules.length === 0 && (
          <EmptyState
            icon={Network}
            title={groupFilter && !selectedGroup ? t("rules.groupGone") : groupRules.length === 0 ? (selectedGroup ? t("rules.groupEmpty") : t("rules.none")) : t("rules.noMatch")}
            action={groupRules.length === 0 && (!groupFilter || selectedGroup)
              ? <Button disabled={readOnly} onClick={() => openEditor(null)}>{selectedGroup ? t("rules.createFirstForGroup") : t("rules.createFirst")}</Button>
              : <Button variant="ghost" onClick={() => { setSearch(""); setStateFilter(""); setGroupFilter(selectedGroup ? groupFilter : ""); }}>{t("rules.resetSelection")}</Button>}
          />
        )}
        <fieldset disabled={readOnly} className="contents">
        {visibleRules.map((rule) => {
          const hasPending = rule.deployments.some((d) => d.status === "pending");
          return (
            <Card key={rule.id} className="p-5">
              <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-medium text-slate-900">{rule.description}</h2>
                    <WorkflowBadge state={rule.workflow_state} />
                  </div>
                  <p className="mt-1 text-sm text-slate-500">
                    {rule.interface} · 
                    {rule.source_net}
                     → {rule.destination_net}
                     → {rule.external}
                  </p>
                  <p className="mt-2 text-xs text-slate-500">{t("rules.targets", { active: rule.workflow_counts.active_targets, total: rule.workflow_counts.targets })}{rule.workflow_counts.retained > 0 ? t("rules.retainedTargets", { count: rule.workflow_counts.retained }) : ""}{!rule.enabled ? t("rules.disabledSuffix") : ""}</p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {rule.policy_hierarchy_group_id && <span className="rounded-full bg-violet-50 px-2.5 py-0.5 text-xs text-violet-800">{t("rules.hierarchyTag", { name: hierarchyGroups.find(g => g.id === rule.policy_hierarchy_group_id)?.name ?? rule.policy_hierarchy_group_id.slice(0, 8) })}</span>}
                    {rule.target_group_ids.map(id => <span key={id} className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs text-blue-800">{t("rules.groupTag", { name: groups.find(g => g.id === id)?.name ?? id.slice(0, 8) })}</span>)}
                    {rule.target_host_ids.map((id) => (
                      <span key={id} className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">
                        {hostsById.get(id)?.name ?? id.slice(0, 8)}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <Button variant="outline" onClick={() => openEditor(rule)} disabled={rule.deployments.some(d => ["pending", "revert_failed"].includes(d.status))}><Pencil size={14} />{t("rules.edit")}</Button>
                  <Button onClick={() => setReview({ rule, mode: "preview" })}><Rocket size={14} />{t("rules.reviewPush")}</Button>
                  <Tooltip content={t("rules.historyTitle")}><Button variant="ghost" onClick={() => setReview({ rule, mode: "history" })} aria-label={t("rules.historyTitle")}><History size={14} /></Button></Tooltip>
                  <Tooltip content={t("rules.duplicateTitle")}><Button variant="ghost" onClick={() => openEditor(rule, true)} aria-label={t("rules.duplicateTitle")}><Copy size={14} /></Button></Tooltip>
                  <Button variant="dangerSubtle" onClick={() => handleDelete(rule.id)}>
                    <Trash2 size={14} />
                    {t("rules.delete")}
                  </Button>
                </div>
              </div>

              {rule.deployments.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-slate-100 pt-3">
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
                    {t("rules.deployResult")}
                  </p>
                  <ul className="flex flex-wrap gap-x-5 gap-y-1.5">
                    {rule.deployments.map((d) => (
                      <DeploymentRow key={d.host_id} deployment={d} secondsLeft={secondsLeft} />
                    ))}
                  </ul>
                  {hasPending && (
                    <Button className="ml-auto" variant="secondary" onClick={() => handleConfirm(rule.id)} disabled={confirmingId === rule.id}>
                      <ShieldCheck size={14} />
                      {confirmingId === rule.id ? t("rules.confirming") : t("rules.confirm")}
                    </Button>
                  )}
                </div>
              )}
            </Card>
          );
        })}
        </fieldset>
      </div>
        </>
      )}

      <fieldset disabled={readOnly} className="contents">
      {review && <OneToOneReview rule={review.rule} mode={review.mode} onClose={() => setReview(null)} onDeployed={() => { setReview(null); load(); }} onChanged={load} onRestore={snapshot => openEditor(review.rule, false, snapshot)} />}
      {showModal && (
        <Modal title={editing ? t("rules.editDraft") : t("rules.new")} onClose={() => setShowModal(false)}>
          <form onSubmit={handleCreate} className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <FieldGroup label={t("common.description")}>
              <Input
                required
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Allow SSH from management"
              />
            </FieldGroup>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label="Interface">
                {manualInterface || interfaceOptions.length === 0 ? (
                  <Input
                    required
                    value={form.interface}
                    onChange={(e) => setForm({ ...form, interface: e.target.value })}
                    placeholder="lan"
                  />
                ) : (
                  <Select
                    required
                    value={form.interface}
                    onChange={(e) => setForm({ ...form, interface: e.target.value })}
                  >
                    {interfaceSelectOptions.map((opt) => (
                      <option key={opt.name} value={opt.name}>
                        {opt.label} ({opt.name})
                      </option>
                    ))}
                  </Select>
                )}
                {interfaceOptions.length > 0 && (
                  <button
                    type="button"
                    className="mt-1 text-xs text-sky-700 underline"
                    onClick={() => setManualInterface((m) => !m)}
                  >
                    {manualInterface ? t("rules.pickFromList") : t("rules.enterManually")}
                  </button>
                )}
                {interfaceMismatch && (
                  <p className="mt-1 text-xs text-red-600">
                    {t("rules.interfaceMismatch", { label: selectedInterfaceLabel ?? "" })}
                  </p>
                )}
                {interfacesError && <p className="mt-1 text-xs text-flame-600">{interfacesError}</p>}
              </FieldGroup>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label="Source">
                <ComboInput
                  value={form.source_net}
                  onChange={(v) => setForm({ ...form, source_net: v })}
                  options={netComboOptions}
                  placeholder={t("rules.netPlaceholder")}
                />
              </FieldGroup>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label="Destination">
                <ComboInput
                  value={form.destination_net}
                  onChange={(v) => setForm({ ...form, destination_net: v })}
                  options={netComboOptions}
                  placeholder={t("rules.netPlaceholder")}
                />
              </FieldGroup>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />{t("rules.active")}</label>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("nat.externalNet")}><ComboInput required value={form.external} onChange={v => setForm({...form, external: v})} options={[]} placeholder="192.0.2.10" /></FieldGroup>
              <FieldGroup label="NAT Reflection"><Select value={form.natreflection} onChange={e => setForm({...form, natreflection: e.target.value})}><option value="">{t("nat.reflectionDefault")}</option><option value="enable">{t("nat.reflectionEnabled")}</option><option value="disable">{t("nat.reflectionDisabled")}</option></Select></FieldGroup>
            </div>
            <div className="flex flex-wrap gap-4 text-sm"><label><input type="checkbox" checked={form.source_not} onChange={e => setForm({...form, source_not: e.target.checked})} />{t("nat.invertSource")}</label><label><input type="checkbox" checked={form.destination_not} onChange={e => setForm({...form, destination_not: e.target.checked})} />{t("nat.invertDestination")}</label></div>
            <FieldGroup label={t("nat.type")}><Select value={form.type} onChange={e => setForm({...form, type: e.target.value as "binat" | "nat"})}><option value="binat">{t("nat.binat")}</option><option value="nat">{t("nat.natOutbound")}</option></Select></FieldGroup>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.log} onChange={e => setForm({...form, log: e.target.checked})} />{t("fields.log")}</label>
            <FieldGroup label={t("rules.deviceGroups")}>
              <div className="space-y-2 rounded-lg border border-slate-200 p-3">
                {groups.length === 0 && <p className="text-sm text-slate-500">{t("rules.createGroupsHint")}</p>}
                {groups.map(group => <label key={group.id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.target_group_ids.includes(group.id)} onChange={() => setForm(f => ({ ...f, target_group_ids: f.target_group_ids.includes(group.id) ? f.target_group_ids.filter(id => id !== group.id) : [...f.target_group_ids, group.id] }))} />{t("rules.groupHosts", { name: group.name, count: group.host_ids.length })}</label>)}
              </div>
            </FieldGroup>
            <FieldGroup label={t("rules.hierarchyOptional")} hint={t("nat.hierarchyHint")}>
              <Select value={form.policy_hierarchy_group_id ?? ""} onChange={e => setForm({...form, policy_hierarchy_group_id: e.target.value || null})}>
                <option value="">{t("rules.noInheritance")}</option>
                {hierarchyGroups.map(group => <option key={group.id} value={group.id}>{t("rules.andSubgroups", { name: group.name })}</option>)}
              </Select>
            </FieldGroup>
            <FieldGroup label={t("rules.additionalTargets")}>
              <div className="space-y-1.5 rounded-lg border border-slate-200 p-3">
                {hosts.length === 0 && <p className="text-sm text-slate-400">{t("rules.noHosts")}</p>}
                {hosts.map((host) => {
                  const coveringGroups = groups.filter(
                    (g) => form.target_group_ids.includes(g.id) && g.host_ids.includes(host.id)
                  );
                  return (
                    <label key={host.id} className="flex items-center gap-2 text-sm text-slate-700">
                      <input
                        type="checkbox"
                        checked={form.target_host_ids.includes(host.id)}
                        onChange={() => toggleTarget(host.id)}
                      />
                      {host.name}
                      {coveringGroups.length > 0 && (
                        <span className="text-xs text-slate-400">
                          {t("rules.coveredBy", { groups: coveringGroups.map((g) => g.name).join(", ") })}
                        </span>
                      )}
                    </label>
                  );
                })}
              </div>
            </FieldGroup>

            {formError && <p className="text-sm text-red-600">{formError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="ghost" onClick={() => setShowModal(false)}>
                {t("common.cancel")}
              </Button>
              <Button type="submit" disabled={saving} loading={saving}>
                {saving ? t("common.saving") : t("rules.saveDraft")}
              </Button>
            </div>
          </form>
        </Modal>
      )}
      </fieldset>
    </div>
  );
}

function DeploymentRow({
  deployment,
  secondsLeft,
}: {
  deployment: DeploymentResult;
  secondsLeft: (deadline: string) => number;
}) {
  const { t, ts } = useLanguage();
  const d = deployment;
  return (
    <li className="flex items-center gap-2 text-sm">
      <DeploymentIcon status={d.status} />
      <span className="font-medium text-slate-700">{d.host_name}</span>
      {d.status === "pending" && d.revert_deadline && (
        <span className="text-flame-600">
          {t("rules.expiresIn", { seconds: secondsLeft(d.revert_deadline) })}
        </span>
      )}
      {d.status === "reverted" && (
        <span className="text-slate-400">{t("rules.revertedNote")}</span>
      )}
      {d.status === "revert_failed" && (
        <span className="text-red-600">{t("rules.revertFailed", { message: ts(d.message ?? "") })}</span>
      )}
      {(d.status === "error" || d.status === "success") && d.message && (
        <span className="text-slate-400">— {ts(d.message ?? "")}</span>
      )}
    </li>
  );
}

function DeploymentIcon({ status }: { status: DeploymentResult["status"] }) {
  switch (status) {
    case "success":
      return <CheckCircle2 size={15} className="text-emerald-500" />;
    case "error":
    case "revert_failed":
      return <AlertTriangle size={15} className="text-red-500" />;
    case "pending":
      return <Clock size={15} className="text-flame-500" />;
    case "reverted":
      return <RotateCcw size={15} className="text-slate-400" />;
    default:
      return <XCircle size={15} className="text-red-500" />;
  }
}


// Native <datalist> only filters by an option's `value`, so a lookup like
// "https" never surfaces an alias whose *description* says "DC_MGMT net"
// or a well-known port whose name isn't the value we actually want to
// insert (443, not the string "https" — see WELL_KNOWN_PORTS). This
// filters against value AND label and always inserts the option's value.
function ComboInput({
  value,
  onChange,
  options,
  placeholder,
  required,
}: {
  value: string;
  onChange: (value: string) => void;
  options: ComboOption[];
  placeholder?: string;
  required?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const query = value.trim().toLowerCase();
  const suggestions = (
    query ? options.filter((o) => o.value.toLowerCase().includes(query) || o.label.toLowerCase().includes(query)) : options
  ).slice(0, 20);

  return (
    <div className="relative">
      <Input
        required={required}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
      />
      {open && suggestions.length > 0 && (
        <ul className="absolute z-10 mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
          {suggestions.map((o) => (
            <li key={o.value}>
              <button
                type="button"
                className="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-50"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
              >
                <span className="font-medium text-slate-800">{o.value}</span>
                {o.label && o.label !== o.value && <span className="ml-1.5 text-slate-400">{o.label}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
