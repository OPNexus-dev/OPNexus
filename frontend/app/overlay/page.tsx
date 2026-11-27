"use client";

import { useChanged, useDirty } from "@/lib/useDirty";
import { DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageHeader } from "@/components/ui/PageHeader";
import { useConfirm } from "@/components/ui/Confirm";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { api, Host, OverlayInstructions, OverlayPeer, OverlaySettings } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useNotAdmin } from "@/lib/useReadOnly";
import { Copy, FileText, Link2, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

const formatBytes = (n: number) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

export default function OverlayPage() {
  const { t, errorMessage, locale } = useLanguage();
  const confirmAction = useConfirm();
  const notAdmin = useNotAdmin();
  const toast = useToast();
  const [settings, setSettings] = useState<OverlaySettings | null>(null);
  const [peers, setPeers] = useState<OverlayPeer[]>([]);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [endpoint, setEndpoint] = useState("");
  const [port, setPort] = useState(51820);
  const [subnet, setSubnet] = useState("10.99.0.0/24");
  const [applied, setApplied] = useState<OverlaySettings | null>(null);   // the settings the form was last filled from
  const dirty = useDirty({ enabled, endpoint, port, subnet }, applied);
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState(false);
  const [showing, setShowing] = useState<{ name: string; ip: string; instructions: OverlayInstructions } | null>(null);
  const [editing, setEditing] = useState<OverlayPeer | null>(null);

  // The poll refreshes the status (peers, hub state) but must NOT overwrite what the user is typing: the form fields are filled from the
  // server only on the first load and after a save (applyForm).
  async function load(applyForm = false) {
    try {
      const data = await api.getOverlay();
      setSettings(data.settings);
      setPeers(data.peers);
      if (applyForm) {
        setApplied(data.settings);
        setEnabled(data.settings.enabled);
        setEndpoint(data.settings.endpoint ?? "");
        setPort(data.settings.listen_port);
        setSubnet(data.settings.subnet);
      }
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "overlay.loadFailed"));
    }
  }

  useEffect(() => {
    load(true);
    api.listHosts().then(setHosts).catch(() => {});
    const timer = window.setInterval(() => load(), 10000);
    return () => window.clearInterval(timer);
  }, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      await api.updateOverlay({ enabled, endpoint, listen_port: port, subnet });
      await load(true);
      toast.show(t("overlay.saved"), "success");
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function showInstructions(peer: OverlayPeer) {
    try {
      setShowing({ name: peer.name, ip: peer.ip, instructions: await api.overlayInstructions(peer.id) });
    } catch (err) {
      setError(errorMessage(err, "overlay.loadFailed"));
    }
  }

  async function remove(peer: OverlayPeer) {
    if (!(await confirmAction(t("overlay.deleteConfirm", { name: peer.name }), { danger: true }))) return;
    try {
      await api.deleteOverlayPeer(peer.id);
      await load();
      toast.show(t("overlay.deleted", { name: peer.name }), "success");
    } catch (err) {
      setError(errorMessage(err, "common.deleteFailed"));
    }
  }

  const hubTone = settings?.hub_state === "running" ? "success" : settings?.hub_state === "stale" ? "warning" : "neutral";

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("overlay.title")}
        info={t("overlay.intro")}
        actions={
          <Button onClick={() => setAdding(true)} disabled={notAdmin || !settings?.enabled}>
            <Plus size={16} />
            {t("overlay.addPeer")}
          </Button>
        }
      />
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

      <Card className="p-5">
        {!settings ? <Skeleton className="h-24 w-full" /> : (
          <form onSubmit={save} className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input type="checkbox" checked={enabled} disabled={notAdmin} onChange={(e) => setEnabled(e.target.checked)} />
                {t("overlay.enable")}
              </label>
              {settings.enabled && <Badge tone={hubTone}>{t(`overlay.hub.${settings.hub_state}`)}</Badge>}
            </div>
            {settings.enabled && settings.hub_state === "absent" && <p className="text-sm text-amber-700">{t("overlay.hubAbsent")}</p>}
            <fieldset disabled={!enabled || notAdmin} className="grid gap-3 disabled:opacity-50 sm:grid-cols-3">
              <FieldGroup label={t("overlay.endpoint")} hint={t("overlay.endpointHint")}>
                <Input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="vpn.example.com" />
              </FieldGroup>
              <FieldGroup label={t("overlay.port")}>
                <Input type="number" min={1} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value))} />
              </FieldGroup>
              <FieldGroup label={t("overlay.subnet")} hint={t("overlay.subnetHint")}>
                <Input value={subnet} onChange={(e) => setSubnet(e.target.value)} disabled={peers.length > 0} />
              </FieldGroup>
            </fieldset>
            {settings.hub_public_key && (
              <p className="text-xs text-slate-500">
                {t("overlay.hubKey")} <code className="break-all font-mono">{settings.hub_public_key}</code> · {t("overlay.hubAddress")} <code className="font-mono">{settings.hub_address}</code>
              </p>
            )}
            <Button type="submit" disabled={saving || notAdmin || !dirty} loading={saving}>{t("common.save")}</Button>
          </form>
        )}
      </Card>

      <DataTable
        label={t("overlay.title")}
        rows={peers}
        rowKey={(peer) => peer.id}
        empty={<EmptyState icon={Link2} title={t("overlay.empty")} />}
        columns={[
          { key: "name", header: t("overlay.colName"), priority: "primary", isTitle: true, cell: (peer) => peer.name },
          {
            key: "status", header: t("overlay.colStatus"), priority: "primary",
            cell: (peer) => !peer.public_key ? <Badge tone="warning">{t("overlay.needsKey")}</Badge> : peer.connected ? (
              <span className="flex flex-col gap-0.5">
                <Badge tone="success">{t("overlay.connected")}</Badge>
                <span className="text-xs font-normal text-slate-500">
                  {peer.last_handshake_at ? new Date(peer.last_handshake_at).toLocaleTimeString(locale) : ""} · ↓{formatBytes(peer.rx_bytes)} ↑{formatBytes(peer.tx_bytes)}
                </span>
              </span>
            ) : <Badge tone="neutral">{t("overlay.waiting")}</Badge>,
          },
          { key: "ip", header: t("overlay.colAddress"), priority: "high", mono: true, cell: (peer) => peer.ip },
          { key: "host", header: t("overlay.colHost"), priority: "high", cell: (peer) => peer.host_id ? <Link className="text-brand-600 hover:underline" href={`/hosts/${peer.host_id}`}>{peer.host_name}</Link> : <span className="text-slate-300">—</span> },
        ]}
        // Write actions are disabled one by one (the menu is rendered outside any fieldset).
        actions={(peer) => [
          { key: "instructions", label: t("overlay.instructions"), icon: FileText, onSelect: () => showInstructions(peer) },
          { key: "link", label: t("overlay.link"), icon: Link2, disabled: notAdmin, onSelect: () => setEditing(peer) },
          { key: "delete", label: t("rules.delete"), icon: Trash2, tone: "danger", disabled: notAdmin, onSelect: () => remove(peer) },
        ]}
      />

      {adding && (
        <AddPeerModal
          onClose={() => setAdding(false)}
          onCreated={async (created) => { setAdding(false); setShowing(created); await load(); }}
        />
      )}
      {showing && <InstructionsModal data={showing} onClose={() => setShowing(null)} />}
      {editing && (
        <LinkModal
          peer={editing}
          hosts={hosts}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await load(); toast.show(t("overlay.saved"), "success"); }}
        />
      )}
    </div>
  );
}

function AddPeerModal({ onClose, onCreated }: { onClose: () => void; onCreated: (c: { name: string; ip: string; instructions: OverlayInstructions }) => void }) {
  const { t, errorMessage } = useLanguage();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createOverlayPeer(name.trim()));
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={t("overlay.addPeer")} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <FieldGroup label={t("overlay.colName")} hint={t("overlay.nameHint")}>
          <Input required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder="filiale-nord" />
        </FieldGroup>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button type="submit" disabled={busy} loading={busy}>{t("overlay.create")}</Button>
        </div>
      </form>
    </Modal>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const { t } = useLanguage();
  const toast = useToast();
  return (
    <div className="flex items-center gap-2">
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
        <code className="block break-all font-mono text-xs text-slate-900">{value}</code>
      </div>
      <Button type="button" variant="ghost" onClick={() => { void navigator.clipboard?.writeText(value); toast.show(t("tokens.copied"), "success"); }}>
        <Copy size={14} />
      </Button>
    </div>
  );
}

function InstructionsModal({ data, onClose }: { data: { name: string; ip: string; instructions: OverlayInstructions }; onClose: () => void }) {
  const { t } = useLanguage();
  const i = data.instructions;
  return (
    <Modal title={t("overlay.howTitle", { name: data.name })} onClose={onClose}>
      <div className="space-y-4 text-sm text-slate-700">
        <p>{t("overlay.howIntro")}</p>
        <h3 className="font-medium text-slate-900">{t("overlay.step1")}</h3>
        <div className="space-y-2 rounded-md border border-slate-200 p-3">
          <p className="text-xs text-slate-500">{t("overlay.step1Hint")}</p>
          <CopyRow label={t("overlay.fieldAddress")} value={i.tunnel_address} />
        </div>
        <h3 className="font-medium text-slate-900">{t("overlay.step2")}</h3>
        <div className="space-y-2 rounded-md border border-slate-200 p-3">
          <CopyRow label={t("overlay.fieldPublicKey")} value={i.hub_public_key} />
          <CopyRow label={t("overlay.fieldPsk")} value={i.psk} />
          <CopyRow label={t("overlay.fieldEndpoint")} value={i.endpoint ?? ""} />
          <CopyRow label={t("overlay.fieldAllowed")} value={i.allowed_ips} />
          <CopyRow label={t("overlay.fieldKeepalive")} value={String(i.keepalive)} />
        </div>
        <h3 className="font-medium text-slate-900">{t("overlay.step3")}</h3>
        <p>{t("overlay.step3Text", { hub: i.hub_ip })}</p>
        <h3 className="font-medium text-slate-900">{t("overlay.step4")}</h3>
        <p>{t("overlay.step4Text")}</p>
        <h3 className="font-medium text-slate-900">{t("overlay.step5")}</h3>
        <div className="space-y-2 rounded-md border border-slate-200 p-3">
          <p className="text-xs text-slate-500">{t("overlay.step5Text")}</p>
          <CopyRow label={t("overlay.fieldUrl")} value={i.host_url} />
        </div>
        <div className="flex justify-end"><Button onClick={onClose}>{t("tokens.done")}</Button></div>
      </div>
    </Modal>
  );
}

function LinkModal({ peer, hosts, onClose, onSaved }: { peer: OverlayPeer; hosts: Host[]; onClose: () => void; onSaved: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [key, setKey] = useState(peer.public_key ?? "");
  const [hostId, setHostId] = useState(peer.host_id ?? "");
  const changed = useChanged({ key, hostId });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.updateOverlayPeer(peer.id, { public_key: key.trim(), ...(hostId ? { host_id: hostId } : { clear_host: true }) });
      onSaved();
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={t("overlay.linkTitle", { name: peer.name })} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <FieldGroup label={t("overlay.firewallKey")} hint={t("overlay.firewallKeyHint")}>
          <Input value={key} onChange={(e) => setKey(e.target.value)} className="font-mono" placeholder="44 Zeichen, endet auf =" />
        </FieldGroup>
        <FieldGroup label={t("overlay.colHost")} hint={t("overlay.hostHint")}>
          <Select value={hostId} onChange={(e) => setHostId(e.target.value)}>
            <option value="">—</option>
            {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
          </Select>
        </FieldGroup>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button type="submit" disabled={busy || !changed} loading={busy}>{t("common.save")}</Button>
        </div>
      </form>
    </Modal>
  );
}
