"use client";

import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { AcmeAccount, AcmeCertificateRequest, AcmeCertificateRequestCreate, AcmeKeyLength, api, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { FormEvent, useState } from "react";

const keyLengthLabels: Record<AcmeKeyLength, string> = {
  key_2048: "RSA 2048",
  key_3072: "RSA 3072",
  key_4096: "RSA 4096",
  key_ec256: "EC 256",
  key_ec384: "EC 384",
};

function emptyForm(hostId: string, accountId: string): AcmeCertificateRequestCreate {
  return {
    host_id: hostId,
    name: "",
    alt_names: "",
    account_id: accountId,
    validation_ref: "",
    key_length: "key_4096",
    ocsp: false,
    auto_renewal: true,
    renew_interval: 60,
    enabled: true,
  };
}

export function AcmeCertificateForm({ hosts, accounts, editing, onClose, onSaved }: {
  hosts: Host[]; accounts: AcmeAccount[]; editing: AcmeCertificateRequest | null; onClose: () => void; onSaved: () => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [form, setForm] = useState<AcmeCertificateRequestCreate>(
    editing ?? emptyForm(hosts[0]?.id ?? "", accounts.find((a) => a.host_id === hosts[0]?.id)?.id ?? "")
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const accountsForHost = accounts.filter((a) => a.host_id === form.host_id);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      if (editing) await api.editAcmeCertificateRequest(editing.id, form, editing.updated_at);
      else await api.createAcmeCertificateRequest(form);
      onSaved();
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={editing ? t("acmeRequest.edit") : t("acmeRequest.new")} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        <p className="text-xs text-slate-500">
          {t("acmeRequest.localNote")}
        </p>
        <FieldGroup label={t("acmeAccount.host")}>
          <Select
            value={form.host_id}
            onChange={(e) => setForm({ ...form, host_id: e.target.value, account_id: accounts.find((a) => a.host_id === e.target.value)?.id ?? "" })}
            disabled={!!editing}
            required
          >
            {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
          </Select>
        </FieldGroup>
        <FieldGroup label={t("acmeRequest.account")}>
          <Select value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })} required>
            <option value="" disabled>{t("acmeRequest.chooseAccount")}</option>
            {accountsForHost.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </Select>
          {accountsForHost.length === 0 && (
            <p className="mt-1 text-xs text-flame-700">{t("acmeRequest.noAccount")}</p>
          )}
        </FieldGroup>
        <FieldGroup label={t("acmeRequest.commonName")}>
          <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="www.example.com" required />
        </FieldGroup>
        <FieldGroup label={t("acmeRequest.altNames")}>
          <Input value={form.alt_names} onChange={(e) => setForm({ ...form, alt_names: e.target.value })} placeholder="example.com,mail.example.com" />
        </FieldGroup>
        <FieldGroup label={t("acmeRequest.validation")}>
          <Input value={form.validation_ref} onChange={(e) => setForm({ ...form, validation_ref: e.target.value })} placeholder={t("acmeRequest.validationPlaceholder")} required />
        </FieldGroup>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <FieldGroup label={t("acmeRequest.keyLength")}>
            <Select value={form.key_length} onChange={(e) => setForm({ ...form, key_length: e.target.value as AcmeKeyLength })}>
              {Object.entries(keyLengthLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </Select>
          </FieldGroup>
          <FieldGroup label={t("acmeRequest.renewInterval")}>
            <Input type="number" min={0} max={5000} value={form.renew_interval} onChange={(e) => setForm({ ...form, renew_interval: Number(e.target.value) })} />
            <p className="mt-1 text-xs text-flame-700">{t("acmeRequest.renewWarning")}</p>
          </FieldGroup>
        </div>
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.ocsp} onChange={(e) => setForm({ ...form, ocsp: e.target.checked })} />
            {t("acmeRequest.ocsp")}
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.auto_renewal} onChange={(e) => setForm({ ...form, auto_renewal: e.target.checked })} />
            {t("acmeRequest.autoRenewal")}
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            {t("acmeRequest.active")}
          </label>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>{t("common.cancel")}</Button>
          <Button type="submit" disabled={saving || accountsForHost.length === 0} loading={saving}>{saving ? t("common.saving") : t("common.save")}</Button>
        </div>
      </form>
    </Modal>
  );
}
