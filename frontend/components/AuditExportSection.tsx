"use client";

import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, AuditExportSettings } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** Forwarding of the audit trail to syslog and/or a webhook. Admin only: renders nothing for other roles. */
export function AuditExportSection() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const [cfg, setCfg] = useState<AuditExportSettings | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [syslogEnabled, setSyslogEnabled] = useState(false);
  const [syslogHost, setSyslogHost] = useState("");
  const [syslogPort, setSyslogPort] = useState(514);
  const [syslogProtocol, setSyslogProtocol] = useState<AuditExportSettings["syslog_protocol"]>("udp");
  const [webhookEnabled, setWebhookEnabled] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [clearSecret, setClearSecret] = useState(false);

  function apply(data: AuditExportSettings) {
    setCfg(data);
    setSyslogEnabled(data.syslog_enabled);
    setSyslogHost(data.syslog_host ?? "");
    setSyslogPort(data.syslog_port);
    setSyslogProtocol(data.syslog_protocol);
    setWebhookEnabled(data.webhook_enabled);
    setWebhookUrl(data.webhook_url ?? "");
    setSecret("");
    setClearSecret(false);
  }

  useEffect(() => {
    api.getAuditExport().then(apply).catch(() => setHidden(true));
  }, []);

  if (hidden) return null;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      apply(
        await api.updateAuditExport({
          syslog_enabled: syslogEnabled,
          syslog_host: syslogHost,
          syslog_port: syslogPort,
          syslog_protocol: syslogProtocol,
          webhook_enabled: webhookEnabled,
          webhook_url: webhookUrl,
          ...(clearSecret ? { clear_webhook_signing_secret: true } : secret ? { webhook_signing_secret: secret } : {}),
        }),
      );
      toast.show(t("auditExport.saved"), "success");
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setError("");
    try {
      await api.testAuditExport();
      toast.show(t("auditExport.testSent"), "success");
    } catch (err) {
      setError(errorMessage(err, "auditExport.testFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id="audit-export" className="scroll-mt-4 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("auditExport.title")}
        <InfoTip text={t("auditExport.intro")} />
      </h2>
      <form onSubmit={save} className="space-y-4">
        <div className="space-y-3 rounded-lg border border-slate-100 p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={syslogEnabled} onChange={(e) => setSyslogEnabled(e.target.checked)} />
            {t("auditExport.syslog")}
          </label>
          <fieldset disabled={!syslogEnabled} className="grid grid-cols-6 gap-3 disabled:opacity-50">
            <label className="col-span-3 block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("auditExport.host")}</span>
              <Input value={syslogHost} onChange={(e) => setSyslogHost(e.target.value)} placeholder="syslog.example" />
            </label>
            <label className="col-span-1 block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("auditExport.port")}</span>
              <Input type="number" min={1} max={65535} value={syslogPort} onChange={(e) => setSyslogPort(Number(e.target.value))} />
            </label>
            <label className="col-span-2 block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("auditExport.protocol")}</span>
              <Select value={syslogProtocol} onChange={(e) => setSyslogProtocol(e.target.value as AuditExportSettings["syslog_protocol"])}>
                <option value="udp">UDP</option>
                <option value="tcp">TCP</option>
                <option value="tls">TLS</option>
              </Select>
            </label>
          </fieldset>
        </div>

        <div className="space-y-3 rounded-lg border border-slate-100 p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={webhookEnabled} onChange={(e) => setWebhookEnabled(e.target.checked)} />
            {t("auditExport.webhook")}
          </label>
          <fieldset disabled={!webhookEnabled} className="space-y-3 disabled:opacity-50">
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("auditExport.webhookUrl")}</span>
              <Input value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} placeholder="https://…" />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">
                {t("auditExport.secret", { state: cfg?.webhook_signing_secret_set && !clearSecret ? t("auditExport.isSet") : t("auditExport.notSet") })}
              </span>
              <Input
                type="password"
                value={secret}
                onChange={(e) => { setSecret(e.target.value); setClearSecret(false); }}
                placeholder={cfg?.webhook_signing_secret_set ? t("auditExport.keep") : t("auditExport.optional")}
              />
            </label>
            {cfg?.webhook_signing_secret_set && (
              <label className="flex items-center gap-2 text-sm text-slate-500">
                <input type="checkbox" checked={clearSecret} onChange={(e) => { setClearSecret(e.target.checked); if (e.target.checked) setSecret(""); }} />
                {t("auditExport.removeSecret")}
              </label>
            )}
          </fieldset>
        </div>

        {cfg?.last_error && (
          <p role="alert" className="text-sm text-red-600">
            {t("auditExport.lastError", { when: cfg.last_error_at ? new Date(cfg.last_error_at).toLocaleString(locale) : "", message: cfg.last_error })}
          </p>
        )}
        {!cfg?.last_error && cfg?.last_success_at && (
          <p className="text-sm text-slate-500">{t("auditExport.lastSuccess", { when: new Date(cfg.last_success_at).toLocaleString(locale) })}</p>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex gap-2">
          <Button type="submit" disabled={busy || !cfg} loading={busy}>{t("common.save")}</Button>
          <Button type="button" variant="ghost" disabled={busy || !cfg || !(cfg.syslog_enabled || cfg.webhook_enabled)} onClick={test}>
            {t("auditExport.test")}
          </Button>
        </div>
      </form>
    </section>
  );
}
