"use client";

import { Tooltip, InfoTip } from "@/components/ui/Tooltip";
import { WorkflowBadge } from "@/components/WorkflowBadge";
import { Badge } from "@/components/ui/Badge";
import { OpenVpnInstanceReview } from "@/components/OpenVpnInstanceReview";
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
  HostCa,
  HostCertificate,
  OpenVpnDeploymentResult,
  OpenVpnInstance,
  OpenVpnInstanceBase,
  OpenVpnInstanceCreate,
  OpenVpnProto,
  OpenVpnRole,
  OpenVpnTlsKeyMode,
} from "@/lib/api";
import { AlertTriangle, CheckCircle2, History, KeyRound, Pencil, Rocket, Trash2, Plus } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";

import { Loading } from "@/components/ui/Loading";
const emptyForm: OpenVpnInstanceCreate = {
  description: "",
  role: "server",
  proto: "udp",
  port: "1194",
  local: "",
  remote: "",
  server_network: "",
  cert_refs: {},
  ca_refs: {},
  tls_key_enabled: false,
  tls_key_mode: "crypt",
  enabled: true,
  target_host_ids: [],
  target_group_ids: [],
  tls_key_material: "",
};

interface HostTrustStore {
  certs: HostCertificate[];
  cas: HostCa[];
  loading: boolean;
  error: string | null;
}

export default function OpenVpnPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("ovpn.loading")}</Loading>}>
      <OpenVpnContent />
    </Suspense>
  );
}

function OpenVpnContent() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const router = useRouter();
  const params = useSearchParams();
  const instanceFilter = params.get("instance") ?? "";
  const [instances, setInstances] = useState<OpenVpnInstance[] | null>(null);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<OpenVpnInstanceCreate>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState<OpenVpnInstance | null>(null);
  const [review, setReview] = useState<{ instance: OpenVpnInstance; mode: "preview" | "history" } | null>(null);
  const toast = useToast();
  const [trustStores, setTrustStores] = useState<Record<string, HostTrustStore>>({});

  // Every target host, resolved from direct picks + members of picked
  // groups — a cert/CA picker fundamentally needs one row per host whose
  // Trust Store it's actually reading, so this list drives that UI.
  const resolvedHostIds = Array.from(new Set([
    ...form.target_host_ids,
    ...form.target_group_ids.flatMap((gid) => groups.find((g) => g.id === gid)?.host_ids ?? []),
  ]));

  useEffect(() => {
    for (const hostId of resolvedHostIds) {
      if (trustStores[hostId]) continue;
      setTrustStores((prev) => ({ ...prev, [hostId]: { certs: [], cas: [], loading: true, error: null } }));
      Promise.all([api.getHostCertificates(hostId), api.getHostCas(hostId)])
        .then(([certs, cas]) => setTrustStores((prev) => ({ ...prev, [hostId]: { certs, cas, loading: false, error: null } })))
        .catch((e) => setTrustStores((prev) => ({
          ...prev, [hostId]: { certs: [], cas: [], loading: false, error: errorMessage(e, "ovpn.trustLoadFailed") },
        })));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedHostIds.join(",")]);

  function setCertRef(hostId: string, uuid: string) {
    setForm((f) => ({ ...f, cert_refs: { ...f.cert_refs, [hostId]: uuid } }));
  }
  function setCaRef(hostId: string, uuid: string) {
    setForm((f) => ({ ...f, ca_refs: { ...f.ca_refs, [hostId]: uuid } }));
  }

  async function load() {
    try {
      const [instanceList, hostList, groupList] = await Promise.all([api.listOpenVpnInstances(), api.listHosts(), api.listGroups()]);
      setInstances(instanceList);
      setHosts(hostList);
      setGroups(groupList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "ovpn.loadFailed"));
    }
  }

  useEffect(() => { load(); }, []);

  function openEditor(instance: OpenVpnInstance | null, restored?: OpenVpnInstanceBase) {
    setEditing(instance);
    const base = restored ?? instance ?? emptyForm;
    setForm({ ...base, tls_key_material: "" });
    setFormError(null);
    setReview(null);
    setShowModal(true);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      const { tls_key_material, ...rest } = form;
      if (editing) {
        await api.updateOpenVpnInstance(editing.id, tls_key_material ? form : rest, editing.updated_at);
        toast.show(t("ovpn.saved", { name: form.description }), "success");
      } else {
        await api.createOpenVpnInstance(tls_key_material ? form : rest);
        toast.show(t("ovpn.created", { name: form.description }), "success");
      }
      setShowModal(false);
      setForm(emptyForm);
      await load();
    } catch (err) {
      setFormError(errorMessage(err, "ovpn.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm(t("ovpn.deleteConfirm"))) return;
    const name = instances?.find((i) => i.id === id)?.description ?? t("ovpn.instanceFallback");
    try { await api.deleteOpenVpnInstance(id); toast.show(t("ovpn.deleted", { name }), "success"); await load(); }
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
        <h1 className="text-2xl font-semibold text-slate-900 flex items-center gap-2">{t("ovpn.title")}<InfoTip text={t("ovpn.intro")} size={18} /></h1>
        <Button disabled={readOnly} onClick={() => openEditor(null)}>
          <Plus size={16} />
          {t("ovpn.new")}
        </Button>
      </div>


      {error && <div className="mb-4"><p role="alert" className="text-sm text-red-600">{error}</p><Button variant="ghost" onClick={load}>{t("rules.retry")}</Button></div>}
      {instanceFilter && <Button variant="ghost" onClick={() => router.push("/openvpn")}>{t("ovpn.showAll")}</Button>}

      <fieldset disabled={readOnly} className="contents">
      <div className="space-y-4">
        {instances === null && !error && <Card className="p-6"><Loading>{t("ovpn.loading")}</Loading></Card>}
        {instances !== null && instances.length === 0 && (
          <EmptyState icon={KeyRound} title={t("ovpn.none")} action={<Button onClick={() => openEditor(null)}>{t("ovpn.createFirst")}</Button>} />
        )}
        {(instanceFilter ? instances?.filter((i) => i.id === instanceFilter) : instances)?.map((instance) => (
          <Card key={instance.id} className="p-5">
            <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="font-medium text-slate-900">{instance.description}</h2>
                  <WorkflowBadge state={instance.workflow_state} />
                  {instance.tls_key_enabled && !instance.tls_key_material_set && (
                    <Badge tone="warning">
                      <KeyRound size={12} />{t("ovpn.tlsKeyMissing")}
                    </Badge>
                  )}
                </div>
                <p className="mt-1 text-sm text-slate-500">
                  {instance.role === "server" ? t("ovpn.roleServer") : t("ovpn.roleClient")} · {instance.proto.toUpperCase()}:{instance.port} ·{" "}
                  {instance.role === "server" ? instance.server_network : instance.remote}
                  {instance.tls_key_enabled ? t("ovpn.tlsHardening", { mode: instance.tls_key_mode }) : ""}
                </p>
                <p className="mt-2 text-xs text-slate-500">
                  {t("rules.targets", { active: instance.workflow_counts.active_targets, total: instance.workflow_counts.targets })}
                  {instance.workflow_counts.retained > 0 ? t("rules.retainedTargets", { count: instance.workflow_counts.retained }) : ""}
                  {!instance.enabled ? t("ovpn.disabledSuffix") : ""}
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {instance.target_group_ids.map((id) => <span key={id} className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs text-blue-800">{t("rules.groupTag", { name: groups.find(g => g.id === id)?.name ?? id.slice(0, 8) })}</span>)}
                  {instance.target_host_ids.map((id) => <span key={id} className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">{hosts.find(h => h.id === id)?.name ?? id.slice(0, 8)}</span>)}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => openEditor(instance)}><Pencil size={14} />{t("rules.edit")}</Button>
                <Button onClick={() => setReview({ instance, mode: "preview" })}><Rocket size={14} />{t("rules.reviewPush")}</Button>
                <Tooltip content={t("ovpn.historyTitle")}><Button variant="ghost" onClick={() => setReview({ instance, mode: "history" })} aria-label={t("ovpn.historyTitle")}><History size={14} /></Button></Tooltip>
                <Button variant="dangerSubtle" onClick={() => handleDelete(instance.id)}><Trash2 size={14} />{t("rules.delete")}</Button>
              </div>
            </div>

            {instance.deployments.length > 0 && (
              <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-slate-100 pt-3">
                <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{t("rules.deployResult")}</p>
                <ul className="flex flex-wrap gap-x-5 gap-y-1.5">
                  {instance.deployments.map((d) => <DeploymentRow key={d.host_id} deployment={d} />)}
                </ul>
              </div>
            )}
          </Card>
        ))}
      </div>
      </fieldset>

      <fieldset disabled={readOnly} className="contents">
      {review && <OpenVpnInstanceReview
        instance={review.instance} mode={review.mode}
        onClose={() => setReview(null)}
        onDeployed={() => { setReview(null); load(); }}
        onChanged={load}
        onRestore={(snapshot) => openEditor(review.instance, snapshot)}
      />}

      {showModal && (
        <Modal title={editing ? t("ovpn.editTitle") : t("ovpn.newTitle")} onClose={() => setShowModal(false)}>
          <form onSubmit={handleSubmit} className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
            <FieldGroup label={t("common.description")}>
              <Input required value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder={t("ovpn.descPlaceholder")} />
            </FieldGroup>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <FieldGroup label={t("ovpn.role")}>
                <Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as OpenVpnRole })}>
                  <option value="server">{t("ovpn.roleServer")}</option>
                  <option value="client">{t("ovpn.roleClient")}</option>
                </Select>
              </FieldGroup>
              <FieldGroup label={t("nat.protocol")}>
                <Select value={form.proto} onChange={(e) => setForm({ ...form, proto: e.target.value as OpenVpnProto })}>
                  <option value="udp">UDP</option>
                  <option value="tcp">TCP</option>
                </Select>
              </FieldGroup>
              <FieldGroup label="Port">
                <Input required value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} placeholder="1194" />
              </FieldGroup>
            </div>

            {form.role === "server" ? (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <FieldGroup label={t("ovpn.bindAddress")}>
                  <Input value={form.local} onChange={(e) => setForm({ ...form, local: e.target.value })} placeholder={t("ovpn.bindPlaceholder")} />
                </FieldGroup>
                <FieldGroup label={t("ovpn.tunnelNetwork")}>
                  <Input required value={form.server_network} onChange={(e) => setForm({ ...form, server_network: e.target.value })} placeholder="10.8.0.0/24" />
                </FieldGroup>
              </div>
            ) : (
              <FieldGroup label={t("ovpn.remoteTarget")}>
                <Input required value={form.remote} onChange={(e) => setForm({ ...form, remote: e.target.value })} placeholder="vpn.example.com:1194" />
              </FieldGroup>
            )}

            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.tls_key_enabled} onChange={(e) => setForm({ ...form, tls_key_enabled: e.target.checked })} />
              {t("ovpn.tlsHardeningLabel")}
            </label>
            {form.tls_key_enabled && <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FieldGroup label={t("ovpn.staticKeyMode")}>
                <Select value={form.tls_key_mode} onChange={(e) => setForm({ ...form, tls_key_mode: e.target.value as OpenVpnTlsKeyMode })}>
                  <option value="crypt">{t("ovpn.modeCrypt")}</option>
                  <option value="auth">{t("ovpn.modeAuth")}</option>
                </Select>
              </FieldGroup>
              <FieldGroup label={editing ? t("ovpn.staticKeyEditing", { state: editing.tls_key_material_set ? t("ipsec.pskSet") : t("ipsec.pskNotSet") }) : t("ovpn.staticKey")}>
                <Input
                  type="password"
                  value={form.tls_key_material ?? ""}
                  onChange={(e) => setForm({ ...form, tls_key_material: e.target.value })}
                  placeholder={editing ? t("ipsec.pskKeep") : t("ovpn.keyPlaceholder")}
                  autoComplete="new-password"
                />
              </FieldGroup>
            </div>}

            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />{t("ovpn.active")}</label>

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

            <FieldGroup label={t("ovpn.certCaPerHost")}>
              {resolvedHostIds.length === 0 && (
                <p className="rounded-lg border border-slate-200 p-3 text-sm text-slate-500">
                  {t("ovpn.noTargets")}
                </p>
              )}
              <div className="space-y-3">
                {resolvedHostIds.map((hostId) => {
                  const hostName = hosts.find((h) => h.id === hostId)?.name ?? hostId.slice(0, 8);
                  const store = trustStores[hostId];
                  return (
                    <div key={hostId} className="rounded-lg border border-slate-200 p-3">
                      <p className="mb-2 text-sm font-medium text-slate-700">{hostName}</p>
                      {store?.loading && <Loading>{t("ovpn.trustLoading")}</Loading>}
                      {store?.error && <p className="text-sm text-red-600">{store.error}</p>}
                      {store && !store.loading && !store.error && (
                        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                          <FieldGroup label={t("ovpn.certificate")}>
                            {store.certs.length === 0 ? (
                              <p className="text-sm text-slate-400">{t("ovpn.noCerts")}</p>
                            ) : (
                              <Select required value={form.cert_refs[hostId] ?? ""} onChange={(e) => setCertRef(hostId, e.target.value)}>
                                <option value="" disabled>{t("ovpn.pleaseChoose")}</option>
                                {store.certs.map((c) => <option key={c.uuid} value={c.refid}>{c.descr}{c.common_name ? ` (${c.common_name})` : ""}</option>)}
                              </Select>
                            )}
                          </FieldGroup>
                          <FieldGroup label={t("ovpn.ca")}>
                            {store.cas.length === 0 ? (
                              <p className="text-sm text-slate-400">{t("ovpn.noCas")}</p>
                            ) : (
                              <Select required value={form.ca_refs[hostId] ?? ""} onChange={(e) => setCaRef(hostId, e.target.value)}>
                                <option value="" disabled>{t("ovpn.pleaseChoose")}</option>
                                {store.cas.map((c) => <option key={c.uuid} value={c.refid}>{c.descr}{c.common_name ? ` (${c.common_name})` : ""}</option>)}
                              </Select>
                            )}
                          </FieldGroup>
                        </div>
                      )}
                    </div>
                  );
                })}
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

function DeploymentRow({ deployment }: { deployment: OpenVpnDeploymentResult }) {
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
