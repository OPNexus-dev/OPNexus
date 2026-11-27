"use client";

import { useChanged } from "@/lib/useDirty";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Column, DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { PageHeader } from "@/components/ui/PageHeader";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { StatusDot } from "@/components/ui/StatusDot";
import { useToast } from "@/components/ui/Toast";
import { api, ApiError, Host, HostCreate, HostUpdate, PolicyHierarchyGroup } from "@/lib/api";
import { useNotAdmin } from "@/lib/useReadOnly";
import { ExternalLink, Network, Pencil, Plus, Trash2 } from "lucide-react";
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

  const columns: Column<Host>[] = [
    {
      key: "name", header: t("hosts.name"), priority: "primary",
      cell: (host) => (
        <span className="inline-flex items-center gap-2 font-medium">
          <Link href={`/hosts/${host.id}`} className="text-sky-700 hover:underline">{host.name}</Link>
          <Tooltip content={host.tls_pinned ? t("tls.pinnedTip", { fp: (host.tls_fingerprint ?? "").slice(0, 23) }) : host.verify_tls ? t("tls.systemTip") : t("tls.insecureTip")} focusable>
            {host.tls_pinned ? <ShieldCheck size={15} className="text-emerald-600" aria-label={t("tls.pinned")} /> : <ShieldAlert size={15} className={host.verify_tls ? "text-slate-400" : "text-flame-600"} aria-label={host.verify_tls ? t("tls.notPinned") : t("tls.insecure")} />}
          </Tooltip>
        </span>
      ),
    },
    {
      key: "status", header: t("hosts.status"), priority: "primary",
      cell: (host) => (
        <div>
          <StatusDot status={host.status} />
          {host.status_detail && <div className="mt-0.5 text-xs text-slate-400" title={host.status_detail}>{host.status_detail.slice(0, 60)}</div>}
        </div>
      ),
    },
    { key: "address", header: t("hosts.address"), priority: "high", className: "text-slate-500", cell: (host) => host.base_url },
    {
      key: "carp", header: "CARP", priority: "high",
      cell: (host) => host.carp_status ? <Badge tone={host.carp_status === "MASTER" ? "success" : "warning"}>{host.carp_status}</Badge> : <span className="text-slate-300">—</span>,
    },
    { key: "cluster", header: t("hosts.cluster"), priority: "medium", cell: (host) => host.cluster_name ? <Badge tone="neutral">{host.cluster_name}</Badge> : <span className="text-slate-300">—</span> },
    {
      key: "hierarchy", header: t("hosts.policyHierarchy"), priority: "low",
      cell: (host) => host.policy_hierarchy_group_id ? (
        <Link href={`/policy-hierarchy?group=${host.policy_hierarchy_group_id}`} className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs text-blue-800 hover:underline">
          {hierarchyGroups.find((g) => g.id === host.policy_hierarchy_group_id)?.name ?? host.policy_hierarchy_group_id.slice(0, 8)}
        </Link>
      ) : <span className="text-slate-300">—</span>,
    },
  ];

  return (
    <>
      {/* Read-only accounts: the write controls are disabled one by one (header button, row actions, dialogs). The table itself must
          stay outside a disabled fieldset, or its read-only controls (details, sorting) would be disabled too. */}
      <PageHeader
        title="Hosts"
        actions={
          <Button disabled={readOnly} onClick={() => { setConnectionFailed(false); setCreateAnyway(false); setFormError(null); setShowModal(true); }}>
            <Plus size={16} />
            {t("hosts.add")}
          </Button>
        }
      />

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {hosts === null && (
        <Card className="space-y-3 p-5" aria-busy="true">
          {[0, 1, 2].map((i) => <Skeleton key={i} className="h-5 w-full" />)}
        </Card>
      )}
      {hosts && (
        <DataTable
          label="Hosts"
          rows={hosts}
          rowKey={(host) => host.id}
          columns={columns}
          visibleActions={2}
          actions={(host) => [
            { key: "open", label: t("hosts.openInOpnsense"), icon: ExternalLink, href: host.base_url, external: true },
            { key: "edit", label: t("hosts.edit"), icon: Pencil, onSelect: () => setEditHost(host), disabled: readOnly },
            { key: "delete", label: t("hosts.delete"), icon: Trash2, tone: "danger", onSelect: () => { setDeleting({ id: host.id, name: host.name }); setDeleteName(""); }, disabled: readOnly },
          ]}
          empty={<EmptyState icon={Network} title={t("hosts.empty")} />}
        />
      )}

      <fieldset disabled={readOnly} className="contents">
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
    </>
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
  const changed = useChanged({ name, baseUrl, clusterName, hierarchyGroupId, pin, enabled });

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
          <Button type="submit" variant="secondary" disabled={saving || !changed} loading={saving}>
            {saving ? t("hosts.saving") : t("common.save")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
