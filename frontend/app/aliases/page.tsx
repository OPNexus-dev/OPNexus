"use client";

import { Tooltip, InfoTip } from "@/components/ui/Tooltip";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Plus,
  Pencil,
  History,
  Copy,
  Rocket,
  RotateCcw,
  ShieldCheck,
  Tags,
  Trash2,
  XCircle,
  Download,
} from "lucide-react";
import { WorkflowBadge, workflowStates } from "@/components/WorkflowBadge";
import { useLanguage } from "@/components/LanguageProvider";
import { AliasReview } from "@/components/AliasReview";
import { LiveAliasesDrift } from "@/components/LiveAliasesDrift";
import { AdoptObjectsModal } from "@/components/AdoptObjectsModal";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { useReadOnly } from "@/lib/useReadOnly";
import { Loading } from "@/components/ui/Loading";
import {
  api,
  DeploymentResult,
  DeviceGroup,
  Host,
  PolicyAlias,
  PolicyAliasCreate,
  PolicyAliasType,
  PolicyHierarchyGroup,
} from "@/lib/api";

const emptyForm: PolicyAliasCreate = {
  name: "",
  type: "host",
  content: "",
  description: "",
  enabled: true,
  target_host_ids: [],
  target_group_ids: [],
  policy_hierarchy_group_id: null,
};

const aliasTypes: PolicyAliasType[] = ["host", "network", "port", "networkgroup", "mac", "asn"];

export default function AliasesPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("aliases.loading")}</Loading>}>
      <AliasesContent />
    </Suspense>
  );
}

function AliasesContent() {
  const { t, errorMessage } = useLanguage();
  const typeLabel = (type: string) => (aliasTypes.includes(type as PolicyAliasType) ? t(`aliases.type.${type}`) : type);
  const typeHint = (type: PolicyAliasType) => t(`aliases.hint.${type}`);
  const readOnly = useReadOnly();
  const [showAdopt, setShowAdopt] = useState(false);
  const toast = useToast();
  const router = useRouter();
  const params = useSearchParams();
  const groupFilter = params.get("group") ?? "";
  const aliasFilter = params.get("alias") ?? "";
  const [stateFilter, setStateFilter] = useState("");
  const autoOpened = useRef("");
  const [aliases, setAliases] = useState<PolicyAlias[] | null>(null);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [hierarchyGroups, setHierarchyGroups] = useState<PolicyHierarchyGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<PolicyAliasCreate>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState<PolicyAlias | null>(null);
  const [review, setReview] = useState<{ alias: PolicyAlias; mode: "preview" | "history" } | null>(null);
  const [search, setSearch] = useState("");
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [activeTab, setActiveTab] = useState<"managed" | "live">("managed");

  useEffect(() => {
    setActiveTab(params.get("tab") === "live" ? "live" : "managed");
  }, [params]);

  const hostsById = useMemo(() => new Map(hosts.map((h) => [h.id, h])), [hosts]);

  async function load() {
    try {
      const [aliasList, hostList, groupList, hierarchyGroupList] = await Promise.all([api.listAliases(), api.listHosts(), api.listGroups(), api.listPolicyHierarchyGroups()]);
      setGroups(groupList);
      setHierarchyGroups(hierarchyGroupList);
      setAliases(aliasList);
      setHosts(hostList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "aliases.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const aliasesRef = useRef(aliases);
  aliasesRef.current = aliases;
  useEffect(() => {
    const t = setInterval(() => {
      const hasPending = aliasesRef.current?.some((a) => a.deployments.some((d) => d.status === "pending"));
      if (hasPending) load();
    }, 5000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const key = params.toString();
    if (aliases !== null && params.get("new") === "1" && key !== autoOpened.current) {
      autoOpened.current = key;
      openEditor(null);
      const query = new URLSearchParams(params.toString());
      query.delete("new");
      router.replace(`/aliases${query.size ? `?${query.toString()}` : ""}`, { scroll: false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, aliases]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      if (editing) await api.updateAlias(editing.id, form, editing.updated_at);
      else await api.createAlias(form);
      setShowModal(false);
      setForm(emptyForm);
      await load();
    } catch (err) {
      setFormError(errorMessage(err, "aliases.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  function openEditor(alias: PolicyAlias | null, copy = false, restored?: PolicyAliasCreate) {
    setEditing(copy ? null : alias);
    const candidate =
      restored ??
      (alias
        ? { ...alias, name: copy ? `${alias.name}_copy`.slice(0, 64) : alias.name }
        : { ...emptyForm, target_group_ids: groupFilter && groups.some((g) => g.id === groupFilter) ? [groupFilter] : [] });
    const available = restored
      ? {
          ...candidate,
          target_host_ids: candidate.target_host_ids.filter((id) => hosts.some((h) => h.id === id)),
          target_group_ids: candidate.target_group_ids.filter((id) => groups.some((g) => g.id === id)),
          policy_hierarchy_group_id: hierarchyGroups.some((g) => g.id === candidate.policy_hierarchy_group_id) ? candidate.policy_hierarchy_group_id : null,
        }
      : candidate;
    setForm(available);
    setFormError(
      available.target_host_ids.length !== candidate.target_host_ids.length ||
        available.target_group_ids.length !== candidate.target_group_ids.length
        || available.policy_hierarchy_group_id !== candidate.policy_hierarchy_group_id
        ? t("rules.staleTargets")
        : null
    );
    setReview(null);
    setShowModal(true);
  }

  async function handleConfirm(id: string) {
    setConfirmingId(id);
    try {
      await api.confirmAlias(id);
      await load();
      toast.show(t("rules.confirmed"), "success");
    } catch (err) {
      setError(errorMessage(err, "rules.confirmFailed"));
    } finally {
      setConfirmingId(null);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm(t("aliases.deleteConfirm"))) return;
    const name = aliases?.find((a) => a.id === id)?.name;
    try {
      await api.deleteAlias(id);
      await load();
      toast.show(name ? t("aliases.deletedNamed", { name }) : t("aliases.deleted"), "success");
    } catch (e) {
      setError(errorMessage(e, "common.deleteFailed"));
    }
  }

  function toggleTarget(id: string) {
    setForm((f) => ({
      ...f,
      target_host_ids: f.target_host_ids.includes(id) ? f.target_host_ids.filter((x) => x !== id) : [...f.target_host_ids, id],
    }));
  }

  function secondsLeft(deadline: string): number {
    return Math.max(0, Math.round((new Date(deadline).getTime() - now) / 1000));
  }

  const selectedGroup = groups.find((g) => g.id === groupFilter);
  const groupAliases = (aliases ?? []).filter((a) => !groupFilter || a.target_group_ids.includes(groupFilter));
  const visibleAliases = groupAliases.filter(
    (a) =>
      (!aliasFilter || a.id === aliasFilter) &&
      (!stateFilter || a.workflow_state === stateFilter) &&
      [a.name, a.description, a.content].join(" ").toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{selectedGroup ? t("aliases.titleNamed", { name: selectedGroup.name }) : t("aliases.title")}<InfoTip text={t("aliases.subtitle")} size={18} /></h1>
        {aliasFilter && (
          <Button variant="ghost" onClick={() => router.push(groupFilter ? `/aliases?group=${groupFilter}` : "/aliases")}>
            {t("aliases.showAll")}
          </Button>
        )}
        {activeTab === "managed" && (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setShowAdopt(true)} disabled={readOnly}>
              <Download size={16} />
              {t("adopt.button")}
            </Button>
            <Button disabled={readOnly} onClick={() => openEditor(null)}>
              <Plus size={16} />
              {t("aliases.new")}
            </Button>
          </div>
        )}
      </div>

      {showAdopt && <AdoptObjectsModal kind="aliases" hosts={hosts} onClose={() => setShowAdopt(false)} onAdopted={load} />}

      <Tabs
        className="mb-6"
        items={[
          { key: "managed", label: t("rules.managed") },
          { key: "live", label: t("rules.live") },
        ]}
        active={activeTab}
        onChange={(key) => setActiveTab(key as "managed" | "live")}
      />

      {activeTab === "live" ? (
        <LiveAliasesDrift />
      ) : (
        <>

      {error && (
        <div className="mb-4">
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
          <Button variant="ghost" onClick={load}>
            {t("rules.retry")}
          </Button>
        </div>
      )}

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Input aria-label={t("aliases.searchLabel")} placeholder={t("aliases.searchPlaceholder")} value={search} onChange={(e) => setSearch(e.target.value)} />
        <Select aria-label={t("rules.stateFilter")} value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
          <option value="">{t("rules.allStates")}</option>
          {workflowStates.map((value) => (
            <option key={value} value={value}>
              {t(`workflow.${value}`)}
            </option>
          ))}
        </Select>
      </div>

      <div className="flex flex-col gap-4">
        {aliases === null && !error && <Card className="p-6"><Loading>{t("aliases.loading")}</Loading></Card>}
        {aliases !== null && visibleAliases.length === 0 && (
          <EmptyState
            icon={Tags}
            title={groupAliases.length === 0 ? t("aliases.none") : t("aliases.noMatch")}
            action={groupAliases.length === 0 ? (
              <Button disabled={readOnly} onClick={() => openEditor(null)}>{t("aliases.createFirst")}</Button>
            ) : (
              <Button variant="ghost" onClick={() => { setSearch(""); setStateFilter(""); }}>
                {t("rules.resetSelection")}
              </Button>
            )}
          />
        )}
        <fieldset disabled={readOnly} className="contents">
        {visibleAliases.map((alias) => {
          const hasPending = alias.deployments.some((d) => d.status === "pending");
          return (
            <Card key={alias.id} className="p-5">
              <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-mono font-medium text-slate-900">{alias.name}</h2>
                    <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">{typeLabel(alias.type)}</span>
                    <WorkflowBadge state={alias.workflow_state} />
                  </div>
                  {alias.description && <p className="mt-1 text-sm text-slate-500">{alias.description}</p>}
                  <p className="mt-1 whitespace-pre-wrap font-mono text-xs text-slate-500">{alias.content || t("aliases.empty")}</p>
                  <p className="mt-2 text-xs text-slate-500">
                    {t("rules.targets", { active: alias.workflow_counts.active_targets, total: alias.workflow_counts.targets })}
                    {alias.workflow_counts.retained > 0 ? t("rules.retainedTargets", { count: alias.workflow_counts.retained }) : ""}
                    {!alias.enabled ? t("aliases.disabledSuffix") : ""}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {alias.policy_hierarchy_group_id && (
                      <span className="rounded-full bg-violet-50 px-2.5 py-0.5 text-xs text-violet-800">
                        {t("rules.hierarchyTag", { name: hierarchyGroups.find((g) => g.id === alias.policy_hierarchy_group_id)?.name ?? alias.policy_hierarchy_group_id.slice(0, 8) })}
                      </span>
                    )}
                    {alias.target_group_ids.map((id) => (
                      <span key={id} className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs text-blue-800">
                        {t("rules.groupTag", { name: groups.find((g) => g.id === id)?.name ?? id.slice(0, 8) })}
                      </span>
                    ))}
                    {alias.target_host_ids.map((id) => (
                      <span key={id} className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">
                        {hostsById.get(id)?.name ?? id.slice(0, 8)}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <Button
                    variant="outline"
                    onClick={() => openEditor(alias)}
                    disabled={alias.deployments.some((d) => ["pending", "revert_failed"].includes(d.status))}
                  >
                    <Pencil size={14} />
                    {t("rules.edit")}
                  </Button>
                  <Button onClick={() => setReview({ alias, mode: "preview" })}>
                    <Rocket size={14} />
                    {t("rules.reviewPush")}
                  </Button>
                  <Tooltip content={t("aliases.historyTitle")}><Button variant="ghost" onClick={() => setReview({ alias, mode: "history" })} aria-label={t("aliases.historyTitle")}><History size={14} /></Button></Tooltip>
                  <Tooltip content={t("aliases.duplicateTitle")}><Button variant="ghost" onClick={() => openEditor(alias, true)} aria-label={t("aliases.duplicateTitle")}><Copy size={14} /></Button></Tooltip>
                  <Button variant="dangerSubtle" onClick={() => handleDelete(alias.id)}>
                    <Trash2 size={14} />
                    {t("rules.delete")}
                  </Button>
                </div>
              </div>

              {alias.deployments.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-slate-100 pt-3">
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{t("rules.deployResult")}</p>
                  <ul className="flex flex-wrap gap-x-5 gap-y-1.5">
                    {alias.deployments.map((d) => (
                      <DeploymentRow key={d.host_id} deployment={d} secondsLeft={secondsLeft} />
                    ))}
                  </ul>
                  {hasPending && (
                    <Button className="ml-auto" variant="secondary" onClick={() => handleConfirm(alias.id)} disabled={confirmingId === alias.id}>
                      <ShieldCheck size={14} />
                      {confirmingId === alias.id ? t("rules.confirming") : t("rules.confirm")}
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
      {review && (
        <AliasReview
          alias={review.alias}
          mode={review.mode}
          onClose={() => setReview(null)}
          onDeployed={() => {
            setReview(null);
            load();
          }}
          onChanged={load}
          onRestore={(snapshot) => openEditor(review.alias, false, snapshot)}
        />
      )}
      {showModal && (
        <Modal title={editing ? t("aliases.editDraft") : t("aliases.new")} onClose={() => setShowModal(false)}>
          <form onSubmit={handleCreate} className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("hosts.name")}>
                <Input
                  required
                  pattern="[A-Za-z0-9_]+"
                  title={t("aliases.nameHint")}
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="Web_Backends"
                />
              </FieldGroup>
              <FieldGroup label={t("aliases.typeLabel")}>
                <Select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value as PolicyAliasType })}>
                  {aliasTypes.map((value) => (
                    <option key={value} value={value}>
                      {typeLabel(value)}
                    </option>
                  ))}
                </Select>
              </FieldGroup>
            </div>
            <FieldGroup label={t("common.description")}>
              <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Webserver Backends" />
            </FieldGroup>
            <FieldGroup label={t("aliases.content")}>
              <textarea
                className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 shadow-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
                rows={5}
                value={form.content}
                onChange={(e) => setForm({ ...form, content: e.target.value })}
                placeholder={typeHint(form.type)}
              />
              <p className="mt-1 text-xs text-slate-500">{typeHint(form.type)}</p>
            </FieldGroup>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              {t("aliases.active")}
            </label>
            <FieldGroup label={t("rules.deviceGroups")}>
              <div className="space-y-2 rounded-lg border border-slate-200 p-3">
                {groups.length === 0 && <p className="text-sm text-slate-500">{t("rules.createGroupsHint")}</p>}
                {groups.map((group) => (
                  <label key={group.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.target_group_ids.includes(group.id)}
                      onChange={() =>
                        setForm((f) => ({
                          ...f,
                          target_group_ids: f.target_group_ids.includes(group.id)
                            ? f.target_group_ids.filter((id) => id !== group.id)
                            : [...f.target_group_ids, group.id],
                        }))
                      }
                    />
                    {t("rules.groupHosts", { name: group.name, count: group.host_ids.length })}
                  </label>
                ))}
              </div>
            </FieldGroup>
            <FieldGroup label={t("rules.hierarchyOptional")} hint={t("aliases.hierarchyHint")}>
              <Select value={form.policy_hierarchy_group_id ?? ""} onChange={(e) => setForm({ ...form, policy_hierarchy_group_id: e.target.value || null })}>
                <option value="">{t("rules.noInheritance")}</option>
                {hierarchyGroups.map((group) => <option key={group.id} value={group.id}>{t("rules.andSubgroups", { name: group.name })}</option>)}
              </Select>
            </FieldGroup>
            <FieldGroup label={t("rules.additionalTargets")}>
              <div className="space-y-1.5 rounded-lg border border-slate-200 p-3">
                {hosts.length === 0 && <p className="text-sm text-slate-400">{t("rules.noHosts")}</p>}
                {hosts.map((host) => {
                  const coveringGroups = groups.filter((g) => form.target_group_ids.includes(g.id) && g.host_ids.includes(host.id));
                  return (
                    <label key={host.id} className="flex items-center gap-2 text-sm text-slate-700">
                      <input type="checkbox" checked={form.target_host_ids.includes(host.id)} onChange={() => toggleTarget(host.id)} />
                      {host.name}
                      {coveringGroups.length > 0 && (
                        <span className="text-xs text-slate-400">{t("aliases.coveredBy", { groups: coveringGroups.map((g) => g.name).join(", ") })}</span>
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

function DeploymentRow({ deployment, secondsLeft }: { deployment: DeploymentResult; secondsLeft: (deadline: string) => number }) {
  const { t, ts } = useLanguage();
  const d = deployment;
  return (
    <li className="flex items-center gap-2 text-sm">
      <DeploymentIcon status={d.status} />
      <span className="font-medium text-slate-700">{d.host_name}</span>
      {d.status === "pending" && d.revert_deadline && <span className="text-flame-600">{t("rules.expiresIn", { seconds: secondsLeft(d.revert_deadline) })}</span>}
      {d.status === "reverted" && <span className="text-slate-400">{t("rules.revertedNote")}</span>}
      {d.status === "revert_failed" && <span className="text-red-600">{t("rules.revertFailed", { message: ts(d.message ?? "") })}</span>}
      {(d.status === "error" || d.status === "success") && d.message && <span className="text-slate-400">— {ts(d.message ?? "")}</span>}
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

