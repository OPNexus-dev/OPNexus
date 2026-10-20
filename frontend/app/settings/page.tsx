"use client";
import { TwoFactorSection } from "@/components/TwoFactorSection";
import { OpnexusUpdateSection } from "@/components/OpnexusUpdateSection";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { useEffect, useState } from "react";
import { api, AppSettings, LogSettings, NotificationSettings, SmtpSecurity, Theme } from "@/lib/api";
import { useTheme } from "@/components/ThemeProvider";
import { useLanguage } from "@/components/LanguageProvider";
import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Field";
import { useNotAdmin, useReadOnly } from "@/lib/useReadOnly";
import { OidcSection } from "@/components/OidcSection";
import { BackupSettingsSection } from "@/components/BackupSettingsSection";
import { AuditExportSection } from "@/components/AuditExportSection";
import { APP_CODENAME, APP_MAINTAINER, APP_VERSION } from "@/lib/version";
import { translate } from "@/lib/i18n";

function SetupAssistantSection() {
  const { t } = useLanguage();
  const readOnly = useReadOnly();
  if (readOnly) return null;
  return (
    <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-5">
      <div>
        <h2 className="text-lg font-medium">{t("settings.setup.title")}</h2>
        <p className="mt-1 text-sm text-slate-600">{t("settings.setup.intro")}</p>
      </div>
      <Button href="/setup" variant="outline">{t("settings.setup.open")}</Button>
    </section>
  );
}

function NotificationsSection() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useNotAdmin();
  const [ns, setNs] = useState<NotificationSettings | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<"webhook" | "email" | null>(null);

  const [webhookEnabled, setWebhookEnabled] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [clearWebhookSecret, setClearWebhookSecret] = useState(false);

  const [emailEnabled, setEmailEnabled] = useState(false);
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState(587);
  const [smtpSecurity, setSmtpSecurity] = useState<SmtpSecurity>("starttls");
  const [smtpUsername, setSmtpUsername] = useState("");
  const [smtpPassword, setSmtpPassword] = useState("");
  const [clearSmtpPassword, setClearSmtpPassword] = useState(false);
  const [smtpFrom, setSmtpFrom] = useState("");
  const [smtpTo, setSmtpTo] = useState("");

  const [hostOffline, setHostOffline] = useState(true);
  const [certExpiring, setCertExpiring] = useState(true);
  const [ruleDrift, setRuleDrift] = useState(true);
  const [revertFailed, setRevertFailed] = useState(true);
  const [maintenanceUnknown, setMaintenanceUnknown] = useState(true);
  const [carpHealth, setCarpHealth] = useState(true);
  const [natDrift, setNatDrift] = useState(true);
  const [networkDrift, setNetworkDrift] = useState(true);
  const [carpAutoRevertFailed, setCarpAutoRevertFailed] = useState(true);
  const [scheduledFailed, setScheduledFailed] = useState(true);
  const [backupFailed, setBackupFailed] = useState(true);
  const [complianceAlerts, setComplianceAlerts] = useState(false);
  const [pollInterval, setPollInterval] = useState(300);

  function applyFromServer(data: NotificationSettings) {
    setNs(data);
    setWebhookEnabled(data.webhook_enabled);
    setWebhookUrl(data.webhook_url ?? "");
    setEmailEnabled(data.email_enabled);
    setSmtpHost(data.smtp_host ?? "");
    setSmtpPort(data.smtp_port);
    setSmtpSecurity(data.smtp_security);
    setSmtpUsername(data.smtp_username ?? "");
    setSmtpFrom(data.smtp_from_address ?? "");
    setSmtpTo(data.smtp_to_addresses.join(", "));
    setHostOffline(data.host_offline_enabled);
    setCertExpiring(data.cert_expiring_enabled);
    setRuleDrift(data.rule_drift_enabled);
    setRevertFailed(data.revert_failed_enabled);
    setMaintenanceUnknown(data.maintenance_unknown_enabled);
    setCarpHealth(data.carp_health_enabled);
    setNatDrift(data.nat_drift_enabled);
    setNetworkDrift(data.network_drift_enabled);
    setCarpAutoRevertFailed(data.carp_auto_revert_failed_enabled);
    setScheduledFailed(data.scheduled_maintenance_failed_enabled);
    setBackupFailed(data.backup_failed_enabled);
    setComplianceAlerts(data.compliance_alerts_enabled);
    setPollInterval(data.poll_interval_seconds);
    setWebhookSecret("");
    setClearWebhookSecret(false);
    setSmtpPassword("");
    setClearSmtpPassword(false);
  }

  useEffect(() => {
    api.notificationSettings().then(applyFromServer).catch((e) => setError(errorMessage(e)));
  }, [errorMessage]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");
    setSaving(true);
    try {
      const data = await api.saveNotificationSettings({
        webhook_enabled: webhookEnabled,
        webhook_url: webhookUrl,
        ...(webhookSecret ? { webhook_signing_secret: webhookSecret } : {}),
        clear_webhook_signing_secret: clearWebhookSecret,
        email_enabled: emailEnabled,
        smtp_host: smtpHost,
        smtp_port: smtpPort,
        smtp_security: smtpSecurity,
        smtp_username: smtpUsername,
        ...(smtpPassword ? { smtp_password: smtpPassword } : {}),
        clear_smtp_password: clearSmtpPassword,
        smtp_from_address: smtpFrom,
        smtp_to_addresses: smtpTo.split(",").map((s) => s.trim()).filter(Boolean),
        host_offline_enabled: hostOffline,
        cert_expiring_enabled: certExpiring,
        rule_drift_enabled: ruleDrift,
        revert_failed_enabled: revertFailed,
        maintenance_unknown_enabled: maintenanceUnknown,
        carp_health_enabled: carpHealth,
        nat_drift_enabled: natDrift,
        network_drift_enabled: networkDrift,
        carp_auto_revert_failed_enabled: carpAutoRevertFailed,
        scheduled_maintenance_failed_enabled: scheduledFailed,
        backup_failed_enabled: backupFailed,
        compliance_alerts_enabled: complianceAlerts,
        poll_interval_seconds: pollInterval,
      });
      applyFromServer(data);
      setNotice(t("settings.notify.saved"));
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function test(channel: "webhook" | "email") {
    setError("");
    setNotice("");
    setTesting(channel);
    try {
      await api.testNotification(channel);
      setNotice(t("settings.notify.testSent", { channel: channel === "webhook" ? t("settings.notify.channelWebhook") : t("settings.notify.channelEmail") }));
    } catch (e) {
      setError(errorMessage(e, "settings.notify.testFailed"));
    } finally {
      setTesting(null);
    }
  }

  return (
    <section id="notifications" className="scroll-mt-4 space-y-5 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">{t("settings.notify.title")}<InfoTip text={t("settings.notify.intro", { seconds: pollInterval })} /></h2>

      <form onSubmit={save} className="space-y-5">
        <fieldset disabled={readOnly} className="min-w-0 space-y-4">
        <div className="space-y-3 rounded-lg border border-slate-100 p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={webhookEnabled} onChange={(e) => setWebhookEnabled(e.target.checked)} />
            {t("settings.notify.webhook")}
          </label>
          <fieldset disabled={!webhookEnabled} className="space-y-3 disabled:opacity-50">
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.webhookUrl")}</span>
              <Input value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} placeholder="https://…" />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.signingSecret", { state: ns?.webhook_signing_secret_set && !clearWebhookSecret ? t("settings.notify.isSet") : t("settings.notify.notSet") })}</span>
              <Input
                type="password"
                value={webhookSecret}
                onChange={(e) => { setWebhookSecret(e.target.value); setClearWebhookSecret(false); }}
                placeholder={ns?.webhook_signing_secret_set ? t("settings.notify.keepUnchanged") : t("settings.notify.optional")}
              />
            </label>
            {ns?.webhook_signing_secret_set && (
              <label className="flex items-center gap-2 text-sm text-slate-500">
                <input type="checkbox" checked={clearWebhookSecret} onChange={(e) => { setClearWebhookSecret(e.target.checked); if (e.target.checked) setWebhookSecret(""); }} />
                {t("settings.notify.removeSecret")}
              </label>
            )}
            <Button type="button" variant="ghost" disabled={testing !== null || !ns} onClick={() => test("webhook")}>
              {testing === "webhook" ? t("settings.notify.sending") : t("settings.notify.testWebhook")}
            </Button>
          </fieldset>
        </div>

        <div className="space-y-3 rounded-lg border border-slate-100 p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={emailEnabled} onChange={(e) => setEmailEnabled(e.target.checked)} />
            {t("settings.notify.email")}
          </label>
          <fieldset disabled={!emailEnabled} className="space-y-3 disabled:opacity-50">
            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.smtpHost")}</span>
                <Input value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.port")}</span>
                <Input type="number" value={smtpPort} onChange={(e) => setSmtpPort(Number(e.target.value))} />
              </label>
            </div>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.security")}</span>
              <Select value={smtpSecurity} onChange={(e) => setSmtpSecurity(e.target.value as SmtpSecurity)}>
                <option value="none">{t("settings.notify.securityNone")}</option>
                <option value="starttls">STARTTLS</option>
                <option value="ssl">SSL/TLS</option>
              </Select>
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.username")}</span>
              <Input value={smtpUsername} onChange={(e) => setSmtpUsername(e.target.value)} />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.password", { state: ns?.smtp_password_set && !clearSmtpPassword ? t("settings.notify.isSet") : t("settings.notify.notSet") })}</span>
              <Input
                type="password"
                value={smtpPassword}
                onChange={(e) => { setSmtpPassword(e.target.value); setClearSmtpPassword(false); }}
                placeholder={ns?.smtp_password_set ? t("settings.notify.keepUnchanged") : t("settings.notify.optional")}
              />
            </label>
            {ns?.smtp_password_set && (
              <label className="flex items-center gap-2 text-sm text-slate-500">
                <input type="checkbox" checked={clearSmtpPassword} onChange={(e) => { setClearSmtpPassword(e.target.checked); if (e.target.checked) setSmtpPassword(""); }} />
                {t("settings.notify.removePassword")}
              </label>
            )}
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.from")}</span>
              <Input value={smtpFrom} onChange={(e) => setSmtpFrom(e.target.value)} />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.to")}</span>
              <Input value={smtpTo} onChange={(e) => setSmtpTo(e.target.value)} />
            </label>
            <Button type="button" variant="ghost" disabled={testing !== null || !ns} onClick={() => test("email")}>
              {testing === "email" ? t("settings.notify.sending") : t("settings.notify.testEmail")}
            </Button>
          </fieldset>
        </div>

        <div className="space-y-2 rounded-lg border border-slate-100 p-4">
          <p className="text-sm font-medium">{t("settings.notify.events")}</p>
          {[
            [t("settings.notify.event.hostOffline"), hostOffline, setHostOffline],
            [t("settings.notify.event.certExpiring"), certExpiring, setCertExpiring],
            [t("settings.notify.event.ruleDrift"), ruleDrift, setRuleDrift],
            [t("settings.notify.event.revertFailed"), revertFailed, setRevertFailed],
            [t("settings.notify.event.maintenanceUnknown"), maintenanceUnknown, setMaintenanceUnknown],
            [t("settings.notify.event.carpHealth"), carpHealth, setCarpHealth],
            [t("settings.notify.event.natDrift"), natDrift, setNatDrift],
            [t("settings.notify.event.networkDrift"), networkDrift, setNetworkDrift],
            [t("settings.notify.event.carpAutoRevertFailed"), carpAutoRevertFailed, setCarpAutoRevertFailed],
            [t("settings.notify.event.scheduledFailed"), scheduledFailed, setScheduledFailed],
            [t("settings.notify.event.backupFailed"), backupFailed, setBackupFailed],
            [t("settings.notify.event.complianceAlerts"), complianceAlerts, setComplianceAlerts],
          ].map(([label, value, setter]) => (
            <label key={label as string} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={value as boolean} onChange={(e) => (setter as (v: boolean) => void)(e.target.checked)} />
              {label as string}
            </label>
          ))}
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-slate-700">{t("settings.notify.pollInterval")}</span>
            <Input type="number" min={60} value={pollInterval} onChange={(e) => setPollInterval(Number(e.target.value))} />
            <span className="mt-1 block text-xs text-slate-500">{t("settings.notify.pollHint")}</span>
          </label>
        </div>

        <div className="flex justify-end border-t border-slate-100 pt-4"><Button type="submit" disabled={saving || !ns}>{t("common.save")}</Button></div>
        </fieldset>
      </form>
      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {notice && <p role="status" className="text-sm text-emerald-700">{notice}</p>}
    </section>
  );
}

function formatBytes(bytes: number, locale: string) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toLocaleString(locale, { maximumFractionDigits: unit >= 3 ? 1 : 0 })} ${units[unit]}`;
}

function LogUsage({ used, entries, limitGb, oldest }: { used: number; entries: number; limitGb: number; oldest: string | null }) {
  const { t, locale } = useLanguage();
  const limit = limitGb * 1024 ** 3;
  const share = limit > 0 ? Math.min(1, used / limit) : 0;
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="text-slate-700">
          {t("settings.logs.usage", { used: formatBytes(used, locale), limit: limitGb > 0 ? formatBytes(limit, locale) : t("settings.logs.unlimited") })}
        </span>
        <span className="text-xs text-slate-500">{oldest ? t("settings.logs.storedSince", { count: entries.toLocaleString(locale), date: new Date(oldest).toLocaleDateString(locale) }) : t("settings.logs.stored", { count: entries.toLocaleString(locale) })}</span>
      </div>
      {limitGb > 0 && (
        <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(share * 100)} aria-label={t("settings.logs.usageAria")} className="h-2 overflow-hidden rounded-full bg-slate-200">
          <div className={`h-full rounded-full ${share > 0.8 ? "bg-flame-500" : "bg-brand-500"}`} style={{ width: `${Math.max(2, share * 100)}%` }} />
        </div>
      )}
    </div>
  );
}

function LogIngestSection() {
  const { t, errorMessage } = useLanguage();
  const readOnly = useNotAdmin();
  const [ls, setLs] = useState<LogSettings | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);

  const [enabled, setEnabled] = useState(true);
  const [pollInterval, setPollInterval] = useState(30);
  const [retentionDays, setRetentionDays] = useState(7);
  const [maxSizeGb, setMaxSizeGb] = useState(10);

  function applyFromServer(data: LogSettings) {
    setLs(data);
    setEnabled(data.enabled);
    setPollInterval(data.poll_interval_seconds);
    setRetentionDays(data.retention_days);
    setMaxSizeGb(data.max_size_gb);
  }

  useEffect(() => {
    api.logSettings().then(applyFromServer).catch((e) => setError(errorMessage(e)));
  }, [errorMessage]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");
    setSaving(true);
    try {
      const data = await api.saveLogSettings({
        enabled,
        poll_interval_seconds: pollInterval,
        retention_days: retentionDays,
        max_size_gb: maxSizeGb,
      });
      applyFromServer(data);
      setNotice(t("settings.logs.saved"));
    } catch (e) {
      setError(errorMessage(e, "common.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">{t("settings.logs.title")}<InfoTip text={t("settings.logs.intro")} /></h2>
      <form onSubmit={save} className="space-y-4">
        <fieldset disabled={readOnly} className="min-w-0 space-y-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            {t("settings.logs.enabled")}
          </label>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.logs.pollInterval")}</span>
              <Input type="number" min={10} value={pollInterval} onChange={(e) => setPollInterval(Number(e.target.value))} />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">{t("settings.logs.retention")}</span>
              <Input type="number" min={1} value={retentionDays} onChange={(e) => setRetentionDays(Number(e.target.value))} />
            </label>
            <label className="block text-sm">
              <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("settings.logs.maxSize")}<InfoTip text={t("settings.logs.maxSizeHint")} size={13} /></span>
              <Input type="number" min={0} step={0.5} value={maxSizeGb} onChange={(e) => setMaxSizeGb(Number(e.target.value))} />
            </label>
          </div>
          {ls && <LogUsage used={ls.used_bytes} entries={ls.entries} limitGb={ls.max_size_gb} oldest={ls.oldest_at} />}
          <div className="flex justify-end border-t border-slate-100 pt-4"><Button type="submit" disabled={saving || !ls}>{t("common.save")}</Button></div>
        </fieldset>
      </form>
      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {notice && <p role="status" className="text-sm text-emerald-700">{notice}</p>}
    </section>
  );
}

export default function SettingsPage(){
  const {theme,setTheme}=useTheme();const [settings,setSettings]=useState<AppSettings|null>(null);
  const {language,setLanguage,t,errorMessage}=useLanguage();
  const [error,setError]=useState('');const [notice,setNotice]=useState('');const [saving,setSaving]=useState(false);
  const [current,setCurrent]=useState('');const [password,setPassword]=useState('');const [repeat,setRepeat]=useState('');
  useEffect(()=>{api.settings().then(setSettings).catch(e=>setError(errorMessage(e)));},[errorMessage]);
  async function saveTheme(value:Theme){setSaving(true);setError('');setNotice('');const old=theme;setTheme(value);try{setSettings(await api.saveSettings({theme:value,language}));setNotice(t('settings.appearanceSaved'));}catch(e){setTheme(old);setError(errorMessage(e));}finally{setSaving(false);}}
  async function saveLanguage(value:"de"|"en"){setSaving(true);setError('');setNotice('');const old=language;setLanguage(value);try{setSettings(await api.saveSettings({theme,language:value}));setNotice(translate(value,'settings.language.saved'));}catch(e){setLanguage(old);setError(errorMessage(e));}finally{setSaving(false);}}
  async function changePassword(e:React.FormEvent){e.preventDefault();setError('');setNotice('');if(password!==repeat){setError(t('settings.passwordMismatch'));return;}setSaving(true);try{await api.changePassword(current,password);setCurrent('');setPassword('');setRepeat('');setNotice(t('settings.passwordChanged'));}catch(e){setError(errorMessage(e,'settings.passwordFailed'));}finally{setSaving(false);}}
  return <div className="mx-auto max-w-3xl space-y-6"><h1 className="text-2xl font-semibold">{t("settings.title")}</h1>{error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}{notice && <p role="status" className="text-sm text-emerald-700">{notice}</p>}<section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5"><h2 className="flex items-center gap-2 text-lg font-medium">{t('settings.appearance')}<InfoTip text={t('settings.appearanceHint')} /></h2><div role="radiogroup" aria-label={t('settings.colorScheme')} className="flex flex-wrap gap-3">{(['light','dark','system'] as const).map((v)=><label key={v} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-4 py-3 text-sm ${theme===v?'border-sky-500 bg-sky-50 text-sky-700':'border-slate-200'}`}><input type="radio" name="theme" checked={theme===v} disabled={saving || !settings} onChange={()=>saveTheme(v)}/>{t(`settings.theme.${v}`)}</label>)}</div></section><section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5"><h2 className="text-lg font-medium">{t('settings.language.label')}</h2><div role="radiogroup" aria-label={t('settings.language.label')} className="flex flex-wrap gap-3">{(['de','en'] as const).map(value=><label key={value} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-4 py-3 text-sm ${language===value?'border-sky-500 bg-sky-50 text-sky-700':'border-slate-200'}`}><input type="radio" name="language" checked={language===value} disabled={saving || !settings} onChange={()=>saveLanguage(value)}/>{t(`settings.language.${value}`)}</label>)}</div></section><section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5"><h2 className="text-lg font-medium">{t('settings.account')} {settings?.username ? `· ${settings.username}`:''}</h2><form onSubmit={changePassword} className="space-y-4"><label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{t('settings.currentPassword')}</span><Input aria-label={t('settings.currentPassword')} type="password" autoComplete="current-password" required value={current} onChange={e=>setCurrent(e.target.value)}/></label><label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{t('settings.newPassword')}</span><Input aria-label={t('settings.newPassword')} type="password" autoComplete="new-password" minLength={10} maxLength={1024} required value={password} onChange={e=>setPassword(e.target.value)}/></label><label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{t('settings.repeatPassword')}</span><Input aria-label={t('settings.repeatPassword')} type="password" autoComplete="new-password" minLength={10} required value={repeat} onChange={e=>setRepeat(e.target.value)}/></label><Button type="submit" disabled={saving}>{t('settings.changePassword')}</Button></form></section><section className="space-y-3 rounded-xl border border-slate-200 bg-white p-5"><h2 className="flex items-center gap-2 text-lg font-medium">{t('settings.operational')}<InfoTip text={t('settings.serverManaged')} /></h2>{settings?<dl className="grid grid-cols-2 gap-3 text-sm"><dt>{t('settings.deployWindow')}</dt><dd>{t('settings.seconds',{n:settings.deploy_confirm_window_seconds})}</dd><dt>{t('settings.sessionLifetime')}</dt><dd>{t('settings.days',{n:Math.round(settings.session_lifetime_seconds/86400)})}</dd></dl>:<p className="text-sm text-slate-500">{t('common.loading')}</p>}</section><SetupAssistantSection/><TwoFactorSection/><NotificationsSection/><LogIngestSection/><OidcSection/><BackupSettingsSection/><AuditExportSection/><OpnexusUpdateSection/><section className="space-y-2 rounded-xl border border-slate-200 bg-white p-5"><h2 className="flex items-center gap-2 text-lg font-medium">{t('settings.about')}<InfoTip text={t('settings.changelogNote',{file:'CHANGELOG.md'})} /></h2><dl className="grid grid-cols-2 gap-3 text-sm"><dt>{t('settings.version')}</dt><dd>{APP_VERSION} {language==='en'?'“':'„'}{APP_CODENAME}{language==='en'?'”':'“'}</dd><dt>{t('settings.development')}</dt><dd>{APP_MAINTAINER}</dd></dl></section></div>;
}
