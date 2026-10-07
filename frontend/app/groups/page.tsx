"use client";

import { InfoTip } from "@/components/ui/Tooltip";
import { Suspense, useEffect, useState } from "react";
import { api, DeviceGroup, GroupWrite, GroupImpact, Host, PolicyAlias, Rule } from "@/lib/api";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { Layers, Plus, Pencil, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";

import { Loading } from "@/components/ui/Loading";
const empty: GroupWrite = { name: "", description: "", host_ids: [] };

export default function GroupsPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading className="py-8">{t("groups.loading")}</Loading>}>
      <GroupsContent />
    </Suspense>
  );
}

function GroupsContent() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const toast = useToast();
  const router = useRouter();
  const params = useSearchParams();
  const groupFilter = params.get("group") ?? "";
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [aliases, setAliases] = useState<PolicyAlias[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState<DeviceGroup | null>(null);
  const [form, setForm] = useState<GroupWrite>(empty);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [impact, setImpact] = useState<GroupImpact | null>(null);
  const [saved, setSaved] = useState<{ id: string; name: string } | null>(null);
  const [search, setSearch] = useState("");
  async function load() {
    try {
      const [g, h, r, a] = await Promise.all([api.listGroups(), api.listHosts(), api.listRules(), api.listAliases()]);
      setGroups(g); setHosts(h); setRules(r); setAliases(a); setError(null);
    } catch (e) { setError(errorMessage(e, "groups.loadFailed")); }
    finally { setLoading(false); }
  }
  useEffect(() => { load(); }, []);
  function edit(group: DeviceGroup | null) { setEditing(group); setForm(group ?? empty); setFormError(null); setImpact(null); setOpen(true); }
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setFormError(null);
    try {
      if (editing && !impact) { setImpact(await api.previewGroup(editing.id, form)); return; }
      const result = editing && impact ? await api.updateGroup(editing.id, form, impact.token) : await api.createGroup(form);
      setSaved({ id: result.id, name: result.name });
      setOpen(false); await load();
    } catch (e) { setFormError(errorMessage(e, "common.saveFailed")); setImpact(null); }
    finally { setBusy(false); }
  }
  async function remove(group: DeviceGroup) {
    if (!confirm(t("groups.deleteConfirm", { name: group.name }))) return;
    try { await api.deleteGroup(group.id); await load(); toast.show(t("groups.deleted", { name: group.name }), "success"); }
    catch (e) { setError(errorMessage(e, "common.deleteFailed")); }
  }
  const selectedGroup = groups.find(g => g.id === groupFilter);
  const visibleGroups = groups.filter(g => (!groupFilter || g.id === groupFilter) && `${g.name} ${g.description}`.toLowerCase().includes(search.toLowerCase()));
  return <div>
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3"><h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{selectedGroup ? t("groups.titleNamed", { name: selectedGroup.name }) : t("groups.title")}<InfoTip text={t("groups.subtitle")} size={18} /></h1><Button disabled={readOnly} onClick={() => edit(null)}><Plus size={16} />{t("groups.new")}</Button></div>
    {!loading && !error && <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-4">
      {[ [t("groups.statGroups"), groups.length], [t("groups.statHosts"), new Set(groups.flatMap(g => g.host_ids)).size], [t("groups.statRules"), rules.filter(r => r.target_group_ids.length > 0).length], [t("groups.statAliases"), aliases.filter(a => a.target_group_ids.length > 0).length] ].map(([label, count]) => <Card key={label} className="p-4"><p className="text-sm text-slate-500">{label}</p><p className="mt-1 text-2xl font-semibold text-slate-800">{count}</p></Card>)}
    </div>}
    {saved && <p role="status" className="mb-4 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{t("groups.savedNote", { name: saved.name })}<Link className="font-medium underline" href={`/rules?group=${saved.id}`}>{t("groups.checkRules")}</Link></p>}
    {error && <div className="mb-4"><p role="alert" className="text-sm text-red-600">{error}</p><Button variant="ghost" onClick={load}>{t("rules.retry")}</Button></div>}
    {groupFilter && <Button variant="ghost" className="mb-3" onClick={() => router.push("/groups")}>{t("groups.showAll")}</Button>}
    <Input aria-label={t("groups.searchLabel")} placeholder={t("groups.searchPlaceholder")} value={search} onChange={e => setSearch(e.target.value)} />
    {loading && <Loading className="py-8">{t("groups.loading")}</Loading>}
    {!loading && !error && groups.length === 0 && <Card className="mt-4 p-8 text-center"><Layers className="mx-auto mb-3 text-sky-500" /><p className="font-medium">{t("groups.emptyTitle")}</p><p className="mt-2 text-sm text-slate-500">{t("groups.emptyText")}</p><Button className="mt-4" disabled={readOnly} onClick={() => edit(null)}>{t("groups.createFirst")}</Button></Card>}
    {!loading && !error && groups.length > 0 && visibleGroups.length === 0 && <Card className="mt-4 p-6 text-center"><p className="mb-3 text-sm text-slate-600">{t("groups.noMatch")}</p><Button variant="ghost" onClick={() => setSearch("")}>{t("groups.resetSearch")}</Button></Card>}
    <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
      {visibleGroups.map(group => {
        const assigned = rules.filter(r => r.target_group_ids.includes(group.id));
        const assignedAliases = aliases.filter(a => a.target_group_ids.includes(group.id));
        const inUse = assigned.length > 0 || assignedAliases.length > 0;
        return <Card key={group.id} className="p-5">
          <div className="flex items-start justify-between gap-2"><h2 className="text-lg font-semibold text-slate-800">{group.name}</h2><div className="flex gap-1"><Button variant="ghost" disabled={readOnly} onClick={() => edit(group)} aria-label={t("groups.editAria", { name: group.name })}><Pencil size={16} /></Button><Button variant="ghost" disabled={readOnly || inUse} onClick={() => remove(group)} aria-label={t("groups.deleteAria", { name: group.name })} title={inUse ? t("groups.deleteInUse") : t("groups.deleteTitle")}><Trash2 size={16} /></Button></div></div>
          {group.description && <p className="mt-1 text-sm text-slate-500">{group.description}</p>}
          <div className="my-4 flex flex-wrap gap-2">{group.host_ids.map(id => <Link key={id} href={`/hosts/${id}`} className="rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-700">{hosts.find(h => h.id === id)?.name ?? id.slice(0, 8)}</Link>)}{group.host_ids.length === 0 && <Button variant="ghost" disabled={readOnly} onClick={() => edit(group)}>{t("groups.assignHosts")}</Button>}</div>
          <div className="space-y-3 border-t border-slate-100 pt-3">
            <Link href={`/rules?group=${group.id}`} className="text-sm font-medium text-sky-700">{t("groups.centralRules", { count: assigned.length })}</Link>
            {assigned.map(rule => <Link key={rule.id} href={`/rules?group=${group.id}&rule=${rule.id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 p-2 text-sm text-slate-700"><span>{rule.description}</span><WorkflowBadge state={rule.workflow_state} /></Link>)}
            {assigned.length === 0 && <p className="text-sm text-slate-500">{t("groups.onlyHosts")}</p>}
            <Link href={`/rules?group=${group.id}&new=1`} className="inline-flex text-sm font-medium text-sky-700 underline">{t("groups.createRule")}</Link>
            <Link href={`/aliases?group=${group.id}`} className="block text-sm font-medium text-sky-700">{t("groups.centralAliases", { count: assignedAliases.length })}</Link>
            {assignedAliases.map(alias => <Link key={alias.id} href={`/aliases?group=${group.id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 p-2 text-sm text-slate-700"><span className="font-mono">{alias.name}</span><WorkflowBadge state={alias.workflow_state} /></Link>)}
            <Link href={`/aliases?group=${group.id}&new=1`} className="inline-flex text-sm font-medium text-sky-700 underline">{t("groups.createAlias")}</Link>
          </div>
        </Card>;
      })}
    </div>
    {open && <Modal title={editing ? t("groups.editTitle") : t("groups.newTitle")} onClose={() => { if (!busy) setOpen(false); }}><form onSubmit={save} className="space-y-4">
      <fieldset disabled={busy || !!impact} className="space-y-4 disabled:opacity-60">
      <FieldGroup label={t("hosts.name")}><Input required maxLength={120} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder={t("groups.namePlaceholder")} /></FieldGroup>
      <FieldGroup label={t("common.description")}><Input maxLength={2000} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} placeholder={t("groups.descPlaceholder")} /></FieldGroup>
      <FieldGroup label={t("groups.members")}><div className="space-y-2 rounded-lg border border-slate-200 p-3">{hosts.map(h => <label key={h.id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.host_ids.includes(h.id)} onChange={() => setForm(f => ({ ...f, host_ids: f.host_ids.includes(h.id) ? f.host_ids.filter(id => id !== h.id) : [...f.host_ids, h.id] }))} />{h.name}{!h.enabled && <span className="text-slate-400">{t("groups.disabled")}</span>}</label>)}{hosts.length === 0 && <p className="text-sm text-slate-500">{t("groups.createHostsFirst")}</p>}</div></FieldGroup>
      </fieldset>
      <p className="rounded-lg bg-sky-50 p-3 text-sm text-sky-800">{t("groups.membershipNote")}</p>
      {impact && <div className="space-y-3 rounded-lg border border-slate-200 p-3 text-sm">
        <h3 className="font-semibold text-slate-800">{t("groups.impactTitle")}</h3>
        <p className="text-emerald-700">{t("groups.added", { names: impact.added_members.map(h => h.name).join(", ") || t("groups.noMembers") })}</p>
        <p className="text-flame-800">{t("groups.removed", { names: impact.removed_members.map(h => h.name).join(", ") || t("groups.noMembers") })}</p>
        {impact.rules.length === 0 && impact.aliases.length === 0 && (impact.nat ?? []).length === 0 && <p className="text-slate-500">{t("groups.impactEmpty")}</p>}
        {impact.rules.map(rule => <div key={rule.rule_id} className="border-t border-slate-100 pt-2"><strong>{rule.description}</strong><p>{t("groups.newRuleTargets", { added: rule.added_host_ids.length, removed: rule.removed_host_ids.length })}</p>{rule.retained_host_ids.length > 0 && <p className="mt-1 text-flame-800">{t("groups.retainedCopies", { count: rule.retained_host_ids.length })}</p>}</div>)}
        {impact.aliases.map(alias => <div key={alias.alias_id} className="border-t border-slate-100 pt-2"><strong>{t("groups.aliasPrefix", { name: alias.description })}</strong><p>{t("groups.newTargets", { added: alias.added_host_ids.length, removed: alias.removed_host_ids.length })}</p>{alias.retained_host_ids.length > 0 && <p className="mt-1 text-flame-800">{t("groups.retainedCopies", { count: alias.retained_host_ids.length })}</p>}</div>)}
        {(impact.nat ?? []).map(n => <div key={n.id} className="border-t border-slate-100 pt-2"><strong>{t("groups.natPrefix", { name: n.description })}</strong><p>{t("groups.newTargets", { added: n.added_host_ids.length, removed: n.removed_host_ids.length })}</p>{n.retained_host_ids.length > 0 && <p className="text-flame-800">{t("groups.natRetained", { count: n.retained_host_ids.length })}</p>}</div>)}
        <p className="text-slate-500">{t("groups.otherGroupNote")}</p>
        <Button type="button" variant="ghost" onClick={() => setImpact(null)} disabled={busy}>{t("groups.changeSelection")}</Button>
      </div>}
      {formError && <p role="alert" className="text-sm text-red-600">{formError}</p>}
      <Button type="submit" disabled={busy || readOnly} loading={busy}>{busy ? t("groups.wait") : editing && !impact ? t("groups.checkImpact") : t("groups.save")}</Button>
    </form></Modal>}
  </div>;
}
