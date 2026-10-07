"use client";

import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { AcmeAccount, AcmeAccountCreate, AcmeCA, api, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { FormEvent, useState } from "react";

const caLabels: Record<AcmeCA, string> = {
  letsencrypt: "Let's Encrypt",
  letsencrypt_test: "Let's Encrypt (Staging/Test)",
  zerossl: "ZeroSSL",
  buypass: "Buypass",
  buypass_test: "Buypass (Test)",
  google: "Google Trust Services",
  google_test: "Google Trust Services (Test)",
  sslcom: "SSL.com",
  custom: "",
};

function emptyForm(hostId: string): AcmeAccountCreate {
  return { host_id: hostId, name: "", description: "", email: "", ca: "letsencrypt_test", custom_ca: "", eab_kid: "", enabled: true, eab_hmac: "" };
}

export function AcmeAccountForm({ hosts, editing, onClose, onSaved }: {
  hosts: Host[]; editing: AcmeAccount | null; onClose: () => void; onSaved: () => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [form, setForm] = useState<AcmeAccountCreate>(editing ? { ...editing, eab_hmac: "" } : emptyForm(hosts[0]?.id ?? ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      if (editing) {
        const { eab_hmac, ...rest } = form;
        await api.editAcmeAccount(editing.id, eab_hmac ? form : { ...rest, eab_hmac: null }, editing.updated_at);
      } else {
        await api.createAcmeAccount(form);
      }
      onSaved();
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={editing ? t("acmeAccount.edit") : t("acmeAccount.new")} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        <p className="text-xs text-slate-500">
          {t("acmeAccount.localNote")}
        </p>
        <FieldGroup label={t("acmeAccount.host")}>
          <Select value={form.host_id} onChange={(e) => setForm({ ...form, host_id: e.target.value })} disabled={!!editing} required>
            {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
          </Select>
        </FieldGroup>
        <FieldGroup label={t("acmeAccount.name")}><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></FieldGroup>
        <FieldGroup label={t("acmeAccount.description")}><Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></FieldGroup>
        <FieldGroup label={t("acmeAccount.email")}><Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></FieldGroup>
        <FieldGroup label={t("acmeAccount.ca")}>
          <Select value={form.ca} onChange={(e) => setForm({ ...form, ca: e.target.value as AcmeCA })}>
            {Object.entries(caLabels).map(([value, label]) => <option key={value} value={value}>{value === "custom" ? t("acmeAccount.caCustom") : label}</option>)}
          </Select>
        </FieldGroup>
        {form.ca === "custom" && (
          <FieldGroup label={t("acmeAccount.customCaUrl")}>
            <Input value={form.custom_ca} onChange={(e) => setForm({ ...form, custom_ca: e.target.value })} placeholder="https://example.com/acme/directory" required />
          </FieldGroup>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <FieldGroup label={t("acmeAccount.eabKid")}>
            <Input value={form.eab_kid} onChange={(e) => setForm({ ...form, eab_kid: e.target.value })} />
          </FieldGroup>
          <FieldGroup label={editing?.eab_hmac_set ? t("acmeAccount.eabHmacKeep") : t("acmeAccount.eabHmac")}>
            <Input type="password" value={form.eab_hmac ?? ""} onChange={(e) => setForm({ ...form, eab_hmac: e.target.value })} />
          </FieldGroup>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          {t("acmeAccount.active")}
        </label>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>{t("common.cancel")}</Button>
          <Button type="submit" disabled={saving} loading={saving}>{saving ? t("common.saving") : t("common.save")}</Button>
        </div>
      </form>
    </Modal>
  );
}
