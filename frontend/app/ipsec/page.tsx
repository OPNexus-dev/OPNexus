"use client";

import { Tooltip, InfoTip } from "@/components/ui/Tooltip";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { Badge } from "@/components/ui/Badge";
import { IpsecTunnelReview } from "@/components/IpsecTunnelReview";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import {
  api,
  DeviceGroup,
  Host,
  IkeVersion,
  IpsecDeploymentResult,
  IpsecTunnel,
  IpsecTunnelBase,
  IpsecTunnelCreate,
} from "@/lib/api";
import { AlertTriangle, CheckCircle2, History, KeyRound, Pencil, Rocket, Trash2, Plus } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";

import { Loading } from "@/components/ui/Loading";
const emptyForm: IpsecTunnelCreate = {
  description: "",
  local_address: "",
  remote_address: "",
  ike_version: "ikev2",
  proposal: "default",
  local_identity: "",
  remote_identity: "",
  local_networks: "",
  remote_networks: "",
  esp_proposal: "default",
  enabled: true,
  target_host_ids: [],
  target_group_ids: [],
  pre_shared_key: "",
};

export default function IpsecPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("ipsec.loading")}</Loading>}>
      <IpsecContent />
    </Suspense>
  );
}

function IpsecContent() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const router = useRouter();
  const params = useSearchParams();
  const tunnelFilter = params.get("tunnel") ?? "";
  const [tunnels, setTunnels] = useState<IpsecTunnel[] | null>(null);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<IpsecTunnelCreate>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState<IpsecTunnel | null>(null);
  const [review, setReview] = useState<{ tunnel: IpsecTunnel; mode: "preview" | "history" } | null>(null);
  const toast = useToast();

  async function load() {
    try {
      const [tunnelList, hostList, groupList] = await Promise.all([api.listIpsecTunnels(), api.listHosts(), api.listGroups()]);
      setTunnels(tunnelList);
      setHosts(hostList);
      setGroups(groupList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "ipsec.loadFailed"));
    }
  }

  useEffect(() => { load(); }, []);

  function openEditor(tunnel: IpsecTunnel | null, restored?: IpsecTunnelBase) {
    setEditing(tunnel);
    const base = restored ?? tunnel ?? emptyForm;
    setForm({ ...base, pre_shared_key: "" });
    setFormError(null);
    setReview(null);
    setShowModal(true);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      if (editing) {
        const { pre_shared_key, ...rest } = form;
        await api.updateIpsecTunnel(editing.id, pre_shared_key ? form : rest, editing.updated_at);
        toast.show(t("ipsec.saved", { name: form.description }), "success");
      } else {
        if (!form.pre_shared_key) { setFormError(t("ipsec.pskRequired")); setSaving(false); return; }
        await api.createIpsecTunnel(form);
        toast.show(t("ipsec.created", { name: form.description }), "success");
      }
      setShowModal(false);
      setForm(emptyForm);
      await load();
    } catch (err) {
      setFormError(errorMessage(err, "ipsec.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm(t("ipsec.deleteConfirm"))) return;
    const name = tunnels?.find((tn) => tn.id === id)?.description ?? t("ipsec.tunnelFallback");
    try { await api.deleteIpsecTunnel(id); toast.show(t("ipsec.deleted", { name }), "success"); await load(); }
    catch (e) { setError(errorMessage(e, "common.deleteFailed")); }
  }

  function toggleTarget(id: string) {
    setForm((f) => ({
      ...f,
      target_host_ids: f.target_host_ids.includes(id) ? f.target_host_ids.filter((x) => x !== id) : [...f.target_host_ids, id],
    }));
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{t("ipsec.title")}<InfoTip text={t("ipsec.intro")} size={18} /></h1>
        <Button disabled={readOnly} onClick={() => openEditor(null)}>
          <Plus size={16} />
          {t("ipsec.new")}
        </Button>
      </div>


      {error && <div className="mb-4"><p role="alert" className="text-sm text-red-600">{error}</p><Button variant="ghost" onClick={load}>{t("rules.retry")}</Button></div>}
      {tunnelFilter && <Button variant="ghost" onClick={() => router.push("/ipsec")}>{t("ipsec.showAll")}</Button>}

      <fieldset disabled={readOnly} className="contents">
      <div className="space-y-4">
        {tunnels === null && !error && <Card className="p-6"><Loading>{t("ipsec.loading")}</Loading></Card>}
        {tunnels !== null && tunnels.length === 0 && (
          <EmptyState icon={KeyRound} title={t("ipsec.none")} action={<Button onClick={() => openEditor(null)}>{t("ipsec.createFirst")}</Button>} />
        )}
        {(tunnelFilter ? tunnels?.filter((tn) => tn.id === tunnelFilter) : tunnels)?.map((tunnel) => (
          <Card key={tunnel.id} className="p-5">
            <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="font-medium text-slate-900">{tunnel.description}</h3>
                  <WorkflowBadge state={tunnel.workflow_state} />
                  {!tunnel.pre_shared_key_set && (
                    <Badge tone="warning">
                      <KeyRound size={12} />{t("ipsec.noPsk")}
                    </Badge>
                  )}
                </div>
                <p className="mt-1 text-sm text-slate-500">
                  {tunnel.local_address} ↔ {tunnel.remote_address} · {tunnel.ike_version.toUpperCase()} ·{" "}
                  {tunnel.local_networks} ⇄ {tunnel.remote_networks}
                </p>
                <p className="mt-2 text-xs text-slate-500">
                  {t("rules.targets", { active: tunnel.workflow_counts.active_targets, total: tunnel.workflow_counts.targets })}
                  {tunnel.workflow_counts.retained > 0 ? t("rules.retainedTargets", { count: tunnel.workflow_counts.retained }) : ""}
                  {!tunnel.enabled ? t("ipsec.disabledSuffix") : ""}
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {tunnel.target_group_ids.map((id) => <span key={id} className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs text-blue-800">{t("rules.groupTag", { name: groups.find(g => g.id === id)?.name ?? id.slice(0, 8) })}</span>)}
                  {tunnel.target_host_ids.map((id) => <span key={id} className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">{hosts.find(h => h.id === id)?.name ?? id.slice(0, 8)}</span>)}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => openEditor(tunnel)}><Pencil size={14} />{t("rules.edit")}</Button>
                <Button onClick={() => setReview({ tunnel, mode: "preview" })}><Rocket size={14} />{t("rules.reviewPush")}</Button>
                <Tooltip content={t("ipsec.historyTitle")}><Button variant="ghost" onClick={() => setReview({ tunnel, mode: "history" })} aria-label={t("ipsec.historyTitle")}><History size={14} /></Button></Tooltip>
                <Button variant="dangerSubtle" onClick={() => handleDelete(tunnel.id)}><Trash2 size={14} />{t("rules.delete")}</Button>
              </div>
            </div>

            {tunnel.deployments.length > 0 && (
              <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-slate-100 pt-3">
                <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{t("rules.deployResult")}</p>
                <ul className="flex flex-wrap gap-x-5 gap-y-1.5">
                  {tunnel.deployments.map((d) => <DeploymentRow key={d.host_id} deployment={d} />)}
                </ul>
              </div>
            )}
          </Card>
        ))}
      </div>
      </fieldset>

      <fieldset disabled={readOnly} className="contents">
      {review && <IpsecTunnelReview
        tunnel={review.tunnel} mode={review.mode}
        onClose={() => setReview(null)}
        onDeployed={() => { setReview(null); load(); }}
        onChanged={load}
        onRestore={(snapshot) => openEditor(review.tunnel, snapshot)}
      />}

      {showModal && (
        <Modal title={editing ? t("ipsec.editTitle") : t("ipsec.newTitle")} onClose={() => setShowModal(false)}>
          <form onSubmit={handleSubmit} className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <FieldGroup label={t("common.description")}>
              <Input required value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder={t("ipsec.descPlaceholder")} />
            </FieldGroup>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("ipsec.localAddress")}>
                <Input required value={form.local_address} onChange={(e) => setForm({ ...form, local_address: e.target.value })} placeholder={t("ipsec.localAddressPlaceholder")} />
              </FieldGroup>
              <FieldGroup label={t("ipsec.remoteAddress")}>
                <Input required value={form.remote_address} onChange={(e) => setForm({ ...form, remote_address: e.target.value })} placeholder="198.51.100.1" />
              </FieldGroup>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <FieldGroup label={t("ipsec.ikeVersion")}>
                <Select value={form.ike_version} onChange={(e) => setForm({ ...form, ike_version: e.target.value as IkeVersion })}>
                  <option value="ikev2">IKEv2</option>
                  <option value="ikev1">IKEv1</option>
                  <option value="ike">IKEv1+IKEv2</option>
                </Select>
              </FieldGroup>
              <FieldGroup label={t("ipsec.proposal")}>
                <Input value={form.proposal} onChange={(e) => setForm({ ...form, proposal: e.target.value })} placeholder="default" />
              </FieldGroup>
              <FieldGroup label={t("ipsec.espProposal")}>
                <Input value={form.esp_proposal} onChange={(e) => setForm({ ...form, esp_proposal: e.target.value })} placeholder="default" />
              </FieldGroup>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("ipsec.localIdentity")}>
                <Input required value={form.local_identity} onChange={(e) => setForm({ ...form, local_identity: e.target.value })} placeholder={t("ipsec.localIdentityPlaceholder")} />
              </FieldGroup>
              <FieldGroup label={t("ipsec.remoteIdentity")}>
                <Input value={form.remote_identity} onChange={(e) => setForm({ ...form, remote_identity: e.target.value })} placeholder={t("ipsec.remoteIdentityPlaceholder")} />
              </FieldGroup>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("ipsec.localNetwork")}>
                <Input required value={form.local_networks} onChange={(e) => setForm({ ...form, local_networks: e.target.value })} placeholder="10.10.0.0/24" />
              </FieldGroup>
              <FieldGroup label={t("ipsec.remoteNetwork")}>
                <Input required value={form.remote_networks} onChange={(e) => setForm({ ...form, remote_networks: e.target.value })} placeholder="10.20.0.0/24" />
              </FieldGroup>
            </div>

            <FieldGroup label={editing ? t("ipsec.pskEditing", { state: editing.pre_shared_key_set ? t("ipsec.pskSet") : t("ipsec.pskNotSet") }) : t("ipsec.psk")}>
              <Input
                type="password"
                required={!editing}
                value={form.pre_shared_key}
                onChange={(e) => setForm({ ...form, pre_shared_key: e.target.value })}
                placeholder={editing ? t("ipsec.pskKeep") : t("ipsec.pskPlaceholder")}
                autoComplete="new-password"
              />
            </FieldGroup>

            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />{t("ipsec.active")}</label>

            <FieldGroup label={t("rules.deviceGroups")}>
              <div className="space-y-2 rounded-lg border border-slate-200 p-3">
                {groups.length === 0 && <p className="text-sm text-slate-500">{t("rules.createGroupsHint")}</p>}
                {groups.map((group) => (
                  <label key={group.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.target_group_ids.includes(group.id)}
                      onChange={() => setForm((f) => ({
                        ...f,
                        target_group_ids: f.target_group_ids.includes(group.id) ? f.target_group_ids.filter((id) => id !== group.id) : [...f.target_group_ids, group.id],
                      }))}
                    />
                    {t("rules.groupHosts", { name: group.name, count: group.host_ids.length })}
                  </label>
                ))}
              </div>
            </FieldGroup>
            <FieldGroup label={t("rules.additionalTargets")}>
              <div className="space-y-1.5 rounded-lg border border-slate-200 p-3">
                {hosts.length === 0 && <p className="text-sm text-slate-400">{t("rules.noHosts")}</p>}
                {hosts.map((host) => (
                  <label key={host.id} className="flex items-center gap-2 text-sm text-slate-700">
                    <input type="checkbox" checked={form.target_host_ids.includes(host.id)} onChange={() => toggleTarget(host.id)} />
                    {host.name}
                  </label>
                ))}
              </div>
            </FieldGroup>

            {formError && <p className="text-sm text-red-600">{formError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="ghost" onClick={() => setShowModal(false)}>{t("common.cancel")}</Button>
              <Button type="submit" disabled={saving} loading={saving}>{saving ? t("common.saving") : t("rules.saveDraft")}</Button>
            </div>
          </form>
        </Modal>
      )}
      </fieldset>
    </div>
  );
}

function DeploymentRow({ deployment }: { deployment: IpsecDeploymentResult }) {
  const { ts } = useLanguage();
  const d = deployment;
  return (
    <li className="flex items-center gap-2 text-sm">
      {d.status === "success" ? <CheckCircle2 size={15} className="text-emerald-500" /> : <AlertTriangle size={15} className="text-red-500" />}
      <span className="font-medium text-slate-700">{d.host_name}</span>
      {d.message && <span className="text-slate-400">— {ts(d.message)}</span>}
    </li>
  );
}
