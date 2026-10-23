"use client";

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { StatusDot } from "@/components/ui/StatusDot";
import { useToast } from "@/components/ui/Toast";
import { api, ApiError, Host, HostCreate, HostUpdate, PolicyHierarchyGroup } from "@/lib/api";
import { useNotAdmin } from "@/lib/useReadOnly";
import { ExternalLink, Pencil, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { TlsPinControl, TlsPinValue } from "@/components/TlsPinControl";
import { ShieldAlert, ShieldCheck } from "lucide-react";

const emptyForm: HostCreate = {
  name: "",
  base_url: "",
  api_key: "",
  api_secret: "",
  verify_tls: true,
  enabled: true,
  cluster_name: "",
  policy_hierarchy_group_id: null,
};

export default function HostsPage() {
  const readOnly = useNotAdmin();
  const toast = useToast();
  const { t, errorMessage } = useLanguage();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [hierarchyGroups, setHierarchyGroups] = useState<PolicyHierarchyGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<HostCreate>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editHost, setEditHost] = useState<Host | null>(null);
  const [tlsPin, setTlsPin] = useState<TlsPinValue>({ pem: null, insecure: false, clear: false });
  // After a failed connection check the user may still save the host (behind NAT, tunnel not up yet).
  const [connectionFailed, setConnectionFailed] = useState(false);
  const [createAnyway, setCreateAnyway] = useState(false);
  const [deleting, setDeleting] = useState<{ id: string; name: string } | null>(null);
  const [deleteName, setDeleteName] = useState("");

  async function load() {
    try {
      const [hostList, groupList] = await Promise.all([api.listHosts(), api.listPolicyHierarchyGroups()]);
      setHosts(hostList);
      setHierarchyGroups(groupList);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "hosts.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    try {
      await api.createHost({ ...form, verify_tls: !tlsPin.insecure, tls_ca_pem: tlsPin.pem, cluster_name: form.cluster_name || null, policy_hierarchy_group_id: form.policy_hierarchy_group_id || null, verify_connection: !createAnyway });
      setShowModal(false);
      setConnectionFailed(false);
      setCreateAnyway(false);
      toast.show(t("hosts.created", { name: form.name }), "success");
      setForm(emptyForm);
      setTlsPin({ pem: null, insecure: false, clear: false });
      await load();
    } catch (e) {
      setConnectionFailed(e instanceof ApiError && e.code === "HOST_CONNECTION_FAILED");
      setFormError(errorMessage(e, "hosts.createFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    try {
      await api.deleteHost(deleting.id, deleteName);
      setDeleting(null);
      await load();
      toast.show(t("hosts.deleted", { name: deleting.name }), "success");
    } catch (e) {
      setDeleting(null);
      setError(errorMessage(e, "common.requestFailed"));
    }
  }

  return (
    <fieldset disabled={readOnly} className="contents">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900">Hosts</h1>
        <Button onClick={() => { setConnectionFailed(false); setCreateAnyway(false); setFormError(null); setShowModal(true); }}>
          <Plus size={16} />
          {t("hosts.add")}
        </Button>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      <Card className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">{t("hosts.name")}</th>
              <th className="px-5 py-3 font-medium">{t("hosts.address")}</th>
              <th className="px-5 py-3 font-medium">{t("hosts.status")}</th>
              <th className="px-5 py-3 font-medium">CARP</th>
              <th className="px-5 py-3 font-medium">{t("hosts.cluster")}</th>
              <th className="px-5 py-3 font-medium">{t("hosts.policyHierarchy")}</th>
              <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
            </tr>
          </thead>
          <tbody>
            {hosts === null &&
              [0, 1, 2].map((i) => (
                <tr key={i} className="border-b border-slate-100 last:border-0">
                  <td className="px-5 py-3">
                    <Skeleton className="h-4 w-24" />
                  </td>
                  <td className="px-5 py-3">
                    <Skeleton className="h-4 w-32" />
                  </td>
                  <td className="px-5 py-3">
                    <Skeleton className="h-4 w-16" />
                  </td>
                  <td className="px-5 py-3">
                    <Skeleton className="h-4 w-14 rounded-full" />
                  </td>
                  <td className="px-5 py-3">
                    <Skeleton className="h-4 w-14 rounded-full" />
                  </td>
                  <td className="px-5 py-3">
                    <Skeleton className="h-4 w-14 rounded-full" />
                  </td>
                  <td className="px-5 py-3" />
                </tr>
              ))}
            {hosts?.length === 0 && (
              <tr>
                <td colSpan={7} className="px-5 py-8 text-center text-slate-400">
                  {t("hosts.empty")}
                </td>
              </tr>
            )}
            {hosts?.map((host) => (
              <tr key={host.id} className="border-b border-slate-100 last:border-0">
                <td className="whitespace-nowrap px-5 py-3 font-medium">
                  <span className="inline-flex items-center gap-2">
                    <Link href={`/hosts/${host.id}`} className="text-sky-700 hover:underline">
                      {host.name}
                    </Link>
                    <Tooltip content={host.tls_pinned ? t("tls.pinnedTip", { fp: (host.tls_fingerprint ?? "").slice(0, 23) }) : host.verify_tls ? t("tls.systemTip") : t("tls.insecureTip")} focusable>
                      {host.tls_pinned ? <ShieldCheck size={15} className="text-emerald-600" aria-label={t("tls.pinned")} /> : <ShieldAlert size={15} className={host.verify_tls ? "text-slate-400" : "text-flame-600"} aria-label={host.verify_tls ? t("tls.notPinned") : t("tls.insecure")} />}
                    </Tooltip>
                  </span>
                </td>
                <td className="px-5 py-3 text-slate-500">{host.base_url}</td>
                <td className="px-5 py-3">
                  <StatusDot status={host.status} />
                  {host.status_detail && (
                    <div className="mt-0.5 text-xs text-slate-400" title={host.status_detail}>
                      {host.status_detail.slice(0, 60)}
                    </div>
                  )}
                </td>
                <td className="px-5 py-3">
                  {host.carp_status ? (
                    <span
                      className={
                        host.carp_status === "MASTER"
                          ? "rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700"
                          : "rounded-full bg-flame-100 px-2.5 py-0.5 text-xs font-medium text-flame-700"
                      }
                    >
                      {host.carp_status}
                    </span>
                  ) : (
                    <span className="text-slate-300">—</span>
                  )}
                </td>
                <td className="px-5 py-3">
                  {host.cluster_name ? (
                    <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">
                      {host.cluster_name}
                    </span>
                  ) : (
                    <span className="text-slate-300">—</span>
                  )}
                </td>
                <td className="px-5 py-3">
                  {host.policy_hierarchy_group_id ? (
                    <Link
                      href={`/policy-hierarchy?group=${host.policy_hierarchy_group_id}`}
                      className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs text-blue-800 hover:underline"
                    >
                      {hierarchyGroups.find((g) => g.id === host.policy_hierarchy_group_id)?.name ??
                        host.policy_hierarchy_group_id.slice(0, 8)}
                    </Link>
                  ) : (
                    <span className="text-slate-300">—</span>
                  )}
                </td>
                <td className="px-5 py-3 text-right">
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="ghost"
                      href={host.base_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={t("hosts.openInOpnsense")}
                    >
                      <ExternalLink size={14} />
                    </Button>
                    <Button variant="outline" onClick={() => setEditHost(host)}>
                      <Pencil size={14} />
                      {t("hosts.edit")}
                    </Button>
                    <Button variant="dangerSubtle" onClick={() => { setDeleting({ id: host.id, name: host.name }); setDeleteName(""); }}>
                      <Trash2 size={14} />
                      {t("hosts.delete")}
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {showModal && (
        <Modal title={t("hosts.add")} onClose={() => setShowModal(false)}>
          <form onSubmit={handleCreate} className="space-y-4">
            <FieldGroup label="Name">
              <Input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="fw-primary"
              />
            </FieldGroup>
            <FieldGroup label={t("hosts.baseUrl")}>
              <Input
                required
                value={form.base_url}
                onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                placeholder="https://192.168.1.1"
              />
            </FieldGroup>
            <FieldGroup label="API-Key">
              <Input
                required
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              />
            </FieldGroup>
            <FieldGroup label="API-Secret">
              <Input
                required
                type="password"
                value={form.api_secret}
                onChange={(e) => setForm({ ...form, api_secret: e.target.value })}
              />
            </FieldGroup>
            <FieldGroup label={t("hosts.clusterOptional")}>
              <Input
                value={form.cluster_name ?? ""}
                onChange={(e) => setForm({ ...form, cluster_name: e.target.value })}
                placeholder={t("hosts.clusterPlaceholder")}
              />
            </FieldGroup>
            <FieldGroup label={t("hosts.policyGroupOptional")}>
              <Select
                value={form.policy_hierarchy_group_id ?? ""}
                onChange={(e) => setForm({ ...form, policy_hierarchy_group_id: e.target.value || null })}
              >
                <option value="">{t("hosts.none")}</option>
                {hierarchyGroups.map((g) => (
                  <option key={g.id} value={g.id}>{g.name}</option>
                ))}
              </Select>
            </FieldGroup>
            <TlsPinControl baseUrl={form.base_url} value={tlsPin} onChange={setTlsPin} />
            {formError && <p role="alert" className="text-sm text-red-600">{formError}</p>}
            {connectionFailed && (
              <label className="flex items-start gap-2 text-sm text-slate-700">
                <input type="checkbox" className="mt-1" checked={createAnyway} onChange={(e) => setCreateAnyway(e.target.checked)} />
                <span>{t("hosts.createAnyway")}</span>
              </label>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="ghost" onClick={() => setShowModal(false)}>
                {t("common.cancel")}
              </Button>
              <Button type="submit" disabled={saving} loading={saving}>
                {saving ? t("hosts.saving") : t("hosts.create")}
              </Button>
            </div>
          </form>
        </Modal>
      )}

      {editHost && (
        <EditHostModal
          host={editHost}
          hierarchyGroups={hierarchyGroups}
          onClose={() => setEditHost(null)}
          onSaved={async () => {
            setEditHost(null);
            await load();
            toast.show(t("hosts.saved", { name: editHost.name }), "success");
          }}
        />
      )}
      {deleting && (
        <Modal title={t("hosts.deleteTitle")} onClose={() => setDeleting(null)}>
          <div className="space-y-4 text-sm">
            <p className="text-slate-700">{t("hosts.deleteConfirm")}</p>
            <label className="block">
              <span className="block">{t("hosts.deleteTypeName", { name: deleting.name })}</span>
              <Input className="mt-1" autoFocus autoComplete="off" spellCheck={false} value={deleteName} placeholder={deleting.name} aria-label={t("hosts.deleteTypeNameAria")}
                onChange={(e) => setDeleteName(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && deleteName === deleting.name) void handleDelete(); }} />
            </label>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setDeleting(null)}>{t("common.cancel")}</Button>
              <Button variant="danger" disabled={deleteName !== deleting.name} onClick={handleDelete}>{t("hosts.delete")}</Button>
            </div>
          </div>
        </Modal>
      )}
    </fieldset>
  );
}

function EditHostModal({ host, hierarchyGroups, onClose, onSaved }: { host: Host; hierarchyGroups: PolicyHierarchyGroup[]; onClose: () => void; onSaved: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [name, setName] = useState(host.name);
  const [baseUrl, setBaseUrl] = useState(host.base_url);
  const [clusterName, setClusterName] = useState(host.cluster_name ?? "");
  const [hierarchyGroupId, setHierarchyGroupId] = useState(host.policy_hierarchy_group_id ?? "");
  const [pin, setPin] = useState<TlsPinValue>({ pem: null, insecure: !host.verify_tls, clear: false });
  const [enabled, setEnabled] = useState(host.enabled);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const payload: HostUpdate = { name, verify_tls: !pin.insecure, enabled };
      if (baseUrl.trim() !== host.base_url) payload.base_url = baseUrl.trim();
      if (pin.pem) payload.tls_ca_pem = pin.pem;
      if (pin.clear) payload.clear_tls_pin = true;
      if (clusterName.trim()) {
        payload.cluster_name = clusterName.trim();
      } else {
        payload.clear_cluster_name = true;
      }
      if (hierarchyGroupId) {
        payload.policy_hierarchy_group_id = hierarchyGroupId;
      } else {
        payload.clear_policy_hierarchy_group_id = true;
      }
      await api.updateHost(host.id, payload);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={t("hosts.editTitle", { name: host.name })} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <FieldGroup label="Name">
          <Input required value={name} onChange={(e) => setName(e.target.value)} />
        </FieldGroup>
        <FieldGroup label={t("hosts.baseUrl")} hint={t("hosts.baseUrlEditHint")}>
          <Input required value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://192.168.1.1" />
        </FieldGroup>
        <FieldGroup label={t("hosts.clusterOptional")}>
          <Input
            value={clusterName}
            onChange={(e) => setClusterName(e.target.value)}
            placeholder={t("hosts.clusterShortPlaceholder")}
          />
        </FieldGroup>
        <FieldGroup label={t("hosts.policyGroupOptional")}>
          <Select value={hierarchyGroupId} onChange={(e) => setHierarchyGroupId(e.target.value)}>
            <option value="">{t("hosts.none")}</option>
            {hierarchyGroups.map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))}
          </Select>
        </FieldGroup>
        <TlsPinControl baseUrl={host.base_url} currentFingerprint={host.tls_fingerprint} value={pin} onChange={setPin} />
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {t("hosts.active")}
        </label>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" variant="secondary" disabled={saving} loading={saving}>
            {saving ? t("hosts.saving") : t("common.save")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
