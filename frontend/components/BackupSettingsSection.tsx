"use client";

import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, BackupSettings } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** Scheduled backups, retention and the encrypted offsite copy. Admin only: renders nothing for other roles. */
export function BackupSettingsSection() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const [cfg, setCfg] = useState<BackupSettings | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | "run" | null>(null);
  const [scheduled, setScheduled] = useState(true);
  const [interval, setIntervalHours] = useState(24);
  const [keep, setKeep] = useState(30);
  const [offsite, setOffsite] = useState(false);
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [bucket, setBucket] = useState("");
  const [prefix, setPrefix] = useState("opnexus");
  const [accessKey, setAccessKey] = useState("");
  const [pathStyle, setPathStyle] = useState(true);
  const [secretKey, setSecretKey] = useState("");
  const [passphrase, setPassphrase] = useState("");

  function apply(d: BackupSettings) {
    setCfg(d);
    setScheduled(d.scheduled_enabled);
    setIntervalHours(d.interval_hours);
    setKeep(d.keep_count);
    setOffsite(d.offsite_enabled);
    setEndpoint(d.s3_endpoint ?? "");
    setRegion(d.s3_region);
    setBucket(d.s3_bucket ?? "");
    setPrefix(d.s3_prefix);
    setAccessKey(d.s3_access_key ?? "");
    setPathStyle(d.s3_path_style);
    setSecretKey("");
    setPassphrase("");
  }

  useEffect(() => {
    api.getBackupSettings().then(apply).catch(() => setHidden(true));
  }, []);

  if (hidden) return null;

  async function save(e?: React.FormEvent) {
    e?.preventDefault();
    setBusy("save");
    setError("");
    try {
      apply(
        await api.updateBackupSettings({
          scheduled_enabled: scheduled, interval_hours: interval, keep_count: keep, offsite_enabled: offsite,
          s3_endpoint: endpoint, s3_region: region, s3_bucket: bucket, s3_prefix: prefix, s3_access_key: accessKey, s3_path_style: pathStyle,
          ...(secretKey ? { s3_secret_key: secretKey } : {}), ...(passphrase ? { passphrase } : {}),
        }),
      );
      toast.show(t("backupSettings.saved"), "success");
      return true;
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    if (!(await save())) return;
    setBusy("test");
    try {
      await api.testBackupOffsite();
      toast.show(t("backupSettings.testOk"), "success");
    } catch (err) {
      setError(errorMessage(err, "backupSettings.testFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function runNow() {
    setBusy("run");
    setError("");
    try {
      const r = await api.runBackupsNow();
      toast.show(t("backupSettings.ran", { taken: r.taken, failed: r.failed, uploaded: r.uploaded }), r.failed ? "danger" : "success");
      apply(await api.getBackupSettings());
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
    } finally {
      setBusy(null);
    }
  }

  const label = (key: string) => <span className="mb-1 block font-medium text-slate-700">{t(key)}</span>;

  return (
    <section id="backups" className="scroll-mt-4 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("backupSettings.title")}
        <InfoTip text={t("backupSettings.intro")} />
      </h2>
      <form onSubmit={save} className="space-y-4">
        <div className="space-y-3 rounded-lg border border-slate-100 p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={scheduled} onChange={(e) => setScheduled(e.target.checked)} />
            {t("backupSettings.scheduled")}
          </label>
          <fieldset disabled={!scheduled} className="grid grid-cols-2 gap-3 disabled:opacity-50">
            <label className="block text-sm">
              {label("backupSettings.interval")}
              <Select value={interval} onChange={(e) => setIntervalHours(Number(e.target.value))}>
                {[6, 12, 24, 48, 168].map((h) => <option key={h} value={h}>{t("backupSettings.everyHours", { n: h })}</option>)}
              </Select>
            </label>
            <label className="block text-sm">
              {label("backupSettings.keep")}
              <Input type="number" min={3} max={1000} value={keep} onChange={(e) => setKeep(Number(e.target.value))} />
            </label>
          </fieldset>
        </div>

        <div className="space-y-3 rounded-lg border border-slate-100 p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={offsite} onChange={(e) => setOffsite(e.target.checked)} />
            {t("backupSettings.offsite")}
          </label>
          <fieldset disabled={!offsite} className="space-y-3 disabled:opacity-50">
            <div className="grid grid-cols-6 gap-3">
              <label className="col-span-4 block text-sm">{label("backupSettings.endpoint")}<Input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="https://s3.example.com" /></label>
              <label className="col-span-2 block text-sm">{label("backupSettings.region")}<Input value={region} onChange={(e) => setRegion(e.target.value)} /></label>
              <label className="col-span-3 block text-sm">{label("backupSettings.bucket")}<Input value={bucket} onChange={(e) => setBucket(e.target.value)} /></label>
              <label className="col-span-3 block text-sm">{label("backupSettings.prefix")}<Input value={prefix} onChange={(e) => setPrefix(e.target.value)} /></label>
              <label className="col-span-3 block text-sm">{label("backupSettings.accessKey")}<Input value={accessKey} onChange={(e) => setAccessKey(e.target.value)} autoComplete="off" /></label>
              <label className="col-span-3 block text-sm">
                <span className="mb-1 block font-medium text-slate-700">{t("backupSettings.secretKey", { state: cfg?.s3_secret_set ? t("auditExport.isSet") : t("auditExport.notSet") })}</span>
                <Input type="password" value={secretKey} onChange={(e) => setSecretKey(e.target.value)} placeholder={cfg?.s3_secret_set ? t("auditExport.keep") : ""} autoComplete="new-password" />
              </label>
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={pathStyle} onChange={(e) => setPathStyle(e.target.checked)} />
              {t("backupSettings.pathStyle")}
            </label>
            <label className="block text-sm">
              <span className="mb-1 block font-medium text-slate-700">
                {t("backupSettings.passphrase", { state: cfg?.passphrase_set ? t("auditExport.isSet") : t("auditExport.notSet") })}
                <InfoTip text={t("backupSettings.passphraseHint")} className="ml-1 align-middle" />
              </span>
              <Input type="password" minLength={12} value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder={cfg?.passphrase_set ? t("auditExport.keep") : t("backupSettings.passphraseNew")} autoComplete="new-password" />
            </label>
            {cfg?.last_offsite_error && <p role="alert" className="text-sm text-red-600">{t("backupSettings.offsiteError", { message: cfg.last_offsite_error })}</p>}
            {!cfg?.last_offsite_error && cfg?.last_offsite_ok_at && <p className="text-sm text-slate-500">{t("backupSettings.offsiteOk", { when: new Date(cfg.last_offsite_ok_at).toLocaleString(locale) })}</p>}
          </fieldset>
        </div>

        {cfg?.last_run_at && <p className="text-sm text-slate-500">{t("backupSettings.lastRun", { when: new Date(cfg.last_run_at).toLocaleString(locale) })}</p>}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={busy !== null || !cfg} loading={busy === "save"}>{t("common.save")}</Button>
          <Button type="button" variant="ghost" disabled={busy !== null || !cfg || !offsite} loading={busy === "test"} onClick={test}>{t("backupSettings.test")}</Button>
          <Button type="button" variant="ghost" disabled={busy !== null || !cfg} loading={busy === "run"} onClick={runNow}>{t("backupSettings.runNow")}</Button>
        </div>
      </form>
    </section>
  );
}
