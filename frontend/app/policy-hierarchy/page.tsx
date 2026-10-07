"use client";

import { InfoTip } from "@/components/ui/Tooltip";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronRight,
  History,
  GripVertical,
  Layers,
  Pencil,
  Plus,
  RefreshCw,
  Rocket,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { PolicyHierarchyRuleReview } from "@/components/PolicyHierarchyRuleReview";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";
import { Loading } from "@/components/ui/Loading";
import {
  api,
  Host,
  PolicyHierarchyGroup,
  PolicyHierarchyGroupWrite,
  PolicyHierarchyRule,
  PolicyHierarchyRuleCreate,
  PolicyHierarchyRulebase,
} from "@/lib/api";

const emptyGroupForm: PolicyHierarchyGroupWrite = { name: "", description: "", parent_id: null };

function emptyRuleForm(groupId: string, rulebase: PolicyHierarchyRulebase): PolicyHierarchyRuleCreate {
  return {
    hierarchy_group_id: groupId,
    rulebase,
    description: "",
    interface: "lan",
    direction: "in",
    ipprotocol: "inet",
    protocol: "tcp",
    action: "pass",
    source_net: "any",
    source_port: null,
    destination_net: "any",
    destination_port: null,
    enabled: true,
  };
}

// group + every descendant group id — used to keep the parent-picker from
// offering a choice that would create a cycle (the backend also enforces
// this — see policy_hierarchy_group_service.assert_no_cycle — this is just
// the client-side UX layer on top).
function descendantIds(groupId: string, groups: PolicyHierarchyGroup[]): Set<string> {
  const byParent = new Map<string | null, PolicyHierarchyGroup[]>();
  for (const g of groups) {
    const list = byParent.get(g.parent_id) ?? [];
    list.push(g);
    byParent.set(g.parent_id, list);
  }
  const result = new Set<string>();
  const stack = [groupId];
  while (stack.length) {
    const id = stack.pop()!;
    if (result.has(id)) continue;
    result.add(id);
    for (const child of byParent.get(id) ?? []) stack.push(child.id);
  }
  return result;
}

export default function PolicyHierarchyPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("ph.loading")}</Loading>}>
      <PolicyHierarchyContent />
    </Suspense>
  );
}

function PolicyHierarchyContent() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const router = useRouter();
  const params = useSearchParams();
  const selectedGroupId = params.get("group") ?? "";

  const [groups, setGroups] = useState<PolicyHierarchyGroup[] | null>(null);
  const [rules, setRules] = useState<PolicyHierarchyRule[]>([]);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [groupModal, setGroupModal] = useState<{ editing: PolicyHierarchyGroup | null } | null>(null);
  const [groupForm, setGroupForm] = useState<PolicyHierarchyGroupWrite>(emptyGroupForm);
  const [groupSaving, setGroupSaving] = useState(false);
  const [groupError, setGroupError] = useState<string | null>(null);

  const [ruleModal, setRuleModal] = useState<{ editing: PolicyHierarchyRule | null } | null>(null);
  const [ruleForm, setRuleForm] = useState<PolicyHierarchyRuleCreate | null>(null);
  const [ruleSaving, setRuleSaving] = useState(false);
  const [ruleError, setRuleError] = useState<string | null>(null);

  const [review, setReview] = useState<{ rule: PolicyHierarchyRule; mode: "preview" | "history" } | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [reasserting, setReasserting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    try {
      const [groupList, ruleList, hostList] = await Promise.all([
        api.listPolicyHierarchyGroups(),
        api.listPolicyHierarchyRules(),
        api.listHosts(),
      ]);
      setGroups(groupList);
      setRules(ruleList);
      setHosts(hostList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "ph.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  const hostsById = useMemo(() => new Map(hosts.map((h) => [h.id, h])), [hosts]);
  const groupsById = useMemo(() => new Map((groups ?? []).map((g) => [g.id, g])), [groups]);
  const byParent = useMemo(() => {
    const m = new Map<string | null, PolicyHierarchyGroup[]>();
    for (const g of groups ?? []) {
      const list = m.get(g.parent_id) ?? [];
      list.push(g);
      m.set(g.parent_id, list);
    }
    for (const list of m.values()) list.sort((a, b) => a.name.localeCompare(b.name));
    return m;
  }, [groups]);

  function selectGroup(id: string) {
    router.push(id ? `/policy-hierarchy?group=${id}` : "/policy-hierarchy");
  }

  function openGroupModal(group: PolicyHierarchyGroup | null) {
    setGroupModal({ editing: group });
    setGroupForm(group ? { name: group.name, description: group.description, parent_id: group.parent_id } : { ...emptyGroupForm, parent_id: selectedGroupId || null });
    setGroupError(null);
  }

  async function saveGroup(e: React.FormEvent) {
    e.preventDefault();
    setGroupSaving(true);
    setGroupError(null);
    try {
      const saved = groupModal?.editing
        ? await api.updatePolicyHierarchyGroup(groupModal.editing.id, groupForm)
        : await api.createPolicyHierarchyGroup(groupForm);
      setGroupModal(null);
      await load();
      selectGroup(saved.id);
    } catch (e) {
      setGroupError(errorMessage(e, "ph.groupSaveFailed"));
    } finally {
      setGroupSaving(false);
    }
  }

  async function deleteGroup(group: PolicyHierarchyGroup) {
    if (!confirm(t("ph.deleteGroupConfirm", { name: group.name }))) return;
    try {
      await api.deletePolicyHierarchyGroup(group.id);
      if (selectedGroupId === group.id) selectGroup("");
      await load();
    } catch (e) {
      setError(errorMessage(e, "common.deleteFailed"));
    }
  }

  function openRuleModal(rulebase: PolicyHierarchyRulebase, editing: PolicyHierarchyRule | null) {
    if (!selectedGroupId && !editing) return;
    setRuleModal({ editing });
    setRuleForm(editing ?? emptyRuleForm(selectedGroupId, rulebase));
    setRuleError(null);
  }

  async function saveRule(e: React.FormEvent) {
    e.preventDefault();
    if (!ruleForm) return;
    setRuleSaving(true);
    setRuleError(null);
    try {
      if (ruleModal?.editing) await api.updatePolicyHierarchyRule(ruleModal.editing.id, ruleForm, ruleModal.editing.updated_at);
      else await api.createPolicyHierarchyRule(ruleForm);
      setRuleModal(null);
      await load();
    } catch (e) {
      setRuleError(errorMessage(e, "ph.ruleSaveFailed"));
    } finally {
      setRuleSaving(false);
    }
  }

  async function deleteRule(rule: PolicyHierarchyRule) {
    if (!confirm(t("ph.deleteRuleConfirm", { name: rule.description }))) return;
    try {
      await api.deletePolicyHierarchyRule(rule.id);
      await load();
    } catch (e) {
      setError(errorMessage(e, "common.deleteFailed"));
    }
  }

  async function moveRule(rule: PolicyHierarchyRule, direction: "up" | "down") {
    try {
      await api.movePolicyHierarchyRule(rule.id, direction);
      await load();
    } catch (e) {
      setError(errorMessage(e, "ph.moveFailed"));
    }
  }

  async function reorderRules(rulebase: PolicyHierarchyRulebase, orderedRules: PolicyHierarchyRule[]) {
    if (!selectedGroupId) return;
    try {
      await api.reorderPolicyHierarchyRules(selectedGroupId, rulebase, orderedRules);
      setNotice(t("ph.orderSaved"));
      await load();
    } catch (e) {
      setError(errorMessage(e, "ph.orderSaveFailed"));
      await load();
    }
  }

  async function handleConfirm(id: string) {
    setConfirmingId(id);
    try {
      await api.confirmPolicyHierarchyRule(id);
      await load();
    } catch (e) {
      setError(errorMessage(e, "rules.confirmFailed"));
    } finally {
      setConfirmingId(null);
    }
  }

  async function handleReassert(group: PolicyHierarchyGroup) {
    setReasserting(true);
    setNotice(null);
    setError(null);
    try {
      await api.reassertHierarchyOrdering(group.id);
      setNotice(t("ph.reassertDone", { name: group.name }));
      await load();
    } catch (e) {
      setError(errorMessage(e, "ph.reassertFailed"));
    } finally {
      setReasserting(false);
    }
  }

  const selectedGroup = selectedGroupId ? groupsById.get(selectedGroupId) ?? null : null;
  const groupRules = rules.filter((r) => r.hierarchy_group_id === selectedGroupId);
  const preRules = groupRules.filter((r) => r.rulebase === "pre").sort((a, b) => a.sequence - b.sequence);
  const postRules = groupRules.filter((r) => r.rulebase === "post").sort((a, b) => a.sequence - b.sequence);

  const invalidParentIds = groupModal?.editing ? descendantIds(groupModal.editing.id, groups ?? []) : new Set<string>();

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{t("ph.title")}<InfoTip text={t("ph.subtitle")} size={18} /></h1>
        </div>
        <Button onClick={() => openGroupModal(null)} disabled={readOnly}>
          <Plus size={16} />
          {t("groups.new")}
        </Button>
      </div>

      {error && (
        <div className="my-4">
          <p role="alert" className="text-sm text-red-600">{error}</p>
          <Button variant="ghost" onClick={load}>{t("rules.retry")}</Button>
        </div>
      )}
      {notice && <p role="status" className="my-4 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}

      <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <Card className="p-3">
          <p className="mb-2 px-2 text-xs font-medium uppercase tracking-wide text-slate-400">{t("ph.groupTree")}</p>
          {groups === null && <Loading className="px-2 py-4">{t("common.loading")}</Loading>}
          {groups !== null && groups.length === 0 && (
            <p className="px-2 py-4 text-sm text-slate-500">{t("ph.noGroups")}</p>
          )}
          <nav className="space-y-0.5">
            {(byParent.get(null) ?? []).map((g) => (
              <GroupTreeNode
                key={g.id}
                group={g}
                depth={0}
                byParent={byParent}
                selectedId={selectedGroupId}
                onSelect={selectGroup}
              />
            ))}
          </nav>
        </Card>

        <fieldset disabled={readOnly} className="contents">
        <div>
          {!selectedGroup && (
            <Card className="p-8 text-center">
              <Layers className="mx-auto mb-3 text-sky-500" />
              <p className="font-medium text-slate-700">{t("ph.selectGroup")}</p>
              <p className="mt-2 text-sm text-slate-500">
                {t("ph.selectGroupHint")}
              </p>
            </Card>
          )}

          {selectedGroup && (
            <>
              <Card className="mb-4 p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-lg font-semibold text-slate-800">{selectedGroup.name}</h2>
                    {selectedGroup.description && <p className="mt-1 text-sm text-slate-500">{selectedGroup.description}</p>}
                    <p className="mt-2 text-xs text-slate-500">
                      {t("ph.directHosts", { count: selectedGroup.host_ids.length })}
                      <Link href="/hosts" className="text-sky-700 underline">{t("ph.assignHosts")}</Link>
                    </p>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {selectedGroup.host_ids.map((id) => (
                        <span key={id} className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">
                          {hostsById.get(id)?.name ?? id.slice(0, 8)}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" onClick={() => openGroupModal(selectedGroup)}>
                      <Pencil size={14} />
                      {t("rules.edit")}
                    </Button>
                    <Button variant="ghost" disabled={reasserting} onClick={() => handleReassert(selectedGroup)} title={t("ph.reassertTitle")}>
                      <RefreshCw size={14} />
                      {reasserting ? t("ph.reasserting") : t("ph.reassert")}
                    </Button>
                    <Button variant="dangerSubtle" onClick={() => deleteGroup(selectedGroup)}>
                      <Trash2 size={14} />
                      {t("rules.delete")}
                    </Button>
                  </div>
                </div>
              </Card>

              <RuleTable
                title={t("ph.preRules")}
                hint={t("ph.preHint")}
                rulebase="pre"
                rules={preRules}
                hostsById={hostsById}
                confirmingId={confirmingId}
                onAdd={() => openRuleModal("pre", null)}
                onEdit={(r) => openRuleModal(r.rulebase, r)}
                onDelete={deleteRule}
                onMove={moveRule}
                onReorder={(ordered) => reorderRules("pre", ordered)}
                dragDisabled={readOnly}
                onConfirm={handleConfirm}
                onReview={(r, mode) => setReview({ rule: r, mode })}
              />
              <div className="h-4" />
              <RuleTable
                title={t("ph.postRules")}
                hint={t("ph.postHint")}
                rulebase="post"
                rules={postRules}
                hostsById={hostsById}
                confirmingId={confirmingId}
                onAdd={() => openRuleModal("post", null)}
                onEdit={(r) => openRuleModal(r.rulebase, r)}
                onDelete={deleteRule}
                onMove={moveRule}
                onReorder={(ordered) => reorderRules("post", ordered)}
                dragDisabled={readOnly}
                onConfirm={handleConfirm}
                onReview={(r, mode) => setReview({ rule: r, mode })}
              />
            </>
          )}
        </div>
        </fieldset>
      </div>

      <fieldset disabled={readOnly} className="contents">
      {review && (
        <PolicyHierarchyRuleReview
          rule={review.rule}
          mode={review.mode}
          onClose={() => setReview(null)}
          onDeployed={() => { setReview(null); load(); }}
          onChanged={load}
          onRestore={(snapshot) => {
            setReview(null);
            setRuleModal({ editing: review.rule });
            setRuleForm({ ...snapshot, hierarchy_group_id: review.rule.hierarchy_group_id, rulebase: review.rule.rulebase });
          }}
        />
      )}

      {groupModal && (
        <Modal title={groupModal.editing ? t("ph.editGroup") : t("groups.new")} onClose={() => { if (!groupSaving) setGroupModal(null); }}>
          <form onSubmit={saveGroup} className="space-y-4">
            <FieldGroup label={t("hosts.name")}>
              <Input required maxLength={120} value={groupForm.name} onChange={(e) => setGroupForm({ ...groupForm, name: e.target.value })} />
            </FieldGroup>
            <FieldGroup label={t("common.description")}>
              <Input maxLength={2000} value={groupForm.description} onChange={(e) => setGroupForm({ ...groupForm, description: e.target.value })} />
            </FieldGroup>
            <FieldGroup label={t("ph.parentGroup")}>
              <Select
                value={groupForm.parent_id ?? ""}
                onChange={(e) => setGroupForm({ ...groupForm, parent_id: e.target.value || null })}
              >
                <option value="">{t("ph.rootGroup")}</option>
                {(groups ?? [])
                  .filter((g) => !invalidParentIds.has(g.id))
                  .map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
              </Select>
            </FieldGroup>
            {groupError && <p className="text-sm text-red-600">{groupError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="ghost" onClick={() => setGroupModal(null)}>{t("common.cancel")}</Button>
              <Button type="submit" disabled={groupSaving} loading={groupSaving}>{groupSaving ? t("common.saving") : t("common.save")}</Button>
            </div>
          </form>
        </Modal>
      )}

      {ruleModal && ruleForm && (
        <Modal title={ruleModal.editing ? t("ph.editRule") : ruleForm.rulebase === "pre" ? t("ph.newPreRule") : t("ph.newPostRule")} onClose={() => { if (!ruleSaving) setRuleModal(null); }}>
          <form onSubmit={saveRule} className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <FieldGroup label={t("common.description")}>
              <Input required value={ruleForm.description} onChange={(e) => setRuleForm({ ...ruleForm, description: e.target.value })} />
            </FieldGroup>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label="Interface">
                <Input required value={ruleForm.interface} onChange={(e) => setRuleForm({ ...ruleForm, interface: e.target.value })} placeholder="lan" />
              </FieldGroup>
              <FieldGroup label={t("fields.direction")}>
                <Select value={ruleForm.direction} onChange={(e) => setRuleForm({ ...ruleForm, direction: e.target.value as PolicyHierarchyRuleCreate["direction"] })}>
                  <option value="in">{t("rules.dirIn")}</option>
                  <option value="out">{t("rules.dirOut")}</option>
                </Select>
              </FieldGroup>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("nat.protocol")}>
                <Input value={ruleForm.protocol} onChange={(e) => setRuleForm({ ...ruleForm, protocol: e.target.value })} placeholder="tcp" />
              </FieldGroup>
              <FieldGroup label={t("fields.action")}>
                <Select value={ruleForm.action} onChange={(e) => setRuleForm({ ...ruleForm, action: e.target.value as PolicyHierarchyRuleCreate["action"] })}>
                  <option value="pass">{t("rules.actionPass")}</option>
                  <option value="block">{t("rules.actionBlock")}</option>
                  <option value="reject">{t("rules.actionReject")}</option>
                </Select>
              </FieldGroup>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label="Source"><Input value={ruleForm.source_net} onChange={(e) => setRuleForm({ ...ruleForm, source_net: e.target.value })} /></FieldGroup>
              <FieldGroup label={t("nat.sourcePortOptional")}><Input value={ruleForm.source_port ?? ""} onChange={(e) => setRuleForm({ ...ruleForm, source_port: e.target.value || null })} /></FieldGroup>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label="Destination"><Input value={ruleForm.destination_net} onChange={(e) => setRuleForm({ ...ruleForm, destination_net: e.target.value })} /></FieldGroup>
              <FieldGroup label={t("nat.destinationPortOptional")}><Input value={ruleForm.destination_port ?? ""} onChange={(e) => setRuleForm({ ...ruleForm, destination_port: e.target.value || null })} /></FieldGroup>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("nat.ipVersion")}>
                <Select value={ruleForm.ipprotocol} onChange={(e) => setRuleForm({ ...ruleForm, ipprotocol: e.target.value })}>
                  <option value="inet">IPv4</option>
                  <option value="inet6">IPv6</option>
                </Select>
              </FieldGroup>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={ruleForm.enabled} onChange={(e) => setRuleForm({ ...ruleForm, enabled: e.target.checked })} />{t("rules.active")}</label>
            </div>
            {ruleError && <p className="text-sm text-red-600">{ruleError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="ghost" onClick={() => setRuleModal(null)}>{t("common.cancel")}</Button>
              <Button type="submit" disabled={ruleSaving} loading={ruleSaving}>{ruleSaving ? t("common.saving") : t("rules.saveDraft")}</Button>
            </div>
          </form>
        </Modal>
      )}
      </fieldset>
    </div>
  );
}

function GroupTreeNode({
  group,
  depth,
  byParent,
  selectedId,
  onSelect,
}: {
  group: PolicyHierarchyGroup;
  depth: number;
  byParent: Map<string | null, PolicyHierarchyGroup[]>;
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const children = byParent.get(group.id) ?? [];
  return (
    <div>
      <button
        onClick={() => onSelect(group.id)}
        className={`flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-sm transition-colors ${
          selectedId === group.id ? "bg-sky-50 text-sky-700 font-medium" : "text-slate-700 hover:bg-slate-50"
        }`}
        style={{ paddingLeft: `${8 + depth * 16}px` }}
      >
        {depth > 0 && <ChevronRight size={12} className="shrink-0 text-slate-300" />}
        <span className="truncate">{group.name}</span>
      </button>
      {children.map((c) => (
        <GroupTreeNode key={c.id} group={c} depth={depth + 1} byParent={byParent} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </div>
  );
}

function RuleTable({
  title,
  hint,
  rules,
  hostsById,
  confirmingId,
  onAdd,
  onEdit,
  onDelete,
  onMove,
  onReorder,
  dragDisabled,
  onConfirm,
  onReview,
}: {
  title: string;
  hint: string;
  rulebase: PolicyHierarchyRulebase;
  rules: PolicyHierarchyRule[];
  hostsById: Map<string, Host>;
  confirmingId: string | null;
  onAdd: () => void;
  onEdit: (rule: PolicyHierarchyRule) => void;
  onDelete: (rule: PolicyHierarchyRule) => void;
  onMove: (rule: PolicyHierarchyRule, direction: "up" | "down") => void;
  onReorder: (rules: PolicyHierarchyRule[]) => void;
  dragDisabled: boolean;
  onConfirm: (id: string) => void;
  onReview: (rule: PolicyHierarchyRule, mode: "preview" | "history") => void;
}) {
  const { t, ts } = useLanguage();
  const [draggingId, setDraggingId] = useState<string | null>(null);

  function dropOn(targetId: string) {
    if (!draggingId || draggingId === targetId) return setDraggingId(null);
    const from = rules.findIndex((rule) => rule.id === draggingId);
    const to = rules.findIndex((rule) => rule.id === targetId);
    if (from < 0 || to < 0) return setDraggingId(null);
    const reordered = [...rules];
    const [moved] = reordered.splice(from, 1);
    reordered.splice(to, 0, moved);
    setDraggingId(null);
    onReorder(reordered);
  }

  return (
    <Card className="p-5">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold text-slate-800">{title}</h3>
        <Button variant="ghost" onClick={onAdd}>
          <Plus size={14} />
          {t("ph.addRule")}
        </Button>
      </div>
      <p className="mb-3 text-xs text-slate-500">{hint}</p>
      {rules.length === 0 && <p className="py-4 text-sm text-slate-500">{t("ph.noRulesInCategory")}</p>}
      <div className="space-y-3">
        {rules.map((rule, i) => {
          const hasPending = rule.deployments.some((d) => d.status === "pending");
          return (
            <div
              key={rule.id}
              draggable={!dragDisabled}
              onDragStart={(event) => {
                setDraggingId(rule.id);
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", rule.id);
              }}
              onDragEnd={() => setDraggingId(null)}
              onDragOver={(event) => {
                if (!dragDisabled) {
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "move";
                }
              }}
              onDrop={(event) => {
                event.preventDefault();
                dropOn(rule.id);
              }}
              className={`rounded-xl border p-4 transition ${draggingId === rule.id ? "border-sky-400 bg-sky-50 opacity-70" : "border-slate-200"}`}
            >
              <div className="flex flex-col items-start justify-between gap-3 sm:flex-row">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-slate-900">{rule.description}</span>
                    <WorkflowBadge state={rule.workflow_state} />
                  </div>
                  <p className="mt-1 break-words text-sm text-slate-500">
                    {rule.interface} · {rule.direction === "in" ? t("rules.dirIn") : t("rules.dirOut")} · {rule.action} · {rule.protocol} ·{" "}
                    {rule.source_net}{rule.source_port ? `:${rule.source_port}` : ""} → {rule.destination_net}{rule.destination_port ? `:${rule.destination_port}` : ""}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">{t("ph.targetHosts", { count: rule.effective_host_ids.length, names: rule.effective_host_ids.map((id) => hostsById.get(id)?.name ?? id.slice(0, 8)).join(", ") || t("ph.none") })}</p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                  <span className="cursor-grab text-slate-400" title={t("ph.dragTitle")} aria-hidden="true"><GripVertical size={16} /></span>
                  <Button variant="ghost" disabled={i === 0} onClick={() => onMove(rule, "up")} aria-label={t("ph.moveUp")}><ArrowUp size={14} /></Button>
                  <Button variant="ghost" disabled={i === rules.length - 1} onClick={() => onMove(rule, "down")} aria-label={t("ph.moveDown")}><ArrowDown size={14} /></Button>
                  <Button variant="outline" onClick={() => onEdit(rule)} disabled={rule.deployments.some((d) => ["pending", "revert_failed"].includes(d.status))}><Pencil size={14} /></Button>
                  <Button onClick={() => onReview(rule, "preview")}><Rocket size={14} />{t("rules.reviewPush")}</Button>
                  <Button variant="ghost" onClick={() => onReview(rule, "history")} title={t("rules.history")}><History size={14} /></Button>
                  <Button variant="dangerSubtle" onClick={() => onDelete(rule)}><Trash2 size={14} /></Button>
                </div>
              </div>
              {rule.deployments.length > 0 && (
                <div className="mt-3 flex flex-wrap items-end justify-between gap-2 border-t border-slate-100 pt-2">
                  <ul className="min-w-0 space-y-1 text-xs text-slate-500">
                    {rule.deployments.map((d) => (
                      <li key={d.host_id}>
                        {d.host_name}: {d.status}
                        {d.message ? ` — ${ts(d.message)}` : ""}
                      </li>
                    ))}
                  </ul>
                  {hasPending && (
                    <Button className="ml-auto" variant="secondary" onClick={() => onConfirm(rule.id)} disabled={confirmingId === rule.id}>
                      <ShieldCheck size={14} />
                      {confirmingId === rule.id ? t("rules.confirming") : t("rules.confirm")}
                    </Button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
