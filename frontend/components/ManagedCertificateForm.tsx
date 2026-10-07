"use client";

import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { api, CertAction, CertDigest, CertKeyType, Host, ManagedCertificate, ManagedCertificateCreate } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { FormEvent, useState } from "react";

const certActions: CertAction[] = ["import", "internal", "external", "sign_csr", "import_csr", "reissue", "manual"];

const dnActions: CertAction[] = ["internal", "external", "reissue"];
const csrActions: CertAction[] = ["sign_csr"];
const crtActions: CertAction[] = ["import", "import_csr", "manual"];

const textareaClass =
  "w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs text-slate-900 shadow-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500";

function emptyForm(hostId: string): ManagedCertificateCreate {
  return {
    host_id: hostId,
    action: "import",
    description: "",
    commonname: "",
    country: "DE",
    state: "",
    city: "",
    organization: "",
    email: "",
    key_type: "2048",
    digest: "sha256",
    cert_type: "server_cert",
    lifetime: 397,
    caref: null,
    csr_payload: null,
    crt_payload: null,
    private_key: "",
  };
}

export function ManagedCertificateForm({ hosts, editing, onClose, onSaved }: {
  hosts: Host[]; editing: ManagedCertificate | null; onClose: () => void; onSaved: () => void;
}) {
  const { t, errorMessage } = useLanguage();
  const [form, setForm] = useState<ManagedCertificateCreate>(
    editing ? { ...editing, private_key: "" } : emptyForm(hosts[0]?.id ?? "")
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      if (editing) {
        const { private_key, ...rest } = form;
        await api.editManagedCertificate(editing.id, private_key ? form : { ...rest, private_key: null }, editing.updated_at);
      } else {
        await api.createManagedCertificate(form);
      }
      onSaved();
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={editing ? t("certForm.edit") : t("certForm.new")} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        <FieldGroup label={t("certForm.host")}>
          <Select value={form.host_id} onChange={(e) => setForm({ ...form, host_id: e.target.value })} disabled={!!editing} required>
            {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
          </Select>
        </FieldGroup>
        <FieldGroup label={t("certForm.action")}>
          <Select value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value as CertAction })}>
            {certActions.map((value) => <option key={value} value={value}>{t(`certAction.${value}`)}</option>)}
          </Select>
        </FieldGroup>
        <FieldGroup label={t("certForm.description")}>
          <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} required />
        </FieldGroup>

        {dnActions.includes(form.action) && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FieldGroup label={t("certForm.commonName")}>
              <Input value={form.commonname} onChange={(e) => setForm({ ...form, commonname: e.target.value })} required placeholder="host.example.com" />
            </FieldGroup>
            <FieldGroup label={t("certForm.country")}>
              <Input value={form.country} onChange={(e) => setForm({ ...form, country: e.target.value.toUpperCase() })} maxLength={2} />
            </FieldGroup>
            <FieldGroup label={t("certForm.state")}>
              <Input value={form.state} onChange={(e) => setForm({ ...form, state: e.target.value })} />
            </FieldGroup>
            <FieldGroup label={t("certForm.city")}>
              <Input value={form.city} onChange={(e) => setForm({ ...form, city: e.target.value })} />
            </FieldGroup>
            <FieldGroup label={t("certForm.organization")}>
              <Input value={form.organization} onChange={(e) => setForm({ ...form, organization: e.target.value })} />
            </FieldGroup>
            <FieldGroup label={t("certForm.email")}>
              <Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </FieldGroup>
          </div>
        )}

        {dnActions.includes(form.action) && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <FieldGroup label={t("certForm.keyType")}>
              <Select value={form.key_type} onChange={(e) => setForm({ ...form, key_type: e.target.value as CertKeyType })}>
                <option value="2048">RSA 2048</option>
                <option value="3072">RSA 3072</option>
                <option value="4096">RSA 4096</option>
                <option value="prime256v1">EC prime256v1</option>
                <option value="secp384r1">EC secp384r1</option>
                <option value="secp521r1">EC secp521r1</option>
              </Select>
            </FieldGroup>
            <FieldGroup label={t("certForm.digest")}>
              <Select value={form.digest} onChange={(e) => setForm({ ...form, digest: e.target.value as CertDigest })}>
                <option value="sha256">SHA256</option>
                <option value="sha384">SHA384</option>
                <option value="sha512">SHA512</option>
              </Select>
            </FieldGroup>
            <FieldGroup label={t("certForm.lifetime")}>
              <Input type="number" min={1} max={36500} value={form.lifetime} onChange={(e) => setForm({ ...form, lifetime: Number(e.target.value) })} />
            </FieldGroup>
          </div>
        )}

        {(form.action === "internal" || form.action === "sign_csr") && (
          <FieldGroup label={form.action === "sign_csr" ? t("certForm.carefRequired") : t("certForm.carefOptional")}>
            <Input
              value={form.caref ?? ""}
              onChange={(e) => setForm({ ...form, caref: e.target.value || null })}
              required={form.action === "sign_csr"}
              placeholder={t("certForm.carefPlaceholder")}
            />
          </FieldGroup>
        )}

        {csrActions.includes(form.action) && (
          <FieldGroup label={t("certForm.csr")}>
            <textarea
              className={textareaClass}
              rows={6}
              value={form.csr_payload ?? ""}
              onChange={(e) => setForm({ ...form, csr_payload: e.target.value })}
              placeholder="-----BEGIN CERTIFICATE REQUEST-----"
              aria-label={t("certForm.csr")}
              required
            />
          </FieldGroup>
        )}

        {crtActions.includes(form.action) && (
          <FieldGroup label={t("certForm.certificate")}>
            <textarea
              className={textareaClass}
              rows={6}
              value={form.crt_payload ?? ""}
              onChange={(e) => setForm({ ...form, crt_payload: e.target.value })}
              placeholder="-----BEGIN CERTIFICATE-----"
              aria-label={t("certForm.certificate")}
              required
            />
          </FieldGroup>
        )}

        {form.action === "import" && (
          <FieldGroup label={editing?.private_key_set ? t("certForm.privateKeyKeep") : t("certForm.privateKeyOptional")} hint={t("certForm.privateKeyNote")}>
            <textarea
              className={textareaClass}
              rows={4}
              value={form.private_key ?? ""}
              onChange={(e) => setForm({ ...form, private_key: e.target.value })}
              placeholder="-----BEGIN PRIVATE KEY-----"
              aria-label={t("certForm.privateKeyAria")}
            />
          </FieldGroup>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>{t("common.cancel")}</Button>
          <Button type="submit" disabled={saving} loading={saving}>{saving ? t("common.saving") : t("common.save")}</Button>
        </div>
      </form>
    </Modal>
  );
}
