"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, ChannelType, NotificationChannel } from "@/lib/api";
import { useNotAdmin } from "@/lib/useReadOnly";
import { BellRing } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

const TYPES: ChannelType[] = ["ntfy", "gotify", "telegram", "teams", "matrix"];

interface FieldSpec { key: string; kind: "text" | "number"; placeholder?: string; min?: number; max?: number }

// Non-secret settings per channel type; the credential is a separate write-only field.
const FIELDS: Record<ChannelType, FieldSpec[]> = {
  ntfy: [{ key: "server_url", kind: "text", placeholder: "https://ntfy.sh" }, { key: "topic", kind: "text" }, { key: "priority", kind: "number", min: 1, max: 5 }],
  gotify: [{ key: "server_url", kind: "text", placeholder: "https://gotify.example.org" }, { key: "priority", kind: "number", min: 0, max: 10 }],
  telegram: [{ key: "chat_id", kind: "text", placeholder: "-1001234567890" }, { key: "api_base", kind: "text", placeholder: "https://api.telegram.org" }],
  teams: [],
  matrix: [{ key: "homeserver_url", kind: "text", placeholder: "https://matrix.example.org" }, { key: "room_id", kind: "text", placeholder: "!abc123:example.org" }],
};

const DEFAULTS: Record<ChannelType, Record<string, string | number>> = {
  ntfy: { server_url: "https://ntfy.sh", topic: "", priority: 4 },
  gotify: { server_url: "", priority: 5 },
  telegram: { chat_id: "" },
  teams: {},
  matrix: { homeserver_url: "", room_id: "" },
};

function ChannelForm({ channel, onClose, onSaved }: { channel: NotificationChannel | null; onClose: () => void; onSaved: () => void }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [type, setType] = useState<ChannelType>(channel?.channel_type ?? "ntfy");
  const [name, setName] = useState(channel?.name ?? "");
  const [enabled, setEnabled] = useState(channel?.enabled ?? true);
  const [config, setConfig] = useState<Record<string, string | number>>(channel ? { ...channel.config } : { ...DEFAULTS.ntfy });
  const [secret, setSecret] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const secretRequired = type !== "ntfy";

  function changeType(next: ChannelType) {
    setType(next);
    setConfig({ ...DEFAULTS[next] });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      if (channel) {
        await api.updateNotificationChannel(channel.id, { name, enabled, config, ...(secret ? { secret } : {}), clear_secret: clearSecret });
      } else {
        await api.createNotificationChannel({ name, channel_type: type, enabled, config, ...(secret ? { secret } : {}) });
      }
      toast.show(t("settings.channels.saved"), "success");
      onSaved();
      onClose();
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={channel ? t("settings.channels.edit") : t("settings.channels.add")} onClose={onClose}>
      <form onSubmit={submit} className="min-h-0 space-y-3 overflow-y-auto pr-1">
        {!channel && (
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-slate-700">{t("settings.channels.type")}</span>
            <Select value={type} onChange={(e) => changeType(e.target.value as ChannelType)}>
              {TYPES.map((v) => <option key={v} value={v}>{t(`settings.channels.types.${v}`)}</option>)}
            </Select>
          </label>
        )}
        <label className="block text-sm">
          <span className="mb-1 block font-medium text-slate-700">{t("settings.channels.name")}</span>
          <Input value={name} maxLength={100} required onChange={(e) => setName(e.target.value)} />
        </label>
        {FIELDS[type].map((f) => (
          <label key={f.key} className="block text-sm">
            <span className="mb-1 block font-medium text-slate-700">{t(`settings.channels.field.${f.key}`)}</span>
            <Input
              type={f.kind}
              min={f.min}
              max={f.max}
              placeholder={f.placeholder}
              value={config[f.key] ?? ""}
              onChange={(e) => setConfig({ ...config, [f.key]: f.kind === "number" ? Number(e.target.value) : e.target.value })}
            />
          </label>
        ))}
        <label className="block text-sm">
          <span className="mb-1 block font-medium text-slate-700">
            {t(`settings.channels.secret.${type}`)} {channel?.secret_set && !clearSecret ? t("settings.notify.isSet") : t("settings.notify.notSet")}
          </span>
          <Input
            type="password"
            autoComplete="off"
            value={secret}
            required={secretRequired && !channel?.secret_set}
            onChange={(e) => { setSecret(e.target.value); setClearSecret(false); }}
            placeholder={channel?.secret_set ? t("settings.notify.keepUnchanged") : secretRequired ? "" : t("settings.notify.optional")}
          />
        </label>
        {channel?.secret_set && !secretRequired && (
          <label className="flex items-center gap-2 text-sm text-slate-500">
            <input type="checkbox" checked={clearSecret} onChange={(e) => { setClearSecret(e.target.checked); if (e.target.checked) setSecret(""); }} />
            {t("settings.channels.removeSecret")}
          </label>
        )}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {t("settings.channels.enabled")}
        </label>
        {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        <div className="flex justify-end gap-2 border-t border-slate-100 pt-3">
          <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button type="submit" loading={saving}>{t("common.save")}</Button>
        </div>
      </form>
    </Modal>
  );
}

export function NotificationChannelsSection() {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const readOnly = useNotAdmin();
  const [channels, setChannels] = useState<NotificationChannel[] | null>(null);
  const [editing, setEditing] = useState<NotificationChannel | "new" | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api.notificationChannels().then(setChannels).catch((e) => { setChannels([]); toast.show(errorMessage(e, "settings.channels.loadFailed"), "danger"); });
  }, [errorMessage, toast]);

  useEffect(() => { load(); }, [load]);

  async function test(c: NotificationChannel) {
    setBusy(c.id);
    try {
      await api.testNotificationChannel(c.id);
      toast.show(t("settings.channels.testSent", { name: c.name }), "success");
    } catch (e) {
      toast.show(errorMessage(e, "settings.notify.testFailed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  async function toggle(c: NotificationChannel) {
    setBusy(c.id);
    try {
      await api.updateNotificationChannel(c.id, { enabled: !c.enabled });
      load();
    } catch (e) {
      toast.show(errorMessage(e, "common.saveFailed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  async function remove(c: NotificationChannel) {
    if (!confirm(t("settings.channels.confirmDelete", { name: c.name }))) return;
    setBusy(c.id);
    try {
      await api.deleteNotificationChannel(c.id);
      toast.show(t("settings.channels.deleted", { name: c.name }), "success");
      load();
    } catch (e) {
      toast.show(errorMessage(e, "common.deleteFailed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-slate-100 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-medium">{t("settings.channels.title")}<InfoTip text={t("settings.channels.hint")} /></h3>
        {!readOnly && <Button type="button" variant="outline" onClick={() => setEditing("new")}>{t("settings.channels.add")}</Button>}
      </div>
      {channels === null ? (
        <Skeleton className="h-12" />
      ) : channels.length === 0 ? (
        <EmptyState icon={BellRing} title={t("settings.channels.empty")} />
      ) : (
        <ul className="divide-y divide-slate-100">
          {channels.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate font-medium text-slate-900">{c.name}</span>
                <Badge tone="info">{t(`settings.channels.types.${c.channel_type}`)}</Badge>
                {!c.enabled && <Badge tone="neutral">{t("settings.channels.disabled")}</Badge>}
              </span>
              <span className="flex flex-wrap items-center gap-1">
                {!readOnly && (
                  <>
                    <Button type="button" variant="ghost" disabled={busy === c.id} onClick={() => test(c)}>{t("settings.channels.test")}</Button>
                    <Button type="button" variant="ghost" disabled={busy === c.id} onClick={() => toggle(c)}>{c.enabled ? t("settings.channels.disable") : t("settings.channels.enable")}</Button>
                    <Button type="button" variant="ghost" disabled={busy === c.id} onClick={() => setEditing(c)}>{t("settings.channels.editAction")}</Button>
                    <Button type="button" variant="ghost" disabled={busy === c.id} onClick={() => remove(c)}>{t("settings.channels.deleteAction")}</Button>
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {editing !== null && <ChannelForm channel={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={load} />}
    </div>
  );
}
