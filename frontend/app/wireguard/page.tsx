"use client";

import { Tooltip, InfoTip } from "@/components/ui/Tooltip";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { Badge } from "@/components/ui/Badge";
import { WireguardTunnelReview } from "@/components/WireguardTunnelReview";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import {
  api,
  DeviceGroup,
  Host,
  WireguardDeploymentResult,
  WireguardTunnel,
  WireguardTunnelBase,
  WireguardTunnelCreate,
} from "@/lib/api";
import { AlertTriangle, CheckCircle2, History, KeyRound, Pencil, Rocket, Trash2, Plus } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";

import { Loading } from "@/components/ui/Loading";
const emptyForm: WireguardTunnelCreate = {
  server_name: "",
  server_public_key: "",
  server_private_key: "",
  listen_port: "51820",
  server_tunnel_address: "",
  dns: "",
  mtu: "",
  peer_name: "",
  peer_public_key: "",
  peer_psk_enabled: false,
  peer_allowed_ips: "",
  peer_endpoint_address: "",
  peer_endpoint_port: "",
  peer_keepalive: "",
  enabled: true,
  target_host_ids: [],
  target_group_ids: [],
  peer_psk_material: "",
};

export default function WireguardPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("ipsec.loading")}</Loading>}>
      <WireguardContent />
    </Suspense>
  );
}

function WireguardContent() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const router = useRouter();
  const params = useSearchParams();
  const tunnelFilter = params.get("tunnel") ?? "";
  const [tunnels, setTunnels] = useState<WireguardTunnel[] | null>(null);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<WireguardTunnelCreate>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState<WireguardTunnel | null>(null);
  const [review, setReview] = useState<{ tunnel: WireguardTunnel; mode: "preview" | "history" } | null>(null);
  const toast = useToast();

  async function load() {
    try {
      const [tunnelList, hostList, groupList] = await Promise.all([api.listWireguardTunnels(), api.listHosts(), api.listGroups()]);
      setTunnels(tunnelList);
      setHosts(hostList);
      setGroups(groupList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "ipsec.loadFailed"));
    }
  }

  useEffect(() => { load(); }, []);

  function openEditor(tunnel: WireguardTunnel | null, restored?: WireguardTunnelBase) {
    setEditing(tunnel);
    const base = restored ?? tunnel ?? emptyForm;
    setForm({ ...base, server_private_key: "", peer_psk_material: "" });
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
        const { server_private_key, ...rest } = form;
        await api.updateWireguardTunnel(editing.id, server_private_key ? form : rest, editing.updated_at);
        toast.show(t("wg.saved", { name: `${form.server_name} → ${form.peer_name}` }), "success");
      } else {
        await api.createWireguardTunnel(form);
        toast.show(t("wg.created", { name: `${form.server_name} → ${form.peer_name}` }), "success");
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
    if (!confirm(t("wg.deleteConfirm"))) return;
    const target = tunnels?.find((tn) => tn.id === id);
    const name = target ? `${target.server_name} → ${target.peer_name}` : t("ipsec.tunnelFallback");
    try { await api.deleteWireguardTunnel(id); toast.show(t("ipsec.deleted", { name }), "success"); await load(); }
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
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{t("wg.title")}<InfoTip text={t("wg.intro")} size={18} /></h1>
        <Button disabled={readOnly} onClick={() => openEditor(null)}>
          <Plus size={16} />
          {t("ipsec.new")}
        </Button>
      </div>


      {error && <div className="mb-4"><p role="alert" className="text-sm text-red-600">{error}</p><Button variant="ghost" onClick={load}>{t("rules.retry")}</Button></div>}
      {tunnelFilter && <Button variant="ghost" onClick={() => router.push("/wireguard")}>{t("ipsec.showAll")}</Button>}

      <fieldset disabled={readOnly} className="contents">
      <div className="space-y-4">
        {tunnels === null && !error && <Card className="p-6"><Loading>{t("ipsec.loading")}</Loading></Card>}
        {tunnels !== null && tunnels.length === 0 && (
          <EmptyState icon={KeyRound} title={t("wg.none")} action={<Button onClick={() => openEditor(null)}>{t("ipsec.createFirst")}</Button>} />
        )}
        {(tunnelFilter ? tunnels?.filter((tn) => tn.id === tunnelFilter) : tunnels)?.map((tunnel) => (
          <Card key={tunnel.id} className="p-5">
            <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="font-medium text-slate-900">{tunnel.server_name} → {tunnel.peer_name}</h2>
                  <WorkflowBadge state={tunnel.workflow_state} />
                  {!tunnel.server_private_key_set && (
                    <Badge tone="warning">
                      <KeyRound size={12} />{t("wg.noPrivateKey")}
                    </Badge>
                  )}
                  {tunnel.peer_psk_enabled && !tunnel.peer_psk_set && (
                    <Badge tone="warning">
                      <KeyRound size={12} />{t("wg.pskMissing")}
                    </Badge>
                  )}
                </div>
                <p className="mt-1 text-sm text-slate-500">
                  {t("wg.summary", { address: tunnel.server_tunnel_address, port: tunnel.listen_port, ips: tunnel.peer_allowed_ips })}
                  {tunnel.peer_psk_enabled ? t("wg.peerPskActive") : ""}
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
      {review && <WireguardTunnelReview
        tunnel={review.tunnel} mode={review.mode}
        onClose={() => setReview(null)}
        onDeployed={() => { setReview(null); load(); }}
        onChanged={load}
        onRestore={(snapshot) => openEditor(review.tunnel, snapshot)}
      />}

      {showModal && (
        <Modal title={editing ? t("ipsec.editTitle") : t("wg.newTitle")} onClose={() => setShowModal(false)}>
          <form onSubmit={handleSubmit} className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{t("wg.serverSection")}</p>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("wg.interfaceName")}>
                <Input required value={form.server_name} onChange={(e) => setForm({ ...form, server_name: e.target.value })} placeholder="fw1-wg0" />
              </FieldGroup>
              <FieldGroup label={t("wg.listenPort")}>
                <Input required value={form.listen_port} onChange={(e) => setForm({ ...form, listen_port: e.target.value })} placeholder="51820" />
              </FieldGroup>
            </div>
            <FieldGroup label={t("ovpn.tunnelNetwork")}>
              <Input required value={form.server_tunnel_address} onChange={(e) => setForm({ ...form, server_tunnel_address: e.target.value })} placeholder="10.9.0.1/24" />
            </FieldGroup>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("wg.dns")}>
                <Input value={form.dns} onChange={(e) => setForm({ ...form, dns: e.target.value })} placeholder={t("wg.dnsPlaceholder")} />
              </FieldGroup>
              <FieldGroup label={t("wg.mtu")}>
                <Input value={form.mtu} onChange={(e) => setForm({ ...form, mtu: e.target.value })} placeholder={t("wg.mtuPlaceholder")} />
              </FieldGroup>
            </div>
            <FieldGroup label={t("wg.serverPublicKey")}>
              <Input required value={form.server_public_key} onChange={(e) => setForm({ ...form, server_public_key: e.target.value })} placeholder={t("wg.serverPublicKeyPlaceholder")} />
            </FieldGroup>
            <FieldGroup label={editing ? t("wg.serverPrivateKeyEditing", { state: editing.server_private_key_set ? t("ipsec.pskSet") : t("ipsec.pskNotSet") }) : t("wg.serverPrivateKey")}>
              <Input
                type="password"
                required={!editing}
                value={form.server_private_key ?? ""}
                onChange={(e) => setForm({ ...form, server_private_key: e.target.value })}
                placeholder={editing ? t("ipsec.pskKeep") : t("wg.privateKeyPlaceholder")}
                autoComplete="new-password"
              />
            </FieldGroup>
            <p className="-mt-2 text-xs text-slate-500">
              {t("wg.keyNote", { cmd: "wg genkey" })}
            </p>

            <p className="pt-2 text-xs font-medium uppercase tracking-wide text-slate-400">{t("wg.peerSection")}</p>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("wg.peerName")}>
                <Input required value={form.peer_name} onChange={(e) => setForm({ ...form, peer_name: e.target.value })} placeholder="fw2-peer" />
              </FieldGroup>
              <FieldGroup label={t("wg.allowedIps")}>
                <Input required value={form.peer_allowed_ips} onChange={(e) => setForm({ ...form, peer_allowed_ips: e.target.value })} placeholder="10.9.0.2/32" />
              </FieldGroup>
            </div>
            <FieldGroup label={t("wg.peerPublicKey")}>
              <Input required value={form.peer_public_key} onChange={(e) => setForm({ ...form, peer_public_key: e.target.value })} placeholder={t("wg.peerPublicKeyPlaceholder")} />
            </FieldGroup>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("wg.endpointAddress")}>
                <Input value={form.peer_endpoint_address} onChange={(e) => setForm({ ...form, peer_endpoint_address: e.target.value })} placeholder={t("wg.endpointAddressPlaceholder")} />
              </FieldGroup>
              <FieldGroup label={t("wg.endpointPort")}>
                <Input value={form.peer_endpoint_port} onChange={(e) => setForm({ ...form, peer_endpoint_port: e.target.value })} placeholder="51820" />
              </FieldGroup>
            </div>
            <FieldGroup label={t("wg.keepalive")}>
              <Input value={form.peer_keepalive} onChange={(e) => setForm({ ...form, peer_keepalive: e.target.value })} placeholder={t("wg.keepalivePlaceholder")} />
            </FieldGroup>

            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.peer_psk_enabled} onChange={(e) => setForm({ ...form, peer_psk_enabled: e.target.checked })} />
              {t("wg.pskLabel")}
            </label>
            {form.peer_psk_enabled && <FieldGroup label={editing ? t("wg.peerPskEditing", { state: editing.peer_psk_set ? t("ipsec.pskSet") : t("ipsec.pskNotSet") }) : t("wg.peerPsk")}>
              <Input
                type="password"
                value={form.peer_psk_material ?? ""}
                onChange={(e) => setForm({ ...form, peer_psk_material: e.target.value })}
                placeholder={editing ? t("ipsec.pskKeep") : t("wg.pskPlaceholder")}
                autoComplete="new-password"
              />
            </FieldGroup>}

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

function DeploymentRow({ deployment }: { deployment: WireguardDeploymentResult }) {
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
